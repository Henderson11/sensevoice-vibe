"""语音输入主循环：采集 → VAD 分段 → 声纹门禁 → 百炼云端识别 → LLM 润色 → IBus 输入。"""
import collections
import concurrent.futures
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import webrtcvad

from sensevoice.text.processing import compact_log_text, sanitize_transcript_text
from sensevoice.config.settings import SenseVoiceConfig
from sensevoice.asr.model import load_asr_client
from sensevoice.inject.ibus import IBusInjector
from sensevoice.llm.postprocessor import LLMPostProcessor
from sensevoice.speaker.verifier import SpeakerVerifierGate
from sensevoice.lexicon.project import ProjectLexicon
from sensevoice.vad.stream import make_arecord_process
from sensevoice.logging import append_state_log, append_jsonl_bounded
from sensevoice.control_socket import ControlServer
from sensevoice.recent import RecentStore


class SenseVoicePipeline:
    def __init__(self, args):
        self.args = args
        self.state_dir = os.path.dirname(args.state_log)
        os.makedirs(self.state_dir, exist_ok=True)

        # --- 状态（控制 socket 写入，主循环每轮消费）---
        self.active = args.active_on_start if args.resident else True
        self.model_ready = False
        self.stop_flag = False
        self.toggle_count = 0
        self.force_active: Optional[bool] = None

        # --- 组件（_init_components 中赋值，提前声明避免 finally 中 AttributeError）---
        self.model = None
        self.injector: Optional[IBusInjector] = None
        self.speaker_gate: Optional[SpeakerVerifierGate] = None
        self.post_llm: Optional[LLMPostProcessor] = None
        self.project_lexicon: Optional[ProjectLexicon] = None
        self.vad = None  # type: ignore[assignment]  # webrtcvad.Vad
        self.control_server: Optional[ControlServer] = None

        # --- VAD 状态 ---
        self.frame_samples = int(args.sample_rate * args.frame_ms / 1000)
        self.frame_bytes = self.frame_samples * 2
        self.start_frames = max(1, int(args.start_ms / args.frame_ms))
        self.end_frames = max(1, int(args.endpoint_silence_ms / args.frame_ms))
        # 软切：段长进入 [上限-窗口, 上限) 后改用短停顿阈值收尾；窗口最多占上限的一半
        self.soft_cut_frames = min(self.end_frames, max(1, int(args.soft_cut_silence_ms / args.frame_ms)))
        self.soft_cut_start_ms = max(args.max_segment_ms // 2, args.max_segment_ms - args.soft_cut_window_ms)
        self.pre_roll_frames = max(0, int(args.pre_roll_ms / args.frame_ms))
        self.proc: Optional[subprocess.Popen] = None
        self._frame_q: Optional["queue.Queue[Optional[bytes]]"] = None
        # 声纹逐窗复核：说话过程中每满一窗就交给后台算分，段结束时只剩不满一窗的尾巴。
        # 所有声纹计算都走这个单线程池，避免多线程同时调用声纹模型。
        self._spk_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="spk")
        self._spk_win_bytes = max(0, int(args.spk_window_ms * args.sample_rate * 2 / 1000))
        self._spk_futs: List[concurrent.futures.Future] = []
        self._spk_win_done = 0
        # 最近语音备份 + 重试队列（通知按钮 / 控制 socket 的 RETRY 都排进这里，由主循环处理）
        self._retry_q: "queue.Queue[str]" = queue.Queue()
        self._last_spk_notify_ts = 0.0  # 声纹被拒提醒的冷却计时
        self._last_tip_notify_ts = 0.0  # 表达建议提醒的冷却计时
        self.recent: Optional[RecentStore] = None
        if args.recent_enable:
            self.recent = RecentStore(os.path.join(self.state_dir, "recent_audio"), max_total=args.recent_max,
                                      max_ok=args.recent_max_ok, max_days=args.recent_days)
        self.pre_roll: Deque[bytes] = collections.deque(maxlen=self.pre_roll_frames)
        self.in_speech = False
        self.segment_id = 0
        self.speech_frames = 0
        self.trailing_silence = 0
        self.segment = bytearray()

    def _init_components(self):
        args = self.args
        self.speaker_gate = SpeakerVerifierGate(
            enabled=bool(args.speaker_verify),
            enroll_wav=args.speaker_enroll_wav,
            threshold=args.speaker_threshold,
            min_ms=args.speaker_min_ms,
            model_id=args.speaker_model,
            cache_dir=args.speaker_cache_dir,
            device=args.device,
            score_agg=args.speaker_score_agg,
            score_topk=args.speaker_score_topk,
            auto_enroll=bool(args.speaker_auto_enroll),
            auto_enroll_dir=args.speaker_auto_enroll_dir,
            auto_enroll_min_score=args.speaker_auto_enroll_min_score,
            auto_enroll_min_ms=args.speaker_auto_enroll_min_ms,
            auto_enroll_cooldown_sec=args.speaker_auto_enroll_cooldown_sec,
            auto_enroll_max_templates=args.speaker_auto_enroll_max_templates,
            adaptive_enable=bool(args.speaker_adaptive),
            adaptive_window=args.speaker_adaptive_window,
            adaptive_min_samples=args.speaker_adaptive_min_samples,
            adaptive_floor=args.speaker_adaptive_floor,
            adaptive_margin=args.speaker_adaptive_margin,
            prune_outliers=bool(args.speaker_prune_outliers),
            prune_keep=args.speaker_prune_keep,
        )
        self.post_llm = LLMPostProcessor(
            enabled=bool(args.post_llm),
            base_url=args.post_llm_base_url,
            api_key=args.post_llm_api_key,
            model=args.post_llm_model,
            fallback_model=args.post_llm_fallback_model,
            mode=args.post_llm_mode,
            timeout_ms=args.post_llm_timeout_ms,
            max_tokens=args.post_llm_max_tokens,
            temperature=args.post_llm_temperature,
            circuit_max_fails=args.post_llm_circuit_max_fails,
            circuit_cooldown_sec=args.post_llm_circuit_cooldown_sec,
            hard_cooldown_sec=args.post_llm_hard_cooldown_sec,
            retry_on_timeout=bool(args.post_llm_retry_on_timeout),
            retry_backoff_ms=args.post_llm_retry_backoff_ms,
            model_auto=bool(args.post_llm_model_auto),
            model_probe_timeout_ms=args.post_llm_model_probe_timeout_ms,
            min_chars=args.post_llm_min_chars,
            dynamic_max_tokens=bool(args.post_llm_dynamic_max_tokens),
            output_token_factor=args.post_llm_output_token_factor,
            pyramid_min_chars=args.post_llm_pyramid_min_chars,
        )
        # fallback API 端点（内网网关不可用时降级到官方 API）
        self.post_llm.fallback_url = getattr(args, 'post_llm_fallback_base_url', '') or ''
        self.post_llm.fallback_api_key = getattr(args, 'post_llm_fallback_api_key', '') or ''
        if self.post_llm.fallback_url:
            self.post_llm.fallback_url = LLMPostProcessor._normalize_endpoint(self.post_llm.fallback_url)
        self.project_lexicon = ProjectLexicon(
            enabled=bool(args.project_lexicon),
            project_root=args.project_root,
            max_terms=args.project_lexicon_max_terms,
            hint_limit=args.project_lexicon_hint_limit,
            min_term_len=args.project_lexicon_min_term_len,
            extra_terms_file=args.project_lexicon_extra_file,
        )
        self.injector = IBusInjector()
        self.vad = webrtcvad.Vad(args.vad_aggressiveness)

    def _log_startup(self):
        assert self.speaker_gate is not None
        assert self.post_llm is not None
        assert self.project_lexicon is not None
        args = self.args
        log = args.state_log
        for line in SenseVoiceConfig.summary(args).split("\n"):
            append_state_log(log, line)
        append_state_log(log, f"SPK_GATE enabled={int(self.speaker_gate.enabled)} threshold={args.speaker_threshold} enroll_n={self.speaker_gate.enroll_count} reason={self.speaker_gate.reason}")
        append_state_log(log, f"POST_LLM enabled={int(self.post_llm.enabled)} model={self.post_llm.model or '-'} fallback={self.post_llm.fallback_model or '-'} reason={self.post_llm.reason}")
        append_state_log(log, f"LEXICON enabled={int(self.project_lexicon.enabled)} terms={len(self.project_lexicon.terms)} reason={self.project_lexicon.reason}")

    def inject(self, mode: str, text: str, critical: bool = False) -> bool:
        assert self.injector is not None
        ok = self.injector.send(mode, text)
        if ok:
            return True
        append_state_log(
            self.args.state_log,
            f"INJECT_FAIL mode={mode} reason={self.injector.last_error} streak={self.injector.fail_streak} text={compact_log_text(text)}",
        )
        if critical:
            try:
                fallback_file = os.path.join(self.state_dir, "last_failed_inject.txt")
                with open(fallback_file, "w", encoding="utf-8") as f:
                    f.write(text)
                append_state_log(self.args.state_log, f"INJECT_FALLBACK file={fallback_file}")
            except Exception:
                pass
        return False

    def reset_capture_state(self):
        self.pre_roll = collections.deque(maxlen=self.pre_roll_frames)
        self.in_speech = False
        self.speech_frames = 0
        self.trailing_silence = 0
        self.segment = bytearray()
        self._spk_futs, self._spk_win_done = [], 0

    def start_capture(self):
        if self.proc is not None:
            return
        self.proc = make_arecord_process(self.args)
        if self.proc.stdout is None:
            raise RuntimeError("arecord stdout unavailable")
        self._frame_q = queue.Queue(maxsize=50 * 600)  # 20ms 帧，最多积压 10 分钟
        threading.Thread(target=self._drain_capture, args=(self.proc.stdout, self._frame_q),
                         daemon=True, name="arecord-reader").start()
        self.inject("CTRL", "RESET")
        self.reset_capture_state()
        append_state_log(self.args.state_log, "STREAM_START")

    def _drain_capture(self, stdout, frame_q: "queue.Queue[Optional[bytes]]") -> None:
        """独立线程持续抽干 arecord 管道，切成整帧入队；EOF 时放入 None。
        主循环同步处理一段（声纹+ASR+润色，3 分钟的段可达 20~30s）期间不读管道，而管道+ALSA
        只能缓冲约 2.6s，超出部分 arecord 静默丢弃（2026-11-02 实测停读 25s 丢 22s 音频）。"""
        pending = bytearray()
        while True:
            try:
                data = stdout.read(4096)
            except (OSError, ValueError):
                break
            if not data:
                break
            pending.extend(data)
            while len(pending) >= self.frame_bytes:
                frame_q.put(bytes(pending[:self.frame_bytes]))
                del pending[:self.frame_bytes]
        frame_q.put(None)

    def stop_capture(self, reason: str):
        if self.in_speech:
            append_state_log(self.args.state_log, "SPEECH_END")

        # 已知子进程：SIGTERM → 2s 等待 → SIGKILL → 短等待
        proc = self.proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception as e:
                append_state_log(self.args.state_log, f"STREAM_STOP_TERM_ERR {type(e).__name__}: {e}")
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                append_state_log(self.args.state_log, f"STREAM_STOP_KILL pid={proc.pid} reason={reason}")
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            except Exception as e:
                append_state_log(self.args.state_log, f"STREAM_STOP_WAIT_ERR {type(e).__name__}: {e}")
            self.proc = None
        self._frame_q = None

        # 兜底：扫所有挂在本 daemon 下的孤儿 arecord（self.proc 与 OS 状态可能漂移）
        try:
            my_pid = os.getpid()
            ps_out = subprocess.run(
                ["ps", "--ppid", str(my_pid), "-o", "pid=,comm="],
                capture_output=True, text=True, timeout=2.0,
            )
            for line in ps_out.stdout.splitlines():
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                pid_str, comm = parts[0], parts[1].strip()
                if comm != "arecord":
                    continue
                try:
                    cpid = int(pid_str)
                except ValueError:
                    continue
                append_state_log(self.args.state_log, f"STREAM_STOP_REAP_ORPHAN pid={cpid}")
                try:
                    os.kill(cpid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                except Exception:
                    pass
                # 短等再 SIGKILL
                for _ in range(20):
                    try:
                        os.kill(cpid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.kill(cpid, signal.SIGKILL)
                    except Exception:
                        pass
                # reap 僵尸
                try:
                    os.waitpid(cpid, os.WNOHANG)
                except ChildProcessError:
                    pass
        except Exception as e:
            append_state_log(self.args.state_log, f"STREAM_STOP_REAP_ERR {type(e).__name__}: {e}")

        self.reset_capture_state()
        append_state_log(self.args.state_log, f"STREAM_STOP reason={reason}")

    def _setup_signals(self):
        # faulthandler：捕获 SIGSEGV/SIGABRT/SIGFPE/SIGBUS 等致命错误的 traceback，写到独立文件
        import faulthandler
        fault_log_path = self.args.state_log.replace(".log", ".fault.log")
        if not fault_log_path.endswith(".fault.log"):
            fault_log_path = self.args.state_log + ".fault.log"
        self._fault_fh = open(fault_log_path, "a", buffering=1)
        faulthandler.enable(file=self._fault_fh, all_threads=True)

        # SIGPIPE 屏蔽：防止 LLM HTTP 连接 reset 时被 SIGPIPE 默认行为杀进程
        if hasattr(signal, "SIGPIPE"):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        def _stop(_sig, _frame):  # noqa: ARG001
            sig_name = signal.Signals(_sig).name if hasattr(signal, 'Signals') else str(_sig)
            append_state_log(
                self.args.state_log,
                f"SIGNAL_STOP sig={sig_name} pid={os.getpid()} ppid={os.getppid()}",
            )
            self.stop_flag = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        append_state_log(self.args.state_log, f"SIGNAL_TRAPS_INSTALLED faulthandler={fault_log_path}")

    def _spk_score_window(self, pcm: bytes) -> Tuple[float, float, float]:
        """后台线程：算一窗的声纹分数，返回 (score, 当前阈值, 窗长 ms)。"""
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        ms = len(x) * 1000.0 / self.args.sample_rate
        _, score, _, thr = self.speaker_gate.verify(x, self.args.sample_rate, ms)
        return float(score), float(thr), ms

    def _verify_speaker(self, samples: np.ndarray, seg_ms: float, spk_windows, compare_rec: Dict):
        """声纹判定。有逐窗结果时：补算尾巴（≥1s）后按时长加权平均；均值落在 [阈值-gray, 阈值) 的灰区
        回退整段判定，保证临界情况的结论与整段判定一致。返回 (ok, score, reason, thr)。"""
        args = self.args
        t0 = time.time()
        mode = "whole"
        if spk_windows:
            futs, tail = spk_windows
            try:
                if len(tail) >= args.sample_rate * 2:  # 不足 1s 的尾巴声纹不稳，不计入
                    futs = futs + [self._spk_pool.submit(self._spk_score_window, tail)]
                res = [f.result() for f in futs]
                score = sum(s * ms for s, _, ms in res) / sum(ms for _, _, ms in res)
                thr = res[-1][1]
                compare_rec["spk_windows"] = [round(s, 3) for s, _, _ in res]
                append_state_log(args.state_log, f"SPK_WINDOWS n={len(res)} mean={score:.3f} "
                                                 f"scores=[{' '.join(f'{s:.2f}' for s, _, _ in res)}]")
                if score >= thr or score < thr - args.spk_window_gray:
                    ok = score >= thr
                    reason = ("pass" if score >= self.speaker_gate.threshold else "pass_adapt") if ok else "below_threshold"
                    append_state_log(args.state_log, f"SPK_TIME mode=windows wait_ms={int((time.time() - t0) * 1000)}")
                    return ok, score, reason, thr
                mode = "gray_whole"
            except Exception as e:
                append_state_log(args.state_log, f"SPK_WINDOWS_ERROR {type(e).__name__}: {e}", level="WARN")
                mode = "error_whole"
        ok, score, reason, thr = self._spk_pool.submit(self.speaker_gate.verify, samples, args.sample_rate, seg_ms).result()
        append_state_log(args.state_log, f"SPK_TIME mode={mode} wait_ms={int((time.time() - t0) * 1000)}")
        return ok, score, reason, thr

    def _process_finalized_segment(self, samples: np.ndarray, seg_ms: float, retry_entry: Optional[Dict] = None,
                                   spk_windows=None):
        """处理已结束的语音段：声纹 → ASR → LLM → 注入。retry_entry 非空 = 用户手动重试一条最近语音，跳过声纹。"""
        assert self.speaker_gate is not None
        assert self.post_llm is not None
        assert self.project_lexicon is not None
        assert self.injector is not None
        args = self.args

        compare_rec: Dict = {
            "ts": time.strftime("%F %T"), "seg_id": int(self.segment_id),
            "seg_ms": int(seg_ms), "raw_asr": "", "after_sanitize": "",
            "spk_pass": None, "spk_score": None, "spk_thr": None,
            "llm_action": "none", "after_llm": None, "after_lexicon": None,
            "final_injected": "", "inject_ok": 0, "drop_reason": "",
        }

        # 1. 先声纹验证——不通过则不调用云端 ASR（也不把旁人的语音传上云）
        if retry_entry is not None:
            spk_ok, spk_reason = True, "retry"
            spk_score = float(retry_entry.get("spk_score") or 0.0)
            spk_thr = float(self.speaker_gate.threshold)
            compare_rec["retry_of"] = retry_entry.get("id", "")
        else:
            spk_ok, spk_score, spk_reason, spk_thr = self._verify_speaker(samples, seg_ms, spk_windows, compare_rec)
        compare_rec["spk_pass"] = int(spk_ok)
        compare_rec["spk_score"] = round(float(spk_score), 4)
        compare_rec["spk_thr"] = round(float(spk_thr), 4)

        if not spk_ok:
            append_state_log(args.state_log, f"DROP_FINAL_SPK score={spk_score:.3f} thr={spk_thr:.3f} reason={spk_reason}")
            compare_rec["drop_reason"] = f"spk:{spk_reason}"
            if bool(args.compare_log):
                append_jsonl_bounded(args.compare_log_file, compare_rec, keep_lines=args.compare_log_keep_lines)
            if spk_score >= args.recent_spk_min_score:  # 接近阈值：可能是本人（混了旁人声音），留底
                win_max = max(compare_rec.get("spk_windows") or [spk_score])
                self._recent_record(samples, seg_ms, "spk_reject", spk_score, "", None, win_max=win_max)
            return

        # 声纹通过（手动重试不喂给自适应阈值/自动注册）
        if self.speaker_gate.requested and self.speaker_gate.enabled and retry_entry is None:
            append_state_log(args.state_log, f"SPK_PASS score={spk_score:.3f} thr={spk_thr:.3f} reason={spk_reason}")
            self.speaker_gate.on_success(spk_score, seg_ms, "")
            added, add_info = self.speaker_gate.maybe_auto_enroll(samples, args.sample_rate, seg_ms, spk_score)
            if added:
                append_state_log(args.state_log, f"SPK_AUTO_ENROLL add={add_info} score={spk_score:.3f} n={self.speaker_gate.enroll_count}")
            elif add_info not in ("auto_off", "score_low", "cooldown"):
                append_state_log(args.state_log, f"SPK_AUTO_ENROLL_SKIP reason={add_info} score={spk_score:.3f} n={self.speaker_gate.enroll_count}")

        # 2. 云端 ASR（热词与前文上下文由百炼客户端自己带上）
        raw_final_text = self.model.transcribe_samples(samples, args.sample_rate)
        asr_failed = bool(self.model.last_failed)
        if asr_failed:
            self._notify_asr_failed(seg_ms)
        final_text = sanitize_transcript_text(raw_final_text, args.language, bool(args.filter_fillers))
        compare_rec["raw_asr"] = raw_final_text
        compare_rec["after_sanitize"] = final_text
        if raw_final_text and not final_text:
            append_state_log(args.state_log, f"DROP_FINAL raw={raw_final_text}")
            compare_rec["drop_reason"] = "sanitize_drop"

        # 3. LLM 润色 → 项目术语标准化 → 注入
        if final_text:
            glossary: List[str] = self.project_lexicon.hints_for_text(final_text) if self.project_lexicon.enabled else []
            if self.post_llm.enabled:
                llm_src = final_text
                revised = self.post_llm.process(final_text, route="high", glossary=glossary)
                if revised != final_text:
                    compare_rec["llm_action"] = "apply"
                    append_state_log(args.state_log, "POST_LLM_APPLY src={} dst={}".format(compact_log_text(final_text), compact_log_text(revised)))
                elif self.post_llm.last_error:
                    compare_rec["llm_action"] = "skip_error"
                    append_state_log(args.state_log, f"POST_LLM_SKIP reason={self.post_llm.last_error}")
                else:
                    compare_rec["llm_action"] = "pass"
                    append_state_log(args.state_log, "POST_LLM_PASS text={}".format(compact_log_text(final_text)))
                if args.post_llm_strict and self.post_llm.last_error:
                    append_state_log(args.state_log, "POST_LLM_BLOCK src={} reason={}".format(compact_log_text(llm_src), self.post_llm.last_error))
                    final_text = ""
                else:
                    final_text = revised
                if self.post_llm.last_tip:
                    compare_rec["expression_tip"] = self.post_llm.last_tip
                    self._expression_tip(llm_src, final_text, self.post_llm.last_tip)
            else:
                compare_rec["llm_action"] = "disabled"
            compare_rec["after_llm"] = final_text

            if final_text and self.project_lexicon.enabled:
                lex_text = self.project_lexicon.normalize_text(final_text)
                if lex_text != final_text:
                    append_state_log(args.state_log, "LEXICON_APPLY src={} dst={}".format(compact_log_text(final_text), compact_log_text(lex_text)))
                    final_text = lex_text
            compare_rec["after_lexicon"] = final_text

            if final_text:
                compare_rec["final_injected"] = final_text
                ok_inject = self.inject("COMMIT", final_text, critical=True)  # IBus 引擎直接 commit_text，不回车
                compare_rec["inject_ok"] = int(ok_inject)
                if ok_inject:
                    append_state_log(args.state_log, f"FINAL mode=COMMIT text={final_text}")
                    self.model.remember(final_text)  # 百炼 *-message：作为下一句的前文
                else:
                    compare_rec["drop_reason"] = "inject_fail"

        if bool(args.compare_log):
            append_jsonl_bounded(args.compare_log_file, compare_rec, keep_lines=args.compare_log_keep_lines)

        if compare_rec["inject_ok"]:
            outcome = "ok"
        elif compare_rec["final_injected"]:
            outcome = "inject_failed"
        elif asr_failed:
            outcome = "asr_failed"
        else:
            outcome = "asr_empty"
        self._recent_record(samples, seg_ms, outcome, spk_score,
                            compare_rec["final_injected"] or compare_rec["raw_asr"], retry_entry)

    def _recent_record(self, samples: np.ndarray, seg_ms: float, status: str, spk_score: float,
                       text: str, retry_entry: Optional[Dict], win_max: float = 0.0) -> None:
        """把一段语音及其结果记进最近语音备份；重试则更新原记录。
        通知分两档：已确认是本人但没出字（识别失败/没能输入）→ 正常通知；声纹被拒 → 默认静默，
        只有某一窗明确像本人（win_max ≥ notify_win_score）才发低优先级通知（不弹横幅），且有冷却。"""
        if self.recent is None:
            return
        args = self.args
        try:
            if retry_entry is not None:
                eid = retry_entry["id"]
                self.recent.update(eid, status="retry_" + status, text=text or retry_entry.get("text", ""))
            else:
                eid = self.recent.save(samples, args.sample_rate, self.segment_id, seg_ms, status, spk_score, text)
            append_state_log(args.state_log, f"RECENT_SAVE id={eid} status={status} seg_ms={int(seg_ms)}"
                                             f"{' retry=1' if retry_entry is not None else ''}")
        except Exception as e:
            append_state_log(args.state_log, f"RECENT_SAVE_ERROR {type(e).__name__}: {e}", level="WARN")
            return
        when = time.strftime("%H:%M:%S")
        sec = seg_ms / 1000.0
        retry_btn = [("retry", "重新识别并输入"), ("ignore", "忽略")]
        if retry_entry is not None:
            if status != "ok":
                self._notify_actions(eid, f"重试后仍没有出字（{status}），录音还在，可按 Shift+F8 查看", [])
        elif status == "spk_reject":
            now = time.time()
            if (seg_ms >= args.recent_notify_min_ms and win_max >= args.recent_notify_win_score
                    and now - self._last_spk_notify_ts >= args.recent_notify_cooldown_sec):
                self._last_spk_notify_ts = now
                self._notify_actions(eid, f"{when} 结束的一段 {sec:.0f} 秒语音里有像你的声音（{win_max:.2f}），"
                                          f"但整段没通过声纹（{spk_score:.2f}），未输入", retry_btn, urgency="low")
        elif status == "asr_failed":
            self._notify_actions(eid, f"{when} 结束的一段 {sec:.0f} 秒语音识别失败/超时，未输入", retry_btn)
        elif status == "inject_failed":
            self._notify_actions(eid, f"已识别但没能输入到光标处：{compact_log_text(text)[:40]}",
                                 [("copy", "复制文字"), ("ignore", "忽略")], text=text)

    def _expression_tip(self, raw: str, polished: str, tip: str) -> None:
        """pyramid 模式的表达建议：记入表达日志，有空时在 Shift+F8 →"表达复盘"查看；EXPRESSION_TIPS=1 才另发低优先级通知。"""
        args = self.args
        append_state_log(args.state_log, f"EXPRESSION_TIP {tip}")
        try:
            append_jsonl_bounded(os.path.join(self.state_dir, "expression_journal.jsonl"),
                                 {"ts": time.strftime("%F %T"), "raw": raw, "polished": polished, "tip": tip}, keep_lines=500)
        except Exception as e:
            append_state_log(args.state_log, f"EXPRESSION_JOURNAL_ERROR {type(e).__name__}: {e}", level="WARN")
        now = time.time()
        if args.expression_tips and now - self._last_tip_notify_ts >= args.expression_tip_cooldown_sec:
            self._last_tip_notify_ts = now
            self._notify_actions("", tip, [], urgency="low", title="表达建议")

    def _notify_actions(self, eid: str, body: str, actions, text: str = "", urgency: str = "normal",
                        title: str = "SenseVoice 没有出字") -> None:
        """弹桌面通知；带按钮时 notify-send -A 会阻塞到用户点击/关闭，所以放后台线程等结果。
        点"重新识别并输入"→ 排进重试队列由主循环处理；点"复制文字"→ wl-copy。"""
        args = self.args

        def run():
            cmd = ["notify-send", "-a", "SenseVoice", "-u", urgency]
            for name, label in actions:
                cmd += ["-A", f"{name}={label}"]
            cmd += [title, body]
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                try:
                    out, _ = p.communicate(timeout=1800)
                except subprocess.TimeoutExpired:
                    p.kill()
                    return
            except Exception:
                return
            action = (out or "").strip()
            if action:
                append_state_log(args.state_log, f"NOTIFY_ACTION id={eid} action={action}")
            if action == "retry":
                self._retry_q.put(eid)
            elif action == "copy" and text:
                try:
                    subprocess.run(["wl-copy", text], timeout=5)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True, name="notify-recent").start()

    def _run_retry(self, eid: str) -> None:
        args = self.args
        entry = self.recent.get(eid) if self.recent is not None else None
        if entry is None:
            append_state_log(args.state_log, f"RETRY_MISSING id={eid}")
            self._notify_actions(eid, "这条录音已过期或不存在，无法重试", [])
            return
        append_state_log(args.state_log, f"RETRY_START id={eid} seg_ms={entry['seg_ms']} status={entry['status']}")
        time.sleep(0.4)  # 点通知按钮/关面板后，等焦点回到原来的输入窗口再注入
        try:
            samples = self.recent.load_samples(entry)
            self._process_finalized_segment(samples, float(entry["seg_ms"]), retry_entry=entry)
        except Exception as e:
            append_state_log(args.state_log, f"RETRY_ERROR id={eid} {type(e).__name__}: {e}", level="WARN")
            return
        append_state_log(args.state_log, f"RETRY_DONE id={eid}")

    def run(self) -> int:
        args = self.args

        for w in SenseVoiceConfig.validate(args):
            append_state_log(args.state_log, f"CONFIG_WARN {w}", level="WARN")

        self._init_components()
        if not (self.speaker_gate.requested and self.speaker_gate.enabled):
            self._spk_win_bytes = 0  # 没开声纹门禁就不做逐窗计算
        assert self.injector is not None
        assert self.vad is not None
        self._log_startup()
        self._setup_signals()

        # 控制 socket 在识别客户端就绪前就 listen：F8 的命令在启动期间也能进队列，ready 后立即消费
        self.control_server = ControlServer(
            get_state=self._control_get_state,
            submit=self._control_submit,
        )
        self.control_server.start()
        append_state_log(args.state_log, "CONTROL_SOCKET_LISTENING")

        self.model = load_asr_client()
        self.model.logger = lambda m: append_state_log(args.state_log, m)  # 对冲/报错/失败/用量事件写入状态日志
        self.model_ready = True
        append_state_log(args.state_log, "MODEL_READY")
        append_state_log(args.state_log, f"DAEMON_START resident={int(args.resident)} active={int(self.active)}")

        try:
          try:
            while not self.stop_flag:
                if self.force_active is not None:
                    if self.active != self.force_active:
                        self.active = self.force_active
                        append_state_log(args.state_log, f"CONTROL_FORCE active={int(self.active)}")
                    self.force_active = None

                while self.toggle_count > 0:
                    self.toggle_count -= 1
                    self.active = not self.active
                    append_state_log(args.state_log, f"CONTROL_TOGGLE active={int(self.active)}")

                while not self._retry_q.empty():  # 关着收音也能重试
                    self._run_retry(self._retry_q.get_nowait())

                if self.active and self.proc is None:
                    try:
                        self.start_capture()
                    except Exception as e:
                        append_state_log(args.state_log, f"STREAM_START_ERROR {type(e).__name__}: {e}")
                        time.sleep(0.2)
                        continue

                if not self.active and self.proc is not None:
                    self.stop_capture("inactive")

                if not self.active:
                    time.sleep(0.05)
                    continue

                if self.proc is None or self._frame_q is None:
                    time.sleep(0.02)
                    continue

                try:
                    chunk = self._frame_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if chunk is None:  # 读线程见到 EOF：arecord 已退出
                    try:
                        self.proc.wait(timeout=2.0)
                    except Exception:
                        pass
                    append_state_log(args.state_log, f"STREAM_PROC_EXIT code={self.proc.returncode}")
                    self.stop_capture("proc_exit")
                    continue

                is_voiced = self.vad.is_speech(chunk, args.sample_rate)
                self.pre_roll.append(chunk)

                if not self.in_speech:
                    if is_voiced:
                        self.speech_frames += 1
                        if self.speech_frames >= self.start_frames:
                            self.in_speech = True
                            self.segment_id += 1
                            self.trailing_silence = 0
                            self.segment = bytearray(b"".join(self.pre_roll))
                            self._spk_futs, self._spk_win_done = [], 0
                            self.inject("SEG", str(self.segment_id))
                            append_state_log(args.state_log, f"SEG id={self.segment_id}")
                            append_state_log(args.state_log, "SPEECH_START")
                    else:
                        self.speech_frames = 0
                    continue

                self.segment.extend(chunk)
                if self._spk_win_bytes and len(self.segment) - self._spk_win_done >= self._spk_win_bytes:
                    win = bytes(self.segment[self._spk_win_done:self._spk_win_done + self._spk_win_bytes])
                    self._spk_win_done += self._spk_win_bytes
                    self._spk_futs.append(self._spk_pool.submit(self._spk_score_window, win))
                if is_voiced:
                    self.trailing_silence = 0
                else:
                    self.trailing_silence += 1

                seg_ms = len(self.segment) * 1000 / (args.sample_rate * 2)
                end_frames = self.soft_cut_frames if seg_ms >= self.soft_cut_start_ms else self.end_frames
                should_finalize = self.trailing_silence >= end_frames or seg_ms >= args.max_segment_ms
                if not should_finalize:
                    continue
                if seg_ms >= args.max_segment_ms:
                    append_state_log(args.state_log, f"SEG_MAX_CUT seg_ms={int(seg_ms)}")
                elif seg_ms >= self.soft_cut_start_ms:
                    append_state_log(args.state_log, f"SEG_SOFT_CUT seg_ms={int(seg_ms)}")

                if seg_ms >= args.min_segment_ms:
                    samples = np.frombuffer(self.segment, dtype=np.int16).astype(np.float32) / 32768.0
                    # 尾巴去掉句末判停用的静音帧，否则静音占了尾巴大头、把该窗分数拉低
                    voiced_end = max(self._spk_win_done, len(self.segment) - self.trailing_silence * self.frame_bytes)
                    spk_windows = (self._spk_futs, bytes(self.segment[self._spk_win_done:voiced_end])) if self._spk_futs else None
                    self._process_finalized_segment(samples, seg_ms, spk_windows=spk_windows)

                append_state_log(args.state_log, "SPEECH_END")
                self.in_speech = False
                self.speech_frames = 0
                self.trailing_silence = 0
                self.segment = bytearray()
                self.pre_roll.clear()
          except BaseException as _exc:
            import traceback as _tb
            tb_text = "".join(_tb.format_exception(type(_exc), _exc, _exc.__traceback__))
            append_state_log(
                args.state_log,
                f"FATAL_EXCEPTION type={type(_exc).__name__} msg={_exc!s}",
                level="ERROR",
            )
            try:
                with open(args.state_log + ".traceback", "a", buffering=1) as _tf:
                    _tf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} ===\n")
                    _tf.write(tb_text)
            except Exception:
                pass
            raise
        finally:
            if self.control_server is not None:
                self.control_server.stop()
            self.stop_capture("shutdown")
            if self.injector:
                self.injector.close()
            append_state_log(args.state_log, "DAEMON_STOP")
        return 0

    def _notify_asr_failed(self, seg_ms: float) -> None:
        """ASR 所有模型都失败/超时：这句话没能输入。日志里 ASR_FAILED 已记录原因；开着最近语音备份时
        由 _recent_record 弹带"重新识别"按钮的通知，否则这里弹一条普通通知，避免静默丢句。"""
        append_state_log(self.args.state_log, f"ASR_DROPPED seg_ms={int(seg_ms)} 这句话未输入(全部ASR模型失败/超时)")
        if self.recent is not None:
            return
        try:
            subprocess.Popen(["notify-send", "-t", "4000", "SenseVoice", "识别失败/超时，这句话没有输入"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _control_get_state(self) -> dict:
        return {
            "active": int(self.active),
            "ready": int(self.model_ready),
            "pid": os.getpid(),
        }

    def _control_submit(self, cmd: str) -> dict:
        # ACTIVATE/DEACTIVATE 直接设 force_active；TOGGLE 增计数；RETRY 排进重试队列。主循环每轮消费。
        # 返回的 active 必须是"这条命令生效后"的预测值：self.active 要等主循环下一轮才翻转，
        # 直接返回它会让 F8 的 ON/OFF 提示永远落后一步、看起来是反的。
        if cmd.startswith("RETRY "):
            eid = cmd.split(None, 1)[1].strip()
            if self.recent is None or self.recent.get(eid) is None:
                return {"ok": False, "err": "not_found"}
            self._retry_q.put(eid)
            return {"ok": True}
        if cmd == "ACTIVATE":
            self.force_active = True
            predicted_active = True
        elif cmd == "DEACTIVATE":
            self.force_active = False
            predicted_active = False
        elif cmd == "TOGGLE":
            self.toggle_count += 1
            predicted_active = not self.active
        else:
            predicted_active = self.active
        return {
            "active": int(predicted_active),
            "ready": int(self.model_ready),
            "queued": not self.model_ready,
        }
