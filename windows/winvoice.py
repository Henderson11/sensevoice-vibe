"""WinVoice —— SenseVoice 语音输入的 Windows 版。

流程: 麦克风(sounddevice) → webrtcvad 断句 → 百炼云端 ASR → (可选)内网 LLM 润色 → 光标处注入文字
热键: 默认 F8 开/关（全局，系统级吞掉，不会透传给前台窗口/远程桌面）

用法:
  python winvoice.py                      # 正常运行（读同目录 config.env）
  python winvoice.py --list-devices       # 列出音频输入设备
  python winvoice.py --selftest-mic 3     # 录 3 秒，打印音量，验证麦克风可用
  python winvoice.py --selftest-asr a.wav # 用 wav 文件测百炼 ASR（16k 单声道）
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import ctypes
import os
import queue
import re
import sys
import threading
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 16000
FRAME_MS = 20
FRAME_SAMPLES = SR * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
LOG_PATH = os.path.join(HERE, "winvoice.log")


# ------------------------------------------------------------------ 配置 / 日志

def load_env_file(path: str) -> None:
    """读 KEY=VALUE 文件进 os.environ（已有的环境变量优先，不覆盖）。"""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = re.split(r"\s+#", v, maxsplit=1)[0]  # 去掉行尾注释（"值   # 说明"）
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with _log_lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        try:
            sys.stdout.write(line.encode(sys.stdout.encoding or "utf-8", "replace")
                             .decode(sys.stdout.encoding or "utf-8", "replace") + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def beep(kind: str) -> None:
    def _b():
        try:
            import winsound
            seq = {"on": [(1000, 90), (1500, 110)], "off": [(1500, 90), (800, 130)],
                   "err": [(300, 250)]}[kind]
            for f, d in seq:
                winsound.Beep(f, d)
        except Exception:
            pass
    threading.Thread(target=_b, daemon=True).start()


def _hotkey_hint(spec: str) -> str:
    names = {"DOUBLETAP:CTRL": "双击 Ctrl", "TAP:RCTRL": "单击右 Ctrl", "TAP:RALT": "单击右 Alt",
             "TAP:LCTRL": "单击左 Ctrl", "TAP:PAUSE": "按 Pause"}
    return names.get(spec.strip().upper(), spec)


class _ToastWorker:
    """常驻 PowerShell 子进程专门弹 Toast。
    之前每次弹窗都新起 PowerShell，实测 ~1.0s 才出来（开关提示明显滞后）；
    常驻后启动时预热一次，之后每条只是往 stdin 写一行，几十毫秒内弹出。"""

    _PS = r"""
