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
        model_path = str(getattr(args, 'model', '?'))
        # 只显示最后两级目录
        model_short = "/".join(model_path.replace("\\", "/").split("/")[-2:]) if "/" in model_path else model_path

        lines = ["=== SenseVoice 配置 ==="]

        lines.append(f"  ASR 模型       : {model_short}")
        lines.append(f"  语言           : {getattr(args, 'language', '?')}")

        # 断句参数
        lines.append(
            f"  断句           : 停顿={getattr(args, 'endpoint_silence_ms', '?')}ms"
            f" 最长={getattr(args, 'max_segment_ms', '?')}ms"
        )

        # 声纹门禁
        spk = getattr(args, "speaker_verify", 0)
        if spk:
            lines.append(f"  声纹门禁       : ON  阈值={getattr(args, 'speaker_threshold', '?')}")
        else:
            lines.append(f"  声纹门禁       : OFF")

        # LLM 润色
        llm = getattr(args, "post_llm", 0)
        if llm:
            lines.append(f"  LLM 润色       : ON  模型={getattr(args, 'post_llm_model', '?')}")
        else:
            lines.append(f"  LLM 润色       : OFF")

        # 置信度路由
        cr = getattr(args, "conf_route", 0)
        if cr:
            lines.append(f"  置信度路由     : ON  高={getattr(args, 'conf_high', '?')} 低={getattr(args, 'conf_low', '?')}")
        else:
            lines.append(f"  置信度路由     : OFF")

        # 项目术语表
        lex = getattr(args, "project_lexicon", 0)
        lines.append(f"  项目术语表     : {'ON' if lex else 'OFF'}")

        lines.append("========================")
        return "\n".join(lines)
