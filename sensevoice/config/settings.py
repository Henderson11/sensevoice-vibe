"""统一配置管理 - 所有 SENSEVOICE_ 环境变量的默认值和验证"""

from __future__ import annotations

import argparse
from typing import List


class SenseVoiceConfig:
    """从 argparse.Namespace 构建，提供配置验证和摘要。

    不替换 argparse，只集中管理默认值校验和启动日志。
    """

    @staticmethod
    def validate(args: argparse.Namespace) -> List[str]:
        """验证配置，返回警告列表。"""
        warnings: List[str] = []

        # --- LLM post-processing ---
        if getattr(args, "post_llm", 0) and not getattr(args, "post_llm_api_key", ""):
            warnings.append("POST_LLM enabled but no API key configured")

        # --- Speaker verification ---
        if getattr(args, "speaker_verify", 0) and not getattr(args, "speaker_enroll_wav", ""):
            warnings.append("Speaker verification enabled but no enrollment data")

        # --- Stream timing ---
        endpoint_ms = getattr(args, "endpoint_silence_ms", 650)
        if endpoint_ms < 500:
            warnings.append(
                f"ENDPOINT_MS={endpoint_ms} too low, may cause frequent splits"
            )

        max_seg_ms = getattr(args, "max_segment_ms", 8000)
        if max_seg_ms < 5000:
            warnings.append(
                f"MAX_SEGMENT_MS={max_seg_ms} too low"
            )

        min_seg_ms = getattr(args, "min_segment_ms", 850)
        if min_seg_ms >= endpoint_ms:
            warnings.append(
                f"MIN_SEGMENT_MS={min_seg_ms} >= ENDPOINT_MS={endpoint_ms}, "
                "segments may never emit"
            )

        # --- Confidence routing ---
        conf_high = getattr(args, "conf_high", 0.78)
        conf_low = getattr(args, "conf_low", 0.52)
        if conf_low >= conf_high:
            warnings.append(
                f"CONF_LOW={conf_low} >= CONF_HIGH={conf_high}, routing will be ineffective"
            )

        # --- Speaker adaptive ---
        if getattr(args, "speaker_adaptive", 0):
            floor = getattr(args, "speaker_adaptive_floor", 0.52)
            threshold = getattr(args, "speaker_threshold", 0.80)
            if floor >= threshold:
                warnings.append(
                    f"SPK_ADAPTIVE_FLOOR={floor} >= SPK_THRESHOLD={threshold}"
                )

        # --- Post-LLM temperature ---
        temp = getattr(args, "post_llm_temperature", 0.1)
        if temp > 1.0:
            warnings.append(
                f"POST_LLM_TEMPERATURE={temp} unusually high for correction tasks"
            )

        return warnings

    @staticmethod
    def summary(args: argparse.Namespace) -> str:
        """生成配置摘要用于启动日志。"""
        lines = [
            "=== SenseVoice Configuration ===",
            f"  model          : {getattr(args, 'model', '?')}",
            f"  device         : {getattr(args, 'device', '?')}",
            f"  language       : {getattr(args, 'language', '?')}",
            f"  resident       : {getattr(args, 'resident', False)}",
            f"  auto_enter     : {getattr(args, 'auto_enter', False)}",
        ]

        # VAD / stream timing
        lines.append(
            f"  stream         : frame={getattr(args, 'frame_ms', '?')}ms "
            f"vad_agg={getattr(args, 'vad_aggressiveness', '?')} "
            f"endpoint={getattr(args, 'endpoint_silence_ms', '?')}ms "
            f"max_seg={getattr(args, 'max_segment_ms', '?')}ms"
        )

        # Speaker verification
        spk = getattr(args, "speaker_verify", 0)
        lines.append(
            f"  speaker_verify : {'ON' if spk else 'off'}"
            + (
                f"  threshold={getattr(args, 'speaker_threshold', '?')}"
                if spk else ""
            )
        )

        # Post-LLM
        llm = getattr(args, "post_llm", 0)
        lines.append(
            f"  post_llm       : {'ON' if llm else 'off'}"
            + (
                f"  mode={getattr(args, 'post_llm_mode', '?')} "
                f"model={getattr(args, 'post_llm_model', '?')}"
                if llm else ""
            )
        )

        # Confidence routing
        cr = getattr(args, "conf_route", 0)
        lines.append(
            f"  conf_route     : {'ON' if cr else 'off'}"
            + (
                f"  high={getattr(args, 'conf_high', '?')} "
                f"low={getattr(args, 'conf_low', '?')}"
                if cr else ""
            )
        )

        # Project lexicon
        lex = getattr(args, "project_lexicon", 0)
        lines.append(
            f"  project_lexicon: {'ON' if lex else 'off'}"
        )

        lines.append("================================")
        return "\n".join(lines)
