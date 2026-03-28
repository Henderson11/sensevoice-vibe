#!/usr/bin/env bash
# IBus 语音引擎安装脚本
# 将引擎注册到 IBus，使其可通过 ibus engine sensevoice-voice 激活
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPONENT_XML="$ROOT_DIR/ibus-sensevoice/sensevoice-voice.xml"
TARGET_DIR="/usr/share/ibus/component"

if [[ ! -f "$COMPONENT_XML" ]]; then
  echo "Error: $COMPONENT_XML not found" >&2
  exit 1
fi

echo "Installing IBus component XML..."
sudo cp "$COMPONENT_XML" "$TARGET_DIR/sensevoice-voice.xml"
echo "Restarting IBus daemon..."
ibus restart 2>/dev/null || true
sleep 1

# 验证
if ibus list-engine 2>/dev/null | grep -q sensevoice-voice; then
  echo "IBus engine 'sensevoice-voice' installed successfully."
else
  echo "Warning: engine not found in ibus list-engine, may need to log out and back in."
fi
