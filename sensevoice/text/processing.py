# sensevoice/text/processing.py
# 文本处理：识别结果清洗（去掉纯噪声片段与英文语气词）、日志文本截断。

import re

from sensevoice.text.constants import (
    FILLER_WORDS_EN,
    PUNCT_EDGE,
    SHORT_NOISE_ZH,
)




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
