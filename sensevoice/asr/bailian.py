"""百炼云端 ASR 客户端（带对冲请求 / 多模型兜底）。

只依赖 numpy / requests / pyyaml，不 import funasr/torch，所以 Linux 的 daemon 和
Windows 的 WinVoice 共用这同一份文件（Windows 部署时拷成 asr_bailian.py）。

为什么要对冲(hedging)：
  qwen-audio-3.0-asr-flash 服务端时快时慢——快时 0.5~1.5s，慢时稳定卡在 ~10s，且会持续
  几十分钟（2026-10-15 实测 14 次里 11 次 >5s，还有一句 15s 超时被丢）。客户端没法修，
  只能让系统扛得住：主模型 hedge_after 秒内没回，就并发再发一份给备用模型，谁先成功用谁。

模型 → 协议（按名字自动选择）：
  *-message（qwen-audio-3.1-asr-flash-message）→ DashScope WebSocket（需 dashscope>=2.0.1，用到时才 import）
  qwen-audio-* / fun-asr-*  → DashScope 原生 multimodal-generation
  其它(qwen3-asr-flash 等)   → OpenAI 兼容 chat/completions
前文上下文：只有 *-message 支持。调用方每成功输入一句就 remember(text)，下一句识别时把最近几句
  打包成"一问一答"的一轮传给 input.context（只有用户消息没有助手回复的上下文会被服务端忽略）。
边说边发（MessageStream，只用于 *-message）：调用方说话一开始 open_stream() 在后台建连并开任务（不发音频
  不计费），确认是本人后 begin() 补发积压音频、feed() 实时续发，说完把会话交给 transcribe_samples(stream=)。
  服务端边收边识别，说完只需处理最后一小段：2026-11-03 实测 25~28s 的话说完后等待 1.6~1.9s → 0.05~0.3s，
  文本与计费 token 和整段发送完全一致。
热词：
  qwen-audio-3.0-*  → parameters.vocabulary 按分类加权（common_errors=超级热词 50）
  兼容协议模型      → system message 自然语言 context（≤200 字）
  fun-asr-* 等其它  → 不支持即时热词，不传（官方文档：只有 qwen-audio-3.0 支持）
"""

from __future__ import annotations

import base64
import collections
import concurrent.futures
import io
import os
import queue
import threading
import time
import wave
from typing import Any, Callable, Dict, List, Optional

import numpy as np

NATIVE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
COMPAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

# 分类 → 权重。common_errors 给官方"超级热词"档 50（每次请求最多 50 个），其余 [1,5]。
CATEGORY_WEIGHT = {"common_errors": 50, "apis": 4, "projects": 4, "frameworks": 3}
SUPER_WEIGHT_CAP = 50

DEFAULT_HEDGE_MODELS = ("fun-asr-flash-2026-06-15", "qwen3-asr-flash")

_YAML_CACHE: dict = {}


def load_hotword_categories(path: str) -> Dict[str, List[str]]:
    """读 hotwords.yaml，按分类返回词条；按文件 mtime 缓存，改完 yaml 下一句立即生效。"""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (path, st.st_mtime_ns, st.st_size)
    if _YAML_CACHE.get("key") == key:
        return _YAML_CACHE["data"]
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return {}
    out: Dict[str, List[str]] = {}
    for cat in ("common_errors", "apis", "projects", "frameworks"):
        group = raw.get(cat)
        if not isinstance(group, dict):
            continue
        terms = [str(t).strip() for t in (group.get("terms") or []) if str(t).strip()]
        if terms:
            out[cat] = terms
    _YAML_CACHE["key"] = key
    _YAML_CACHE["data"] = out
    return out


def _is_native(model: str) -> bool:
    return model.startswith(("qwen-audio", "fun-asr"))


def _is_message(model: str) -> bool:
    return model.endswith("-message")


def _join_sentences(sents: Dict[Any, str]) -> str:
    return "".join(sents[k] for k in sorted(sents, key=lambda k: (k is None, k or 0))).strip()


