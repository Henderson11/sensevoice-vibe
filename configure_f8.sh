#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOGGLE_SCRIPT="$ROOT_DIR/toggle_resident_f8.sh"
LLM_ENV_FILE="$HOME/.config/sensevoice-vibe/llm.env"

if [[ ! -x "$TOGGLE_SCRIPT" ]]; then
  chmod +x "$TOGGLE_SCRIPT"
fi

# F8 快捷键命令：source llm.env 后调用 toggle 脚本
# 不再硬编码任何 SENSEVOICE_ 变量，全部从 llm.env 读取
TOGGLE_COMMAND="bash -c 'set -a; . $LLM_ENV_FILE; set +a; exec $TOGGLE_SCRIPT toggle'"

configure_gnome_hotkey() {
  command -v gsettings >/dev/null 2>&1 || return 0

  local schema="org.gnome.settings-daemon.plugins.media-keys"
  local base="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
  local existing raw target_path

  raw="$(gsettings get "$schema" custom-keybindings)"
  target_path="$(python3 - <<'PY' "$raw"
import ast, sys
arr = ast.literal_eval(sys.argv[1])
target = None
for p in arr:
    if p.endswith('/'):
        path = p
    else:
        path = p + '/'
    if path.endswith('sensevoice-toggle/'):
        target = path
        break
if target is None:
    used = set()
    for p in arr:
        p = p.strip('/')
        if p:
            used.add(p.split('/')[-1])
    i = 0
    while f'custom{i}' in used:
        i += 1
    target = f'/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/sensevoice-toggle/'
print(target)
PY
)"

  # Ensure the keybinding path is included in custom-keybindings list.
  existing="$(python3 - <<'PY' "$raw" "$target_path"
import ast, sys
arr = ast.literal_eval(sys.argv[1])
target = sys.argv[2]
if target not in arr:
    arr.append(target)
print(str(arr))
PY
)"
  gsettings set "$schema" custom-keybindings "$existing"

  local kb_schema="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$target_path"
  gsettings set "$kb_schema" name "SenseVoice Toggle Talk"
  gsettings set "$kb_schema" command "$TOGGLE_COMMAND"
  gsettings set "$kb_schema" binding "F8"

  # Avoid conflicting F8 bindings from other custom shortcuts.
  local p other_schema b
  for p in $(gsettings get "$schema" custom-keybindings | tr -d "[],'"); do
    [[ "$p" == "$target_path" ]] && continue
    other_schema="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$p"
    b="$(gsettings get "$other_schema" binding 2>/dev/null || true)"
    if [[ "$b" == "'F8'" ]]; then
      gsettings set "$other_schema" binding ""
    fi
  done
}

configure_gnome_hotkey

echo "F8 configuration complete."
echo "- GNOME global hotkey: F8 -> $TOGGLE_SCRIPT"
echo "- Config file: $LLM_ENV_FILE"
echo "- All settings read from llm.env (single source of truth)"
echo "State files: ${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
