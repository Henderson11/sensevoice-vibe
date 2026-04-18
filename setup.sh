#!/usr/bin/env bash
# 创建 venv、安装 Python 依赖、注册 FunASR 声纹扩展、自检环境
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
REQ_FILE="$ROOT_DIR/requirements.txt"

echo "==> [1/4] 创建虚拟环境 .venv"
[[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install -q -U pip setuptools wheel

echo ""
echo "==> [2/4] 安装 PyTorch"
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "  GPU 检测到，安装 CUDA 12.1 wheel"
  "$VENV_DIR/bin/python" -m pip install -q -U \
    torch torchaudio --index-url https://download.pytorch.org/whl/cu121
else
  echo "  CPU 模式，安装 CPU wheel"
  "$VENV_DIR/bin/python" -m pip install -q -U \
    torch==2.10.0+cpu torchaudio==2.10.0+cpu \
    --index-url https://download.pytorch.org/whl/cpu
fi

echo ""
echo "==> [3/4] 安装其余 Python 依赖"
grep -vE "^(torch|torchaudio)==" "$REQ_FILE" \
  | "$VENV_DIR/bin/python" -m pip install -q -r /dev/stdin

echo ""
echo "==> [4/4] 注册 ERes2NetV2 到 FunASR"
FUNASR_ERES2NET="$("$VENV_DIR/bin/python" -c \
  "import funasr, os; print(os.path.join(os.path.dirname(funasr.__file__), 'models', 'eres2net'))" 2>/dev/null)"
if [[ -d "$FUNASR_ERES2NET" ]]; then
  cp "$ROOT_DIR/sensevoice/speaker/funasr_patch/eres2netv2.py" "$FUNASR_ERES2NET/"
  cp "$ROOT_DIR/sensevoice/speaker/funasr_patch/pooling_layers.py" "$FUNASR_ERES2NET/"
  if ! grep -q "eres2netv2" "$FUNASR_ERES2NET/__init__.py" 2>/dev/null; then
    echo "from funasr.models.eres2net.eres2netv2 import ERes2NetV2  # noqa" \
      >> "$FUNASR_ERES2NET/__init__.py"
  fi
  echo "  ERes2NetV2 已注册: $FUNASR_ERES2NET"
else
  echo "  WARN: 未找到 funasr eres2net 目录，声纹模块将无法加载"
fi

echo ""
echo "==> 自检：Python 依赖"
"$VENV_DIR/bin/python" - <<'PY'
import importlib, sys
mods = ["funasr", "funasr_onnx", "modelscope", "huggingface_hub",
        "webrtcvad", "numpy", "torch", "torchaudio"]
err = 0
for m in mods:
    try:
        importlib.import_module(m); print(f"  ok   {m}")
    except Exception as e:
        err += 1; print(f"  FAIL {m}: {e}")
sys.exit(err)
PY

echo ""
echo "==> 自检：系统层 IBus 绑定（/usr/bin/python3 + gi.repository.IBus）"
if /usr/bin/python3 -c "from gi.repository import IBus" 2>/dev/null; then
  echo "  ok"
else
  echo "  FAIL: 请执行 sudo apt install -y python3-gi gir1.2-ibus-1.0"
  exit 1
fi

echo ""
echo "Setup 完成。下一步："
echo "  ./download_models.sh                                        # 下载模型 (~1.2GB)"
echo "  ./install_ibus_engine.sh                                    # 注册 IBus 引擎"
echo "  mkdir -p ~/.config/sensevoice-vibe"
echo "  cp config/llm.env.example ~/.config/sensevoice-vibe/llm.env"
echo "  \$EDITOR ~/.config/sensevoice-vibe/llm.env                   # 填入 LLM API key"
echo "  ./enroll_speaker_f8.sh                                      # 录制声纹"
echo "  ./toggle_resident_f8.sh on                                  # 启动"
