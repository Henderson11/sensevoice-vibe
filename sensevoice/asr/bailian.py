"""百炼云端 ASR 客户端（带对冲请求 / 多模型兜底）。

只依赖 numpy / requests / pyyaml，不 import funasr/torch，所以 Linux 的 daemon 和
Windows 的 WinVoice 共用这同一份文件（Windows 部署时拷成 asr_bailian.py）。

为什么要对冲(hedging)：
  qwen-audio-3.0-asr-flash 服务端时快时慢——快时 0.5~1.5s，慢时稳定卡在 ~10s，且会持续
  几十分钟（2026-10-15 实测 14 次里 11 次 >5s，还有一句 15s 超时被丢）。客户端没法修，
  只能让系统扛得住：主模型 hedge_after 秒内没回，就并发再发一份给备用模型，谁先成功用谁。

模型 → 协议（按名字前缀自动选择）：
  qwen-audio-* / fun-asr-*  → DashScope 原生 multimodal-generation
  其它(qwen3-asr-flash 等)   → OpenAI 兼容 chat/completions
热词：
  qwen-audio-3.0-*  → parameters.vocabulary 按分类加权（common_errors=超级热词 50）
  兼容协议模型      → system message 自然语言 context（≤200 字）
  fun-asr-* 等其它  → 不支持即时热词，不传（官方文档：只有 qwen-audio-3.0 支持）
"""

from __future__ import annotations

import base64
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


class BailianASRClient:
    def __init__(self, api_key: str, url: str = "", model: str = "qwen-audio-3.0-asr-flash",
                 hotwords_yaml_path: str = "", timeout_sec: float = 15.0,
                 hedge_models=DEFAULT_HEDGE_MODELS, hedge_after_sec: float = 2.5,
                 timeout_ratio: float = 0.35, hedge_after_ratio: float = 0.1,
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
        self._sessions: Dict[str, Any] = {}
        self._sess_lock = threading.Lock()
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

    # ------------------------------------------------------------ 单模型调用
    def _call_one(self, model: str, data_uri: str, context: str, timeout_sec: float = 0.0) -> str:
        """返回识别文本（可为空串=无语音）；任何错误抛异常。"""
        timeout_sec = timeout_sec or self.timeout_sec
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
                           context: str = "") -> str:
        self.last_error = ""
        self.last_failed = False
        self.last_model = ""
        self.last_hedged = False
        if not self.api_key:
            self.last_error = "api_key_missing"
            self.last_failed = True
            return ""
        data_uri = self._encode(samples, sample_rate)
        models = [self.model] + self.hedge_models
        audio_sec = len(samples) / float(sample_rate)
        timeout_sec = max(self.timeout_sec, audio_sec * self.timeout_ratio)
        hedge_after_sec = max(self.hedge_after_sec, audio_sec * self.hedge_after_ratio)
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
            f = self._pool.submit(self._call_one, m, data_uri, context, timeout_sec)
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
