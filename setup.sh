#!/usr/bin/env bash
# venv 创建 + Python 依赖安装 + FunASR ERes2NetV2 注册
# 系统级依赖（python3-gi / ibus / arecord）请先 apt install，见 README
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
REQ_FILE="$ROOT_DIR/requirements.txt"

echo "==> [1/4] 建 venv (.venv)"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install -q -U pip setuptools wheel

echo ""
echo "==> [2/4] 装 PyTorch（独立的 index-url）"
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "  检测到 NVIDIA GPU，装 CUDA 12.1 wheel"
  "$VENV_DIR/bin/python" -m pip install -q -U \
    torch torchaudio --index-url https://download.pytorch.org/whl/cu121
else
  echo "  未检测到 GPU，装 CPU wheel"
  "$VENV_DIR/bin/python" -m pip install -q -U \
    torch==2.10.0+cpu torchaudio==2.10.0+cpu \
    --index-url https://download.pytorch.org/whl/cpu
fi

echo ""
echo "==> [3/4] 装其余 Python 依赖（requirements.txt）"
# torch/torchaudio 已按上面专属 index 装好，过滤掉避免重装
grep -vE "^(torch|torchaudio)==" "$REQ_FILE" \
  | "$VENV_DIR/bin/python" -m pip install -q -r /dev/stdin

# ffmpeg 兜底（音频解码备用）
"$VENV_DIR/bin/python" -m pip install -q imageio-ffmpeg
FFMPEG_BIN="$("$VENV_DIR/bin/python" -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")"
ln -sf "$FFMPEG_BIN" "$VENV_DIR/bin/ffmpeg"

echo ""
echo "==> [4/4] 注册 ERes2NetV2 到 FunASR（FunASR 1.3.1 不原生支持）"
FUNASR_ERES2NET="$("$VENV_DIR/bin/python" -c \
  "import funasr, os; print(os.path.join(os.path.dirname(funasr.__file__), 'models', 'eres2net'))" 2>/dev/null)"
if [[ -d "$FUNASR_ERES2NET" ]]; then
  cp "$ROOT_DIR/sensevoice/speaker/funasr_patch/eres2netv2.py" "$FUNASR_ERES2NET/"
  cp "$ROOT_DIR/sensevoice/speaker/funasr_patch/pooling_layers.py" "$FUNASR_ERES2NET/"
  if ! grep -q "eres2netv2" "$FUNASR_ERES2NET/__init__.py" 2>/dev/null; then
    echo "from funasr.models.eres2net.eres2netv2 import ERes2NetV2  # noqa" \
      >> "$FUNASR_ERES2NET/__init__.py"
  fi
  echo "  ERes2NetV2 已注册到 $FUNASR_ERES2NET"
else
  echo "  WARN: funasr eres2net 目录未找到，声纹模型可能无法加载"
fi

echo ""
echo "==> 验证关键 import"
"$VENV_DIR/bin/python" - <<'PY'
import importlib, sys
mods = ["funasr", "funasr_onnx", "modelscope", "huggingface_hub",
        "webrtcvad", "soundfile", "numpy", "torch", "torchaudio"]
errs = []
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  ok   {m}")
    except Exception as e:
        errs.append((m, e)); print(f"  FAIL {m}: {e}")
sys.exit(1 if errs else 0)
PY

echo ""
echo "==> 系统依赖检查（IBus engine 用 /usr/bin/python3，需要 gi）"
if /usr/bin/python3 -c "from gi.repository import IBus" 2>/dev/null; then
  echo "  ok   /usr/bin/python3 + gi.repository.IBus"
else
  echo "  FAIL /usr/bin/python3 不能 import gi.repository.IBus"
  echo "       请运行: sudo apt install -y python3-gi gir1.2-ibus-1.0"
fi

echo ""
echo "Setup 完成。下一步："
echo "  ./download_models.sh       # 拉模型 (~1.2GB)"
echo "  ./install_ibus_engine.sh   # 装 IBus 引擎"
echo "  mkdir -p ~/.config/sensevoice-vibe"
echo "  cp config/llm.env.example ~/.config/sensevoice-vibe/llm.env"
echo "  \$EDITOR ~/.config/sensevoice-vibe/llm.env  # 填 LLM API key"
echo "  ./enroll_speaker_f8.sh     # 录声纹"
echo "  ./toggle_resident_f8.sh on # 启动"
