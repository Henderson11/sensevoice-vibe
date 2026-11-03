# sensevoice/asr/model.py
# 构造语音识别客户端：百炼云端 ASR（主模型 + 并发对冲的备用模型），参数全部来自环境变量（llm.env）。

import os

from sensevoice.asr.bailian import BailianASRClient  # 与 Windows 版共用同一份源码


def load_asr_client() -> BailianASRClient:
    custom_yaml = os.environ.get("SENSEVOICE_ASR_CONTEXT_FILE", "").strip()
    if custom_yaml:
        hotwords_yaml_path = os.path.expanduser(custom_yaml)
    else:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        hotwords_yaml_path = os.path.join(project_root, "config", "hotwords.yaml")
    return BailianASRClient(
        api_key=os.environ.get("SENSEVOICE_BAILIAN_API_KEY", ""),
        url=os.environ.get("SENSEVOICE_BAILIAN_URL", ""),
        model=os.environ.get("SENSEVOICE_BAILIAN_MODEL", "qwen-audio-3.1-asr-flash"),
        hotwords_yaml_path=hotwords_yaml_path,
        # 主模型 hedge_after 内没回，就并发再发给备用模型，谁先成功用谁
        hedge_models=[m for m in os.environ.get(
            "SENSEVOICE_BAILIAN_HEDGE_MODELS", "fun-asr-flash-2026-06-15,qwen3-asr-flash").split(",") if m.strip()],
        hedge_after_sec=float(os.environ.get("SENSEVOICE_BAILIAN_HEDGE_AFTER_MS", "2500")) / 1000.0,
        timeout_sec=float(os.environ.get("SENSEVOICE_BAILIAN_TIMEOUT_SEC", "15")),
        timeout_ratio=float(os.environ.get("SENSEVOICE_BAILIAN_TIMEOUT_RATIO", "0.35")),
        hedge_after_ratio=float(os.environ.get("SENSEVOICE_BAILIAN_HEDGE_AFTER_RATIO", "0.1")),
        workspace=os.environ.get("SENSEVOICE_BAILIAN_WORKSPACE", ""),
        context_turns=int(os.environ.get("SENSEVOICE_BAILIAN_CONTEXT_TURNS", "3")),
        context_max_chars=int(os.environ.get("SENSEVOICE_BAILIAN_CONTEXT_MAX_CHARS", "300")),
    )
