#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  send_to_cli_tmux.sh [target-pane]

Input format (stdin):
  CTRL<TAB>RESET     Reset local text state for a new listening session
  SEG<TAB>id         Segment boundary marker (no typing)
  PARTIAL<TAB>text   Update current input line (no Enter)
  FINAL<TAB>text     Update current input line and press Enter
  text               Send as one full line and press Enter

If target-pane is omitted, current pane is used.
Target format example: mysession:0.1
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found" >&2
  exit 2
fi

target="${1:-$(tmux display-message -p '#S:#I.#P')}"

if ! tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}' | grep -Fxq "$target"; then
  echo "tmux pane not found: $target" >&2
  exit 2
fi

LAST_TEXT=""
CURRENT_SEG=""
COMMITTED_TEXT=""
SEGMENT_TEXT=""

reset_text_state() {
  LAST_TEXT=""
  CURRENT_SEG=""
  COMMITTED_TEXT=""
  SEGMENT_TEXT=""
}

join_text() {
  local left="$1"
  local right="$2"
  if [[ -z "$left" ]]; then
    printf '%s' "$right"
    return 0
  fi
  if [[ -z "$right" ]]; then
    printf '%s' "$left"
    return 0
  fi
  local last="${left: -1}"
  local first="${right:0:1}"
  local sep=""
  if [[ "$left" =~ [[:space:]]$ ]]; then
    sep=""
  elif [[ "$last" =~ [A-Za-z0-9] && "$first" =~ [A-Za-z0-9] ]]; then
    sep=" "
  fi
  printf '%s%s%s' "$left" "$sep" "$right"
}

commit_segment_text() {
  [[ -n "$SEGMENT_TEXT" ]] || return 0
  COMMITTED_TEXT="$(join_text "$COMMITTED_TEXT" "$SEGMENT_TEXT")"
  SEGMENT_TEXT=""
}

send_full_line() {
  local text="$1"
  if [[ "$text" == "$LAST_TEXT" ]]; then
    return 0
  fi
  tmux send-keys -t "$target" C-u
  [[ -n "$text" ]] && tmux send-keys -t "$target" -- "$text"
  LAST_TEXT="$text"
}

while IFS= read -r line; do
  [[ -z "$line" ]] && continue

  if [[ "$line" == CTRL$'\t'RESET ]]; then
    reset_text_state
    continue
  fi

  if [[ "$line" == SEG$'\t'* ]]; then
    new_seg="${line#SEG$'\t'}"
    if [[ -n "$CURRENT_SEG" && "$new_seg" != "$CURRENT_SEG" ]]; then
      commit_segment_text
    fi
    CURRENT_SEG="$new_seg"
    LAST_TEXT="$COMMITTED_TEXT"
    continue
  fi

  if [[ "$line" == PARTIAL$'\t'* ]]; then
    text="${line#PARTIAL$'\t'}"
    SEGMENT_TEXT="$text"
    full_text="$(join_text "$COMMITTED_TEXT" "$SEGMENT_TEXT")"
    send_full_line "$full_text"
    continue
  fi

  if [[ "$line" == FINAL$'\t'* ]]; then
    text="${line#FINAL$'\t'}"
    SEGMENT_TEXT="$text"
    commit_segment_text
    send_full_line "$COMMITTED_TEXT"
    tmux send-keys -t "$target" C-m
    reset_text_state
    continue
  fi

  tmux send-keys -t "$target" -- "$line"
  tmux send-keys -t "$target" C-m
  reset_text_state
done
