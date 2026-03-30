"""SenseVoice 语音输入管线 - 协调所有子系统的主循环"""
import collections
import json
import os
import signal
import subprocess
import time
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import webrtcvad

from sensevoice.text.processing import (
    common_prefix, compact_log_text, gate_with_wake_words,
    looks_like_partial_noise, parse_wake_words, sanitize_transcript_text,
)
from sensevoice.asr.confidence import _select_focus_low_tokens
from sensevoice.asr.router import ConfidenceRouter
from sensevoice.asr.model import load_model, transcribe_array, transcribe_array_with_conf
from sensevoice.inject.ibus import IBusInjector
from sensevoice.inject.clipboard import FocusInjector
from sensevoice.ui.indicator import SpeechIndicator
from sensevoice.llm.postprocessor import LLMPostProcessor
from sensevoice.speaker.verifier import SpeakerVerifierGate
from sensevoice.lexicon.project import ProjectLexicon
from sensevoice.vad.stream import make_arecord_process
from sensevoice.logging import append_state_log, append_jsonl_bounded


class SenseVoicePipeline:
    """协调 VAD → ASR → 声纹 → LLM → 注入 的完整管线"""

    def __init__(self, args):
        self.args = args
        self.state_dir = os.path.dirname(args.state_log)
        os.makedirs(self.state_dir, exist_ok=True)
        self.pid_file = os.path.join(self.state_dir, "resident.pid")
        self.status_file = os.path.join(self.state_dir, "resident.status")

        # --- 状态 ---
        self.active = args.active_on_start if args.resident else True
        self.model_ready = False
        self.stop_flag = False
        self.toggle_count = 0
        self.force_active: Optional[bool] = None

        # --- 组件（_init_components 中赋值，提前声明避免 finally 中 AttributeError）---
        self.model = None
        self.indicator = None
        self.injector = None
        self.speaker_gate = None
        self.post_llm = None
        self.project_lexicon = None
        self.conf_router = None
        self.vad = None
        self.inject_mode_name = ""

        # --- VAD 状态 ---
        self.frame_samples = int(args.sample_rate * args.frame_ms / 1000)
        self.frame_bytes = self.frame_samples * 2
        self.start_frames = max(1, int(args.start_ms / args.frame_ms))
        self.end_frames = max(1, int(args.endpoint_silence_ms / args.frame_ms))
        self.pre_roll_frames = max(0, int(args.pre_roll_ms / args.frame_ms))
        self.proc: Optional[subprocess.Popen] = None
        self.pre_roll: Deque[bytes] = collections.deque(maxlen=self.pre_roll_frames)
        self.in_speech = False
        self.segment_id = 0
        self.speech_frames = 0
        self.trailing_silence = 0
        self.segment = bytearray()
        self.last_partial_ts = 0.0
        self.last_partial_text = ""
        self.prev_partial_hyp = ""

        # --- 唤醒词 ---
        self.wake_words = parse_wake_words(args.wake_words)
        self.wake_enabled = len(self.wake_words) > 0
        self.wake_strip = bool(args.wake_strip)

    def _init_components(self):
        """初始化所有子系统组件"""
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
        )
        # 设置 fallback API 端点（内网不可用时降级到官方 API）
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
        self.conf_router = ConfidenceRouter(
            enabled=bool(args.conf_route),
            high=args.conf_high,
            low=args.conf_low,
        )

        if args.inject_mode == "ibus":
            self.injector = IBusInjector()
        else:
            self.injector = FocusInjector(args.inject_script)
        self.inject_mode_name = args.inject_mode

        self.indicator = SpeechIndicator(args.indicator)
        self.vad = webrtcvad.Vad(args.vad_aggressiveness)

    def _log_startup(self):
        """输出启动配置日志"""
        args = self.args
        log = args.state_log

        append_state_log(log, f"BOOT_BEGIN resident={int(args.resident)} active={int(self.active)}")
        append_state_log(log, "WAKE_GATE enabled={} strategy={} strip={} words={}".format(
            int(self.wake_enabled), args.wake_strategy, int(self.wake_strip),
            "|".join(self.wake_words) if self.wake_words else "-",
        ))
        append_state_log(log, "SPK_GATE requested={} enabled={} threshold={} min_ms={} agg={} topk={} auto={} auto_min_score={} auto_min_ms={} auto_cd={} auto_max={} adapt={} aw={} amin={} afloor={} amargin={} prune={} pkeep={} reason={}".format(
            int(bool(args.speaker_verify)), int(self.speaker_gate.enabled),
            args.speaker_threshold, args.speaker_min_ms, args.speaker_score_agg,
            args.speaker_score_topk, int(bool(args.speaker_auto_enroll)),
            args.speaker_auto_enroll_min_score, args.speaker_auto_enroll_min_ms,
            args.speaker_auto_enroll_cooldown_sec, args.speaker_auto_enroll_max_templates,
            int(bool(args.speaker_adaptive)), args.speaker_adaptive_window,
            args.speaker_adaptive_min_samples, args.speaker_adaptive_floor,
            args.speaker_adaptive_margin, int(bool(args.speaker_prune_outliers)),
            args.speaker_prune_keep, self.speaker_gate.reason,
        ))
        append_state_log(log, "POST_LLM requested={} enabled={} strict={} mode={} model={} fallback={} timeout_ms={} max_tokens={} temp={} max_fails={} cooldown_sec={} hard_cd={} retry_to={} retry_backoff_ms={} auto={} probe_ms={} min_chars={} dyn_tokens={} out_factor={} reason={}".format(
            int(bool(args.post_llm)), int(self.post_llm.enabled), int(bool(args.post_llm_strict)),
            args.post_llm_mode, self.post_llm.model or args.post_llm_model,
            self.post_llm.fallback_model or args.post_llm_fallback_model or "-",
            args.post_llm_timeout_ms, args.post_llm_max_tokens, args.post_llm_temperature,
            args.post_llm_circuit_max_fails, args.post_llm_circuit_cooldown_sec,
            args.post_llm_hard_cooldown_sec, int(bool(args.post_llm_retry_on_timeout)),
            args.post_llm_retry_backoff_ms, int(bool(args.post_llm_model_auto)),
            args.post_llm_model_probe_timeout_ms, args.post_llm_min_chars,
            int(bool(args.post_llm_dynamic_max_tokens)), args.post_llm_output_token_factor,
            self.post_llm.reason,
        ))
        append_state_log(log, "LEXICON requested={} enabled={} root={} terms={} hint_limit={} reason={}".format(
            int(bool(args.project_lexicon)), int(self.project_lexicon.enabled),
            self.project_lexicon.project_root or args.project_root,
            len(self.project_lexicon.terms), args.project_lexicon_hint_limit,
            self.project_lexicon.reason,
        ))
        append_state_log(log, "CONF_ROUTE enabled={} high={} low={}".format(
            int(bool(args.conf_route)), self.conf_router.high, self.conf_router.low,
        ))
        append_state_log(log, "COMPARE_LOG enabled={} file={} keep_lines={}".format(
            int(bool(args.compare_log)), args.compare_log_file, args.compare_log_keep_lines,
        ))

    def write_status(self, active: bool, ready: bool, stopping: bool = False):
        with open(self.status_file, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()}\n")
            f.write(f"resident={int(self.args.resident)}\n")
            f.write(f"ready={int(ready)}\n")
            f.write(f"active={int(active)}\n")
            f.write(f"stopping={int(stopping)}\n")
            f.write(f"updated={int(time.time())}\n")

    def inject(self, mode: str, text: str, critical: bool = False) -> bool:
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
        self.last_partial_ts = 0.0
        self.last_partial_text = ""
        self.prev_partial_hyp = ""

    def start_capture(self):
        if self.proc is not None:
            return
        self.proc = make_arecord_process(self.args)
        if self.proc.stdout is None:
            raise RuntimeError("arecord stdout unavailable")
        self.indicator.reset_session()
        self.inject("CTRL", "RESET")
        self.reset_capture_state()
        append_state_log(self.args.state_log, "STREAM_START")

    def stop_capture(self, reason: str):
        if self.proc is None:
            return
        if self.in_speech:
            self.indicator.on_speech_end()
            append_state_log(self.args.state_log, "SPEECH_END")
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=0.5)
        except Exception:
            pass
        self.proc = None
        self.reset_capture_state()
        append_state_log(self.args.state_log, f"STREAM_STOP reason={reason}")

    def _setup_signals(self):
        def _stop(_sig, _frame):
            import traceback
            sig_name = signal.Signals(_sig).name if hasattr(signal, 'Signals') else str(_sig)
            append_state_log(
                self.args.state_log,
                f"SIGNAL_STOP sig={sig_name} pid={os.getpid()} ppid={os.getppid()}",
            )
            self.stop_flag = True

        def _toggle(_sig, _frame):
            self.toggle_count += 1

        def _activate(_sig, _frame):
            self.force_active = True

        def _deactivate(_sig, _frame):
            self.force_active = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, _toggle)
        if hasattr(signal, "SIGUSR2"):
            signal.signal(signal.SIGUSR2, _activate)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _deactivate)

    def _process_finalized_segment(self, samples: np.ndarray, seg_ms: float):
        """处理已结束的语音段：ASR → 声纹 → LLM → 注入"""
        args = self.args
        raw_final_text, native_conf_score, conf_source, native_token_scores = transcribe_array_with_conf(
            self.model, samples, args.language
        )
        final_text = sanitize_transcript_text(raw_final_text, args.language, bool(args.filter_fillers))

        compare_rec: Dict = {
            "ts": time.strftime("%F %T"), "seg_id": int(self.segment_id),
            "seg_ms": int(seg_ms), "raw_asr": raw_final_text,
            "after_sanitize": final_text, "spk_pass": None, "spk_score": None,
            "spk_thr": None, "after_wake": None, "after_memory": None,
            "conf_source": conf_source, "conf_native": native_conf_score,
            "conf_tokens": native_token_scores, "conf_low_tokens": [],
            "conf_score": None, "conf_route": None, "llm_action": "none",
            "after_llm": None, "after_lexicon": None,
            "final_injected": "", "inject_ok": 0, "drop_reason": "",
        }
        spk_score_for_conf: Optional[float] = None
        spk_thr_for_conf: Optional[float] = None

        if raw_final_text and not final_text:
            append_state_log(args.state_log, f"DROP_FINAL raw={raw_final_text}")
            compare_rec["drop_reason"] = "sanitize_drop"

        if final_text:
            spk_ok, spk_score, spk_reason, spk_thr = self.speaker_gate.verify(samples, args.sample_rate, seg_ms)
            compare_rec["spk_pass"] = int(spk_ok)
            compare_rec["spk_score"] = round(float(spk_score), 4)
            compare_rec["spk_thr"] = round(float(spk_thr), 4)
            if not spk_ok:
                append_state_log(args.state_log, f"DROP_FINAL_SPK score={spk_score:.3f} thr={spk_thr:.3f} reason={spk_reason} raw={final_text}")
                compare_rec["drop_reason"] = f"spk:{spk_reason}"
                final_text = ""
            elif self.speaker_gate.requested and self.speaker_gate.enabled:
                append_state_log(args.state_log, f"SPK_PASS score={spk_score:.3f} thr={spk_thr:.3f} reason={spk_reason}")
                spk_score_for_conf = float(spk_score)
                spk_thr_for_conf = float(spk_thr)
                self.speaker_gate.on_success(spk_score, seg_ms, final_text)
                added, add_info = self.speaker_gate.maybe_auto_enroll(samples, args.sample_rate, seg_ms, spk_score)
                if added:
                    append_state_log(args.state_log, f"SPK_AUTO_ENROLL add={add_info} score={spk_score:.3f} n={self.speaker_gate.enroll_count}")
                elif add_info not in ("auto_off", "score_low", "cooldown"):
                    append_state_log(args.state_log, f"SPK_AUTO_ENROLL_SKIP reason={add_info} score={spk_score:.3f} n={self.speaker_gate.enroll_count}")

        if final_text:
            if self.wake_enabled:
                gated_final, matched_wake = gate_with_wake_words(final_text, self.wake_words, args.wake_strategy, self.wake_strip)
                if not gated_final:
                    append_state_log(args.state_log, f"DROP_FINAL_WAKE raw={final_text}")
                    compare_rec["drop_reason"] = "wake_gate"
                    final_text = ""
                else:
                    final_text = gated_final
                    if matched_wake:
                        append_state_log(args.state_log, f"WAKE_MATCH wake={matched_wake} final={final_text}")
            compare_rec["after_wake"] = final_text

            if final_text:
                compare_rec["after_memory"] = final_text
                conf_score = self.conf_router.estimate(
                    raw_final_text, final_text, self.prev_partial_hyp, seg_ms,
                    spk_score_for_conf, spk_thr_for_conf,
                    native_conf=native_conf_score, native_token_scores=native_token_scores,
                )
                conf_source_used = f"{conf_source}:focus_agg" if native_token_scores else (conf_source if native_conf_score is not None else f"fallback_heuristic:{conf_source}")
                conf_route = self.conf_router.route(conf_score)
                low_tokens = _select_focus_low_tokens(native_token_scores, limit=4) if native_token_scores else []
                append_state_log(args.state_log, f"CONF_SCORE score={conf_score:.3f} route={conf_route} seg_ms={int(seg_ms)} source={conf_source_used}")
                compare_rec.update({"conf_source": conf_source_used, "conf_low_tokens": low_tokens, "conf_score": round(float(conf_score), 4), "conf_route": conf_route})

                glossary: List[str] = []
                if self.project_lexicon.enabled:
                    glossary = self.project_lexicon.hints_for_text(final_text)
                focus_token_texts = [str(x.get("token", "")).strip() for x in low_tokens if str(x.get("token", "")).strip()]

                allow_high_route_rewrite = self.post_llm.mode in {"polish_coding", "polish_coding_aggressive"}
                if self.post_llm.enabled and (conf_route != "high" or allow_high_route_rewrite):
                    llm_src = final_text
                    revised = self.post_llm.process(final_text, route=conf_route, glossary=glossary, focus_tokens=focus_token_texts)
                    if revised != final_text:
                        compare_rec["llm_action"] = "apply"
                        append_state_log(args.state_log, "POST_LLM_APPLY src={} dst={} route={}".format(compact_log_text(final_text), compact_log_text(revised), conf_route))
                    elif self.post_llm.last_error:
                        compare_rec["llm_action"] = "skip_error"
                        append_state_log(args.state_log, f"POST_LLM_SKIP reason={self.post_llm.last_error} route={conf_route}")
                    else:
                        compare_rec["llm_action"] = "pass"
                        append_state_log(args.state_log, "POST_LLM_PASS text={} route={}".format(compact_log_text(final_text), conf_route))
                    if args.post_llm_strict and self.post_llm.last_error:
                        append_state_log(args.state_log, "POST_LLM_BLOCK src={} reason={}".format(compact_log_text(llm_src), self.post_llm.last_error))
                        final_text = ""
                    else:
                        final_text = revised
                    compare_rec["after_llm"] = final_text
                elif self.post_llm.enabled:
                    compare_rec["llm_action"] = "skip_high_conf"
                    append_state_log(args.state_log, "POST_LLM_SKIP reason=high_conf_route")
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
                    mode = "FINAL" if args.auto_enter else "PARTIAL"
                    compare_rec["final_injected"] = final_text
                    ok_inject = self.inject(mode, final_text, critical=True)
                    compare_rec["inject_ok"] = int(ok_inject)
                    if ok_inject:
                        append_state_log(args.state_log, f"FINAL mode={mode} text={final_text}")
                    else:
                        compare_rec["drop_reason"] = "inject_fail"

        if bool(args.compare_log):
            append_jsonl_bounded(args.compare_log_file, compare_rec, keep_lines=args.compare_log_keep_lines)

    def run(self) -> int:
        """运行主循环"""
        args = self.args

        with open(self.pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

        self._init_components()
        self._log_startup()
        self.write_status(active=self.active, ready=False)
        self._setup_signals()

        self.model = load_model(args)
        self.model_ready = True
        append_state_log(args.state_log, "MODEL_READY")
        self.write_status(active=self.active, ready=True)
        append_state_log(args.state_log, f"INJECTOR={self.inject_mode_name}")
        append_state_log(args.state_log, f"DAEMON_START resident={int(args.resident)} active={int(self.active)}")

        try:
            while not self.stop_flag:
                if self.force_active is not None:
                    if self.active != self.force_active:
                        self.active = self.force_active
                        append_state_log(args.state_log, f"CONTROL_FORCE active={int(self.active)}")
                        self.write_status(active=self.active, ready=self.model_ready)
                    self.force_active = None

                while self.toggle_count > 0:
                    self.toggle_count -= 1
                    self.active = not self.active
                    append_state_log(args.state_log, f"CONTROL_TOGGLE active={int(self.active)}")
                    self.write_status(active=self.active, ready=self.model_ready)

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

                if self.proc is None or self.proc.stdout is None:
                    time.sleep(0.02)
                    continue

                chunk = self.proc.stdout.read(self.frame_bytes)
                if not chunk or len(chunk) < self.frame_bytes:
                    if self.proc.poll() is not None:
                        append_state_log(args.state_log, f"STREAM_PROC_EXIT code={self.proc.returncode}")
                        self.stop_capture("proc_exit")
                    else:
                        time.sleep(0.01)
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
                            self.last_partial_ts = 0.0
                            self.last_partial_text = ""
                            self.prev_partial_hyp = ""
                            self.inject("SEG", str(self.segment_id))
                            append_state_log(args.state_log, f"SEG id={self.segment_id}")
                            self.indicator.on_speech_start()
                            append_state_log(args.state_log, "SPEECH_START")
                    else:
                        self.speech_frames = 0
                    continue

                self.segment.extend(chunk)
                if is_voiced:
                    self.trailing_silence = 0
                else:
                    self.trailing_silence += 1

                now = time.time()
                seg_ms = len(self.segment) * 1000 / (args.sample_rate * 2)
                should_partial = (
                    seg_ms >= args.min_partial_ms
                    and now - self.last_partial_ts >= args.partial_interval_ms / 1000.0
                )
                if args.emit_partial and should_partial:
                    samples = np.frombuffer(self.segment, dtype=np.int16).astype(np.float32) / 32768.0
                    raw_text = transcribe_array(self.model, samples, args.language)
                    text = sanitize_transcript_text(raw_text, args.language, bool(args.filter_fillers))
                    if raw_text and not text:
                        append_state_log(args.state_log, f"DROP_PARTIAL raw={raw_text}")
                    emit_text = ""
                    if text:
                        if args.partial_strategy == "stable2":
                            if self.prev_partial_hyp:
                                emit_text = common_prefix(self.prev_partial_hyp, text).rstrip()
                            self.prev_partial_hyp = text
                        else:
                            emit_text = text

                    if (
                        emit_text
                        and not looks_like_partial_noise(emit_text)
                        and emit_text != self.last_partial_text
                        and emit_text.startswith(self.last_partial_text)
                    ):
                        if self.wake_enabled:
                            gated_partial, _ = gate_with_wake_words(emit_text, self.wake_words, args.wake_strategy, self.wake_strip)
                            if not gated_partial:
                                self.last_partial_ts = now
                                continue
                            emit_text = gated_partial
                        self.inject("PARTIAL", emit_text)
                        self.last_partial_text = emit_text
                        append_state_log(args.state_log, f"PARTIAL text={emit_text}")
                    self.last_partial_ts = now

                should_finalize = self.trailing_silence >= self.end_frames or seg_ms >= args.max_segment_ms
                if not should_finalize:
                    continue

                if seg_ms >= args.min_segment_ms:
                    samples = np.frombuffer(self.segment, dtype=np.int16).astype(np.float32) / 32768.0
                    self._process_finalized_segment(samples, seg_ms)

                self.indicator.on_speech_end()
                append_state_log(args.state_log, "SPEECH_END")
                self.in_speech = False
                self.speech_frames = 0
                self.trailing_silence = 0
                self.segment = bytearray()
                self.pre_roll.clear()
        finally:
            self.stop_capture("shutdown")
            if self.indicator:
                self.indicator.on_shutdown()
            if self.injector:
                self.injector.close()
            self.write_status(active=False, ready=self.model_ready, stopping=True)
            with open(self.pid_file, "w", encoding="utf-8") as f:
                f.write("")
            append_state_log(args.state_log, "DAEMON_STOP")
        return 0