class MessageStream:
    """一段话的 *-message 流式识别会话。构造时就在后台线程建连并开任务，同一线程随后持续读取服务端事件。
    服务端约 23s 收不到音频就断开任务（2026-11-03 实测空等 10s 正常、30s 失败），所以预建后超过
    IDLE_MAX_SEC 还没开始上传的会话视为作废，由调用方换一条新的。"""

    IDLE_MAX_SEC = 18.0
    PACKET = 3200  # 100ms 一包，与官方示例一致

    def __init__(self, client: "BailianASRClient", model: str, recv_timeout: float):
        self._client = client
        self.model = model
        self._recv_timeout = recv_timeout
        self._rec = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._aborted = False
        self._ended = False
        self._pending = bytearray()
        self._sents: Dict[Any, str] = {}
        self._usage = None
        self._ctx_turns = 0
        self.error: Optional[BaseException] = None
        self.opened_at = 0.0
        self.streaming = False  # 已开始上传音频
        self.early = False      # 说话期间就开始上传（False = 说完、声纹通过后才整段发上去）
        threading.Thread(target=self._run, daemon=True, name="asr-stream").start()

    def _run(self) -> None:
        try:
            rec, self._ctx_turns = self._client._new_recognition(self.model, self._recv_timeout)
            rec.start()
        except BaseException as e:
            self.error = e
            self._ready.set()
            self._done.set()
            return
        with self._lock:
            self._rec = rec
            self.opened_at = time.time()
            aborted = self._aborted
        self._ready.set()
        if aborted:
            self._close()
            self._done.set()
            return
        try:
            for ev in rec.events():
                payload = ev.get("payload") or {}
                sent = (payload.get("output") or {}).get("sentence") or {}
                if sent.get("sentence_end") and not sent.get("heartbeat"):
                    self._sents[sent.get("sentence_id")] = sent.get("text", "")
                self._usage = payload.get("usage") or self._usage
        except BaseException as e:
            if not self._aborted:
                self.error = e
        finally:
            self._done.set()

    def ready(self) -> bool:
        return self._ready.is_set() and self._rec is not None and self.error is None and not self._aborted

    def wait_ready(self, timeout: float) -> bool:
        self._ready.wait(timeout)
        return self.ready()

    def expired(self) -> bool:
        """出错/已放弃，或预建后空等太久（服务端快要断开）。正在上传的会话不会因空等作废。"""
        if self.error is not None or self._aborted:
            return True
        return self.ready() and not self.streaming and time.time() - self.opened_at > self.IDLE_MAX_SEC

    def begin(self, backlog: bytes, early: bool) -> bool:
        """开始上传：先把已录下的音频一次补发，之后用 feed() 续发。会话还没连上或已作废时返回 False。"""
        if self.streaming or not self.ready() or self.expired():
            return False
        self.streaming, self.early = True, early
        self.feed(backlog)
        return self.error is None

    def feed(self, data: bytes) -> None:
        if not self.streaming or self._ended or self.error is not None:
            return
        self._pending.extend(data)
        try:
            while len(self._pending) >= self.PACKET:
                self._rec.send_audio_frame(bytes(self._pending[:self.PACKET]))
                del self._pending[:self.PACKET]
        except BaseException as e:
            self.error = e

    def end(self) -> None:
        """音频发完：补发不满一包的尾巴并通知服务端结束，之后服务端只需处理最后一小段。"""
        if not self.streaming or self._ended:
            return
        self._ended = True
        try:
            if self._pending:
                self._rec.send_audio_frame(bytes(self._pending))
                self._pending.clear()
            self._rec.stop()
        except BaseException as e:
            self.error = e

    def result(self, timeout: float) -> str:
        """end() 之后等服务端返回全部定稿句子；出错或超时抛异常，由对冲流程换备用模型。"""
        if self.error is not None:  # 上传途中已出错：立刻失败，让对冲流程马上换备用模型
            raise self.error
        if not self._done.wait(timeout):
            raise TimeoutError(f"stream_result>{timeout:.0f}s")
        if self.error is not None:
            raise self.error
        if self._usage:
            self._client._log(f"ASR_USAGE model={self.model} in={self._usage.get('input_tokens')} "
                              f"out={self._usage.get('output_tokens')} ctx_turns={self._ctx_turns} "
                              f"stream={'early' if self.early else 'late'}")
        return _join_sentences(self._sents)

    def abort(self) -> None:
        """放弃会话并关闭连接；没上传过音频的会话不计费。可重复调用。"""
        with self._lock:
            self._aborted = True
        self._close()

    def _close(self) -> None:
        """后台关连接：websocket 关闭握手会同步等服务端回帧（最长数秒），不能挡在出字路径上。"""
        rec, self._rec = self._rec, None
        if rec is not None:
            def close():
                try:
                    rec.close()
                except Exception:
                    pass
            threading.Thread(target=close, daemon=True, name="asr-stream-close").start()


