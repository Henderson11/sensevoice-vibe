#!/usr/bin/env bash
# SenseVoice 语音输入系统 - 一键安装脚本
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/sensevoice-vibe"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"

echo "========================================="
echo " SenseVoice 语音输入系统 - 安装"
echo "========================================="

# --- 1. 检查系统依赖 ---
echo ""
echo "[1/6] 检查系统依赖..."
missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")
command -v arecord >/dev/null 2>&1 || missing+=("alsa-utils (arecord)")
command -v ibus >/dev/null 2>&1 || missing+=("ibus")

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "缺少系统依赖: ${missing[*]}"
  echo "请先安装: sudo apt install ${missing[*]}"
  exit 1
fi
echo "系统依赖检查通过 ✓"

# --- 2. 创建虚拟环境 + 安装 Python 依赖 ---
echo ""
echo "[2/6] 安装 Python 依赖..."
if [[ ! -d "$ROOT_DIR/.venv" ]]; then
  python3 -m venv "$ROOT_DIR/.venv"
fi

"$ROOT_DIR/.venv/bin/pip" install -q --upgrade pip
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "检测到 NVIDIA GPU，安装 CUDA 版 PyTorch..."
  "$ROOT_DIR/.venv/bin/pip" install -q torch torchaudio --index-url https://download.pytorch.org/whl/cu121
else
  echo "未检测到 GPU，安装 CPU 版 PyTorch..."
  "$ROOT_DIR/.venv/bin/pip" install -q torch torchaudio --index-url https://download.pytorch.org/whl/cpu
fi
"$ROOT_DIR/.venv/bin/pip" install -q funasr speechbrain webrtcvad-wheels soundfile openai
echo "Python 依赖安装完成 ✓"

# --- 3. 配置文件 ---
echo ""
echo "[3/6] 配置文件..."
mkdir -p "$CONFIG_DIR" "$STATE_DIR"

if [[ ! -f "$CONFIG_DIR/llm.env" ]]; then
  cp "$ROOT_DIR/config/llm.env.example" "$CONFIG_DIR/llm.env"
  # 自动填入模型路径
  sed -i "s|SENSEVOICE_MODEL=.*|SENSEVOICE_MODEL=$ROOT_DIR/models/sensevoice-small|" "$CONFIG_DIR/llm.env"
  sed -i "s|SENSEVOICE_SPK_MODEL=.*|SENSEVOICE_SPK_MODEL=$ROOT_DIR/models/spkrec-ecapa|" "$CONFIG_DIR/llm.env"
  sed -i "s|SENSEVOICE_SPK_CACHE_DIR=.*|SENSEVOICE_SPK_CACHE_DIR=$ROOT_DIR/models/spkrec-ecapa|" "$CONFIG_DIR/llm.env"
  sed -i "s|SENSEVOICE_PROJECT_ROOT=.*|SENSEVOICE_PROJECT_ROOT=$HOME|" "$CONFIG_DIR/llm.env"
  echo "配置文件已创建: $CONFIG_DIR/llm.env"
  echo "  ⚠ 请编辑填入 LLM API 密钥（如不使用 LLM 润色可跳过）"
else
  echo "配置文件已存在，跳过: $CONFIG_DIR/llm.env"
fi
chmod 600 "$CONFIG_DIR/llm.env"

# --- 4. 安装 IBus 引擎 ---
echo ""
echo "[4/6] 安装 IBus 引擎..."
bash "$ROOT_DIR/install_ibus_engine.sh"

# --- 5. 配置 F8 快捷键 ---
echo ""
echo "[5/6] 配置 F8 快捷键..."
if command -v gsettings >/dev/null 2>&1; then
  bash "$ROOT_DIR/configure_f8.sh"
else
  echo "非 GNOME 环境，跳过快捷键配置"
  echo "请手动绑定快捷键到: $ROOT_DIR/toggle_resident_f8.sh toggle"
fi

# --- 6. 完成 ---
echo ""
echo "========================================="
echo " 安装完成！"
echo "========================================="
echo ""
echo "使用方式："
echo "  按 F8 开启/关闭语音输入"
echo ""
echo "配置文件："
echo "  $CONFIG_DIR/llm.env"
echo ""
echo "可选操作："
echo "  采集声纹: bash $ROOT_DIR/enroll_speaker_f8.sh"
echo "  开机自启: bash $ROOT_DIR/install_autostart_service.sh"
echo ""
