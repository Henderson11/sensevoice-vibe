#!/usr/bin/env bash
# SenseVoice 语音输入系统 - 一键安装脚本（系统检查 + setup.sh + 模型 + IBus + F8）
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/sensevoice-vibe"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"

echo "========================================="
echo " SenseVoice 语音输入系统 - 一键安装"
echo "========================================="

# --- 1. 系统依赖检查 ---
echo ""
echo "[1/7] 检查系统依赖..."
missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")
command -v arecord >/dev/null 2>&1 || missing+=("alsa-utils")
command -v ibus >/dev/null 2>&1 || missing+=("ibus")
/usr/bin/python3 -c "from gi.repository import IBus" >/dev/null 2>&1 \
  || missing+=("python3-gi gir1.2-ibus-1.0")

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "  缺少系统依赖: ${missing[*]}"
  echo "  请先安装: sudo apt install -y ${missing[*]}"
  exit 1
fi
echo "  系统依赖检查通过 ✓"

# --- 2. Python venv + 依赖 + funasr patch ---
echo ""
echo "[2/7] 装 Python 依赖（调用 setup.sh）..."
bash "$ROOT_DIR/setup.sh"

# --- 3. 模型下载 ---
echo ""
echo "[3/7] 下载模型（~1.2GB，已存在则跳过）..."
bash "$ROOT_DIR/download_models.sh"

# --- 4. 配置文件 ---
echo ""
echo "[4/7] 配置文件..."
mkdir -p "$CONFIG_DIR" "$STATE_DIR"
if [[ ! -f "$CONFIG_DIR/llm.env" ]]; then
  cp "$ROOT_DIR/config/llm.env.example" "$CONFIG_DIR/llm.env"
  # 自动填入仓库内模型绝对路径
  sed -i "s|^SENSEVOICE_MODEL=.*|SENSEVOICE_MODEL=$ROOT_DIR/models/sensevoice-small|" "$CONFIG_DIR/llm.env"
  sed -i "s|^SENSEVOICE_SPK_MODEL=.*|SENSEVOICE_SPK_MODEL=$ROOT_DIR/models/eres2netv2|" "$CONFIG_DIR/llm.env"
  sed -i "s|^SENSEVOICE_SPK_CACHE_DIR=.*|SENSEVOICE_SPK_CACHE_DIR=$ROOT_DIR/models/eres2netv2|" "$CONFIG_DIR/llm.env"
  sed -i "s|^SENSEVOICE_PROJECT_ROOT=.*|SENSEVOICE_PROJECT_ROOT=$HOME|" "$CONFIG_DIR/llm.env"
  echo "  配置文件已创建: $CONFIG_DIR/llm.env"
  echo "  ⚠ 请编辑填入 LLM API key；如要纯本地模式可设 SENSEVOICE_POST_LLM_ENABLE=0"
else
  echo "  配置文件已存在，跳过: $CONFIG_DIR/llm.env"
fi
chmod 600 "$CONFIG_DIR/llm.env"

# --- 5. IBus 引擎 ---
echo ""
echo "[5/7] 装 IBus 引擎..."
bash "$ROOT_DIR/install_ibus_engine.sh"

# --- 6. F8 快捷键 ---
echo ""
echo "[6/7] 配置 F8 快捷键..."
if command -v gsettings >/dev/null 2>&1; then
  bash "$ROOT_DIR/configure_f8.sh"
else
  echo "  非 GNOME 环境，跳过自动配置。"
  echo "  请手动绑定快捷键调用: $ROOT_DIR/toggle_resident_f8.sh toggle"
fi

# --- 7. 完成 ---
echo ""
echo "========================================="
echo " ✓ 安装完成"
echo "========================================="
echo ""
echo "下一步："
echo "  1) 编辑 $CONFIG_DIR/llm.env 填入 LLM API key（可选）"
echo "  2) 录声纹: ./enroll_speaker_f8.sh"
echo "  3) 启动:   ./toggle_resident_f8.sh on"
echo "  4) 按 F8 开始语音输入"
echo ""
echo "可选: ./install_autostart_service.sh   # 开机自启"
