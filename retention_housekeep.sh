#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
KEEP_RECENT="${SENSEVOICE_RETENTION_KEEP_RECENT:-20}"
KEEP_LOG_LINES="${SENSEVOICE_LOG_KEEP_LINES:-120}"
PROTECT_WAV_CSV="${SENSEVOICE_RETENTION_PROTECT_WAV_CSV:-}"

is_int() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

if ! is_int "$KEEP_RECENT" || [[ "$KEEP_RECENT" -lt 1 ]]; then
  KEEP_RECENT=20
fi
if ! is_int "$KEEP_LOG_LINES" || [[ "$KEEP_LOG_LINES" -lt 1 ]]; then
  KEEP_LOG_LINES="$KEEP_RECENT"
fi

is_protected_wav() {
  local p="$1"
  local rel="${p#$STATE_DIR/}"
  [[ ",$PROTECT_WAV_CSV," == *",$rel,"* ]]
}

delete_old_wavs_global() {
  mapfile -t wavs < <(
    find "$STATE_DIR" \
      -path "$STATE_DIR/archive" -prune -o \
      -type f -name '*.wav' -printf '%T@ %p\n' \
      | sort -nr \
      | awk '{ $1=""; sub(/^ /,""); print }'
  )

  local count="${#wavs[@]}"
  (( count > KEEP_RECENT )) || return 0

  local i f
  for (( i=KEEP_RECENT; i<count; i++ )); do
    f="${wavs[$i]}"
    [[ -f "$f" ]] || continue
    is_protected_wav "$f" && continue
    rm -f -- "$f"
  done
}

trim_log_lines() {
  local log="$1"
  [[ -f "$log" ]] || return 0

  local n
  n="$(wc -l < "$log" | tr -d ' ')"
  is_int "$n" || return 0
  (( n > KEEP_LOG_LINES )) || return 0

  local keep_tmp
  keep_tmp="$(mktemp)"

  tail -n "$KEEP_LOG_LINES" "$log" > "$keep_tmp"
  mv "$keep_tmp" "$log"
}

delete_old_wavs_global

while IFS= read -r lf; do
  trim_log_lines "$lf"
done < <(
  find "$STATE_DIR" \
    -path "$STATE_DIR/archive" -prune -o \
    -type f -name '*.log' -print
)
