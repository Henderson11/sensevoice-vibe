#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCH_SCRIPT="$ROOT_DIR/watch_compare_tail.sh"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
COMPARE_FILE="${SENSEVOICE_COMPARE_LOG_FILE:-$STATE_DIR/post_compare.jsonl}"
PANE_WIDTH="${SENSEVOICE_COMPARE_PANE_WIDTH:-52}"
PANE_TITLE="${SENSEVOICE_COMPARE_PANE_TITLE:-ASR-Compare}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found" >&2
  exit 2
fi

if [[ ! -x "$WATCH_SCRIPT" ]]; then
  chmod +x "$WATCH_SCRIPT"
fi

target="${1:-$(tmux display-message -p '#S:#I.#P')}"
if ! tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}' | grep -Fxq "$target"; then
  echo "tmux pane not found: $target" >&2
  exit 2
fi

target_window="$(tmux display-message -p -t "$target" '#{session_name}:#{window_index}')"
existing_pane="$(
  tmux list-panes -t "$target_window" -F '#{pane_id} #{pane_title}' \
    | awk -v want="$PANE_TITLE" '$2 == want {print $1; exit}'
)"

if [[ -n "$existing_pane" ]]; then
  tmux select-pane -t "$existing_pane"
  tmux respawn-pane -k -t "$existing_pane" "bash -lc '$WATCH_SCRIPT $COMPARE_FILE'"
  tmux select-pane -t "$target"
  echo "refreshed compare pane: $existing_pane"
  exit 0
fi

pane_id="$(
  tmux split-window -h -l "$PANE_WIDTH" -t "$target" -P -F '#{pane_id}' \
    "bash -lc '$WATCH_SCRIPT $COMPARE_FILE'"
)"
tmux select-pane -t "$pane_id" -T "$PANE_TITLE"
tmux select-pane -t "$target"
echo "opened compare pane: $pane_id (target=$target)"

