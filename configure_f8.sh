#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOGGLE_SCRIPT="$ROOT_DIR/toggle_resident_f8.sh"
LOCAL_MODEL_PATH="$HOME/.cache/modelscope/hub/models/iic/SenseVoiceSmall"
TOGGLE_SCRIPT_FOCUS="env SENSEVOICE_SESSION_NOTIFY=0 SENSEVOICE_TOGGLE_DEBOUNCE_MS=450 SENSEVOICE_LANGUAGE=auto SENSEVOICE_MODEL=$LOCAL_MODEL_PATH SENSEVOICE_FILTER_FILLERS=1 SENSEVOICE_SPK_ENABLE=1 SENSEVOICE_SPK_ENROLL_WAV=$HOME/.local/state/sensevoice-vibe/speaker_enroll.d SENSEVOICE_SPK_THRESHOLD=0.45 SENSEVOICE_SPK_MIN_MS=900 SENSEVOICE_SPK_MODEL=speechbrain/spkrec-ecapa-voxceleb SENSEVOICE_SPK_CACHE_DIR=$HOME/.cache/sensevoice-vibe/spkrec SENSEVOICE_SPK_AGG=topk_mean SENSEVOICE_SPK_TOPK=1 SENSEVOICE_SPK_AUTO_ENROLL=0 SENSEVOICE_SPK_AUTO_ENROLL_DIR=$HOME/.local/state/sensevoice-vibe/speaker_enroll.d SENSEVOICE_SPK_AUTO_ENROLL_MIN_SCORE=0.70 SENSEVOICE_SPK_AUTO_ENROLL_MIN_MS=2200 SENSEVOICE_SPK_AUTO_ENROLL_COOLDOWN_SEC=90 SENSEVOICE_SPK_AUTO_ENROLL_MAX_TEMPLATES=20 SENSEVOICE_SPK_ADAPTIVE=1 SENSEVOICE_SPK_ADAPTIVE_WINDOW=80 SENSEVOICE_SPK_ADAPTIVE_MIN_SAMPLES=10 SENSEVOICE_SPK_ADAPTIVE_FLOOR=0.52 SENSEVOICE_SPK_ADAPTIVE_MARGIN=0.04 SENSEVOICE_SPK_PRUNE_OUTLIERS=1 SENSEVOICE_SPK_PRUNE_KEEP=10 SENSEVOICE_PARTIAL_STRATEGY=stable2 SENSEVOICE_EMIT_PARTIAL=0 SENSEVOICE_AUTO_ENTER=0 SENSEVOICE_PREFER_WTYPE=0 SENSEVOICE_PASTE_KEY=ctrl_v SENSEVOICE_YDOTOOL_KEY_DELAY_MS=35 SENSEVOICE_CLIPBOARD_SETTLE_SEC=0.08 SENSEVOICE_PASTE_PRE_DELAY_SEC=0.08 SENSEVOICE_CLIPBOARD_RESTORE=1 SENSEVOICE_CLIPBOARD_RESTORE_DELAY_SEC=0.12 SENSEVOICE_CLIPBOARD_VERIFY=1 SENSEVOICE_INJECT_ACK_TIMEOUT_SEC=1.2 SENSEVOICE_DEBUG_INJECT=1 SENSEVOICE_CLEAR_BEFORE_REPLACE=0 SENSEVOICE_STREAM_INDICATOR=none SENSEVOICE_STREAM_VAD_AGGRESSIVENESS=3 SENSEVOICE_STREAM_START_MS=240 SENSEVOICE_STREAM_FRAME_MS=20 SENSEVOICE_STREAM_PRE_ROLL_MS=700 SENSEVOICE_STREAM_ENDPOINT_MS=1200 SENSEVOICE_STREAM_MAX_SEGMENT_MS=30000 SENSEVOICE_STREAM_MIN_SEGMENT_MS=850 SENSEVOICE_STREAM_PARTIAL_INTERVAL_MS=280 SENSEVOICE_STREAM_MIN_PARTIAL_MS=1300 SENSEVOICE_RETENTION_KEEP_RECENT=20 SENSEVOICE_LOG_KEEP_LINES=120 SENSEVOICE_PROJECT_LEXICON_ENABLE=1 SENSEVOICE_PROJECT_ROOT=$HOME/mosim_workspace SENSEVOICE_PROJECT_LEXICON_MAX_TERMS=2500 SENSEVOICE_PROJECT_LEXICON_HINT_LIMIT=16 SENSEVOICE_PROJECT_LEXICON_MIN_TERM_LEN=3 SENSEVOICE_PROJECT_LEXICON_EXTRA_FILE=$ROOT_DIR/hotwords_coding_zh.txt SENSEVOICE_CONF_ROUTE_ENABLE=1 SENSEVOICE_CONF_ROUTE_HIGH=0.42 SENSEVOICE_CONF_ROUTE_LOW=0.30 SENSEVOICE_POST_LLM_MODE=polish_coding_aggressive SENSEVOICE_LEARN_ENABLE=1 SENSEVOICE_LEARN_STORE=$HOME/.local/state/sensevoice-vibe/correction_memory.json SENSEVOICE_LEARN_MIN_HITS=2 SENSEVOICE_LEARN_MAX_RULES=320 SENSEVOICE_COMPARE_LOG_ENABLE=1 SENSEVOICE_COMPARE_LOG_FILE=$HOME/.local/state/sensevoice-vibe/post_compare.jsonl SENSEVOICE_COMPARE_LOG_KEEP_LINES=300 SENSEVOICE_INJECT_MODE=ibus SENSEVOICE_POST_LLM_ENABLE=1 SENSEVOICE_POST_LLM_MODEL=DeepSeek-V3.2 SENSEVOICE_POST_LLM_FALLBACK_MODEL=DeepSeek-V3.1-Terminus SENSEVOICE_POST_LLM_TIMEOUT_MS=1800 SENSEVOICE_POST_LLM_STRICT=0 SENSEVOICE_POST_LLM_MAX_TOKENS=72 SENSEVOICE_POST_LLM_TEMPERATURE=0 SENSEVOICE_POST_LLM_BASE_URL=http://<INTERNAL_LLM_HOST>:31091/<YOUR_LLM_PROXY_PATH>/v1 SENSEVOICE_POST_LLM_API_KEY=sk-QzYIwr6aFKo5wELX0aE3Ff23FfD649A095242e30947c0aA5 $TOGGLE_SCRIPT toggle"

if [[ ! -x "$TOGGLE_SCRIPT" ]]; then
  chmod +x "$TOGGLE_SCRIPT"
fi

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
  gsettings set "$kb_schema" command "$TOGGLE_SCRIPT_FOCUS"
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
echo "- mode: resident model + F8 toggles listening on/off (no model reload)"
echo "State files: ${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
