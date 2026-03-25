#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install -U pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install -U funasr modelscope
"$VENV_DIR/bin/python" -m pip install -U torch torchaudio --index-url https://download.pytorch.org/whl/cpu
"$VENV_DIR/bin/python" -m pip install -U torchcodec imageio-ffmpeg

# Provide a local ffmpeg binary without requiring system package manager.
FFMPEG_BIN="$("$VENV_DIR/bin/python" - << 'PY'
import imageio_ffmpeg
print(imageio_ffmpeg.get_ffmpeg_exe())
PY
)"
ln -sf "$FFMPEG_BIN" "$VENV_DIR/bin/ffmpeg"

cat << 'EOF'
Setup complete.
Run inference with:
  ./run.sh /path/to/audio.wav
EOF
