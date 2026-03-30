# sensevoice/asr/confidence.py
# Token-level confidence scoring utilities: normalization, punctuation/low-value
# filtering, weighted aggregation, and focus-token selection for display.

import re
from typing import Dict, List, Optional, Tuple

import numpy as np

from sensevoice.text.constants import (
    CONF_LOW_VALUE_ZH,
    CONF_PUNCT_TOKENS,
    FILLER_WORDS_EN,
    SHORT_NOISE_ZH,
)

# Confidence aggregation weights
CONF_WEIGHT_MEAN_ALL = 0.55
CONF_WEIGHT_MEAN_BOTTOM = 0.45
CONF_WEIGHT_SHORT_MEAN_ALL = 0.70
CONF_WEIGHT_SHORT_MEAN_BOTTOM = 0.30
CONF_BOTTOM_N_LIMIT = 4
CONF_SHORT_THRESHOLD = 3


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
    bottom_n = ordered[: max(1, min(CONF_BOTTOM_N_LIMIT, len(ordered)))]
    mean_all = float(np.mean(values))
    mean_bottom = float(np.mean(bottom_n))
    conf = CONF_WEIGHT_MEAN_ALL * mean_all + CONF_WEIGHT_MEAN_BOTTOM * mean_bottom
    if len(values) <= CONF_SHORT_THRESHOLD:
        conf = CONF_WEIGHT_SHORT_MEAN_ALL * mean_all + CONF_WEIGHT_SHORT_MEAN_BOTTOM * mean_bottom
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
