#!/usr/bin/env bash
# systemd ExecStart wrapper —— 让 bash 处理 llm.env 中的 $HOME 等 shell 变量展开。
# systemd 的 EnvironmentFile= 不做 shell 展开（拿到字面 "$HOME/..."），
# 路径错误会让声纹模块、模型路径等静默失效。bash source 是唯一可靠的处理方式。

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE="${HOME}/.config/sensevoice-vibe/llm.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi

exec "$HERE/.venv/bin/python" "$HERE/stream_vad_realtime.py" \
  --resident \
  --language "${SENSEVOICE_LANGUAGE:-auto}" \
  --indicator "${SENSEVOICE_STREAM_INDICATOR:-none}"
