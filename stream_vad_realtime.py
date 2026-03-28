#!/usr/bin/env python3
import argparse
import collections
import difflib
import itertools
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import torch
import webrtcvad
from funasr import AutoModel
from funasr.models.sense_voice.utils.ctc_alignment import ctc_forced_align
from funasr.utils.load_utils import extract_fbank, load_audio_text_image_video
from funasr.utils.postprocess_utils import rich_transcription_postprocess

FILLER_WORDS_EN = {
    "yeah",
    "ok",
    "okay",
    "uh",
    "um",
    "hmm",
    "ah",
    "eh",
    "huh",
    "mm",
    "mhm",
    "hmmhmm",
    "i",
}
SHORT_NOISE_ZH = {
    "嗯",
    "啊",
    "额",
    "诶",
    "欸",
    "哦",
    "喔",
    "哎",
    "唉",
    "呀",
    "哈",
    "呃",
    "我",
}
PUNCT_EDGE = " \t\r\n.,，。!?！？、~～:;；'\"“”‘’`()[]{}<>+-_/\\|"
# SenseVoice rich postprocess may emit emotion/event symbols (emoji). Strip them
# for coding dictation to avoid non-text artifacts in prompts.
EMOJI_ARTIFACTS = "😊😔😡😰🤢😮🎼👏😀😭🤧❓"
WAKE_WORD_STRIP_EDGE = " \t\r\n,，。.!?！？、:：;；-—_"
TECH_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./:=+-]{2,}")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
COMMON_TECH_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "from",
    "this",
    "true",
    "false",
    "none",
    "null",
    "return",
    "class",
    "def",
    "import",
    "const",
    "let",
    "var",
    "function",
    "async",
    "await",
    "public",
    "private",
    "static",
    "final",
    "void",
    "int",
    "float",
    "double",
    "string",
}
CONF_PUNCT_TOKENS = set(".,，。!?！？、:;；()[]{}<>\"'`“”‘’")
CONF_LOW_VALUE_ZH = set("的地得了着过吗么呢啊呀吧嘛将把被与和及并在是有就也都又还才")


def _normalize_conf_token(token: str) -> str:
    return re.sub(r"\s+", "", (token or "").strip())


def _is_punct_conf_token(token: str) -> bool:
    t = _normalize_conf_token(token)
    return bool(t) and all(ch in CONF_PUNCT_TOKENS for ch in t)


def _is_low_value_conf_token(token: str) -> bool:
    t = _normalize_conf_token(token)
    if not t:
        return True
    if _is_punct_conf_token(t):
        return True
    if t.lower() in FILLER_WORDS_EN or t in SHORT_NOISE_ZH:
        return True
    if re.fullmatch(r"[A-Za-z]{1,2}", t):
        return True
    if len(t) <= 2 and all(ch in CONF_LOW_VALUE_ZH for ch in t):
        return True
    return False


def _aggregate_display_conf_scores(token_scores: List[Dict[str, float]]) -> Optional[float]:
    if not token_scores:
        return None
    meaningful = [
        float(row.get("score", 0.0))
        for row in token_scores
        if not _is_low_value_conf_token(str(row.get("token", "")))
    ]
    fallback = [
        float(row.get("score", 0.0))
        for row in token_scores
        if not _is_punct_conf_token(str(row.get("token", "")))
    ]
    values = meaningful or fallback or [float(row.get("score", 0.0)) for row in token_scores]
    if not values:
        return None
    ordered = sorted(values)
    bottom_n = ordered[: max(1, min(4, len(ordered)))]
    mean_all = float(np.mean(values))
    mean_bottom = float(np.mean(bottom_n))
    conf = 0.55 * mean_all + 0.45 * mean_bottom
    if len(values) <= 3:
        conf = 0.70 * mean_all + 0.30 * mean_bottom
    return float(np.clip(conf, 0.0, 1.0))


def _select_focus_low_tokens(token_scores: List[Dict[str, float]], limit: int = 4) -> List[Dict[str, float]]:
    filtered: List[Tuple[int, str, float]] = []
    for idx, row in enumerate(token_scores):
        tok = _normalize_conf_token(str(row.get("token", "")))
        score = float(row.get("score", 0.0))
        if not tok or _is_low_value_conf_token(tok):
            continue
        filtered.append((idx, tok, score))
    if not filtered:
        return []

    candidates: List[Tuple[float, float, int, str, Tuple[int, ...]]] = []
    n = len(filtered)
    for i in range(n):
        idx_i, tok_i, score_i = filtered[i]
        if len(tok_i) >= 2:
            candidates.append((score_i, score_i, -len(tok_i), tok_i, (idx_i,)))
        for win in (2, 3):
            if i + win > n:
                continue
            chunk = filtered[i : i + win]
            idxs = tuple(x[0] for x in chunk)
            if any(idxs[j] != idxs[j - 1] + 1 for j in range(1, len(idxs))):
                continue
            toks = [x[1] for x in chunk]
            scores = [x[2] for x in chunk]
            span = "".join(toks)
            if len(span) < 2:
                continue
            candidates.append((float(np.mean(scores)), min(scores), -len(span), span, idxs))

    ranked = sorted(candidates, key=lambda x: (x[0], x[1], x[2]))
    picked: List[Dict[str, float]] = []
    used = set()
    seen_text = set()
    for avg_score, _, _, span, idxs in ranked:
        key = span.lower()
        if key in seen_text or any(i in used for i in idxs):
            continue
        seen_text.add(key)
        used.update(idxs)
        picked.append({"token": span, "score": round(float(avg_score), 4)})
        if len(picked) >= limit:
            break
    return picked


