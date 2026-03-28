#!/usr/bin/env bash
# IBus 语音引擎安装脚本
# 从模板生成 XML（自动填入当前安装路径），注册到 IBus
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$ROOT_DIR/ibus-sensevoice"
TEMPLATE="$ENGINE_DIR/sensevoice-voice.xml.in"
GENERATED="$ENGINE_DIR/sensevoice-voice.xml"
TARGET_DIR="/usr/share/ibus/component"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "Error: $TEMPLATE not found" >&2
  exit 1
fi

# 从模板生成 XML，替换 @ENGINE_PATH@ 为实际路径
sed "s|@ENGINE_PATH@|$ENGINE_DIR|g" "$TEMPLATE" > "$GENERATED"
echo "Generated: $GENERATED"

echo "Installing IBus component XML (requires sudo)..."
sudo cp "$GENERATED" "$TARGET_DIR/sensevoice-voice.xml"
echo "Restarting IBus daemon..."
ibus restart 2>/dev/null || true
sleep 1

if ibus list-engine 2>/dev/null | grep -q sensevoice-voice; then
  echo "IBus engine 'sensevoice-voice' installed successfully."
else
  echo "Warning: engine not found, may need to log out and back in."
fi