$ErrorActionPreference='SilentlyContinue'
$null=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]
$null=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]
$app='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$n=[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app)
while(($line=[Console]::In.ReadLine()) -ne $null){
  $f=$line.Split('|')
  $title=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($f[0]))
  $body=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($f[1]))
  $audio=''; if($f[2] -ne '1'){ $audio='<audio silent="true"/>' }
  $x=New-Object Windows.Data.Xml.Dom.XmlDocument
  $x.LoadXml("<toast duration='short'><visual><binding template='ToastGeneric'><text>$title</text><text>$body</text></binding></visual>$audio</toast>")
  $t=[Windows.UI.Notifications.ToastNotification]::new($x); $t.Tag='winvoice'; $t.Group='winvoice'
  $n.Show($t)
}
"""

    def __init__(self):
        self.proc = None
        self._lock = threading.Lock()

    def ensure(self):
        import base64
        import subprocess
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            enc = base64.b64encode(self._PS.encode("utf-16-le")).decode()
            self.proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-EncodedCommand", enc],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)  # CREATE_NO_WINDOW

    def show(self, title: str, body: str, sound: bool):
        import base64
        b = lambda t: base64.b64encode(t.encode("utf-8")).decode()
        line = f"{b(title)}|{b(body)}|{'1' if sound else '0'}\n".encode("ascii")
        for _ in range(2):  # 子进程若意外退出，重启后重发一次
            self.ensure()
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                return
            except (OSError, ValueError):
                self.proc = None

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass


_toast_worker = _ToastWorker()


def _toast(title: str, body: str, sound: bool) -> None:
    _toast_worker.show(title, body, sound)


def notify(kind: str, detail: str = "") -> None:
    """开/关/错误提示。WINVOICE_NOTIFY=toast(默认,弹窗)|beep(蜂鸣)|none(静默)；
    WINVOICE_NOTIFY_SOUND=1 时弹窗带 Windows 自带的通知提示音（默认静音）。"""
    mode = os.environ.get("WINVOICE_NOTIFY", "toast").lower()
    if mode == "none":
        return
    if mode == "beep":
        beep(kind)
        return
    hint = _hotkey_hint(os.environ.get("WINVOICE_HOTKEY", "DOUBLETAP:CTRL"))
    text = {"on": ("🎙 语音输入 已开启", f"{hint} 关闭"),
            "off": ("语音输入 已关闭", f"{hint} 开启"),
            "err": ("语音输入 出错", "请查看 winvoice.log")}[kind]
    if detail:
        text = (text[0], detail)
    sound = os.environ.get("WINVOICE_NOTIFY_SOUND", "0") == "1"
    try:
        _toast(text[0], text[1], sound)
    except Exception as e:
        log(f"NOTIFY_FAILED {type(e).__name__}: {e}")


# ------------------------------------------------------------------ 设备

def pick_input_device(spec: str):
    import sounddevice as sd
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    spec_l = spec.lower()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and spec_l in d["name"].lower():
            return i
    log(f"WARN 找不到名称含 '{spec}' 的输入设备，改用系统默认")
    return None


def cmd_list_devices() -> None:
    import sounddevice as sd
    apis = sd.query_hostapis()
    default_in = sd.default.device[0]
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            mark = "*" if i == default_in else " "
            print(f"{mark}[{i:2d}] {d['name']}  ({apis[d['hostapi']]['name']}, "
                  f"{d['max_input_channels']}ch, {int(d['default_samplerate'])}Hz)")


def cmd_selftest_mic(seconds: float) -> int:
    import sounddevice as sd
    dev = pick_input_device(os.environ.get("WINVOICE_INPUT_DEVICE", ""))
    print(f"录音 {seconds}s，请正常说话... 设备={dev if dev is not None else '默认'}")
    data = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16", device=dev)
    sd.wait()
    x = data.astype(np.float32).flatten() / 32768.0
    rms = float(np.sqrt(np.mean(x ** 2)))
    print(f"RMS={rms:.4f} peak={float(np.max(np.abs(x))):.3f}  "
          f"({'正常' if rms > 0.01 else '偏小/可能静音，检查麦克风或系统输入音量'})")
    return 0


# ------------------------------------------------------------------ 引擎

class Engine:
    def __init__(self):
        import webrtcvad
        from asr_bailian import BailianASRClient

        self.asr = BailianASRClient(
            api_key=os.environ.get("SENSEVOICE_BAILIAN_API_KEY", ""),
            url=os.environ.get("SENSEVOICE_BAILIAN_URL", ""),
            model=os.environ.get("SENSEVOICE_BAILIAN_MODEL", "qwen-audio-3.1-asr-flash"),
            hotwords_yaml_path=os.environ.get("WINVOICE_HOTWORDS", os.path.join(HERE, "hotwords.yaml")),
            # 对冲：主模型 hedge_after 内没回，就并发发给备用模型，谁先成功用谁（与 Linux 版同一份代码）
            hedge_models=[m for m in os.environ.get(
                "SENSEVOICE_BAILIAN_HEDGE_MODELS", "fun-asr-flash-2026-06-15,qwen3-asr-flash").split(",") if m.strip()],
            hedge_after_sec=env_float("SENSEVOICE_BAILIAN_HEDGE_AFTER_MS", 2500) / 1000.0,
            timeout_sec=env_float("SENSEVOICE_BAILIAN_TIMEOUT_SEC", 15),
            timeout_ratio=env_float("SENSEVOICE_BAILIAN_TIMEOUT_RATIO", 0.35),
            hedge_after_ratio=env_float("SENSEVOICE_BAILIAN_HEDGE_AFTER_RATIO", 0.1),
            workspace=os.environ.get("SENSEVOICE_BAILIAN_WORKSPACE", ""),
            context_turns=env_int("SENSEVOICE_BAILIAN_CONTEXT_TURNS", 3),
            context_max_chars=env_int("SENSEVOICE_BAILIAN_CONTEXT_MAX_CHARS", 300),
            logger=lambda m: log(m),
        )
        self.vad = webrtcvad.Vad(env_int("SENSEVOICE_STREAM_VAD_AGGRESSIVENESS", 2))
        self.end_frames = max(1, env_int("SENSEVOICE_STREAM_ENDPOINT_MS", 1500) // FRAME_MS)
        self.start_frames = max(1, env_int("SENSEVOICE_STREAM_START_MS", 240) // FRAME_MS)
        self.pre_roll_frames = max(1, env_int("SENSEVOICE_STREAM_PRE_ROLL_MS", 700) // FRAME_MS)
        self.max_segment_ms = env_int("SENSEVOICE_STREAM_MAX_SEGMENT_MS", 180000)
        # 软切：段长进入 [上限-窗口, 上限) 后改用短停顿阈值收尾，不在上限处硬切到词中间（与 Linux 版同逻辑）
        self.soft_cut_frames = min(self.end_frames,
                                   max(1, env_int("SENSEVOICE_STREAM_SOFT_CUT_SILENCE_MS", 300) // FRAME_MS))
        self.soft_cut_start_ms = max(self.max_segment_ms // 2,
                                     self.max_segment_ms - env_int("SENSEVOICE_STREAM_SOFT_CUT_WINDOW_MS", 20000))
        self.min_segment_ms = env_int("SENSEVOICE_STREAM_MIN_SEGMENT_MS", 400)
        self.inject_mode = os.environ.get("WINVOICE_INJECT_MODE", "type").lower()
        self.input_spec = os.environ.get("WINVOICE_INPUT_DEVICE", "")

        self.active = False
        self.stop_flag = False
        self.toggle_evt = threading.Event()
        self.audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=2000)
        self.stream = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="proc")
        # 声纹逐窗复核（与 Linux 版同逻辑）：说话过程中每满一窗就后台算分；所有声纹计算走这个单线程池
        self._spk_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="spk")
        self._spk_win_bytes = max(0, env_int("SENSEVOICE_SPK_WINDOW_MS", 5000) * SR * 2 // 1000)
        self._spk_gray = env_float("SENSEVOICE_SPK_WINDOW_GRAY", 0.08)
        # 边说边发（与 Linux 版同逻辑）：开口即预建连接，任一窗声纹达到门槛才开始上传
        self._asr_stream_on = os.environ.get("SENSEVOICE_ASR_STREAM", "1") == "1"
        self._asr_stream_win = env_float("SENSEVOICE_ASR_STREAM_WIN_SCORE", 0.60)
        self._asr_stream = None
        threading.Thread(target=self._preload_asr, daemon=True).start()
        self.post = None  # 后台线程构建（含预热探测），好了才参与润色
        # 声纹门禁（复用 Linux 版 verifier.py）。加载要几秒，放后台；加载完之前到来的句子会等它。
        self.spk = None
        self.spk_enabled = os.environ.get("SENSEVOICE_SPK_ENABLE", "0") == "1"
        self.spk_ready = threading.Event()
        if self.spk_enabled:
            threading.Thread(target=self._init_spk, daemon=True).start()
        else:
            self.spk_ready.set()
        if os.environ.get("SENSEVOICE_POST_LLM_ENABLE", "0") == "1":
            threading.Thread(target=self._init_post, daemon=True).start()
        self._reset_segmenter()

    def _preload_asr(self):
        try:
            self.asr.preload()
        except Exception as e:
            log(f"ASR_PRELOAD_ERROR {type(e).__name__}: {e}")

    # ---- 边说边发
    def _open_asr_stream(self):
        self._abort_asr_stream()
        if self._asr_stream_on:
            self._asr_stream = self.asr.open_stream(self.max_segment_ms / 1000.0)

    def _abort_asr_stream(self):
        if self._asr_stream is not None:
            self._asr_stream.abort()
            self._asr_stream = None

    def _upload_cleared(self):
        """声纹门禁没开 → 可以上传；门禁开了但模型没就绪 → 不上传（fail-closed）；否则要有一窗达到门槛。"""
        if not self.spk_enabled:
            return 1.0
        if self.spk is None:
            return None
        for f in self._spk_futs:
            if f.done() and f.exception() is None and f.result()[0] >= self._asr_stream_win:
                return f.result()[0]
        return None

    def _pump_asr_stream(self, frame: bytes):
        st = self._asr_stream
        if st.streaming:
            st.feed(frame)
            return
        if st.error is None and st.expired():  # 预建会话空等太久（服务端 ~23s 断开），换一条新的
            self._open_asr_stream()
            return
        win = self._upload_cleared()
        if win is not None and st.begin(bytes(self.segment), early=True):
            log(f"ASR_STREAM_BEGIN at_ms={len(self.segment) * 1000 // (SR * 2)} win={win:.2f}")

    # ---- 声纹门禁
    def _init_spk(self):
        t0 = time.time()
        try:
            from verifier import SpeakerVerifierGate
            g = os.environ.get
            model = g("WINVOICE_SPK_MODEL", os.path.join(HERE, "models", "eres2netv2"))
            enroll = g("WINVOICE_SPK_ENROLL_DIR", os.path.join(HERE, "speaker_enroll.d"))
            self.spk = SpeakerVerifierGate(
                enabled=True, enroll_wav=enroll,
                threshold=env_float("SENSEVOICE_SPK_THRESHOLD", 0.5),
                min_ms=env_int("SENSEVOICE_SPK_MIN_MS", 900),
                model_id=model, cache_dir=model, device="cpu",
                score_agg=g("SENSEVOICE_SPK_AGG", "topk_mean"),
                score_topk=env_int("SENSEVOICE_SPK_TOPK", 1),
                auto_enroll=False, auto_enroll_dir="", auto_enroll_min_score=0.7,
                auto_enroll_min_ms=2200, auto_enroll_cooldown_sec=90, auto_enroll_max_templates=20,
                adaptive_enable=g("SENSEVOICE_SPK_ADAPTIVE", "1") == "1",
                adaptive_window=env_int("SENSEVOICE_SPK_ADAPTIVE_WINDOW", 80),
                adaptive_min_samples=env_int("SENSEVOICE_SPK_ADAPTIVE_MIN_SAMPLES", 10),
                adaptive_floor=env_float("SENSEVOICE_SPK_ADAPTIVE_FLOOR", 0.52),
                adaptive_margin=env_float("SENSEVOICE_SPK_ADAPTIVE_MARGIN", 0.04),
                prune_outliers=g("SENSEVOICE_SPK_PRUNE_OUTLIERS", "1") == "1",
                prune_keep=env_int("SENSEVOICE_SPK_PRUNE_KEEP", 10),
            )
            log(f"SPK_GATE enabled={int(self.spk.enabled)} thr={self.spk.threshold} "
                f"load_s={time.time()-t0:.1f} reason={self.spk.reason}")
        except Exception as e:
            # fail-closed：加载失败时宁可丢弃所有语音也不放行（同 Linux 版理由：
            # 静默降级会让旁人语音直接通过到 ASR）
            self.spk = None
            log(f"SPK_LOAD_FAILED {type(e).__name__}: {e}")
            notify("err")
        finally:
            self.spk_ready.set()

    # ---- 润色模块（复用 Linux 版 postprocessor.py，纯标准库）
    def _init_post(self):
        try:
            from postprocessor import LLMPostProcessor
            g = os.environ.get
            p = LLMPostProcessor(
                enabled=True,
                base_url=g("SENSEVOICE_POST_LLM_BASE_URL", ""),
                api_key=g("SENSEVOICE_POST_LLM_API_KEY", ""),
                model=g("SENSEVOICE_POST_LLM_MODEL", ""),
                fallback_model=g("SENSEVOICE_POST_LLM_FALLBACK_MODEL", ""),
                mode=g("SENSEVOICE_POST_LLM_MODE", "polish_coding_aggressive"),
                timeout_ms=env_int("SENSEVOICE_POST_LLM_TIMEOUT_MS", 1800),
                max_tokens=env_int("SENSEVOICE_POST_LLM_MAX_TOKENS", 96),
                temperature=env_float("SENSEVOICE_POST_LLM_TEMPERATURE", 0.0),
                circuit_max_fails=env_int("SENSEVOICE_POST_LLM_CIRCUIT_MAX_FAILS", 4),
                circuit_cooldown_sec=env_int("SENSEVOICE_POST_LLM_CIRCUIT_COOLDOWN_SEC", 25),
                hard_cooldown_sec=env_int("SENSEVOICE_POST_LLM_HARD_COOLDOWN_SEC", 300),
                retry_on_timeout=g("SENSEVOICE_POST_LLM_RETRY_ON_TIMEOUT", "1") == "1",
                retry_backoff_ms=env_int("SENSEVOICE_POST_LLM_RETRY_BACKOFF_MS", 80),
                model_auto=g("SENSEVOICE_POST_LLM_MODEL_AUTO", "1") == "1",
                model_probe_timeout_ms=env_int("SENSEVOICE_POST_LLM_MODEL_PROBE_TIMEOUT_MS", 450),
                min_chars=env_int("SENSEVOICE_POST_LLM_MIN_CHARS", 5),
                dynamic_max_tokens=g("SENSEVOICE_POST_LLM_DYNAMIC_MAX_TOKENS", "0") == "1",
                output_token_factor=env_float("SENSEVOICE_POST_LLM_OUTPUT_TOKEN_FACTOR", 0.7),
                pyramid_min_chars=env_int("SENSEVOICE_POST_LLM_PYRAMID_MIN_CHARS", 40),
            )
            fb = g("SENSEVOICE_POST_LLM_FALLBACK_BASE_URL", "")
            if fb:
                p.fallback_url = LLMPostProcessor._normalize_endpoint(fb)
                p.fallback_api_key = g("SENSEVOICE_POST_LLM_FALLBACK_API_KEY", "")
            self.post = p
            # reason 在预热失败时也是 "ready:..."，预热结果看 warmup（ok 或失败原因）
            log(f"POST_LLM enabled={int(p.enabled)} model={p.model} fallback={p.fallback_model} "
                f"url={p.url} fallback_url={p.fallback_url or '-'} reason={p.reason} warmup={p.last_error or 'ok'}")
        except Exception as e:
            log(f"POST_LLM init failed: {type(e).__name__}: {e}")

    # ---- 采集
    def _reset_segmenter(self):
        self.in_speech = False
        self.speech_frames = 0
        self.trailing = 0
        self.segment = bytearray()
        self.pre_roll = collections.deque(maxlen=self.pre_roll_frames)
        self._spk_futs, self._spk_win_done = [], 0
        self._abort_asr_stream()

    def _audio_cb(self, indata, frames, t, status):
        if status:
            pass
        try:
            self.audio_q.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def start_capture(self):
        import sounddevice as sd
        dev = pick_input_device(self.input_spec)
        self.stream = sd.RawInputStream(samplerate=SR, channels=1, dtype="int16",
                                        blocksize=FRAME_SAMPLES, device=dev, callback=self._audio_cb)
        self.stream.start()
        self._mic_chk = {"t0": time.monotonic(), "max": 0.0, "n": 0, "done": False}
        log(f"STREAM_START device={dev if dev is not None else 'default'}")

    def stop_capture(self, reason: str):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
            log(f"STREAM_STOP reason={reason}")
        while not self.audio_q.empty():
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                break
        self._reset_segmenter()

    # ---- 开关
    def request_toggle(self):
        self.toggle_evt.set()

    def _apply_toggle(self):
        self.toggle_evt.clear()
        if not self.active:
            try:
                self.start_capture()
                self.active = True
                notify("on")
                log("TOGGLE active=1")
            except Exception as e:
                log(f"STREAM_START_ERROR {type(e).__name__}: {e}")
                notify("err")
        else:
            self.active = False
            self.stop_capture("inactive")
            notify("off")
            log("TOGGLE active=0")

    # ---- 主循环
    def run(self):
        while not self.stop_flag:
            if self.toggle_evt.is_set():
                self._apply_toggle()
            if not self.active:
                time.sleep(0.05)
                continue
            try:
                frame = self.audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if len(frame) != FRAME_BYTES:
                continue
            self._check_mic_alive(frame)
            self._feed(frame)
        self.stop_capture("shutdown")
        self.pool.shutdown(wait=False)

    def _check_mic_alive(self, frame: bytes) -> None:
        """哑麦检测：开启后 4 秒内全是纯数字静音(RMS≈0)，几乎一定是麦克风被静音/被系统或固件
        关掉了（10-19 实测：Windows 里麦克风静音=True，帧正常送达但全是 0，热键/日志全正常，
        表现就是"开了但没反应"）。告警一次，别让用户再靠猜。"""
        c = getattr(self, "_mic_chk", None)
        if not c or c["done"]:
            return
        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x)))
        c["max"] = max(c["max"], rms)
        c["n"] += 1
        if c["max"] >= 3e-4:        # 有真实信号 → 麦克风是活的，不再检查
            c["done"] = True
        elif time.monotonic() - c["t0"] >= 4.0:
            c["done"] = True
            log(f"MIC_SILENT 开启 4 秒内音量恒为 0（max_rms={c['max']:.6f}, frames={c['n']}）："
                f"麦克风可能被静音/被硬件开关或隐私设置关闭")
            notify("err", "麦克风没有声音：可能被静音（检查键盘上的麦克风静音键 / 系统声音设置）")

    def _feed(self, frame: bytes):
        voiced = self.vad.is_speech(frame, SR)
        if not self.in_speech:
            self.pre_roll.append(frame)
            self.speech_frames = self.speech_frames + 1 if voiced else 0
            if self.speech_frames >= self.start_frames:
                self.in_speech = True
                self.segment = bytearray(b"".join(self.pre_roll))
                self.pre_roll.clear()
                self.trailing = 0
                self._open_asr_stream()
                log("SPEECH_START")
            return
        self.segment.extend(frame)
        if (self._spk_win_bytes and self.spk is not None
                and len(self.segment) - self._spk_win_done >= self._spk_win_bytes):
            win = bytes(self.segment[self._spk_win_done:self._spk_win_done + self._spk_win_bytes])
            self._spk_win_done += self._spk_win_bytes
            self._spk_futs.append(self._spk_pool.submit(self._spk_score_window, win))
        if self._asr_stream is not None:
            self._pump_asr_stream(frame)
        self.trailing = 0 if voiced else self.trailing + 1
        seg_ms = len(self.segment) * 1000 // (SR * 2)
        end_frames = self.soft_cut_frames if seg_ms >= self.soft_cut_start_ms else self.end_frames
        if self.trailing >= end_frames or seg_ms >= self.max_segment_ms:
            seg = bytes(self.segment)
            # 尾巴去掉句末判停用的静音帧
            voiced_end = max(self._spk_win_done, len(self.segment) - self.trailing * FRAME_BYTES)
            spk_windows = (self._spk_futs, bytes(self.segment[self._spk_win_done:voiced_end])) if self._spk_futs else None
            stream, self._asr_stream = self._asr_stream, None  # 交给处理线程，采集线程不再碰它
            self._reset_segmenter()
            if seg_ms >= self.min_segment_ms:
                if stream is not None:
                    stream.end()  # 已在上传的先通知服务端收尾，和声纹尾窗计算并行
                self.pool.submit(self._process, seg, seg_ms, spk_windows, stream)
            elif stream is not None:
                stream.abort()
            if seg_ms >= self.max_segment_ms:
                log(f"SEG_MAX_CUT seg_ms={seg_ms}")
            elif seg_ms >= self.soft_cut_start_ms:
                log(f"SEG_SOFT_CUT seg_ms={seg_ms}")
            log("SPEECH_END")

    def _spk_score_window(self, pcm: bytes):
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        ms = len(x) * 1000.0 / SR
        _, score, _, thr = self.spk.verify(x, SR, ms)
        return float(score), float(thr), ms

    def _verify_speaker(self, samples, seg_ms, spk_windows):
        """有逐窗结果时：补算尾巴（≥1s）后按时长加权平均；均值落在 [阈值-gray, 阈值) 回退整段判定。"""
        t0 = time.time()
        mode = "whole"
        if spk_windows:
            futs, tail = spk_windows
            try:
                if len(tail) >= SR * 2:
                    futs = futs + [self._spk_pool.submit(self._spk_score_window, tail)]
                res = [f.result() for f in futs]
                score = sum(s * ms for s, _, ms in res) / sum(ms for _, _, ms in res)
                thr = res[-1][1]
                log(f"SPK_WINDOWS n={len(res)} mean={score:.3f} scores=[{' '.join(f'{s:.2f}' for s, _, _ in res)}]")
                if score >= thr or score < thr - self._spk_gray:
                    ok = score >= thr
                    log(f"SPK_TIME mode=windows wait_ms={int((time.time() - t0) * 1000)}")
                    return ok, score, ("pass" if ok else "below_threshold"), thr
                mode = "gray_whole"
            except Exception as e:
                log(f"SPK_WINDOWS_ERROR {type(e).__name__}: {e}")
                mode = "error_whole"
        ok, score, reason, thr = self._spk_pool.submit(self.spk.verify, samples, SR, seg_ms).result()
        log(f"SPK_TIME mode={mode} wait_ms={int((time.time() - t0) * 1000)}")
        return ok, score, reason, thr

    # ---- 一段话的处理：ASR → 润色 → 注入（单线程串行，保证顺序）
    def _process(self, seg: bytes, seg_ms: int, spk_windows=None, stream=None):
        t0 = time.time()
        try:
            samples = np.frombuffer(seg, dtype=np.int16).astype(np.float32) / 32768.0
            spk_score = None
            if self.spk_enabled:
                self.spk_ready.wait(120)
                if self.spk is None or not self.spk.enabled:
                    log("DROP_SPK_UNAVAILABLE 声纹门禁未就绪，按 fail-closed 丢弃该段")
                    if stream is not None:
                        stream.abort()
                    return
                ok, spk_score, reason, thr = self._verify_speaker(samples, seg_ms, spk_windows)
                if not ok:
                    if stream is not None:
                        if stream.streaming:  # 某一窗明确像本人、已上传，但整段没通过：结果丢弃（费用已发生）
                            log("ASR_STREAM_DISCARD reason=spk_reject")
                        stream.abort()
                    log(f"DROP_FINAL_SPK score={spk_score:.3f} thr={thr:.3f} reason={reason} "
                        f"spk_ms={int((time.time()-t0)*1000)}")
                    return
                log(f"SPK_PASS score={spk_score:.3f} thr={thr:.3f} reason={reason}")
            t_spk = time.time()
            text = self.asr.transcribe_samples(samples, SR, stream=stream)
            stream = None  # transcribe_samples 已负责关闭
            t_asr = time.time()
            if not text:
                if self.asr.last_failed:
                    log(f"ASR_DROPPED seg_ms={seg_ms} asr_ms={int((t_asr-t_spk)*1000)} 这句话未输入(全部ASR模型失败/超时) err={self.asr.last_error}")
                    notify("err", "识别失败/超时，这句话没有输入")
                else:
                    log(f"DROP_EMPTY seg_ms={seg_ms} asr_ms={int((t_asr-t_spk)*1000)}")
                return
            if not re.search(r"[\w一-鿿]", text):
                log(f"DROP_NO_WORDS text={text!r}")
                return
            raw = text
            # 润色状态写进 FINAL：失败/熔断/降级到外网备用模型时会原样返回或换模型，不记下来就看不出来
            if self.post is None:
                polish = "not_ready" if os.environ.get("SENSEVOICE_POST_LLM_ENABLE", "0") == "1" else "off"
            elif not self.post.enabled:
                polish = f"disabled:{self.post.reason}"
            else:
                text = self.post.process(text, route="high") or text
                polish = self.post.last_status or "?"
                if polish == "ok" and text == raw:
                    polish = "ok_unchanged"
                polish += f" polish_model={self.post.last_model or '-'}"
            t_llm = time.time()
            ok = self._inject(text)
            if ok:
                self.asr.remember(text)  # 百炼 *-message：作为下一句的前文
            t_end = time.time()
            if spk_score is not None:
                self.spk.on_success(spk_score, seg_ms, text)
            log(f"FINAL ok={int(ok)} seg_ms={seg_ms} spk_ms={int((t_spk-t0)*1000)} asr_ms={int((t_asr-t_spk)*1000)} "
                f"llm_ms={int((t_llm-t_asr)*1000)} polish={polish} inject_ms={int((t_end-t_llm)*1000)} "
                f"total_ms={int((t_end-t0)*1000)} model={self.asr.last_model}{'(hedged)' if self.asr.last_hedged else ''} "
                f"stream={self.asr.last_stream or 'off'} text={text}")
            if raw != text:
                log(f"POLISH_DIFF raw={raw} dst={text}")
        except Exception as e:
            log(f"PROCESS_ERROR {type(e).__name__}: {e}")
            if stream is not None:
                stream.abort()

    def _inject(self, text: str) -> bool:
        import win_input
        if self.inject_mode == "paste":
            return win_input.paste_text(text)
        return win_input.type_text(text)


# ------------------------------------------------------------------ 入口

def cmd_enroll(seconds: float, name: str) -> int:
    """用本机麦克风录一段声音作为声纹模板，存到 speaker_enroll.d/<name>.wav（重启后生效）。"""
    import sounddevice as sd
    dev = pick_input_device(os.environ.get("WINVOICE_INPUT_DEVICE", ""))
    out_dir = os.environ.get("WINVOICE_SPK_ENROLL_DIR", os.path.join(HERE, "speaker_enroll.d"))
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n将录音 {seconds:.0f} 秒。请用【平时语音输入的坐姿和音量】连续说话（读一段技术文字即可）。")
    for i in (3, 2, 1):
        print(f"  {i}...", flush=True)
        time.sleep(1)
    print("开始录音！", flush=True)
    data = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16", device=dev)
    sd.wait()
    path = os.path.join(out_dir, f"{name}.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(data.tobytes())
    rms = float(np.sqrt(np.mean((data.astype(np.float32) / 32768.0) ** 2)))
    print(f"已保存 {path}  (RMS={rms:.4f})。重启 WinVoice 后生效。")
    return 0


def cmd_selftest_spk() -> int:
    """同步加载声纹门禁，让每个注册模板互相打分，验证移植是否正确，并测单次耗时。"""
    os.environ["SENSEVOICE_SPK_ENABLE"] = "1"
    eng = Engine.__new__(Engine)
    eng.spk = None
    eng.spk_ready = threading.Event()
    eng._init_spk()
    if eng.spk is None:
        return 2
    d = os.environ.get("WINVOICE_SPK_ENROLL_DIR", os.path.join(HERE, "speaker_enroll.d"))
    for f in sorted(os.listdir(d)):
        if not f.lower().endswith(".wav"):
            continue
        with wave.open(os.path.join(d, f), "rb") as wf:
            sr = wf.getframerate()
            x = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        seg = x[: 5 * sr]
        t = time.time()
        ok, score, reason, thr = eng.spk.verify(seg, sr, len(seg) * 1000 / sr)
        print(f"{f}: 前5秒 vs 全部模板 score={score:.3f} thr={thr:.3f} pass={ok}  耗时{int((time.time()-t)*1000)}ms")
    noise = (np.random.RandomState(0).randn(16000 * 3) * 0.05).astype(np.float32)
    ok, score, reason, thr = eng.spk.verify(noise, 16000, 3000)
    print(f"白噪声(应被拒): score={score:.3f} pass={ok}")
    return 0


def cmd_selftest_hotkey(spec: str) -> int:
    """在用户桌面会话里用合成按键验证热键逻辑。需在交互会话运行（SSH 会话里钩子收不到按键）。"""
    import win_input
    hits = []
    lis = win_input.TapHotkeyListener(spec, lambda: hits.append(time.monotonic()), accept_injected=True,
                                      logger=log)
    lis.start()
    lis.ready.wait(3)
    if lis.error:
        log("SELFTEST FAIL " + lis.error)
        return 2
    vk = sorted(lis.vks)[-1]
    other = 0x87  # F24：几乎没有程序响应，自检时不会误触发别的功能

    def tap():
        win_input.synth_key(vk); time.sleep(0.05); win_input.synth_key(vk, up=True)

    def case(name, fn, expect):
        hits.clear(); fn(); time.sleep(0.7)
        ok = (len(hits) == expect)
        log(f"SELFTEST {'PASS' if ok else 'FAIL'} {name}: fired={len(hits)} expect={expect}")
        return ok

    def combo():  # Ctrl+C 之类：目标键按住期间按了别的键，不应触发
        win_input.synth_key(vk); time.sleep(0.03)
        win_input.synth_key(other); win_input.synth_key(other, up=True)
        time.sleep(0.03); win_input.synth_key(vk, up=True)

    def slow():   # 按住太久（>0.45s）不算单击
        win_input.synth_key(vk); time.sleep(0.7); win_input.synth_key(vk, up=True)

    def double():
        tap(); time.sleep(0.15); tap()

    def stale_double():  # 10-20 回归：锁屏(Win+L)漏掉 Win 的抬起，残留在 _held 里不能让热键永久失效
        lis._held[0x5B] = time.monotonic()
        double()

    results = []
    time.sleep(0.5)
    if lis.mode == "TAP":
        results += [case("单击应触发", tap, 1), case("组合键(Ctrl+C)不应触发", combo, 0),
                    case("长按不应触发", slow, 0)]
    else:
        results += [case("双击应触发", double, 1), case("单击不应触发", tap, 0),
                    case("组合键不应触发", combo, 0), case("残留脏键(锁屏漏抬起)后双击应触发", stale_double, 1)]
    lis.stop()
    log("SELFTEST " + ("ALL PASS" if all(results) else "HAS FAILURE") + f" spec={spec}")
    return 0 if all(results) else 1


def single_instance_guard() -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, False, "Local\\WinVoiceSingleton")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def main() -> int:
    for _n in ("stdout", "stderr"):
        if getattr(sys, _n) is None:
            setattr(sys, _n, open(os.devnull, "w", encoding="utf-8"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.env"))
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--selftest-mic", type=float, metavar="SECONDS")
    ap.add_argument("--selftest-asr", metavar="WAV")
    ap.add_argument("--selftest-notify", action="store_true", help="弹一条开启+一条关闭通知（需桌面会话）")
    ap.add_argument("--enroll", type=float, metavar="SECONDS", help="录声纹模板（本机麦克风）")
    ap.add_argument("--enroll-name", default="03_local", help="模板文件名(不含.wav)")
    ap.add_argument("--selftest-spk", action="store_true", help="验证声纹门禁移植是否正确")
    ap.add_argument("--selftest-hotkey", metavar="SPEC", help="如 TAP:RCTRL，需在桌面会话运行")
    args = ap.parse_args()
    load_env_file(args.config)

    if args.list_devices:
        cmd_list_devices()
        return 0
    if args.enroll:
        return cmd_enroll(args.enroll, args.enroll_name)
    if args.selftest_spk:
        return cmd_selftest_spk()
    if args.selftest_notify:
        _toast_worker.ensure(); time.sleep(2.5)
        t0 = time.time(); notify("on"); log(f"NOTIFY on dispatched in {int((time.time()-t0)*1000)}ms")
        time.sleep(4); notify("off"); time.sleep(2)
        return 0
    if args.selftest_hotkey:
        return cmd_selftest_hotkey(args.selftest_hotkey)
    if args.selftest_mic:
        return cmd_selftest_mic(args.selftest_mic)
    if args.selftest_asr:
        from asr_bailian import BailianASRClient
        with wave.open(args.selftest_asr, "rb") as wf:
            sr = wf.getframerate()
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        c = BailianASRClient(os.environ.get("SENSEVOICE_BAILIAN_API_KEY", ""),
                             os.environ.get("SENSEVOICE_BAILIAN_URL", ""),
                             os.environ.get("SENSEVOICE_BAILIAN_MODEL", "qwen-audio-3.0-asr-flash"),
                             os.path.join(HERE, "hotwords.yaml"))
        t = time.time()
        text = c.transcribe_samples(pcm, sr)
        print(f"asr_ms={int((time.time()-t)*1000)} text={text!r} err={c.last_error!r}")
        return 0

    if not single_instance_guard():
        print("WinVoice 已经在运行（同一用户会话只允许一个实例）")
        return 1

    import win_input
    if os.environ.get("WINVOICE_NOTIFY", "toast").lower() == "toast":
        _toast_worker.ensure()  # 预热，开关时通知几十毫秒内弹出
    eng = Engine()
    hk = win_input.make_hotkey_listener(os.environ.get("WINVOICE_HOTKEY", "TAP:RCTRL"), eng.request_toggle,
                                        logger=log)
    hk.start()
    hk.ready.wait(3)
    if hk.error:
        log("FATAL " + hk.error)
        notify("err")
        return 2
    log(f"READY hotkey={hk.spec} inject={eng.inject_mode} asr_model={eng.asr.model} "
        f"polish={os.environ.get('SENSEVOICE_POST_LLM_ENABLE', '0')}")
    try:
        eng.run()
    except KeyboardInterrupt:
        eng.stop_flag = True
    finally:
        hk.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
