# sensevoice/asr/router.py
# ConfidenceRouter: maps ASR segment confidence scores to routing tiers
# (high/mid/low) for downstream processing decisions (e.g. LLM post-processing).

from typing import Dict, List, Optional

from sensevoice.asr.confidence import _aggregate_display_conf_scores
from sensevoice.text.processing import common_prefix


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