class BailianASRClient:
    def __init__(self, api_key: str, url: str = "", model: str = "qwen-audio-3.0-asr-flash",
                 hotwords_yaml_path: str = "", timeout_sec: float = 15.0,
                 hedge_models=DEFAULT_HEDGE_MODELS, hedge_after_sec: float = 2.5,
                 timeout_ratio: float = 0.35, hedge_after_ratio: float = 0.1,
                 workspace: str = "", context_turns: int = 3, context_max_chars: int = 300,
                 logger: Optional[Callable[[str], None]] = None):
        self.api_key = api_key
        self.model = model
        self.hotwords_yaml_path = hotwords_yaml_path
        self.timeout_sec = timeout_sec
        self.hedge_after_sec = max(0.3, float(hedge_after_sec))
        # 长音频识别时延 ≈ 2s + 0.11×音频秒数（2026-11-02 实测 120s≈15s、180s≈22s），超时和对冲阈值
        # 按音频时长放大：取 max(固定值, 比例×时长)，≤25s 的短句仍是原来的 15s / 2.5s
        self.timeout_ratio = max(0.0, float(timeout_ratio))
        self.hedge_after_ratio = max(0.0, float(hedge_after_ratio))
        self.hedge_models = [m.strip() for m in hedge_models if m and m.strip() and m.strip() != model]
        self.logger = logger or (lambda msg: None)
        # 主模型若是兼容协议且调用方给了自定义 URL，沿用；原生协议一律用官方原生地址
        self._compat_url = url if (url and "compatible-mode" in url) else COMPAT_URL
        # 结果信息，供调用方记日志/提示
        self.last_error = ""
        self.last_failed = False      # True = 所有模型都没给出结果（这句话丢了）
        self.last_model = ""          # 实际胜出的模型
        self.last_hedged = False      # 这次是否触发过对冲
        self.last_stream = ""         # 流式会话的用法：early=说话期间就上传 / late=说完才整段发 / ""=没用
        self._sessions: Dict[str, Any] = {}
        self._sess_lock = threading.Lock()
        # *-message（WebSocket）用：工作空间可选（不配则走旧共享网关）；最近几句已输入的话作前文
        self.workspace = workspace
        self.context_max_chars = max(0, int(context_max_chars))
        self._history: "collections.deque[str]" = collections.deque(maxlen=max(1, int(context_turns)))
        self._history_on = int(context_turns) > 0
        self._dash = None
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="asr")

    # ------------------------------------------------------------ 内部工具
    def _log(self, msg: str) -> None:
        try:
            self.logger(msg)
        except Exception:
            pass

    def _session(self, model: str):
        """每个模型一条独立的 keep-alive 连接（并发请求不抢同一条连接）。"""
        with self._sess_lock:
            s = self._sessions.get(model)
            if s is None:
                import requests
                s = requests.Session()
                s.trust_env = False  # 直连，不走环境里的代理变量（daemon 本来也没有）
                headers = {"Content-Type": "application/json",
                           "Authorization": f"Bearer {self.api_key}"}
                if _is_native(model):
                    headers["X-DashScope-SSE"] = "disable"
                s.headers.update(headers)
                self._sessions[model] = s
            return s

    def _vocabulary(self) -> dict:
        vocab: Dict[str, int] = {}
        cats = load_hotword_categories(self.hotwords_yaml_path) if self.hotwords_yaml_path else {}
        for cat, terms in cats.items():
            w = CATEGORY_WEIGHT.get(cat, 3)
            if w == 50:
                terms = terms[:SUPER_WEIGHT_CAP]
            for t in terms:
                vocab[t] = w
        return vocab

    def _context_text(self) -> str:
        cats = load_hotword_categories(self.hotwords_yaml_path) if self.hotwords_yaml_path else {}
        if not cats:
            return ""
        parts = [f"{c}: {'、'.join(ts)}" for c, ts in cats.items()]
        return ("本段对话涉及以下技术词汇——" + "; ".join(parts))[:200]

    @staticmethod
    def _encode(samples: np.ndarray, sample_rate: int) -> str:
        buf = io.BytesIO()
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm.tobytes())
        return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def remember(self, text: str) -> None:
        """记下一句已成功输入的话，作为 *-message 下一句识别的前文。"""
        text = (text or "").strip()
        if text and self._history_on:
            self._history.append(text)

    def _message_context(self) -> Optional[dict]:
        if not self._history:
            return None
        joined = " / ".join(self._history)[-self.context_max_chars:]
        return {"context": [
            {"role": "user", "content": [{"type": "input_text", "text": "我前面说过：" + joined}]},
            {"role": "assistant", "content": [{"type": "text", "text": "好的。"}]},
        ]}

    def preload(self) -> None:
        """启动时就加载 dashscope 并建好 Client（约 0.3s），免得第一句话才付这笔开销。"""
        if _is_message(self.model) or any(map(_is_message, self.hedge_models)):
            self._dash_client()

    def _dash_client(self):
        import warnings
        import dashscope  # 只有用 *-message 时才需要；普通版不依赖它
        warnings.filterwarnings("ignore", message="no workspace configured")
        with self._sess_lock:
            if self._dash is None:
                self._dash = dashscope.Client(api_key=self.api_key, region="cn-beijing", workspace=self.workspace or None)
        return self._dash

    def _new_recognition(self, model: str, timeout_sec: float):
        """建一个 *-message 识别会话（未 start），带热词与前文；返回 (会话, 前文句数)。"""
        params: Dict[str, Any] = {}
        vocab = self._vocabulary()
        if vocab:
            params["vocabulary"] = vocab
        ctx = self._message_context()
        rec = self._dash_client().audio.asr.recognize(model=model, format="pcm", sample_rate=16000,
                                                      parameters=params, input=ctx, timeout=timeout_sec)
        return rec, (len(self._history) if ctx else 0)

    def open_stream(self, max_audio_sec: float = 180.0) -> Optional[MessageStream]:
        """主模型是 *-message 时返回一个正在后台建连的流式会话，否则 None（调用方照旧整段识别）。
        读超时放宽到整段上限之外：一句话很长时服务端可能长时间不推事件，结果等待由对冲流程兜底。"""
        if not (self.api_key and _is_message(self.model)):
            return None
        return MessageStream(self, self.model, recv_timeout=max_audio_sec + 30.0)

    def _call_message(self, model: str, pcm: bytes, timeout_sec: float) -> str:
        """qwen-audio-3.1-asr-flash-message：WebSocket 会话，整段 PCM 按 100ms 一包发完再取结果。"""
        rec, ctx_turns = self._new_recognition(model, timeout_sec)
        sents: Dict[Any, str] = {}
        usage = None
        with rec:
            for i in range(0, len(pcm), 3200):
                rec.send_audio_frame(pcm[i:i + 3200])
            rec.stop()
            for ev in rec.events():
                payload = ev.get("payload") or {}
                sent = (payload.get("output") or {}).get("sentence") or {}
                if sent.get("sentence_end") and not sent.get("heartbeat"):
                    sents[sent.get("sentence_id")] = sent.get("text", "")
                usage = payload.get("usage") or usage
        if usage:
            self._log(f"ASR_USAGE model={model} in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
                      f"ctx_turns={ctx_turns}")
        return _join_sentences(sents)

    # ------------------------------------------------------------ 单模型调用
    def _call_one(self, model: str, data_uri: str, context: str, timeout_sec: float = 0.0, pcm: bytes = b"") -> str:
        """返回识别文本（可为空串=无语音）；任何错误抛异常。"""
        timeout_sec = timeout_sec or self.timeout_sec
        if _is_message(model):
            return self._call_message(model, pcm, timeout_sec)
        if _is_native(model):
            payload = {
                "model": model,
                "input": {"messages": [{"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {"data": data_uri}}]}]},
                "parameters": {"format": "wav", "sample_rate": "16000"},
            }
            # qwen-audio-3.x 支持即时热词（3.1 实测带上 37 条热词每次请求多计 127 输入 token，确认被读入）
            if model.startswith(("qwen-audio-3.0", "qwen-audio-3.1")):
                vocab = self._vocabulary()
                if vocab:
                    payload["parameters"]["vocabulary"] = vocab
            resp = self._session(model).post(NATIVE_URL, json=payload, timeout=timeout_sec)
            if resp.status_code == 400 and "NO_WORDS" in resp.text:
                return ""  # 纯静音/无词：正常情况
            resp.raise_for_status()
            out = resp.json().get("output", {})
            if isinstance(out.get("text"), str):
                return out["text"].strip()
            sent = out.get("sentence")
            if isinstance(sent, dict) and isinstance(sent.get("text"), str):
                return sent["text"].strip()
            return ""
        messages = []
        ctx = context or self._context_text()
        if ctx:
            messages.append({"role": "system", "content": [{"text": ctx}]})
        messages.append({"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": data_uri}}]})
        resp = self._session(model).post(self._compat_url, json={"model": model, "messages": messages},
                                         timeout=timeout_sec)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]["content"]
        if isinstance(msg, list):
            return "".join(p.get("text", "") for p in msg if isinstance(p, dict)).strip()
        return str(msg).strip()

    # ------------------------------------------------------------ 对冲主流程
    def transcribe_samples(self, samples: np.ndarray, sample_rate: int = 16000,
                           context: str = "", stream: Optional[MessageStream] = None) -> str:
        """对冲识别。传入 stream 时，主模型这一路改为取流式会话的结果：已在说话期间上传的直接收尾；
        预建但还没上传的，此时（声纹已通过）把整段发上去；还没连上或已作废的放弃，照常整段识别。"""
        self.last_error = ""
        self.last_failed = False
        self.last_model = ""
        self.last_hedged = False
        self.last_stream = ""
        if not self.api_key:
            self.last_error = "api_key_missing"
            self.last_failed = True
            if stream is not None:
                stream.abort()
            return ""
        try:
            return self._transcribe(samples, sample_rate, context, stream)
        finally:
            if stream is not None:
                stream.abort()  # 结果已定（或对冲胜出），关掉连接

    def _transcribe(self, samples: np.ndarray, sample_rate: int, context: str,
                    stream: Optional[MessageStream]) -> str:
        data_uri = self._encode(samples, sample_rate)
        models = [self.model] + self.hedge_models
        need_pcm = stream is not None or any(map(_is_message, models))
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes() if need_pcm else b""
        audio_sec = len(samples) / float(sample_rate)
        timeout_sec = max(self.timeout_sec, audio_sec * self.timeout_ratio)
        hedge_after_sec = max(self.hedge_after_sec, audio_sec * self.hedge_after_ratio)
        if stream is not None and not stream.streaming:
            if not (stream.wait_ready(1.0) and stream.begin(pcm, early=False)):
                reason = type(stream.error).__name__ if stream.error else ("expired" if stream.ready() else "not_ready")
                self._log(f"ASR_STREAM_SKIP reason={reason}")
                stream.abort()
                stream = None
        if stream is not None:
            stream.end()
            self.last_stream = "early" if stream.early else "late"
            if stream.early:
                hedge_after_sec = self.hedge_after_sec  # 服务端只剩最后一小段要处理，慢了就该对冲，不按全长放大
        t0 = time.time()
        deadline = t0 + timeout_sec
        done_q: "queue.Queue" = queue.Queue()
        inflight: Dict[object, str] = {}
        state = {"next": 0, "last_launch": t0, "pending": 0}
        errors: List[str] = []

        def launch() -> None:
            m = models[state["next"]]
            state["next"] += 1
            state["last_launch"] = time.time()
            state["pending"] += 1
            if state["next"] == 1 and stream is not None:
                f = self._pool.submit(stream.result, timeout_sec)
            else:
                f = self._pool.submit(self._call_one, m, data_uri, context, timeout_sec, pcm)
            inflight[f] = m
            f.add_done_callback(done_q.put)
            if state["next"] > 1:
                self.last_hedged = True
                self._log(f"ASR_HEDGE launch model={m} after_ms={int((time.time()-t0)*1000)}")

        launch()
        empty_seen = False
        while True:
            now = time.time()
            if now >= deadline:
                break
            more = state["next"] < len(models)
            if state["pending"] == 0 and not more:
                break
            wait = deadline - now
            if more:
                wait = min(wait, max(0.0, state["last_launch"] + hedge_after_sec - now))
            try:
                f = done_q.get(timeout=wait)
            except queue.Empty:
                if state["next"] < len(models) and time.time() < deadline:
                    launch()  # 到点还没回 → 对冲
                continue
            state["pending"] -= 1
            m = inflight[f]
            try:
                text = f.result()
            except Exception as e:
                errors.append(f"{m}:{type(e).__name__}:{str(e)[:80]}")
                self._log(f"ASR_ERROR model={m} {type(e).__name__}: {str(e)[:100]}")
                if state["next"] < len(models):
                    launch()  # 失败立刻换下一个，不必等满 hedge_after
                continue
            if text:
                self.last_model = m
                ms = int((time.time() - t0) * 1000)
                if self.last_hedged or m != self.model:
                    self._log(f"ASR_WIN model={m} total_ms={ms}")
                return text
            # 空文本：只有在没有别的请求在途时才当真；否则继续等一个非空的
            empty_seen = True
            self.last_model = m
            if state["pending"] == 0:
                break  # 没有别的在途请求：这就是最终答案(无语音)，不再启动备用模型
        if empty_seen:
            return ""  # 有模型明确返回"无语音"
        self.last_failed = True
        self.last_error = "; ".join(errors) or f"timeout>{timeout_sec:.0f}s"
        self._log(f"ASR_FAILED models={models[:state['next']]} err={self.last_error}")
        return ""
