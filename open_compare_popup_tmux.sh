#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCH_SCRIPT="$ROOT_DIR/watch_compare_tail.sh"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
COMPARE_FILE="${SENSEVOICE_COMPARE_LOG_FILE:-$STATE_DIR/post_compare.jsonl}"
POPUP_WIDTH="${SENSEVOICE_COMPARE_POPUP_WIDTH:-85%}"
POPUP_HEIGHT="${SENSEVOICE_COMPARE_POPUP_HEIGHT:-75%}"
POPUP_TITLE="${SENSEVOICE_COMPARE_POPUP_TITLE:-ASR Compare}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found" >&2
  exit 2
fi

if [[ ! -x "$WATCH_SCRIPT" ]]; then
  chmod +x "$WATCH_SCRIPT"
fi

tmux display-popup \
  -E \
  -w "$POPUP_WIDTH" \
  -h "$POPUP_HEIGHT" \
  -T "$POPUP_TITLE" \
  "bash -lc '$WATCH_SCRIPT $COMPARE_FILE'"

