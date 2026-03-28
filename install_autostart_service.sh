#!/usr/bin/env bash
# 安装 systemd user service，开机自启 SenseVoice 语音输入
# 所有配置从 ~/.config/sensevoice-vibe/llm.env 读取
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/sensevoice-vibe.service"
TEMPLATE="$ROOT_DIR/config/sensevoice-vibe.service.example"
LLM_ENV="$HOME/.config/sensevoice-vibe/llm.env"

mkdir -p "$UNIT_DIR"
mkdir -p "$(dirname "$LLM_ENV")"

# 如果配置文件不存在，从模板复制
if [[ ! -f "$LLM_ENV" ]]; then
  cp "$ROOT_DIR/config/llm.env.example" "$LLM_ENV"
  echo "Created config: $LLM_ENV"
  echo "Please edit it to fill in API keys before starting the service."
fi

# 从模板生成 service 文件，替换安装路径
sed "s|__INSTALL_DIR__|$ROOT_DIR|g" "$TEMPLATE" > "$UNIT_FILE"

systemctl --user daemon-reload
systemctl --user enable --now sensevoice-vibe.service

echo "Installed: $UNIT_FILE"
echo "Config: $LLM_ENV"
systemctl --user --no-pager --full status sensevoice-vibe.service | sed -n '1,20p'