class IBusInjector:
    """通过 IBus engine 的 Unix socket 注入文本 (不使用剪贴板)。

    每次注入时做毫秒级微切换:
      rime → sensevoice-voice → commit_text → rime
    rime 始终保持活跃，打字不受影响。
    """

    VOICE_ENGINE = "sensevoice-voice"
    RESTORE_ENGINE = "rime"

    def __init__(self):
        import socket as _socket
        self._socket_mod = _socket
        self.last_error = ""
        self.fail_streak = 0
        self.ack_timeout_sec = max(
            0.2, float(os.environ.get("SENSEVOICE_INJECT_ACK_TIMEOUT_SEC", "1.2"))
        )
        self._sock_path = os.path.join(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
            "sensevoice-ibus.sock",
        )
        self.RESTORE_ENGINE = os.environ.get(
            "SENSEVOICE_IBUS_RESTORE_ENGINE", "rime"
        )
        self._active = False

    def _switch_engine(self, name: str) -> bool:
        try:
            subprocess.run(
                ["ibus", "engine", name],
                capture_output=True, timeout=3,
            )
            return True
        except Exception:
            return False

    def _wait_socket(self, timeout: float = 3.0) -> bool:
        """等待 socket 可连接 (引擎进程可能需要 IBus daemon 自动启动)"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self._sock_path):
                try:
                    s = self._socket_mod.socket(
                        self._socket_mod.AF_UNIX, self._socket_mod.SOCK_STREAM
                    )
                    s.settimeout(0.3)
                    s.connect(self._sock_path)
                    s.close()
                    return True
                except OSError:
                    pass
            time.sleep(0.1)
        return False

    def send(self, mode: str, text: str) -> bool:
        if not text.strip():
            return True

        # COMMIT/FINAL/PARTIAL 都需要注入（PARTIAL 是 auto_enter=0 时的最终文本）
        is_text_inject = mode in ("COMMIT", "FINAL", "PARTIAL")
        if not is_text_inject:
            self._try_socket_send(f"{mode}\t{text}\n")
            return True

        # 微切换 rime → voice → commit → rime
        self._switch_engine(self.VOICE_ENGINE)
        time.sleep(0.15)

        if not self._wait_socket(timeout=3.0):
            self.last_error = "socket_not_ready"
            self._switch_engine(self.RESTORE_ENGINE)
            self.fail_streak += 1
            return False

        ok = self._try_socket_send(f"{mode}\t{text}\n")

        time.sleep(0.05)
        self._switch_engine(self.RESTORE_ENGINE)

        if ok:
            self.fail_streak = 0
        else:
            self.fail_streak += 1
        return ok

    def _try_socket_send(self, line: str) -> bool:
        for _ in range(2):
            try:
                s = self._socket_mod.socket(
                    self._socket_mod.AF_UNIX, self._socket_mod.SOCK_STREAM
                )
                s.settimeout(self.ack_timeout_sec)
                s.connect(self._sock_path)
                s.sendall(line.encode("utf-8"))
                ack = s.recv(256).decode("utf-8", errors="replace").strip()
                s.close()
                if ack.startswith("OK\t"):
                    self.last_error = ""
                    return True
                if ack.startswith("ERR\t"):
                    self.last_error = ack[4:] or "inject_error"
                else:
                    self.last_error = f"bad_ack:{ack[:80]}"
            except Exception as e:
                self.last_error = f"{type(e).__name__}:{e}"
                try:
                    s.close()  # type: ignore[possibly-undefined]
                except Exception:
                    pass
        return False

    def close(self) -> None:
        pass


class FocusInjector:
    def __init__(self, script_path: str):
        self.script_path = script_path
        self.proc: Optional[subprocess.Popen] = None
        self.last_error = ""
        self.fail_streak = 0
        self.ack_timeout_sec = max(0.2, float(os.environ.get("SENSEVOICE_INJECT_ACK_TIMEOUT_SEC", "1.2")))

    def _ensure(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            [self.script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def _restart(self) -> None:
        self.close()
        self._ensure()

    def send(self, mode: str, text: str) -> bool:
        if not text.strip():
            return True
        line = f"{mode}\t{text}\n"
        for _ in range(2):
            try:
                self._ensure()
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                if self._await_ack():
                    self.last_error = ""
                    self.fail_streak = 0
                    return True
            except BrokenPipeError:
                self.last_error = "broken_pipe"
            except Exception as e:
                self.last_error = f"{type(e).__name__}"
            self._restart()
        self.fail_streak += 1
        return False

    def _await_ack(self) -> bool:
        if self.proc is None or self.proc.stdout is None:
            self.last_error = "no_stdout"
            return False
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + self.ack_timeout_sec
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                self.last_error = "ack_timeout"
                return False
            ready, _, _ = select.select([fd], [], [], remain)
            if not ready:
                self.last_error = "ack_timeout"
                return False
            ack = self.proc.stdout.readline()
            if ack == "":
                self.last_error = "injector_exit"
                return False
            ack = ack.strip()
            if not ack:
                continue
            if ack.startswith("OK\t"):
                return True
            if ack.startswith("ERR\t"):
                self.last_error = ack[4:] or "inject_error"
                return False
            self.last_error = f"bad_ack:{ack[:80]}"
            return False

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        self.proc = None


class SpeechIndicator:
    def __init__(self, mode: str):
        self.mode = mode
        self.active = False
        self.notified_once = False
        self.sync_key = "sensevoice-stream-vad"

    def _notify(self, text: str, timeout_ms: int) -> None:
        if self.mode not in ("notify", "notify_once"):
            return
        if not shutil_which("notify-send"):
            return
        subprocess.run(
            [
                "notify-send",
                "-a",
                "SenseVoice VAD",
                "-u",
                "low",
                "-t",
                str(timeout_ms),
                "-h",
                f"string:x-canonical-private-synchronous:{self.sync_key}",
                text,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def on_speech_start(self) -> None:
        if self.mode == "none":
            return
        if self.active:
            return
        if self.mode == "notify_once" and self.notified_once:
            return
        self.active = True
        self.notified_once = True
        self._notify("Listening: speech detected", 1200)

    def on_speech_end(self) -> None:
        if not self.active:
            return
        self.active = False

    def on_shutdown(self) -> None:
        if self.active:
            self.on_speech_end()

    def reset_session(self) -> None:
        self.active = False
        self.notified_once = False


class ProjectLexicon:
    def __init__(
        self,
        enabled: bool,
        project_root: str,
        max_terms: int,
        hint_limit: int,
        min_term_len: int,
        extra_terms_file: str,
    ):
        self.requested = bool(enabled)
        self.enabled = False
        self.reason = "disabled"
        self.project_root = ""
        self.max_terms = max(200, int(max_terms))
        self.hint_limit = max(0, int(hint_limit))
        self.min_term_len = max(2, int(min_term_len))
        self.extra_terms_file = os.path.expanduser((extra_terms_file or "").strip())
        self.terms: List[str] = []
        self._terms_lower: set[str] = set()
        self._terms_by_initial: Dict[str, List[str]] = {}

        if not self.requested:
            return
        root = os.path.abspath(os.path.expanduser((project_root or "").strip() or os.getcwd()))
        if not os.path.isdir(root):
            self.reason = f"root_missing:{root}"
            return
        self.project_root = root

        counter: collections.Counter[str] = collections.Counter()
        self._collect_from_paths(counter)
        self._collect_from_files(counter)
        self._collect_from_extra_file(counter)

        if not counter:
            self.reason = "empty_lexicon"
            return

        ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[: self.max_terms]
        self.terms = [k for k, _ in ranked]
        self._terms_lower = {t.lower() for t in self.terms}
        for t in self.terms:
            key = t[0].lower()
            self._terms_by_initial.setdefault(key, []).append(t)
        self.enabled = True
        self.reason = f"ready:terms={len(self.terms)}"

    @staticmethod
    def _is_source_file(path: str) -> bool:
        lower = path.lower()
        exts = (
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".cpp",
            ".cc",
            ".c",
            ".h",
            ".hpp",
            ".sh",
            ".yaml",
            ".yml",
            ".json",
            ".toml",
            ".md",
            ".txt",
            ".ini",
            ".cfg",
        )
        return lower.endswith(exts)

    def _collect_term(self, counter: collections.Counter[str], token: str, weight: int = 1) -> None:
        t = (token or "").strip("_- ")
        if len(t) < self.min_term_len:
            return
        if not IDENT_RE.fullmatch(t):
            return
        if t.isdigit():
            return
        if t.lower() in COMMON_TECH_STOPWORDS:
            return
        counter[t] += weight

    def _collect_from_paths(self, counter: collections.Counter[str]) -> None:
        ignore_dirs = {
            ".git",
            ".venv",
            ".mypy_cache",
            "__pycache__",
            "node_modules",
            ".idea",
            ".vscode",
            "dist",
            "build",
            ".cache",
            ".trash",
        }
        max_files = 1800
        seen_files = 0
        for cur, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            rel = os.path.relpath(cur, self.project_root)
            for tok in IDENT_RE.findall(rel):
                self._collect_term(counter, tok, weight=2)
            for fn in files:
                seen_files += 1
                if seen_files > max_files:
                    return
                base, _ = os.path.splitext(fn)
                for tok in IDENT_RE.findall(base):
                    self._collect_term(counter, tok, weight=2)

    def _collect_from_files(self, counter: collections.Counter[str]) -> None:
        ignore_dirs = {
            ".git",
            ".venv",
            ".mypy_cache",
            "__pycache__",
            "node_modules",
            ".idea",
            ".vscode",
            "dist",
            "build",
            ".cache",
            ".trash",
        }
        max_files = 260
        max_bytes = 128 * 1024
        scanned = 0
        for cur, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            for fn in files:
                if scanned >= max_files:
                    return
                fp = os.path.join(cur, fn)
                if not self._is_source_file(fp):
                    continue
                try:
                    sz = os.path.getsize(fp)
                    if sz > max_bytes:
                        continue
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        raw = f.read(max_bytes)
                except Exception:
                    continue
                scanned += 1
                for tok in IDENT_RE.findall(raw):
                    self._collect_term(counter, tok, weight=1)

    def _collect_from_extra_file(self, counter: collections.Counter[str]) -> None:
        if not self.extra_terms_file:
            return
        if not os.path.isfile(self.extra_terms_file):
            return
        try:
            with open(self.extra_terms_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    t = line.strip()
                    if not t or t.startswith("#"):
                        continue
                    for tok in IDENT_RE.findall(t):
                        self._collect_term(counter, tok, weight=3)
        except Exception:
            return

    def contains(self, token: str) -> bool:
        return token.lower() in self._terms_lower

    def hints_for_text(self, text: str) -> List[str]:
        if not self.enabled or self.hint_limit <= 0:
            return []
        src = (text or "").strip()
        if not src:
            return []
        hints: List[str] = []
        seen = set()
        raw = TECH_TOKEN_RE.findall(src)
        for tok in raw:
            t = tok.strip()
            if len(t) < self.min_term_len:
                continue
            low = t.lower()
            if low in self._terms_lower:
                if low not in seen:
                    hints.append(t)
                    seen.add(low)
                continue
            bucket = self._terms_by_initial.get(low[0], self.terms)
            best = difflib.get_close_matches(t, bucket, n=1, cutoff=0.82)
            if not best:
                continue
            b = best[0]
            bl = b.lower()
            if bl in seen:
                continue
            hints.append(b)
            seen.add(bl)
            if len(hints) >= self.hint_limit:
                break
        return hints

    def normalize_text(self, text: str, cutoff: float = 0.90, max_rewrites: int = 4) -> str:
        if not self.enabled:
            return text
        src = text or ""
        if not src.strip():
            return src

        rewrites = 0
        out_parts: List[str] = []
        last = 0
        for m in TECH_TOKEN_RE.finditer(src):
            out_parts.append(src[last : m.start()])
            tok = m.group(0)
            low = tok.lower()
            new_tok = tok
            if low not in self._terms_lower and rewrites < max_rewrites:
                bucket = self._terms_by_initial.get(low[0], self.terms)
                best = difflib.get_close_matches(tok, bucket, n=1, cutoff=cutoff)
                if best:
                    cand = best[0]
                    ratio = difflib.SequenceMatcher(None, tok.lower(), cand.lower()).ratio()
                    if ratio >= cutoff and abs(len(tok) - len(cand)) <= 3:
                        new_tok = cand
                        rewrites += 1
            out_parts.append(new_tok)
            last = m.end()
        out_parts.append(src[last:])
        return "".join(out_parts)


class ConfidenceRouter:
    def __init__(self, enabled: bool, high: float, low: float):
        self.enabled = bool(enabled)
        self.high = max(0.0, min(1.0, float(high)))
        self.low = max(0.0, min(1.0, float(low)))
        if self.low > self.high:
            self.low, self.high = self.high, self.low

    def estimate(
        self,
        raw_text: str,
        clean_text: str,
        last_partial_hyp: str,
        seg_ms: float,
        spk_score: Optional[float],
        spk_thr: Optional[float],
        native_conf: Optional[float] = None,
        native_token_scores: Optional[List[Dict[str, float]]] = None,
    ) -> float:
        refined_native = _aggregate_display_conf_scores(native_token_scores or [])
        if refined_native is not None:
            return max(0.0, min(1.0, float(refined_native)))
        if native_conf is not None:
            return max(0.0, min(1.0, float(native_conf)))
        t = (clean_text or "").strip()
        if not t:
            return 0.0

        score = 0.45
        score += min(0.2, len(t) / 90.0)
        if raw_text and raw_text.strip() == t:
            score += 0.05
        if t and t[-1] in "。！？?!":
            score += 0.04
        if seg_ms < 1100 and len(t) >= 8:
            score -= 0.08

        if last_partial_hyp:
            cp = common_prefix(last_partial_hyp, t).strip()
            if cp:
                score += 0.25 * (len(cp) / max(1, len(t)))

        if spk_score is not None and spk_thr is not None and spk_thr < 1.0:
            norm = (spk_score - spk_thr) / max(1e-6, (1.0 - spk_thr))
            norm = max(-1.0, min(1.0, norm))
            score += 0.10 * norm

        punct_n = sum(1 for ch in t if ch in ".,，。!?！？、:;；")
        if punct_n > 0 and punct_n / max(1, len(t)) > 0.35:
            score -= 0.08
        return max(0.0, min(1.0, score))

    def route(self, score: float) -> str:
        if not self.enabled:
            return "mid"
        if score >= self.high:
            return "high"
        if score <= self.low:
            return "low"
        return "mid"


class CorrectionMemory:
    _LOW_VALUE_CN_CHARS = set("的地得了着过吗么呢啊呀吧嘛将把被与和及并在是有就也都又还才")

    def __init__(self, enabled: bool, store_path: str, min_hits: int, max_rules: int):
        self.requested = bool(enabled)
        self.enabled = False
        self.reason = "disabled"
        self.store_path = os.path.expanduser((store_path or "").strip())
        self.min_hits = max(1, int(min_hits))
        self.max_rules = max(20, int(max_rules))
        self._exact: Dict[str, Dict[str, object]] = {}
        self._rules: Dict[str, Dict[str, object]] = {}
        self._dirty = False
        self._last_save_ts = 0.0

        if not self.requested:
            return
        if not self.store_path:
            self.reason = "path_missing"
            return
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        self._load()
        self._sanitize_loaded_entries()
        self.enabled = True
        self.reason = f"ready:exact={len(self._exact)},rules={len(self._rules)}"

    def _load(self) -> None:
        if not os.path.isfile(self.store_path):
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            exact = obj.get("exact", {})
            rules = obj.get("rules", {})
            if isinstance(exact, dict):
                for k, v in exact.items():
                    if not isinstance(k, str) or not isinstance(v, dict):
                        continue
                    dst = str(v.get("dst", "")).strip()
                    hits = int(v.get("hits", 0))
                    if k.strip() and dst and hits > 0:
                        self._exact[k] = {"dst": dst, "hits": hits, "ts": float(v.get("ts", 0))}
            if isinstance(rules, dict):
                for k, v in rules.items():
                    if not isinstance(k, str) or not isinstance(v, dict):
                        continue
                    dst = str(v.get("dst", "")).strip()
                    hits = int(v.get("hits", 0))
                    if k.strip() and dst and hits > 0:
                        self._rules[k] = {"dst": dst, "hits": hits, "ts": float(v.get("ts", 0))}
        except Exception:
            self._exact = {}
            self._rules = {}

    def _save(self, force: bool = False) -> None:
        if not self._dirty:
            return
        now = time.time()
        if not force and now - self._last_save_ts < 2.0:
            return
        payload = {"exact": self._exact, "rules": self._rules}
        tmp = f"{self.store_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.store_path)
            self._last_save_ts = now
            self._dirty = False
        except Exception:
            return

    @staticmethod
    def _trim_rules_in_place(bank: Dict[str, Dict[str, object]], max_n: int) -> None:
        if len(bank) <= max_n:
            return
        ranked = sorted(
            bank.items(),
            key=lambda kv: (-int(kv[1].get("hits", 0)), -float(kv[1].get("ts", 0)), -len(kv[0])),
        )[:max_n]
        bank.clear()
        for k, v in ranked:
            bank[k] = v

    @staticmethod
    def _safe_fragment(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if len(t) > 24:
            return False
        if re.fullmatch(r"[0-9\W_]+", t):
            return False
        return True

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))

    @staticmethod
    def _normalize_edit_text(text: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", (text or "").strip())

    @classmethod
    def _is_low_value_fragment(cls, text: str) -> bool:
        t = cls._normalize_edit_text(text)
        if not t:
            return True
        return len(t) <= 3 and all(ch in cls._LOW_VALUE_CN_CHARS for ch in t)

    @classmethod
    def _safe_rule_pair(cls, src: str, dst: str) -> bool:
        a = (src or "").strip()
        b = (dst or "").strip()
        if not cls._safe_fragment(a) or not cls._safe_fragment(b):
            return False
        if a == b:
            return False
        a_norm = cls._normalize_edit_text(a)
        b_norm = cls._normalize_edit_text(b)
        if not a_norm or not b_norm:
            return False
        if len(a_norm) == 1 or len(b_norm) == 1:
            return False
        if cls._contains_cjk(a_norm + b_norm) and min(len(a_norm), len(b_norm)) < 2:
            return False
        if cls._is_low_value_fragment(a_norm) and cls._is_low_value_fragment(b_norm):
            return False
        return True

    @classmethod
    def _meaningful_exact_pair(cls, src: str, dst: str) -> bool:
        s = (src or "").strip()
        d = (dst or "").strip()
        if not s or not d or s == d:
            return False
        meaningful_edits = 0
        sm = difflib.SequenceMatcher(None, s, d, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            a = cls._normalize_edit_text(s[i1:i2])
            b = cls._normalize_edit_text(d[j1:j2])
            if not a and not b:
                continue
            if cls._is_low_value_fragment(a) and cls._is_low_value_fragment(b):
                continue
            meaningful_edits += 1
        return meaningful_edits > 0

    def _sanitize_loaded_entries(self) -> None:
        exact_clean: Dict[str, Dict[str, object]] = {}
        for src, meta in self._exact.items():
            dst = str(meta.get("dst", "")).strip()
            if not self._meaningful_exact_pair(src, dst):
                self._dirty = True
                continue
            exact_clean[src] = meta
        rules_clean: Dict[str, Dict[str, object]] = {}
        for src, meta in self._rules.items():
            dst = str(meta.get("dst", "")).strip()
            if not self._safe_rule_pair(src, dst):
                self._dirty = True
                continue
            rules_clean[src] = meta
        self._exact = exact_clean
        self._rules = rules_clean
        if self._dirty:
            self._save(force=True)

    def _upsert(self, bank: Dict[str, Dict[str, object]], src: str, dst: str) -> None:
        now = time.time()
        row = bank.get(src)
        if row is None:
            bank[src] = {"dst": dst, "hits": 1, "ts": now}
            self._dirty = True
            return
        prev_dst = str(row.get("dst", ""))
        hits = int(row.get("hits", 0))
        if prev_dst == dst:
            row["hits"] = hits + 1
            row["ts"] = now
            self._dirty = True
            return
        if hits <= 2:
            row["dst"] = dst
            row["hits"] = 1
            row["ts"] = now
            self._dirty = True

    def apply(self, text: str) -> str:
        if not self.enabled:
            return text
        src = (text or "").strip()
        if not src:
            return text
        row = self._exact.get(src)
        if row and int(row.get("hits", 0)) >= max(3, self.min_hits):
            dst = str(row.get("dst", "")).strip()
            if dst and self._meaningful_exact_pair(src, dst):
                return dst

        out = src
        ranked = sorted(
            self._rules.items(),
            key=lambda kv: (-int(kv[1].get("hits", 0)), -len(kv[0])),
        )
        for bad, meta in ranked:
            hits = int(meta.get("hits", 0))
            if hits < self.min_hits:
                continue
            good = str(meta.get("dst", "")).strip()
            if not good or bad == good or not self._safe_rule_pair(bad, good):
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{1,}", bad):
                out = re.sub(rf"\b{re.escape(bad)}\b", good, out)
            else:
                out = out.replace(bad, good)
        return out

    def learn(self, src: str, dst: str) -> None:
        if not self.enabled:
            return
        s = (src or "").strip()
        d = (dst or "").strip()
        if not s or not d or s == d:
            return
        ratio = difflib.SequenceMatcher(None, s, d).ratio()
        if ratio < 0.32:
            return
        if not self._meaningful_exact_pair(s, d):
            return
        self._upsert(self._exact, s, d)

        sm = difflib.SequenceMatcher(None, s, d, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "replace":
                continue
            a = s[i1:i2].strip()
            b = d[j1:j2].strip()
            if not self._safe_rule_pair(a, b):
                continue
            self._upsert(self._rules, a, b)

        self._trim_rules_in_place(self._exact, self.max_rules)
        self._trim_rules_in_place(self._rules, self.max_rules)
        self._save(force=False)

    def flush(self) -> None:
        self._save(force=True)


class LLMPostProcessor:
    PROMPTS = {
        "correct": (
            "你是ASR后处理器。只做同音字/错别字/标点修正。"
            "不要扩写、不要解释、不要新增事实。"
            "代码、路径、命令、参数、英文术语必须保持原样。"
            "若无法确定修改点，请原样返回输入文本。"
            "只返回最终文本。"
        ),
        "polish_light": (
            "你是ASR后处理器。先做同音字/错别字/标点修正，"
            "并允许轻度润色语义（补连接词、断句、轻微书面化），"
            "但不得新增事实或改变用户意图。"
            "禁止把问句改写成解释性回答。"
            "禁止输出“意思是/是指/指的是”等释义句式。"
            "代码、路径、命令、参数、英文术语必须保持原样。"
            "若无法确定修改点，请原样返回输入文本。"
            "不要使用Markdown格式或反引号。"
            "只返回最终文本。"
        ),
        "polish_coding": (
            "你是面向中文编程口述场景的ASR后处理器。"
            "优先积极修正明显同音字、口误、断句、标点和口语残片，"
            "并允许适度重组语序，让句子更自然、更像真实提问或指令。"
            "优先修正真正影响语义的内容词、技术术语、名词短语、命令词。"
            "不要为了书面化去改动的/地/得、吗/呢/啊、了/着/过这类低价值虚词；"
            "除非语法错误极其明确，否则保持原样。"
            "如果一句里同时存在术语错误和虚词歧义，只修术语错误，不做无关紧要的字面替换。"
            "但不得新增事实、不得改变用户意图、不得把问题改成答案。"
            "代码、路径、命令、参数、英文术语、数字必须保持原样。"
            "禁止输出解释、释义、总结、Markdown、反引号。"
            "若不能确定，就保留原文局部。"
            "只返回最终文本。"
        ),
        "polish_coding_aggressive": (
            "你是中文编程口述场景的ASR后处理器。"
            "用户正在通过语音与编程助手交流，话题涉及代码开发、编译构建、调试、架构设计、技术讨论等软件工程场景。"
            "\n处理步骤："
            "1. 通读全句，主动识别在编程语境下不通顺、不合理的词语或片段；"
            "2. 对每个不通顺之处，根据发音相似性和编程语境推断说话者的原本意图"
            '（如"船舱"在编程语境下很可能是"传参"，"保护红"很可能是"保护宏"，"走网"很可能是"组网/整网"）；'
            "3. 修正同音字、近音字、错别字、口误、重复片段、错误断句和标点；"
            "4. 允许适度重组语序，让结果更像清晰自然的技术沟通文本。"
            "\n约束："
            "不得新增事实、不得编造未说出的内容、不得把问题改成答案；"
            "代码、路径、命令、参数、英文术语、数字保持原样；"
            "不要做的/地/得、吗/呢/啊等低价值虚词替换；"
            "禁止输出解释、释义、Markdown、反引号；"
            "只返回最终文本。"
        ),
    }

    def __init__(
        self,
        enabled: bool,
        base_url: str,
        api_key: str,
        model: str,
        fallback_model: str,
        mode: str,
        timeout_ms: int,
        max_tokens: int,
        temperature: float,
        circuit_max_fails: int,
        circuit_cooldown_sec: int,
        hard_cooldown_sec: int,
        retry_on_timeout: bool,
        retry_backoff_ms: int,
        model_auto: bool,
        model_probe_timeout_ms: int,
        min_chars: int,
        cache_ttl_sec: int,
        cache_max_entries: int,
        dynamic_max_tokens: bool,
        output_token_factor: float,
    ):
        self.requested = bool(enabled)
        self.enabled = False
        self.reason = "off"
        self.last_error = ""
        self.timeout_sec = max(0.2, float(timeout_ms) / 1000.0)
        self.max_tokens = max(32, int(max_tokens))
        self.temperature = float(temperature)
        self.circuit_max_fails = max(1, int(circuit_max_fails))
        self.circuit_cooldown_sec = max(5, int(circuit_cooldown_sec))
        self.hard_cooldown_sec = max(self.circuit_cooldown_sec, int(hard_cooldown_sec))
        self.retry_on_timeout = bool(retry_on_timeout)
        self.retry_backoff_sec = max(0.0, float(retry_backoff_ms) / 1000.0)
        self._fail_streak = 0
        self._circuit_open_until = 0.0
        self.model_auto = bool(model_auto)
        self.model_probe_timeout_sec = max(0.2, float(model_probe_timeout_ms) / 1000.0)
        self.min_chars = max(1, int(min_chars))
        self.cache_ttl_sec = max(0, int(cache_ttl_sec))
        self.cache_max_entries = max(0, int(cache_max_entries))
        self.dynamic_max_tokens = bool(dynamic_max_tokens)
        self.output_token_factor = max(0.2, min(2.0, float(output_token_factor)))
        self._cache: "collections.OrderedDict[str, Tuple[float, str]]" = collections.OrderedDict()
        self._model_ids: List[str] = []
        self.model = (model or "").strip()
        self.initial_model = self.model
        self.fallback_model = (fallback_model or "").strip()
        mode_norm = (mode or "").strip().lower()
        self.mode = mode_norm if mode_norm in self.PROMPTS else "correct"
        # Guardrails vary by rewrite mode.
        if self.mode == "polish_light":
            self.min_keep_ratio = 0.60
            self.max_expand_ratio = 1.50
        elif self.mode == "polish_coding":
            self.min_keep_ratio = 0.45
            self.max_expand_ratio = 1.80
        elif self.mode == "polish_coding_aggressive":
            self.min_keep_ratio = 0.35
            self.max_expand_ratio = 2.10
        else:
            self.min_keep_ratio = 0.50
            self.max_expand_ratio = 2.20
        self.url = ""
        self.models_url = ""
        self.api_key = (api_key or "").strip()

        if not self.requested:
            return
        if not base_url.strip():
            self.reason = "base_url_missing"
            return
        if not self.api_key:
            self.reason = "api_key_missing"
            return

        self.url = self._normalize_endpoint(base_url)
        self.models_url = self._normalize_models_endpoint(base_url)
        if self.model_auto:
            self._autoselect_models()
        if not self.model:
            self.reason = "model_missing"
            return
        self.enabled = True
        self.reason = f"ready:model={self.model},fallback={self.fallback_model or '-'}"
        self._warmup_probe()

    def _note_success(self) -> None:
        self._fail_streak = 0
        self._circuit_open_until = 0.0
        self.last_error = ""

    def _note_failure(self, reason: str) -> None:
        hard_tags = ("model_not_found", "http_auth", "api_key_missing", "base_url_missing")
        if any(tag in reason for tag in hard_tags):
            self._fail_streak = self.circuit_max_fails
            self._circuit_open_until = time.time() + self.hard_cooldown_sec
            self.last_error = f"{reason};hard_circuit_open:{self.hard_cooldown_sec}s"
            return
        self._fail_streak += 1
        self.last_error = reason
        if self._fail_streak >= self.circuit_max_fails:
            self._circuit_open_until = time.time() + self.circuit_cooldown_sec
            self.last_error = f"{reason};circuit_open:{self.circuit_cooldown_sec}s"

    @staticmethod
    def _normalize_endpoint(base_url: str) -> str:
        base = base_url.strip().rstrip("/")
        if base.endswith("/chat/completions") or base.endswith("/completions"):
            return base
        return f"{base}/chat/completions"

    @staticmethod
    def _normalize_models_endpoint(base_url: str) -> str:
        base = base_url.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return f"{base}/models"

    @staticmethod
    def _model_eq(a: str, b: str) -> bool:
        return a.strip().lower() == b.strip().lower()

    @staticmethod
    def _extract_model_ids(obj: dict) -> List[str]:
        data = obj.get("data")
        out: List[str] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id")
                if isinstance(mid, str) and mid.strip():
                    out.append(mid.strip())
        return out

    def _fetch_model_ids(self) -> Tuple[List[str], str]:
        req = urllib.request.Request(
            self.models_url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.model_probe_timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(raw)
            mids = self._extract_model_ids(obj)
            if not mids:
                return [], "models_empty"
            return mids, ""
        except urllib.error.HTTPError as e:
            return [], f"models_http_{e.code}"
        except Exception as e:
            return [], f"models_err:{type(e).__name__}"

    def _pick_preferred_model(self, model_ids: List[str]) -> str:
        if not model_ids:
            return ""
        # 1) keep user configured primary/fallback if available
        for pref in (self.model, self.fallback_model):
            if not pref:
                continue
            for mid in model_ids:
                if self._model_eq(pref, mid):
                    return mid
        # 2) keyword preference order for coding post-processing
        keywords = ("deepseek", "qwen", "gpt", "claude")
        for kw in keywords:
            for mid in model_ids:
                if kw in mid.lower():
                    return mid
        # 3) fallback to first available
        return model_ids[0]

    def _pick_fallback_model(self, model_ids: List[str], primary: str) -> str:
        if not model_ids:
            return ""
        for pref in (self.fallback_model, self.initial_model):
            if not pref:
                continue
            for mid in model_ids:
                if self._model_eq(pref, mid) and not self._model_eq(mid, primary):
                    return mid
        for mid in model_ids:
            if not self._model_eq(mid, primary):
                return mid
        return ""

    def _autoselect_models(self) -> bool:
        mids, err = self._fetch_model_ids()
        if err:
            # Keep configured model path when probing fails.
            if self.model:
                self.last_error = err
                return False
            self.reason = err
            return False
        self._model_ids = mids
        primary = self._pick_preferred_model(mids)
        if not primary:
            self.reason = "model_unavailable"
            return False
        self.model = primary
        if not self.fallback_model or self._model_eq(self.fallback_model, self.model):
            self.fallback_model = self._pick_fallback_model(mids, self.model)
        return True

    def _cache_get(self, key: str) -> str:
        if self.cache_max_entries <= 0 or self.cache_ttl_sec <= 0:
            return ""
        row = self._cache.get(key)
        if row is None:
            return ""
        ts, out = row
        if time.time() - ts > self.cache_ttl_sec:
            self._cache.pop(key, None)
            return ""
        # refresh LRU order
        self._cache.move_to_end(key)
        return out

    def _cache_put(self, key: str, out: str) -> None:
        if self.cache_max_entries <= 0 or self.cache_ttl_sec <= 0:
            return
        self._cache[key] = (time.time(), out)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_max_entries:
            self._cache.popitem(last=False)

    def _choose_max_tokens(self, src: str) -> int:
        if not self.dynamic_max_tokens:
            return self.max_tokens
        n = len((src or "").strip())
        # Single-turn rewrite rarely needs long output; shrink response budget
        # to reduce latency and over-expansion risk.
        dyn = int(round(n * self.output_token_factor)) + 8
        dyn = max(16, dyn)
        return min(self.max_tokens, dyn)

    def _warmup_probe(self) -> None:
        if not self.enabled:
            return
        probe_src = "请修正这一句中的错别字。"
        prompt = self._system_prompt("mid", [])
        out, err = self._request_once(self.model, probe_src, prompt, self._choose_max_tokens(probe_src))
        if out:
            self._note_success()
            return
        if self.fallback_model and not self._model_eq(self.fallback_model, self.model):
            out2, err2 = self._request_once(
                self.fallback_model,
                probe_src,
                prompt,
                self._choose_max_tokens(probe_src),
            )
            if out2:
                self.model = self.fallback_model
                self._note_success()
                self.reason = f"ready:model={self.model},fallback=-"
                return
            err = f"primary:{err or 'failed'};fallback:{err2 or 'failed'}"
        self._note_failure(err or "warmup_failed")
        if "hard_circuit_open" in self.last_error:
            self.reason = f"degraded:{self.last_error}"

    @staticmethod
    def _extract_text(obj: dict) -> str:
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        out_parts: List[str] = []
                        for item in content:
                            if isinstance(item, dict):
                                t = item.get("text")
                                if isinstance(t, str) and t:
                                    out_parts.append(t)
                        if out_parts:
                            return "".join(out_parts)
                text = first.get("text")
                if isinstance(text, str):
                    return text
        output_text = obj.get("output_text")
        if isinstance(output_text, str):
            return output_text
        return ""

    @staticmethod
    def _clean_text(text: str) -> str:
        t = (text or "").replace("\r", " ").replace("`", "").strip()
        if not t:
            return ""
        # Remove a single surrounding quote pair if present.
        quote_pairs = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")]
        for ql, qr in quote_pairs:
            if len(t) >= 2 and t.startswith(ql) and t.endswith(qr):
                t = t[1:-1].strip()
                break
        return t

    @staticmethod
    def _looks_like_meta(text: str) -> bool:
        lower = text.lower()
        prefixes = (
            "corrected text:",
            "纠正后",
            "修正后",
            "以下是",
            "抱歉",
            "as an ai",
        )
        return any(lower.startswith(p) for p in prefixes)

    @staticmethod
    def _protected_tokens(src: str) -> List[str]:
        # Keep technical/code-like tokens stable across rewrite.
        raw = re.findall(r"[A-Za-z0-9_./:=+-]{2,}", src or "")
        out: List[str] = []
        seen = set()
        for t in raw:
            # Ignore pure numbers; keep command/code-like fragments.
            if t.isdigit():
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    @staticmethod
    def _contains_token(dst: str, token: str) -> bool:
        return token.lower() in (dst or "").lower()

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if "?" in t or "？" in t:
            return True
        cues = ("吗", "么", "呢", "什么", "为何", "为什么", "如何", "怎么", "是不是", "能否", "可否")
        return any(c in t for c in cues)

    @staticmethod
    def _looks_like_explanatory_answer(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        cues = ("意思是", "是指", "指的是", "通常是", "可以理解为", "也就是说")
        return any(c in t for c in cues)

    def _system_prompt(
        self,
        route: str = "mid",
        glossary: Optional[List[str]] = None,
        focus_tokens: Optional[List[str]] = None,
    ) -> str:
        base = self.PROMPTS.get(self.mode, self.PROMPTS["correct"])
        if route == "low":
            if self.mode == "polish_coding_aggressive":
                base += " 当前语句置信度较低，请尽可能利用上下文主动修正明显错误和不自然表达，但仍不得新增事实。"
            else:
                base += " 当前语句置信度较低，请更积极地修正明显同音误识别，但仍不得新增事实。"
        elif route == "high":
            if self.mode in {"polish_coding", "polish_coding_aggressive"}:
                if self.mode == "polish_coding_aggressive":
                    base += " 当前语句置信度较高，但若存在明显术语错误、命令词错误、错词或不自然表达，仍应主动修正；不要做的/地/得之类低价值虚词替换。"
                else:
                    base += " 当前语句置信度较高，但仍允许修正明显术语错误、口语误识别和断句问题；不要做低价值虚词替换，也不要过度改写。"
            else:
                base += " 当前语句置信度高，尽量少改，仅修正明显错误。"
        if glossary:
            joined = ", ".join(glossary[:24])
            if joined:
                base += (
                    " 优先参考以下项目术语，仅在语义明显匹配时替换："
                    f"{joined}。"
                    " 术语不确定时保留原样。"
                )
        if focus_tokens:
            joined_focus = ", ".join([t for t in focus_tokens if t][:6])
            if joined_focus:
                base += (
                    " 当前低置信重点词片段："
                    f"{joined_focus}。"
                    " 优先检查这些词及其紧邻上下文，只在必要范围内局部修正，避免整句无关改写。"
                )
        return base

    def _request_once(self, model_name: str, src: str, prompt: str, req_max_tokens: int) -> Tuple[str, str]:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": src},
            ],
            "temperature": self.temperature,
            "max_tokens": req_max_tokens,
            "stream": False,
            # Best-effort flags for providers that support disabling reasoning mode.
            "thinking": False,
            "enable_thinking": False,
            "reasoning_effort": "low",
            "chat_template_kwargs": {"thinking": False},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(raw)
            out = self._clean_text(self._extract_text(obj))
            if not out:
                reason_text = ""
                choices = obj.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message")
                    if isinstance(msg, dict):
                        reason_text = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
                if reason_text.strip():
                    return "", "reasoning_only"
                return "", "empty_output"
            if self._looks_like_meta(out):
                return "", "meta_output"
            # Guardrail: avoid verbose rewrites/hallucination.
            if len(out) > max(80, int(len(src) * self.max_expand_ratio)):
                return "", "too_long_output"
            if len(src) >= 8 and len(out) < int(len(src) * self.min_keep_ratio):
                return "", "too_short_output"
            # Guardrail: preserve question intent, do not rewrite question into explanation answer.
            if self._looks_like_question(src):
                if not self._looks_like_question(out) and self._looks_like_explanatory_answer(out):
                    return "", "qa_shift"
            for tok in self._protected_tokens(src):
                if not self._contains_token(out, tok):
                    return "", f"token_missing:{tok}"
            return out, ""
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            lower = body.lower()
            if e.code in (401, 403):
                return "", f"http_auth:{e.code}"
            if e.code == 429:
                return "", "http_rate_limited"
            if (
                e.code in (400, 404)
                and (
                    "notfound" in lower
                    or "not found" in lower
                    or "does not exist" in lower
                    or ("invalid" in lower and "model" in lower)
                )
            ):
                return "", "model_not_found"
            return "", f"http_error:{e.code}"
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e)).lower()
            if "timed out" in reason:
                return "", "timeout"
            return "", f"url_error:{type(e).__name__}"
        except Exception as e:
            if "timeout" in type(e).__name__.lower():
                return "", "timeout"
            return "", f"err:{type(e).__name__}"

    def process(
        self,
        text: str,
        route: str = "mid",
        glossary: Optional[List[str]] = None,
        focus_tokens: Optional[List[str]] = None,
    ) -> str:
        if not self.enabled:
            return text
        src = (text or "").strip()
        if not src:
            return text
        if len(src) < self.min_chars:
            return text
        route_tag = route if route in ("high", "mid", "low") else "mid"
        if route_tag == "high" and self.mode not in {"polish_coding", "polish_coding_aggressive"}:
            self.last_error = "skip_high_conf"
            return text

        prompt = self._system_prompt(route_tag, glossary, focus_tokens)
        req_max_tokens = self._choose_max_tokens(src)
        if route_tag == "low":
            req_max_tokens = min(self.max_tokens, int(req_max_tokens * 1.5) + 12)

        glossary_key = ",".join((glossary or [])[:8])
        focus_key = ",".join((focus_tokens or [])[:6])
        cache_key = f"{route_tag}|g={glossary_key}|f={focus_key}|{src}"
        cached = self._cache_get(cache_key)
        if cached:
            self.last_error = ""
            return cached
        now = time.time()
        if now < self._circuit_open_until:
            remain = int(max(1, self._circuit_open_until - now))
            self.last_error = f"circuit_open:{remain}s"
            return text

        out, err = self._request_once(self.model, src, prompt, req_max_tokens)
        if not out and err == "timeout" and self.retry_on_timeout:
            if self.retry_backoff_sec > 0:
                time.sleep(self.retry_backoff_sec)
            out_retry_to, err_retry_to = self._request_once(self.model, src, prompt, req_max_tokens)
            if out_retry_to:
                self._note_success()
                self._cache_put(cache_key, out_retry_to)
                return out_retry_to
            err = f"timeout_retry:{err_retry_to or 'failed'}"
        if out:
            self._note_success()
            self._cache_put(cache_key, out)
            return out
        if err == "model_not_found" and self.model_auto and self._autoselect_models():
            out_retry, err_retry = self._request_once(self.model, src, prompt, req_max_tokens)
            if out_retry:
                self._note_success()
                self._cache_put(cache_key, out_retry)
                return out_retry
            err = f"{err};reprobe:{err_retry or 'failed'}"

        # Some providers/models return reasoning-only responses for thinking models.
        # Fallback model keeps the chain usable for low-latency correction.
        if self.fallback_model and self.fallback_model != self.model:
            out2, err2 = self._request_once(self.fallback_model, src, prompt, req_max_tokens)
            if out2:
                self._note_success()
                self._cache_put(cache_key, out2)
                return out2
            self._note_failure(f"primary:{err or 'failed'};fallback:{err2 or 'failed'}")
            return text

        self._note_failure(err or "failed")
        return text


class SpeakerVerifierGate:
    def __init__(
        self,
        enabled: bool,
        enroll_wav: str,
        threshold: float,
        min_ms: int,
        model_id: str,
        cache_dir: str,
        device: str,
        score_agg: str,
        score_topk: int,
        auto_enroll: bool,
        auto_enroll_dir: str,
        auto_enroll_min_score: float,
        auto_enroll_min_ms: int,
        auto_enroll_cooldown_sec: int,
        auto_enroll_max_templates: int,
        adaptive_enable: bool,
        adaptive_window: int,
        adaptive_min_samples: int,
        adaptive_floor: float,
        adaptive_margin: float,
        prune_outliers: bool,
        prune_keep: int,
    ):
        self.requested = enabled
        self.enabled = False
        self.threshold = threshold
        self.min_ms = min_ms
        self.score_agg = score_agg
        self.score_topk = max(1, int(score_topk))
        self.reason = "disabled"
        self.enroll_count = 0
        self._enroll_paths: List[str] = []
        self.pruned_count = 0

        self.auto_enroll_requested = bool(auto_enroll)
        self.auto_enroll_enabled = False
        self.auto_enroll_dir = ""
        self.auto_enroll_min_score = float(auto_enroll_min_score)
        self.auto_enroll_min_ms = max(300, int(auto_enroll_min_ms))
        self.auto_enroll_cooldown_sec = max(0, int(auto_enroll_cooldown_sec))
        self.auto_enroll_max_templates = max(1, int(auto_enroll_max_templates))
        self._last_auto_enroll_ts = 0.0

        # Adaptive threshold: lower gate slightly when the user's recent valid scores drift down.
        self.adaptive_enable = bool(adaptive_enable)
        self.adaptive_window = max(10, int(adaptive_window))
        self.adaptive_min_samples = max(5, int(adaptive_min_samples))
        self.adaptive_floor = float(adaptive_floor)
        self.adaptive_margin = float(adaptive_margin)
        self._recent_good_scores: Deque[float] = collections.deque(maxlen=self.adaptive_window)
        self._last_threshold_used = self.threshold

        # Outlier pruning: keep the most central templates when template bank is noisy/too large.
        self.prune_outliers = bool(prune_outliers)
        self.prune_keep = max(2, int(prune_keep))

        self._torch = None
        self._F = None
        self._classifier = None
        self._enroll_embeddings = None

        if not enabled:
            return

        enroll_paths = self._resolve_enroll_paths(enroll_wav or "")
        if not enroll_paths:
            self.reason = f"enroll_missing:{(enroll_wav or '').strip() or '-'}"
            return

        try:
            import torchaudio  # type: ignore

            # speechbrain still expects these legacy torchaudio helpers in some versions.
            if not hasattr(torchaudio, "list_audio_backends"):
                torchaudio.list_audio_backends = lambda: ["soundfile"]  # type: ignore[attr-defined]
            if not hasattr(torchaudio, "set_audio_backend"):
                torchaudio.set_audio_backend = lambda backend: None  # type: ignore[attr-defined]
            if not hasattr(torchaudio, "get_audio_backend"):
                torchaudio.get_audio_backend = lambda: "soundfile"  # type: ignore[attr-defined]

            import torch  # type: ignore
            import torch.nn.functional as F  # type: ignore
            from speechbrain.inference.speaker import SpeakerRecognition  # type: ignore

            self._torch = torch
            self._F = F

            model_cache = os.path.expanduser(cache_dir or "~/.cache/sensevoice-vibe/spkrec")
            os.makedirs(model_cache, exist_ok=True)
            self._classifier = SpeakerRecognition.from_hparams(
                source=model_id,
                savedir=model_cache,
                run_opts={"device": device},
            )

            emb_list = []
            for enroll_path in enroll_paths:
                enroll_samples, sr = self._load_wav_mono_float32(enroll_path)
                if sr != 16000:
                    enroll_samples = self._resample_linear(enroll_samples, sr, 16000)
                if enroll_samples.size < 16000:
                    # Ignore enrollment clips shorter than 1s; they are unstable for speaker verification.
                    continue
                wav = torch.from_numpy(enroll_samples).float().unsqueeze(0)
                with torch.no_grad():
                    emb = self._classifier.encode_batch(wav).reshape(1, -1)
                emb_list.append(emb)
                self._enroll_paths.append(enroll_path)

            if not emb_list:
                self.reason = "enroll_invalid:no_valid_wav"
                return

            self._enroll_embeddings = torch.cat(emb_list, dim=0)
            self.enroll_count = int(self._enroll_embeddings.shape[0])
            self._configure_auto_enroll(enroll_wav, auto_enroll_dir)
            self._apply_in_memory_prune()

            self.enabled = True
            self.reason = (
                f"ready:n={self.enroll_count},agg={self.score_agg},k={self.score_topk},"
                f"adapt={int(self.adaptive_enable)},pruned={self.pruned_count}"
            )
        except Exception as e:
            self.reason = f"init_error:{type(e).__name__}"
            self.enabled = False

    @staticmethod
    def _resolve_enroll_paths(spec: str) -> List[str]:
        raw = os.path.expanduser((spec or "").strip())
        if not raw:
            return []

        parts = [p.strip() for p in re.split(r"[;,]", raw) if p.strip()]
        paths: List[str] = []
        if len(parts) > 1:
            paths = [os.path.expanduser(p) for p in parts]
        elif os.path.isdir(raw):
            for name in sorted(os.listdir(raw)):
                if name.lower().endswith(".wav"):
                    paths.append(os.path.join(raw, name))
        else:
            paths = [raw]

        out: List[str] = []
        seen = set()
        for p in paths:
            p2 = os.path.abspath(os.path.expanduser(p))
            if not os.path.isfile(p2):
                continue
            if p2 in seen:
                continue
            seen.add(p2)
            out.append(p2)
        return out

    def _configure_auto_enroll(self, enroll_spec: str, auto_enroll_dir: str) -> None:
        if not self.auto_enroll_requested:
            return
        chosen = os.path.expanduser((auto_enroll_dir or "").strip())
        if not chosen:
            spec_raw = os.path.expanduser((enroll_spec or "").strip())
            if spec_raw and os.path.isdir(spec_raw):
                chosen = spec_raw
        if not chosen:
            return
        os.makedirs(chosen, exist_ok=True)
        self.auto_enroll_dir = os.path.abspath(chosen)
        self.auto_enroll_enabled = True

    def _apply_in_memory_prune(self) -> None:
        if not self.prune_outliers:
            return
        if self._enroll_embeddings is None or self._F is None or self._torch is None:
            return

        n = int(self._enroll_embeddings.shape[0])
        if n < 4 or n <= self.prune_keep:
            return

        F = self._F
        torch = self._torch
        emb = self._enroll_embeddings

        # Keep templates that are most similar to their nearest neighbors (central samples).
        norm = F.normalize(emb, dim=1)
        sim = torch.matmul(norm, norm.transpose(0, 1))
        sim.fill_diagonal_(-1.0)
        k = min(3, n - 1)
        neigh_mean = sim.topk(k=k, dim=1).values.mean(dim=1)
        keep_n = min(self.prune_keep, n)
        keep_idx = torch.topk(neigh_mean, k=keep_n).indices
        keep_idx = torch.sort(keep_idx).values.tolist()

        self._enroll_embeddings = emb[keep_idx]
        if self._enroll_paths:
            self._enroll_paths = [self._enroll_paths[i] for i in keep_idx if i < len(self._enroll_paths)]
        self.pruned_count = n - keep_n
        self.enroll_count = int(self._enroll_embeddings.shape[0])

    def _effective_threshold(self) -> float:
        if not self.adaptive_enable:
            return self.threshold
        if len(self._recent_good_scores) < self.adaptive_min_samples:
            return self.threshold
        q25 = float(np.percentile(np.array(self._recent_good_scores, dtype=np.float32), 25))
        adaptive = max(self.adaptive_floor, q25 - self.adaptive_margin)
        # Never make the gate stricter than configured base threshold.
        return min(self.threshold, adaptive)

    @staticmethod
    def _load_wav_mono_float32(path: str) -> Tuple[np.ndarray, int]:
        with wave.open(path, "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            sample_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

        if sampwidth == 2:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 1:
            # 8-bit PCM in WAV is typically unsigned
            x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sampwidth == 4:
            x = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise RuntimeError(f"unsupported_sample_width:{sampwidth}")

        if channels > 1:
            x = x.reshape(-1, channels).mean(axis=1)
        return x.astype(np.float32, copy=False), sample_rate

    @staticmethod
    def _resample_linear(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        if src_sr == dst_sr or samples.size == 0:
            return samples
        duration = samples.shape[0] / float(src_sr)
        n_dst = max(1, int(round(duration * dst_sr)))
        src_x = np.linspace(0.0, duration, num=samples.shape[0], endpoint=False, dtype=np.float64)
        dst_x = np.linspace(0.0, duration, num=n_dst, endpoint=False, dtype=np.float64)
        y = np.interp(dst_x, src_x, samples.astype(np.float64, copy=False))
        return y.astype(np.float32, copy=False)

    @staticmethod
    def _save_wav_mono_float32(path: str, samples: np.ndarray, sample_rate: int) -> None:
        x = np.clip(samples, -1.0, 1.0)
        pcm = (x * 32767.0).astype(np.int16)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm.tobytes())

    def verify(self, samples: np.ndarray, sample_rate: int, seg_ms: float) -> Tuple[bool, float, str, float]:
        if not self.enabled:
            # Fail-open when verifier is unavailable.
            return True, 1.0, self.reason, self.threshold
        if seg_ms < self.min_ms:
            return False, 0.0, "too_short", self.threshold

        assert self._torch is not None
        assert self._F is not None
        assert self._classifier is not None
        assert self._enroll_embeddings is not None

        torch = self._torch
        F = self._F

        x = samples.astype(np.float32, copy=False)
        if sample_rate != 16000:
            x = self._resample_linear(x, sample_rate, 16000)
        wav = torch.from_numpy(x).float().unsqueeze(0)

        with torch.no_grad():
            emb = self._classifier.encode_batch(wav)
        emb = emb.reshape(1, -1)

        # Compare against all enrollment templates.
        scores = F.cosine_similarity(self._enroll_embeddings, emb, dim=1)
        if self.score_agg == "max":
            score = float(scores.max().item())
        elif self.score_agg == "mean":
            score = float(scores.mean().item())
        else:
            k = min(self.score_topk, int(scores.numel()))
            score = float(scores.topk(k=k).values.mean().item())
        eff_thr = self._effective_threshold()
        self._last_threshold_used = eff_thr
        if score >= eff_thr:
            reason = "pass" if score >= self.threshold else "pass_adapt"
            return True, score, reason, eff_thr
        return False, score, "below_threshold", eff_thr

    def on_success(self, score: float, seg_ms: float, text: str) -> None:
        if not self.adaptive_enable:
            return
        if seg_ms < self.min_ms:
            return
        if len((text or "").strip()) < 4:
            return
        self._recent_good_scores.append(float(score))

    def maybe_auto_enroll(
        self,
        samples: np.ndarray,
        sample_rate: int,
        seg_ms: float,
        score: float,
    ) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        if not self.auto_enroll_enabled:
            return False, "auto_off"
        if seg_ms < self.auto_enroll_min_ms:
            return False, "too_short"
        if score < self.auto_enroll_min_score:
            return False, "score_low"
        if self.enroll_count >= self.auto_enroll_max_templates:
            return False, "max_templates"
        now = time.time()
        if now - self._last_auto_enroll_ts < self.auto_enroll_cooldown_sec:
            return False, "cooldown"

        assert self._torch is not None
        assert self._classifier is not None
        assert self._enroll_embeddings is not None

        x = samples.astype(np.float32, copy=False)
        sr = sample_rate
        if sr != 16000:
            x = self._resample_linear(x, sr, 16000)
            sr = 16000
        if x.size < 16000:
            return False, "too_short"

        torch = self._torch
        wav = torch.from_numpy(x).float().unsqueeze(0)
        with torch.no_grad():
            emb = self._classifier.encode_batch(wav).reshape(1, -1)

        ts = time.strftime("%Y%m%d_%H%M%S")
        ms = int((now - int(now)) * 1000)
        base = f"auto_{ts}_{ms:03d}_{int(score*1000):03d}"
        out = os.path.join(self.auto_enroll_dir, f"{base}.wav")
        if os.path.exists(out):
            for i in range(1, 100):
                candidate = os.path.join(self.auto_enroll_dir, f"{base}_{i:02d}.wav")
                if not os.path.exists(candidate):
                    out = candidate
                    break
        self._save_wav_mono_float32(out, x, sr)

        self._enroll_embeddings = torch.cat([self._enroll_embeddings, emb], dim=0)
        self._enroll_paths.append(out)
        self.enroll_count = int(self._enroll_embeddings.shape[0])
        self._last_auto_enroll_ts = now
        return True, out


def shutil_which(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Continuous auto-VAD realtime transcription")
    p.add_argument(
        "--model",
        default=os.environ.get("SENSEVOICE_MODEL", "iic/SenseVoiceSmall"),
        help="Model id or local model path",
    )
    p.add_argument("--device", default=os.environ.get("SENSEVOICE_DEVICE", "cpu"))
    p.add_argument("--language", default=os.environ.get("SENSEVOICE_LANGUAGE", "auto"))
    p.add_argument("--sample-rate", type=int, default=16000)
    p.add_argument(
        "--frame-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_FRAME_MS", "20")),
        choices=[10, 20, 30],
    )
    p.add_argument(
        "--vad-aggressiveness",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_VAD_AGGRESSIVENESS", "2")),
        choices=[0, 1, 2, 3],
    )
    p.add_argument("--start-ms", type=int, default=int(os.environ.get("SENSEVOICE_STREAM_START_MS", "240")))
    p.add_argument(
        "--endpoint-silence-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_ENDPOINT_MS", "650")),
    )
    p.add_argument(
        "--max-segment-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_MAX_SEGMENT_MS", "8000")),
    )
    p.add_argument(
        "--pre-roll-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_PRE_ROLL_MS", "300")),
    )
    p.add_argument(
        "--min-segment-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_MIN_SEGMENT_MS", "850")),
    )
    p.add_argument(
        "--partial-interval-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_PARTIAL_INTERVAL_MS", "350")),
    )
    p.add_argument(
        "--min-partial-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_MIN_PARTIAL_MS", "900")),
    )
    p.add_argument(
        "--auto-enter",
        action="store_true",
        default=os.environ.get("SENSEVOICE_AUTO_ENTER", "0") == "1",
        help="Send FINAL (Enter) when endpoint is detected",
    )
    p.add_argument(
        "--indicator",
        choices=["none", "notify", "notify_once"],
        default=os.environ.get("SENSEVOICE_STREAM_INDICATOR", "notify_once"),
        help="Non-intrusive speaking indicator",
    )
    p.add_argument(
        "--input-device",
        default=os.environ.get("SENSEVOICE_ARECORD_DEVICE", ""),
        help="Optional arecord device, e.g. hw:Microphone,0",
    )
    p.add_argument(
        "--inject-script",
        default=os.path.join(os.path.dirname(__file__), "send_to_focus_ydotool.sh"),
    )
    p.add_argument(
        "--state-log",
        default=os.path.join(
            os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local/state")),
            "sensevoice-vibe",
            "stream_vad.log",
        ),
    )
    p.add_argument(
        "--filter-fillers",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_FILTER_FILLERS", "1")),
        help="Drop short filler/interjection outputs in Chinese mode",
    )
    p.add_argument(
        "--partial-strategy",
        choices=["stable2", "raw"],
        default=os.environ.get("SENSEVOICE_PARTIAL_STRATEGY", "stable2"),
        help="stable2: only emit stable prefix agreed by 2 consecutive partial hypotheses",
    )
    p.add_argument(
        "--emit-partial",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_EMIT_PARTIAL", "1")),
        help="Whether to emit interim PARTIAL updates",
    )
    p.add_argument(
        "--resident",
        action="store_true",
        default=os.environ.get("SENSEVOICE_RESIDENT", "0") == "1",
        help="Keep process resident and toggle active listening via SIGUSR1",
    )
    p.add_argument(
        "--active-on-start",
        action="store_true",
        default=os.environ.get("SENSEVOICE_STREAM_ACTIVE_ON_START", "0") == "1",
        help="Start listening immediately in resident mode",
    )
    p.add_argument(
        "--wake-words",
        default=os.environ.get("SENSEVOICE_WAKE_WORDS", ""),
        help="Optional wake words (comma-separated). If set, only matched utterances are injected.",
    )
    p.add_argument(
        "--wake-strategy",
        choices=["prefix", "contains"],
        default=os.environ.get("SENSEVOICE_WAKE_STRATEGY", "prefix"),
        help="Wake word match mode: prefix (recommended) or contains",
    )
    p.add_argument(
        "--wake-strip",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_WAKE_STRIP", "1")),
        help="If enabled, strip wake word token from injected text",
    )
    p.add_argument(
        "--speaker-verify",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_SPK_ENABLE", "0")),
        help="Enable speaker verification gate before text injection",
    )
    p.add_argument(
        "--speaker-enroll-wav",
        default=os.environ.get(
            "SENSEVOICE_SPK_ENROLL_WAV",
            os.path.join(
                os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local/state")),
                "sensevoice-vibe",
                "speaker_enroll.wav",
            ),
        ),
        help="Enrollment source: WAV file, directory with *.wav, or comma-separated WAV paths",
    )
    p.add_argument(
        "--speaker-threshold",
        type=float,
        default=float(os.environ.get("SENSEVOICE_SPK_THRESHOLD", "0.80")),
        help="Cosine similarity threshold for speaker verification",
    )
    p.add_argument(
        "--speaker-min-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_MIN_MS", "900")),
        help="Minimum segment duration required before speaker verification",
    )
    p.add_argument(
        "--speaker-model",
        default=os.environ.get("SENSEVOICE_SPK_MODEL", "speechbrain/spkrec-ecapa-voxceleb"),
        help="Speaker verification model id",
    )
    p.add_argument(
        "--speaker-cache-dir",
        default=os.environ.get(
            "SENSEVOICE_SPK_CACHE_DIR",
            os.path.join(os.path.expanduser("~"), ".cache", "sensevoice-vibe", "spkrec"),
        ),
        help="Cache directory for speaker verification model",
    )
    p.add_argument(
        "--speaker-score-agg",
        choices=["max", "mean", "topk_mean"],
        default=os.environ.get("SENSEVOICE_SPK_AGG", "topk_mean"),
        help="Aggregation over multiple enrollment templates",
    )
    p.add_argument(
        "--speaker-score-topk",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_TOPK", "3")),
        help="Top-k used when --speaker-score-agg=topk_mean",
    )
    p.add_argument(
        "--speaker-auto-enroll",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL", "0")),
        help="Auto-add high-confidence passed segments into speaker template set",
    )
    p.add_argument(
        "--speaker-auto-enroll-dir",
        default=os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_DIR", ""),
        help="Directory for auto-enrolled speaker templates (default: enrollment dir if it is a directory)",
    )
    p.add_argument(
        "--speaker-auto-enroll-min-score",
        type=float,
        default=float(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_MIN_SCORE", "0.74")),
        help="Minimum speaker score required to auto-enroll a segment",
    )
    p.add_argument(
        "--speaker-auto-enroll-min-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_MIN_MS", "2200")),
        help="Minimum segment duration required to auto-enroll",
    )
    p.add_argument(
        "--speaker-auto-enroll-cooldown-sec",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_COOLDOWN_SEC", "180")),
        help="Cooldown between auto-enroll operations",
    )
    p.add_argument(
        "--speaker-auto-enroll-max-templates",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_MAX_TEMPLATES", "12")),
        help="Maximum number of templates kept in memory for speaker matching",
    )
    p.add_argument(
        "--speaker-adaptive",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_SPK_ADAPTIVE", "1")),
        help="Enable adaptive speaker threshold from recent valid scores",
    )
    p.add_argument(
        "--speaker-adaptive-window",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_ADAPTIVE_WINDOW", "80")),
        help="History window size for adaptive threshold",
    )
    p.add_argument(
        "--speaker-adaptive-min-samples",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_ADAPTIVE_MIN_SAMPLES", "10")),
        help="Minimum number of recent valid samples before adaptive threshold activates",
    )
    p.add_argument(
        "--speaker-adaptive-floor",
        type=float,
        default=float(os.environ.get("SENSEVOICE_SPK_ADAPTIVE_FLOOR", "0.52")),
        help="Lower bound of adaptive threshold",
    )
    p.add_argument(
        "--speaker-adaptive-margin",
        type=float,
        default=float(os.environ.get("SENSEVOICE_SPK_ADAPTIVE_MARGIN", "0.04")),
        help="Adaptive threshold = max(floor, q25 - margin)",
    )
    p.add_argument(
        "--speaker-prune-outliers",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_SPK_PRUNE_OUTLIERS", "1")),
        help="Prune outlier templates in memory at startup",
    )
    p.add_argument(
        "--speaker-prune-keep",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_PRUNE_KEEP", "10")),
        help="Number of central templates to keep after pruning",
    )
    p.add_argument(
        "--post-llm",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_ENABLE", "0")),
        help="Enable OpenAI-compatible LLM post-processing for final text",
    )
    p.add_argument(
        "--post-llm-base-url",
        default=os.environ.get("SENSEVOICE_POST_LLM_BASE_URL", os.environ.get("OPENAI_BASE_URL", "")),
        help="OpenAI-compatible base URL, e.g. http://host:port/v1",
    )
    p.add_argument(
        "--post-llm-api-key",
        default=os.environ.get("SENSEVOICE_POST_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        help="API key for OpenAI-compatible endpoint",
    )
    p.add_argument(
        "--post-llm-model",
        default=os.environ.get("SENSEVOICE_POST_LLM_MODEL", "DeepSeek-V3.1-Terminus"),
        help="Model name for LLM post-processing",
    )
    p.add_argument(
        "--post-llm-fallback-model",
        default=os.environ.get("SENSEVOICE_POST_LLM_FALLBACK_MODEL", ""),
        help="Optional fallback model if primary returns reasoning-only or invalid output",
    )
    p.add_argument(
        "--post-llm-mode",
        choices=["correct", "polish_light", "polish_coding", "polish_coding_aggressive"],
        default=os.environ.get("SENSEVOICE_POST_LLM_MODE", "correct"),
        help="LLM post mode: strict correction or light semantic polish",
    )
    p.add_argument(
        "--post-llm-timeout-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_TIMEOUT_MS", "900")),
        help="Timeout for LLM post-processing HTTP call",
    )
    p.add_argument(
        "--post-llm-max-tokens",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_MAX_TOKENS", "192")),
        help="Max tokens for LLM post-processing output",
    )
    p.add_argument(
        "--post-llm-temperature",
        type=float,
        default=float(os.environ.get("SENSEVOICE_POST_LLM_TEMPERATURE", "0.1")),
        help="Sampling temperature for LLM post-processing",
    )
    p.add_argument(
        "--post-llm-strict",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_STRICT", "0")),
        help="If enabled, block text injection when post-LLM fails",
    )
    p.add_argument(
        "--post-llm-circuit-max-fails",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_CIRCUIT_MAX_FAILS", "2")),
        help="Open post-LLM circuit after this many consecutive failures",
    )
    p.add_argument(
        "--post-llm-circuit-cooldown-sec",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_CIRCUIT_COOLDOWN_SEC", "120")),
        help="Cooldown while post-LLM circuit is open",
    )
    p.add_argument(
        "--post-llm-hard-cooldown-sec",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_HARD_COOLDOWN_SEC", "1800")),
        help="Long cooldown for hard failures (model/auth missing)",
    )
    p.add_argument(
        "--post-llm-retry-on-timeout",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_RETRY_ON_TIMEOUT", "1")),
        help="Retry once when post-LLM times out",
    )
    p.add_argument(
        "--post-llm-retry-backoff-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_RETRY_BACKOFF_MS", "80")),
        help="Backoff before post-LLM timeout retry",
    )
    p.add_argument(
        "--post-llm-model-auto",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_MODEL_AUTO", "1")),
        help="Auto-probe /models and select available post-LLM model",
    )
    p.add_argument(
        "--post-llm-model-probe-timeout-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_MODEL_PROBE_TIMEOUT_MS", "450")),
        help="Timeout for /models probe",
    )
    p.add_argument(
        "--post-llm-min-chars",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_MIN_CHARS", "5")),
        help="Skip LLM post-processing for too-short texts",
    )
    p.add_argument(
        "--post-llm-cache-ttl-sec",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_CACHE_TTL_SEC", "300")),
        help="TTL for successful post-LLM result cache",
    )
    p.add_argument(
        "--post-llm-cache-max-entries",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_CACHE_MAX_ENTRIES", "120")),
        help="Max entries for successful post-LLM result cache",
    )
    p.add_argument(
        "--post-llm-dynamic-max-tokens",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_DYNAMIC_MAX_TOKENS", "1")),
        help="Use dynamic per-utterance max_tokens for one-shot rewrite",
    )
    p.add_argument(
        "--post-llm-output-token-factor",
        type=float,
        default=float(os.environ.get("SENSEVOICE_POST_LLM_OUTPUT_TOKEN_FACTOR", "0.7")),
        help="Dynamic max_tokens factor by input length",
    )
    p.add_argument(
        "--project-lexicon",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_PROJECT_LEXICON_ENABLE", "1")),
        help="Enable project lexicon constraints for coding terms",
    )
    p.add_argument(
        "--project-root",
        default=os.environ.get("SENSEVOICE_PROJECT_ROOT", os.path.expanduser("~/mosim_workspace")),
        help="Project root used to build coding lexicon",
    )
    p.add_argument(
        "--project-lexicon-max-terms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_PROJECT_LEXICON_MAX_TERMS", "2500")),
        help="Maximum number of lexicon terms extracted from project",
    )
    p.add_argument(
        "--project-lexicon-hint-limit",
        type=int,
        default=int(os.environ.get("SENSEVOICE_PROJECT_LEXICON_HINT_LIMIT", "16")),
        help="Max lexicon hints injected into post-LLM prompt",
    )
    p.add_argument(
        "--project-lexicon-min-term-len",
        type=int,
        default=int(os.environ.get("SENSEVOICE_PROJECT_LEXICON_MIN_TERM_LEN", "3")),
        help="Minimum identifier length for lexicon extraction",
    )
    p.add_argument(
        "--project-lexicon-extra-file",
        default=os.environ.get(
            "SENSEVOICE_PROJECT_LEXICON_EXTRA_FILE",
            os.path.join(os.path.dirname(__file__), "hotwords_coding_zh.txt"),
        ),
        help="Optional extra lexicon file (one term per line)",
    )
    p.add_argument(
        "--conf-route",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_CONF_ROUTE_ENABLE", "1")),
        help="Enable confidence-based routing for post-processing",
    )
    p.add_argument(
        "--conf-high",
        type=float,
        default=float(os.environ.get("SENSEVOICE_CONF_ROUTE_HIGH", "0.78")),
        help="High-confidence threshold (skip heavy post-LLM)",
    )
    p.add_argument(
        "--conf-low",
        type=float,
        default=float(os.environ.get("SENSEVOICE_CONF_ROUTE_LOW", "0.52")),
        help="Low-confidence threshold (stronger post-LLM correction)",
    )
    p.add_argument(
        "--learn-loop",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_LEARN_ENABLE", "1")),
        help="Enable correction memory loop",
    )
    p.add_argument(
        "--learn-store",
        default=os.environ.get(
            "SENSEVOICE_LEARN_STORE",
            os.path.join(
                os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local/state")),
                "sensevoice-vibe",
                "correction_memory.json",
            ),
        ),
        help="Persistent store for correction memory",
    )
    p.add_argument(
        "--learn-min-hits",
        type=int,
        default=int(os.environ.get("SENSEVOICE_LEARN_MIN_HITS", "2")),
        help="Minimum hits before phrase replacement rule becomes active",
    )
    p.add_argument(
        "--learn-max-rules",
        type=int,
        default=int(os.environ.get("SENSEVOICE_LEARN_MAX_RULES", "320")),
        help="Maximum number of stored correction rules",
    )
    p.add_argument(
        "--compare-log",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_COMPARE_LOG_ENABLE", "1")),
        help="Write stage-by-stage compare records (raw/clean/llm/final) to JSONL",
    )
    p.add_argument(
        "--compare-log-file",
        default=os.environ.get(
            "SENSEVOICE_COMPARE_LOG_FILE",
            os.path.join(
                os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local/state")),
                "sensevoice-vibe",
                "post_compare.jsonl",
            ),
        ),
        help="JSONL file for before/after post-processing comparison",
    )
    p.add_argument(
        "--compare-log-keep-lines",
        type=int,
        default=int(os.environ.get("SENSEVOICE_COMPARE_LOG_KEEP_LINES", "300")),
        help="Maximum number of compare JSONL lines to keep",
    )
    return p.parse_args()


def load_model(args: argparse.Namespace) -> AutoModel:
    return AutoModel(
        model=args.model,
        trust_remote_code=False,
        device=args.device,
        disable_update=True,
        disable_log=True,
    )


def _native_ctc_confidence(
    model: AutoModel,
    samples: np.ndarray,
    language: str,
) -> Tuple[str, Optional[float], str, List[Dict[str, float]]]:
    if samples.size == 0:
        return "", None, "empty", []
    inner = getattr(model, "model", None)
    tokenizer = model.kwargs.get("tokenizer")
    frontend = model.kwargs.get("frontend")
    if inner is None or tokenizer is None or frontend is None:
        return "", None, "missing_components", []

    device = model.kwargs.get("device", "cpu")
    data_type = model.kwargs.get("data_type", "sound")
    audio_fs = model.kwargs.get("fs", 16000)
    use_itn = True
    textnorm = "withitn" if use_itn else "woitn"
    lang_key = language if language in getattr(inner, "lid_dict", {}) else "auto"

    try:
        inner.eval()
        with torch.no_grad():
            audio_sample_list = load_audio_text_image_video(
                samples,
                fs=frontend.fs,
                audio_fs=audio_fs,
                data_type=data_type,
                tokenizer=tokenizer,
            )
            speech, speech_lengths = extract_fbank(
                audio_sample_list,
                data_type=data_type,
                frontend=frontend,
            )
            speech = speech.to(device=device)
            speech_lengths = speech_lengths.to(device=device)

            language_query = inner.embed(
                torch.LongTensor([[inner.lid_dict[lang_key] if lang_key in inner.lid_dict else 0]]).to(speech.device)
            ).repeat(speech.size(0), 1, 1)
            textnorm_query = inner.embed(
                torch.LongTensor([[inner.textnorm_dict[textnorm]]]).to(speech.device)
            ).repeat(speech.size(0), 1, 1)
            speech = torch.cat((textnorm_query, speech), dim=1)
            speech_lengths += 1

            event_emo_query = inner.embed(torch.LongTensor([[1, 2]]).to(speech.device)).repeat(
                speech.size(0), 1, 1
            )
            input_query = torch.cat((language_query, event_emo_query), dim=1)
            speech = torch.cat((input_query, speech), dim=1)
            speech_lengths += 3

            encoder_out, encoder_out_lens = inner.encoder(speech, speech_lengths)
            if isinstance(encoder_out, tuple):
                encoder_out = encoder_out[0]

            ctc_log_probs = inner.ctc.log_softmax(encoder_out)
            x = ctc_log_probs[0, : encoder_out_lens[0].item(), :]
            yseq = x.argmax(dim=-1)
            yseq = torch.unique_consecutive(yseq, dim=-1)
            mask = yseq != inner.blank_id
            token_int = yseq[mask].tolist()
            raw_text = tokenizer.decode(token_int)
            processed_text = rich_transcription_postprocess(raw_text).strip()
            processed_text = processed_text.translate(str.maketrans("", "", EMOJI_ARTIFACTS)).strip()

            tokens = tokenizer.text2tokens(raw_text)[4:]
            token_back_to_id = tokenizer.tokens2ids(tokens)
            token_ids: List[int] = []
            for tok_ls in token_back_to_id:
                if tok_ls:
                    token_ids.extend(tok_ls)
                else:
                    token_ids.append(124)
            if not token_ids:
                return processed_text, None, "empty_token_ids", []

            speech_probs = inner.ctc.softmax(encoder_out)[0, 4 : encoder_out_lens[0].item(), :]
            pred = speech_probs.argmax(-1)
            speech_probs[pred == inner.blank_id, inner.blank_id] = 0
            align = ctc_forced_align(
                speech_probs.unsqueeze(0).float(),
                torch.tensor(token_ids, device=speech_probs.device).unsqueeze(0).long(),
                (encoder_out_lens[0] - 4).reshape(1).long(),
                torch.tensor([len(token_ids)], device=speech_probs.device).long(),
                ignore_id=inner.ignore_id,
            )

            token_scores: List[float] = []
            display_token_scores: List[Dict[str, float]] = []
            align_seq = align[0, : int((encoder_out_lens[0] - 4).item())].tolist()
            start = 0
            for token_id, group in itertools.groupby(align_seq):
                group_len = len(list(group))
                end = start + group_len
                if token_id != inner.blank_id and group_len > 0:
                    probs = speech_probs[start:end, int(token_id)]
                    if probs.numel() > 0:
                        token_scores.append(float(probs.mean().item()))
                start = end
            per_id_scores = token_scores[:]
            if per_id_scores:
                pos = 0
                for tok, tok_ids in zip(tokens, token_back_to_id):
                    ids = tok_ids if tok_ids else [124]
                    n_ids = len(ids)
                    if pos >= len(per_id_scores):
                        break
                    seg = per_id_scores[pos : pos + n_ids]
                    pos += n_ids
                    if not seg:
                        continue
                    disp = tok.lstrip("▁").strip()
                    if not disp:
                        continue
                    display_token_scores.append(
                        {"token": disp, "score": round(float(np.mean(seg)), 4)}
                    )
            if not token_scores:
                return processed_text, None, "empty_token_scores", []
            utter_conf = _aggregate_display_conf_scores(display_token_scores)
            if utter_conf is None:
                utter_conf = float(np.clip(np.mean(token_scores), 0.0, 1.0))
            return processed_text, utter_conf, "native_ctc", display_token_scores
    except Exception as e:
        return "", None, f"native_err:{type(e).__name__}", []


def transcribe_array(model: AutoModel, samples: np.ndarray, language: str) -> str:
    if samples.size == 0:
        return ""
    res = model.generate(
        input=samples,
        cache={},
        language=language,
        use_itn=True,
        batch_size=1,
        disable_pbar=True,
        disable_log=True,
    )
    if res and isinstance(res, list) and isinstance(res[0], dict):
        text = rich_transcription_postprocess(res[0].get("text", "")).strip()
        return text.translate(str.maketrans("", "", EMOJI_ARTIFACTS)).strip()
    return ""


def transcribe_array_with_conf(
    model: AutoModel,
    samples: np.ndarray,
    language: str,
) -> Tuple[str, Optional[float], str, List[Dict[str, float]]]:
    text, conf, source, token_scores = _native_ctc_confidence(model, samples, language)
    if text:
        return text, conf, source, token_scores
    return transcribe_array(model, samples, language), None, source, []


def looks_like_partial_noise(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    allowed = set("0123456789.,，。!?！？~～-_=+*#@ ")
    if len(t) <= 6 and all(ch in allowed for ch in t):
        return True
    normalized = re.sub(rf"[{re.escape(PUNCT_EDGE)}]+", "", t).strip().lower()
    if normalized in FILLER_WORDS_EN:
        return True
    if normalized in SHORT_NOISE_ZH:
        return True
    return False


def common_prefix(a: str, b: str) -> str:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


def sanitize_transcript_text(text: str, language: str, filter_fillers: bool) -> str:
    t = text.strip()
    if not t:
        return ""
    if not filter_fillers:
        return t

    # Drop punctuation-only fragments early (e.g. ".", "...")
    normalized = re.sub(rf"[{re.escape(PUNCT_EDGE)}]+", "", t).lower()
    if not normalized:
        return ""
    # Drop obvious English interjections regardless of language mode.
    if normalized in FILLER_WORDS_EN:
        return ""
    if normalized in SHORT_NOISE_ZH:
        return ""

    lang = (language or "").lower()
    # For auto/chinese dictation mode, strip common interjection tokens.
    # Keep strict mode off for explicitly non-Chinese language settings.
    if lang not in ("zn", "zh", "yue", "auto", ""):
        return t

    # Remove filler tokens in space-delimited output.
    tokens = t.split()
    if tokens:
        kept = []
        for tok in tokens:
            core = tok.strip(PUNCT_EDGE).lower()
            if core and core in FILLER_WORDS_EN:
                continue
            kept.append(tok)
        if not kept:
            return ""
        t = " ".join(kept).strip()

    # Remove leading/trailing fillers even without explicit spaces.
    t = re.sub(r"^\s*(?:(?:yeah|ok|okay|uh|um|hmm|ah|eh|huh|mm|mhm)[\s，。,.!?！？、]*)+", "", t, flags=re.I)
    t = re.sub(r"[\s，。,.!?！？、]*(?:(?:yeah|ok|okay|uh|um|hmm|ah|eh|huh|mm|mhm))\s*$", "", t, flags=re.I)
    t = t.strip()
    normalized_zh = re.sub(rf"[{re.escape(PUNCT_EDGE)}]+", "", t).strip()
    if normalized_zh in SHORT_NOISE_ZH:
        return ""
    return t


def parse_wake_words(raw: str) -> List[str]:
    if not raw:
        return []
    words = [p.strip() for p in re.split(r"[,，;；\n]+", raw) if p.strip()]
    # Preserve user order while deduplicating
    seen = set()
    out: List[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def gate_with_wake_words(
    text: str,
    wake_words: List[str],
    strategy: str,
    strip_trigger: bool,
) -> Tuple[str, Optional[str]]:
    """
    Returns (gated_text, matched_wake_word).
    If wake words are configured and no match is found, gated_text is empty.
    """
    t = text.strip()
    if not t:
        return "", None
    if not wake_words:
        return t, None

    lower = t.lower()

    if strategy == "contains":
        for w in wake_words:
            w_lower = w.lower()
            idx = lower.find(w_lower)
            if idx < 0:
                continue
            if not strip_trigger:
                return t, w
            rest = t[idx + len(w):].lstrip(WAKE_WORD_STRIP_EDGE)
            return rest, w
        return "", None

    # Prefix strategy (default/recommended for anti-noise gating).
    t2 = t.lstrip()
    t2_lower = t2.lower()
    for w in wake_words:
        w_lower = w.lower()
        if not t2_lower.startswith(w_lower):
            continue
        if not strip_trigger:
            return t2, w
        rest = t2[len(w):].lstrip(WAKE_WORD_STRIP_EDGE)
        return rest, w

    return "", None


def make_arecord_process(args: argparse.Namespace) -> subprocess.Popen:
    cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(args.sample_rate), "-c", "1", "-t", "raw"]
    if args.input_device:
        cmd.extend(["-D", args.input_device])
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def append_state_log(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = f"{time.strftime('%F %T')} {text}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

    # Keep state log bounded in long-running sessions so disk usage does not grow.
    keep_lines_raw = os.environ.get("SENSEVOICE_LOG_KEEP_LINES", "120")
    try:
        keep_lines = int(keep_lines_raw)
    except Exception:
        keep_lines = 20
    if keep_lines < 1:
        keep_lines = 20

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= keep_lines:
            return

        kept = lines[-keep_lines:]
        with open(path, "w", encoding="utf-8") as wf:
            wf.writelines(kept)
    except Exception:
        pass


def append_jsonl_bounded(path: str, obj: dict, keep_lines: int = 400) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

    if keep_lines < 1:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= keep_lines:
            return
        with open(path, "w", encoding="utf-8") as wf:
            wf.writelines(lines[-keep_lines:])
    except Exception:
        return


def compact_log_text(text: str, limit: int = 120) -> str:
    t = (text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(t) <= limit:
        return t
    return t[:limit] + "..."


def main() -> int:
    args = parse_args()
    state_dir = os.path.dirname(args.state_log)
    os.makedirs(state_dir, exist_ok=True)
    pid_file = os.path.join(state_dir, "resident.pid")
    status_file = os.path.join(state_dir, "resident.status")

    def write_status(active: bool, ready: bool, stopping: bool = False) -> None:
        with open(status_file, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()}\n")
            f.write(f"resident={int(args.resident)}\n")
            f.write(f"ready={int(ready)}\n")
            f.write(f"active={int(active)}\n")
            f.write(f"stopping={int(stopping)}\n")
            f.write(f"updated={int(time.time())}\n")

    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    wake_words = parse_wake_words(args.wake_words)
    wake_enabled = len(wake_words) > 0
    wake_strip = bool(args.wake_strip)
    speaker_gate = SpeakerVerifierGate(
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
    post_llm = LLMPostProcessor(
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
        cache_ttl_sec=args.post_llm_cache_ttl_sec,
        cache_max_entries=args.post_llm_cache_max_entries,
        dynamic_max_tokens=bool(args.post_llm_dynamic_max_tokens),
        output_token_factor=args.post_llm_output_token_factor,
    )
    project_lexicon = ProjectLexicon(
        enabled=bool(args.project_lexicon),
        project_root=args.project_root,
        max_terms=args.project_lexicon_max_terms,
        hint_limit=args.project_lexicon_hint_limit,
        min_term_len=args.project_lexicon_min_term_len,
        extra_terms_file=args.project_lexicon_extra_file,
    )
    conf_router = ConfidenceRouter(
        enabled=bool(args.conf_route),
        high=args.conf_high,
        low=args.conf_low,
    )
    correction_memory = CorrectionMemory(
        enabled=bool(args.learn_loop),
        store_path=args.learn_store,
        min_hits=args.learn_min_hits,
        max_rules=args.learn_max_rules,
    )

    active = args.active_on_start if args.resident else True
    model_ready = False
    append_state_log(args.state_log, f"BOOT_BEGIN resident={int(args.resident)} active={int(active)}")
    append_state_log(
        args.state_log,
        "WAKE_GATE enabled={} strategy={} strip={} words={}".format(
            int(wake_enabled),
            args.wake_strategy,
            int(wake_strip),
            "|".join(wake_words) if wake_words else "-",
        ),
    )
    append_state_log(
        args.state_log,
        "SPK_GATE requested={} enabled={} threshold={} min_ms={} agg={} topk={} auto={} auto_min_score={} auto_min_ms={} auto_cd={} auto_max={} adapt={} aw={} amin={} afloor={} amargin={} prune={} pkeep={} reason={}".format(
            int(bool(args.speaker_verify)),
            int(speaker_gate.enabled),
            args.speaker_threshold,
            args.speaker_min_ms,
            args.speaker_score_agg,
            args.speaker_score_topk,
            int(bool(args.speaker_auto_enroll)),
            args.speaker_auto_enroll_min_score,
            args.speaker_auto_enroll_min_ms,
            args.speaker_auto_enroll_cooldown_sec,
            args.speaker_auto_enroll_max_templates,
            int(bool(args.speaker_adaptive)),
            args.speaker_adaptive_window,
            args.speaker_adaptive_min_samples,
            args.speaker_adaptive_floor,
            args.speaker_adaptive_margin,
            int(bool(args.speaker_prune_outliers)),
            args.speaker_prune_keep,
            speaker_gate.reason,
        ),
    )
    append_state_log(
        args.state_log,
        "POST_LLM requested={} enabled={} strict={} mode={} model={} fallback={} timeout_ms={} max_tokens={} temp={} max_fails={} cooldown_sec={} hard_cd={} retry_to={} retry_backoff_ms={} auto={} probe_ms={} min_chars={} cache_ttl={} cache_n={} dyn_tokens={} out_factor={} reason={}".format(
            int(bool(args.post_llm)),
            int(post_llm.enabled),
            int(bool(args.post_llm_strict)),
            args.post_llm_mode,
            post_llm.model or args.post_llm_model,
            post_llm.fallback_model or args.post_llm_fallback_model or "-",
            args.post_llm_timeout_ms,
            args.post_llm_max_tokens,
            args.post_llm_temperature,
            args.post_llm_circuit_max_fails,
            args.post_llm_circuit_cooldown_sec,
            args.post_llm_hard_cooldown_sec,
            int(bool(args.post_llm_retry_on_timeout)),
            args.post_llm_retry_backoff_ms,
            int(bool(args.post_llm_model_auto)),
            args.post_llm_model_probe_timeout_ms,
            args.post_llm_min_chars,
            args.post_llm_cache_ttl_sec,
            args.post_llm_cache_max_entries,
            int(bool(args.post_llm_dynamic_max_tokens)),
            args.post_llm_output_token_factor,
            post_llm.reason,
        ),
    )
    append_state_log(
        args.state_log,
        "LEXICON requested={} enabled={} root={} terms={} hint_limit={} reason={}".format(
            int(bool(args.project_lexicon)),
            int(project_lexicon.enabled),
            project_lexicon.project_root or args.project_root,
            len(project_lexicon.terms),
            args.project_lexicon_hint_limit,
            project_lexicon.reason,
        ),
    )
    append_state_log(
        args.state_log,
        "CONF_ROUTE enabled={} high={} low={}".format(
            int(bool(args.conf_route)),
            conf_router.high,
            conf_router.low,
        ),
    )
    append_state_log(
        args.state_log,
        "LEARN_LOOP requested={} enabled={} min_hits={} max_rules={} store={} reason={}".format(
            int(bool(args.learn_loop)),
            int(correction_memory.enabled),
            args.learn_min_hits,
            args.learn_max_rules,
            args.learn_store,
            correction_memory.reason,
        ),
    )
    append_state_log(
        args.state_log,
        "COMPARE_LOG enabled={} file={} keep_lines={}".format(
            int(bool(args.compare_log)),
            args.compare_log_file,
            args.compare_log_keep_lines,
        ),
    )
    write_status(active=active, ready=False)

    frame_samples = int(args.sample_rate * args.frame_ms / 1000)
    frame_bytes = frame_samples * 2
    start_frames = max(1, int(args.start_ms / args.frame_ms))
    end_frames = max(1, int(args.endpoint_silence_ms / args.frame_ms))
    pre_roll_frames = max(0, int(args.pre_roll_ms / args.frame_ms))

    stop_flag = False
    toggle_count = 0
    force_active: Optional[bool] = None

    def _stop(_sig, _frame):
        nonlocal stop_flag
        stop_flag = True

    def _toggle(_sig, _frame):
        nonlocal toggle_count
        toggle_count += 1

    def _activate(_sig, _frame):
        nonlocal force_active
        force_active = True

    def _deactivate(_sig, _frame):
        nonlocal force_active
        force_active = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _toggle)
    if hasattr(signal, "SIGUSR2"):
        signal.signal(signal.SIGUSR2, _activate)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _deactivate)

    vad = webrtcvad.Vad(args.vad_aggressiveness)
    model = load_model(args)
    model_ready = True
    append_state_log(args.state_log, "MODEL_READY")
    write_status(active=active, ready=True)

    inject_mode = os.environ.get("SENSEVOICE_INJECT_MODE", "clipboard")
    if inject_mode == "ibus":
        injector = IBusInjector()
        append_state_log(args.state_log, "INJECTOR=ibus")
    else:
        injector = FocusInjector(args.inject_script)
        append_state_log(args.state_log, "INJECTOR=clipboard")
    indicator = SpeechIndicator(args.indicator)
    append_state_log(args.state_log, f"DAEMON_START resident={int(args.resident)} active={int(active)}")

    proc: Optional[subprocess.Popen] = None
    pre_roll: Deque[bytes] = collections.deque(maxlen=pre_roll_frames)
    in_speech = False
    segment_id = 0
    speech_frames = 0
    trailing_silence = 0
    segment = bytearray()
    last_partial_ts = 0.0
    last_partial_text = ""
    prev_partial_hyp = ""

    def inject(mode: str, text: str, critical: bool = False) -> bool:
        ok = injector.send(mode, text)
        if ok:
            return True
        append_state_log(
            args.state_log,
            f"INJECT_FAIL mode={mode} reason={injector.last_error} streak={injector.fail_streak} text={compact_log_text(text)}",
        )
        if critical:
            try:
                fallback_file = os.path.join(state_dir, "last_failed_inject.txt")
                with open(fallback_file, "w", encoding="utf-8") as f:
                    f.write(text)
                append_state_log(args.state_log, f"INJECT_FALLBACK file={fallback_file}")
            except Exception:
                pass
        return False

    def reset_capture_state() -> None:
        nonlocal pre_roll, in_speech, speech_frames, trailing_silence, segment, last_partial_ts, last_partial_text, prev_partial_hyp
        pre_roll = collections.deque(maxlen=pre_roll_frames)
        in_speech = False
        speech_frames = 0
        trailing_silence = 0
        segment = bytearray()
        last_partial_ts = 0.0
        last_partial_text = ""
        prev_partial_hyp = ""

    def start_capture() -> None:
        nonlocal proc
        if proc is not None:
            return
        proc = make_arecord_process(args)
        if proc.stdout is None:
            raise RuntimeError("arecord stdout unavailable")
        indicator.reset_session()
        inject("CTRL", "RESET")
        reset_capture_state()
        append_state_log(args.state_log, "STREAM_START")

    def stop_capture(reason: str) -> None:
        nonlocal proc, in_speech
        if proc is None:
            return
        if in_speech:
            indicator.on_speech_end()
            append_state_log(args.state_log, "SPEECH_END")
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass
        proc = None
        reset_capture_state()
        append_state_log(args.state_log, f"STREAM_STOP reason={reason}")

    try:
        while not stop_flag:
            if force_active is not None:
                if active != force_active:
                    active = force_active
                    append_state_log(args.state_log, f"CONTROL_FORCE active={int(active)}")
                    write_status(active=active, ready=model_ready)
                force_active = None

            while toggle_count > 0:
                toggle_count -= 1
                active = not active
                append_state_log(args.state_log, f"CONTROL_TOGGLE active={int(active)}")
                write_status(active=active, ready=model_ready)

            if active and proc is None:
                try:
                    start_capture()
                except Exception as e:
                    append_state_log(args.state_log, f"STREAM_START_ERROR {type(e).__name__}: {e}")
                    time.sleep(0.2)
                    continue

            if not active and proc is not None:
                stop_capture("inactive")

            if not active:
                time.sleep(0.05)
                continue

            if proc is None or proc.stdout is None:
                time.sleep(0.02)
                continue

            chunk = proc.stdout.read(frame_bytes)
            if not chunk or len(chunk) < frame_bytes:
                if proc.poll() is not None:
                    append_state_log(args.state_log, f"STREAM_PROC_EXIT code={proc.returncode}")
                    stop_capture("proc_exit")
                else:
                    time.sleep(0.01)
                continue

            is_voiced = vad.is_speech(chunk, args.sample_rate)
            pre_roll.append(chunk)

            if not in_speech:
                if is_voiced:
                    speech_frames += 1
                    if speech_frames >= start_frames:
                        in_speech = True
                        segment_id += 1
                        trailing_silence = 0
                        segment = bytearray(b"".join(pre_roll))
                        last_partial_ts = 0.0
                        last_partial_text = ""
                        prev_partial_hyp = ""
                        inject("SEG", str(segment_id))
                        append_state_log(args.state_log, f"SEG id={segment_id}")
                        indicator.on_speech_start()
                        append_state_log(args.state_log, "SPEECH_START")
                else:
                    speech_frames = 0
                continue

            segment.extend(chunk)
            if is_voiced:
                trailing_silence = 0
            else:
                trailing_silence += 1

            now = time.time()
            seg_ms = len(segment) * 1000 / (args.sample_rate * 2)
            should_partial = (
                seg_ms >= args.min_partial_ms
                and now - last_partial_ts >= args.partial_interval_ms / 1000.0
            )
            if args.emit_partial and should_partial:
                samples = np.frombuffer(segment, dtype=np.int16).astype(np.float32) / 32768.0
                raw_text = transcribe_array(model, samples, args.language)
                text = sanitize_transcript_text(raw_text, args.language, bool(args.filter_fillers))
                if raw_text and not text:
                    append_state_log(args.state_log, f"DROP_PARTIAL raw={raw_text}")
                emit_text = ""
                if text:
                    if args.partial_strategy == "stable2":
                        # Local-agreement style: only expose stable prefix between two consecutive hypotheses.
                        if prev_partial_hyp:
                            emit_text = common_prefix(prev_partial_hyp, text).rstrip()
                        prev_partial_hyp = text
                    else:
                        emit_text = text

                if (
                    emit_text
                    and not looks_like_partial_noise(emit_text)
                    and emit_text != last_partial_text
                    and emit_text.startswith(last_partial_text)
                ):
                    if wake_enabled:
                        gated_partial, _ = gate_with_wake_words(
                            emit_text,
                            wake_words,
                            args.wake_strategy,
                            wake_strip,
                        )
                        if not gated_partial:
                            last_partial_ts = now
                            continue
                        emit_text = gated_partial
                    inject("PARTIAL", emit_text)
                    last_partial_text = emit_text
                    append_state_log(args.state_log, f"PARTIAL text={emit_text}")
                last_partial_ts = now

            should_finalize = trailing_silence >= end_frames or seg_ms >= args.max_segment_ms
            if not should_finalize:
                continue

            if seg_ms >= args.min_segment_ms:
                samples = np.frombuffer(segment, dtype=np.int16).astype(np.float32) / 32768.0
                raw_final_text, native_conf_score, conf_source, native_token_scores = transcribe_array_with_conf(
                    model, samples, args.language
                )
                final_text = sanitize_transcript_text(raw_final_text, args.language, bool(args.filter_fillers))
                compare_rec = {
                    "ts": time.strftime("%F %T"),
                    "seg_id": int(segment_id),
                    "seg_ms": int(seg_ms),
                    "raw_asr": raw_final_text,
                    "after_sanitize": final_text,
                    "spk_pass": None,
                    "spk_score": None,
                    "spk_thr": None,
                    "after_wake": None,
                    "after_memory": None,
                    "conf_source": conf_source,
                    "conf_native": native_conf_score,
                    "conf_tokens": native_token_scores,
                    "conf_low_tokens": [],
                    "conf_score": None,
                    "conf_route": None,
                    "llm_action": "none",
                    "after_llm": None,
                    "after_lexicon": None,
                    "final_injected": "",
                    "inject_ok": 0,
                    "drop_reason": "",
                }
                spk_score_for_conf: Optional[float] = None
                spk_thr_for_conf: Optional[float] = None
                if raw_final_text and not final_text:
                    append_state_log(args.state_log, f"DROP_FINAL raw={raw_final_text}")
                    compare_rec["drop_reason"] = "sanitize_drop"
                if final_text:
                    spk_ok, spk_score, spk_reason, spk_thr = speaker_gate.verify(
                        samples,
                        args.sample_rate,
                        seg_ms,
                    )
                    compare_rec["spk_pass"] = int(spk_ok)
                    compare_rec["spk_score"] = round(float(spk_score), 4)
                    compare_rec["spk_thr"] = round(float(spk_thr), 4)
                    if not spk_ok:
                        append_state_log(
                            args.state_log,
                            f"DROP_FINAL_SPK score={spk_score:.3f} thr={spk_thr:.3f} reason={spk_reason} raw={final_text}",
                        )
                        compare_rec["drop_reason"] = f"spk:{spk_reason}"
                        final_text = ""
                    elif speaker_gate.requested and speaker_gate.enabled:
                        append_state_log(
                            args.state_log,
                            f"SPK_PASS score={spk_score:.3f} thr={spk_thr:.3f} reason={spk_reason}",
                        )
                        spk_score_for_conf = float(spk_score)
                        spk_thr_for_conf = float(spk_thr)
                        speaker_gate.on_success(spk_score, seg_ms, final_text)
                        added, add_info = speaker_gate.maybe_auto_enroll(
                            samples,
                            args.sample_rate,
                            seg_ms,
                            spk_score,
                        )
                        if added:
                            append_state_log(
                                args.state_log,
                                f"SPK_AUTO_ENROLL add={add_info} score={spk_score:.3f} n={speaker_gate.enroll_count}",
                            )
                        elif add_info not in ("auto_off", "score_low", "cooldown"):
                            append_state_log(
                                args.state_log,
                                f"SPK_AUTO_ENROLL_SKIP reason={add_info} score={spk_score:.3f} n={speaker_gate.enroll_count}",
                            )
                if final_text:
                    if wake_enabled:
                        gated_final, matched_wake = gate_with_wake_words(
                            final_text,
                            wake_words,
                            args.wake_strategy,
                            wake_strip,
                        )
                        if not gated_final:
                            append_state_log(args.state_log, f"DROP_FINAL_WAKE raw={final_text}")
                            compare_rec["drop_reason"] = "wake_gate"
                            final_text = ""
                        else:
                            final_text = gated_final
                            if matched_wake:
                                append_state_log(
                                    args.state_log,
                                    f"WAKE_MATCH wake={matched_wake} final={final_text}",
                                )
                    compare_rec["after_wake"] = final_text
                    if not final_text:
                        pass
                    else:
                        source_text = final_text
                        if correction_memory.enabled:
                            mem_text = correction_memory.apply(final_text)
                            if mem_text != final_text:
                                append_state_log(
                                    args.state_log,
                                    "LEARN_APPLY src={} dst={}".format(
                                        compact_log_text(final_text),
                                        compact_log_text(mem_text),
                                    ),
                                )
                                final_text = mem_text
                        compare_rec["after_memory"] = final_text

                        conf_score = conf_router.estimate(
                            raw_final_text,
                            final_text,
                            prev_partial_hyp,
                            seg_ms,
                            spk_score_for_conf,
                            spk_thr_for_conf,
                            native_conf=native_conf_score,
                            native_token_scores=native_token_scores,
                        )
                        if native_token_scores:
                            conf_source_used = f"{conf_source}:focus_agg"
                        elif native_conf_score is not None:
                            conf_source_used = conf_source
                        else:
                            conf_source_used = f"fallback_heuristic:{conf_source}"
                        conf_route = conf_router.route(conf_score)
                        low_tokens = _select_focus_low_tokens(native_token_scores, limit=4) if native_token_scores else []
                        append_state_log(
                            args.state_log,
                            f"CONF_SCORE score={conf_score:.3f} route={conf_route} seg_ms={int(seg_ms)} source={conf_source_used}",
                        )
                        compare_rec["conf_source"] = conf_source_used
                        compare_rec["conf_low_tokens"] = low_tokens
                        compare_rec["conf_score"] = round(float(conf_score), 4)
                        compare_rec["conf_route"] = conf_route

                        glossary: List[str] = []
                        if project_lexicon.enabled:
                            glossary = project_lexicon.hints_for_text(final_text)
                        focus_token_texts = [str(x.get("token", "")).strip() for x in low_tokens if str(x.get("token", "")).strip()]

                        allow_high_route_rewrite = post_llm.mode in {"polish_coding", "polish_coding_aggressive"}
                        if post_llm.enabled and (conf_route != "high" or allow_high_route_rewrite):
                            llm_src = final_text
                            revised = post_llm.process(
                                final_text,
                                route=conf_route,
                                glossary=glossary,
                                focus_tokens=focus_token_texts,
                            )
                            if revised != final_text:
                                compare_rec["llm_action"] = "apply"
                                append_state_log(
                                    args.state_log,
                                    "POST_LLM_APPLY src={} dst={} route={}".format(
                                        compact_log_text(final_text),
                                        compact_log_text(revised),
                                        conf_route,
                                    ),
                                )
                            elif post_llm.last_error:
                                compare_rec["llm_action"] = "skip_error"
                                append_state_log(
                                    args.state_log,
                                    f"POST_LLM_SKIP reason={post_llm.last_error} route={conf_route}",
                                )
                            else:
                                compare_rec["llm_action"] = "pass"
                                append_state_log(
                                    args.state_log,
                                    "POST_LLM_PASS text={} route={}".format(
                                        compact_log_text(final_text),
                                        conf_route,
                                    ),
                                )
                            if args.post_llm_strict and post_llm.last_error:
                                append_state_log(
                                    args.state_log,
                                    "POST_LLM_BLOCK src={} reason={}".format(
                                        compact_log_text(llm_src),
                                        post_llm.last_error,
                                    ),
                                )
                                final_text = ""
                            else:
                                final_text = revised
                            compare_rec["after_llm"] = final_text
                        elif post_llm.enabled:
                            compare_rec["llm_action"] = "skip_high_conf"
                            append_state_log(args.state_log, "POST_LLM_SKIP reason=high_conf_route")
                        else:
                            compare_rec["llm_action"] = "disabled"
                            compare_rec["after_llm"] = final_text

                        if final_text and project_lexicon.enabled:
                            lex_text = project_lexicon.normalize_text(final_text)
                            if lex_text != final_text:
                                append_state_log(
                                    args.state_log,
                                    "LEXICON_APPLY src={} dst={}".format(
                                        compact_log_text(final_text),
                                        compact_log_text(lex_text),
                                    ),
                                )
                                final_text = lex_text
                        compare_rec["after_lexicon"] = final_text

                        if correction_memory.enabled and final_text and final_text != source_text:
                            correction_memory.learn(source_text, final_text)
                            append_state_log(
                                args.state_log,
                                "LEARN_UPDATE src={} dst={}".format(
                                    compact_log_text(source_text),
                                    compact_log_text(final_text),
                                ),
                            )
                        if final_text:
                            mode = "FINAL" if args.auto_enter else "PARTIAL"
                            compare_rec["final_injected"] = final_text
                            ok_inject = inject(mode, final_text, critical=True)
                            compare_rec["inject_ok"] = int(ok_inject)
                            if ok_inject:
                                append_state_log(args.state_log, f"FINAL mode={mode} text={final_text}")
                            else:
                                compare_rec["drop_reason"] = "inject_fail"
                if bool(args.compare_log):
                    append_jsonl_bounded(
                        args.compare_log_file,
                        compare_rec,
                        keep_lines=args.compare_log_keep_lines,
                    )
            indicator.on_speech_end()
            append_state_log(args.state_log, "SPEECH_END")
            in_speech = False
            speech_frames = 0
            trailing_silence = 0
            segment = bytearray()
            pre_roll.clear()
    finally:
        stop_capture("shutdown")
        indicator.on_shutdown()
        injector.close()
        correction_memory.flush()
        write_status(active=False, ready=model_ready, stopping=True)
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write("")
        append_state_log(args.state_log, "DAEMON_STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
