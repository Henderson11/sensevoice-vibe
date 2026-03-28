# sensevoice/text/processing.py
# Text processing utilities: noise detection, prefix matching, transcript
# sanitization, wake-word gating, and log text compaction.

import re
from typing import List, Optional, Tuple

from sensevoice.text.constants import (
    FILLER_WORDS_EN,
    PUNCT_EDGE,
    SHORT_NOISE_ZH,
    WAKE_WORD_STRIP_EDGE,
)


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


def compact_log_text(text: str, limit: int = 120) -> str:
    t = (text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(t) <= limit:
        return t
    return t[:limit] + "..."


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
