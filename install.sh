#!/usr/bin/env bash
# 一键安装：系统依赖检查 → Python 环境 → 声纹模型 → 配置文件 → IBus 引擎 → 快捷键（F8 / Shift+F8）
# 开机自启另跑 ./install_autostart_service.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
CONFIG_DIR="$HOME/.config/sensevoice-vibe"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"

echo "[1/6] 检查系统依赖"
missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3 python3-venv")
command -v arecord >/dev/null 2>&1 || missing+=("alsa-utils")
command -v ibus >/dev/null 2>&1 || missing+=("ibus")
/usr/bin/python3 -c "from gi.repository import IBus" >/dev/null 2>&1 || missing+=("python3-gi gir1.2-ibus-1.0")
command -v notify-send >/dev/null 2>&1 || missing+=("libnotify-bin")
command -v zenity >/dev/null 2>&1 || missing+=("zenity")
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "  缺少: ${missing[*]}"
  echo "  请先执行: sudo apt install -y ${missing[*]}"
  exit 1
fi
command -v wl-copy >/dev/null 2>&1 || echo "  提示: 未装 wl-clipboard，最近语音面板的\"复制文字\"不可用"

echo "[2/6] Python 环境（.venv）"
[[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install -q -U pip setuptools wheel
# 声纹模型只需 CPU 版 PyTorch
"$VENV_DIR/bin/python" -m pip install -q torch==2.10.0+cpu torchaudio==2.10.0+cpu \
  --index-url https://download.pytorch.org/whl/cpu
grep -vE "^(torch|torchaudio)==" "$ROOT_DIR/requirements.txt" | "$VENV_DIR/bin/python" -m pip install -q -r /dev/stdin
# ERes2NetV2 不在 FunASR 发行版里，拷入补丁并注册
FUNASR_ERES2NET="$("$VENV_DIR/bin/python" -c \
  "import importlib.util as u, os; print(os.path.join(list(u.find_spec('funasr').submodule_search_locations)[0], 'models', 'eres2net'))")"
cp "$ROOT_DIR/sensevoice/speaker/funasr_patch/eres2netv2.py" "$ROOT_DIR/sensevoice/speaker/funasr_patch/pooling_layers.py" "$FUNASR_ERES2NET/"
grep -q "eres2netv2" "$FUNASR_ERES2NET/__init__.py" 2>/dev/null \
  || echo "from funasr.models.eres2net.eres2netv2 import ERes2NetV2  # noqa" >> "$FUNASR_ERES2NET/__init__.py"
"$VENV_DIR/bin/python" -c "import funasr, torch, webrtcvad, numpy, requests, yaml, dashscope; print('  Python 依赖 ok')"

echo "[3/6] 声纹模型"
bash "$ROOT_DIR/download_models.sh"

echo "[4/6] 配置文件"
mkdir -p "$CONFIG_DIR" "$STATE_DIR/speaker_enroll.d"
if [[ ! -f "$CONFIG_DIR/llm.env" ]]; then
  sed -e "s|__INSTALL_DIR__|$ROOT_DIR|g" "$ROOT_DIR/config/llm.env.example" > "$CONFIG_DIR/llm.env"
  echo "  已创建 $CONFIG_DIR/llm.env —— 需填入百炼 API Key 和润色用的 LLM 配置"
else
  echo "  已存在，跳过: $CONFIG_DIR/llm.env"
fi
chmod 600 "$CONFIG_DIR/llm.env"

echo "[5/6] IBus 引擎（需要 sudo）"
bash "$ROOT_DIR/install_ibus_engine.sh"

echo "[6/6] 快捷键"
if command -v gsettings >/dev/null 2>&1; then
  bash "$ROOT_DIR/configure_f8.sh"
else
  echo "  非 GNOME 环境，请手动绑定："
  echo "    F8       → $ROOT_DIR/toggle_resident_f8.sh toggle"
  echo "    Shift+F8 → $VENV_DIR/bin/python $ROOT_DIR/recent_voice.py"
fi

cat <<EOF

安装完成。接下来：
  1) 编辑 $CONFIG_DIR/llm.env，填入 SENSEVOICE_BAILIAN_API_KEY 与 SENSEVOICE_POST_LLM_*
  2) 录声纹：./enroll_speaker_f8.sh            （正常说话 20 秒）
  3) 开机自启并启动：./install_autostart_service.sh
  4) 按 F8 开关收音；Shift+F8 查看最近语音
EOF
