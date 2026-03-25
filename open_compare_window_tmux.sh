#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCH_SCRIPT="$ROOT_DIR/watch_compare_tail.sh"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
COMPARE_FILE="${SENSEVOICE_COMPARE_LOG_FILE:-$STATE_DIR/post_compare.jsonl}"
WINDOW_NAME="${SENSEVOICE_COMPARE_WINDOW_NAME:-ASR-Compare}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found" >&2
  exit 2
fi

if [[ ! -x "$WATCH_SCRIPT" ]]; then
  chmod +x "$WATCH_SCRIPT"
fi

session="${1:-$(tmux display-message -p '#S')}"
if ! tmux has-session -t "$session" 2>/dev/null; then
  echo "tmux session not found: $session" >&2
  exit 2
fi

# Remove legacy compare pane if it still exists in any window.
legacy_pane="$(
  tmux list-panes -t "$session" -a -F '#{pane_id} #{pane_title}' \
    | awk '$2 == "ASR-Compare" {print $1; exit}'
)"
if [[ -n "$legacy_pane" ]]; then
  tmux kill-pane -t "$legacy_pane" || true
fi

window_target="${session}:${WINDOW_NAME}"
if tmux list-windows -t "$session" -F '#W' | grep -Fxq "$WINDOW_NAME"; then
  tmux respawn-pane -k -t "$window_target" "bash -lc '$WATCH_SCRIPT $COMPARE_FILE'"
  tmux select-window -t "$window_target"
  echo "refreshed compare window: $window_target"
  exit 0
fi

tmux new-window -t "$session" -n "$WINDOW_NAME" "bash -lc '$WATCH_SCRIPT $COMPARE_FILE'"
tmux select-window -t "$window_target"
echo "opened compare window: $window_target"

