#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  send_to_focus_ydotool.sh

Input format (stdin):
  CTRL<TAB>RESET     Reset local text state for a new listening session
  SEG<TAB>id         Segment boundary marker (no typing)
  PARTIAL<TAB>text   Update current input line (Ctrl+U then paste text)
  FINAL<TAB>text     Update line then press Enter
  text               Paste text then press Enter
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

USE_WTYPE=0
PASTE_KEY="${SENSEVOICE_PASTE_KEY:-ctrl_v}" # ctrl_v | ctrl_shift_v | shift_insert
YD_KEY_DELAY_MS="${SENSEVOICE_YDOTOOL_KEY_DELAY_MS:-20}"
CLIPBOARD_SETTLE_SEC="${SENSEVOICE_CLIPBOARD_SETTLE_SEC:-0.04}"
PASTE_PRE_DELAY_SEC="${SENSEVOICE_PASTE_PRE_DELAY_SEC:-0.08}"
CLIPBOARD_RESTORE="${SENSEVOICE_CLIPBOARD_RESTORE:-1}"
CLIPBOARD_RESTORE_DELAY_SEC="${SENSEVOICE_CLIPBOARD_RESTORE_DELAY_SEC:-0.12}"
CLIPBOARD_VERIFY="${SENSEVOICE_CLIPBOARD_VERIFY:-1}" # 0: non-blocking set only, 1: verify with timeout
CLEAR_BEFORE_REPLACE="${SENSEVOICE_CLEAR_BEFORE_REPLACE:-0}" # 1: send Ctrl+U before replace (legacy behavior)
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
DEBUG_LOG="$STATE_DIR/focus_inject.log"
CLIPBOARD_SNAPSHOT_DIR="$STATE_DIR/clipboard_snapshot"
CLIPBOARD_SNAPSHOT_DATA="$CLIPBOARD_SNAPSHOT_DIR/data.bin"
CURRENT_ACK_LABEL="boot"
CLIPBOARD_SAVED=0
CLIPBOARD_SAVE_KIND=""
CLIPBOARD_SAVE_TYPE=""
LAST_INJECTED_CLIPBOARD_TEXT=""

debug_log() {
  [[ "${SENSEVOICE_DEBUG_INJECT:-0}" == "1" ]] || return 0
  mkdir -p "$STATE_DIR"
  printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$DEBUG_LOG"
}

emit_ok() {
  printf 'OK\t%s\n' "$1"
}

emit_err() {
  printf 'ERR\t%s\n' "$1"
}

trap 'rc=$?; debug_log "ack=err label=${CURRENT_ACK_LABEL} rc=${rc}"; emit_err "${CURRENT_ACK_LABEL}:rc=${rc}"; exit "${rc}"' ERR
if [[ "${SENSEVOICE_PREFER_WTYPE:-1}" == "1" ]] && [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]] && command -v wtype >/dev/null 2>&1; then
  USE_WTYPE=1
fi

ensure_ydotool_backend() {
  command -v ydotool >/dev/null 2>&1 || { echo "ydotool not found" >&2; exit 2; }
  command -v ydotoold >/dev/null 2>&1 || { echo "ydotoold not found" >&2; exit 2; }
  ensure_daemon
}

ensure_daemon() {
  # Never send synthetic key presses here: they can alter IME state
  # (e.g. Rime Shift-based ASCII toggle) even before actual text injection.
  if pgrep -x ydotoold >/dev/null 2>&1; then
    return 0
  fi

  # Avoid inheriting caller's lock fd (e.g. toggle_talk_f8.sh uses fd 9).
  nohup bash -c 'exec 9>&-; ydotoold' >/dev/null 2>&1 &
  for _ in $(seq 1 10); do
    if pgrep -x ydotoold >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done

  echo "ydotoold is not available (likely /dev/uinput permission issue)" >&2
  exit 2
}

yd_key() {
  local seq="$1"
  debug_log "yd_key seq=${seq}"
  ydotool key --key-delay "$YD_KEY_DELAY_MS" "$seq" >/dev/null 2>&1
}

pick_clipboard_type() {
  local types="$1"
  local preferred=(
    "image/png"
    "image/jpeg"
    "image/webp"
    "image/gif"
    "text/html"
    "text/uri-list"
    "text/plain;charset=utf-8"
    "text/plain"
  )
  local want line
  for want in "${preferred[@]}"; do
    while IFS= read -r line; do
      [[ "$line" == "$want" ]] && { printf '%s' "$line"; return 0; }
    done <<<"$types"
  done
  while IFS= read -r line; do
    [[ -n "$line" ]] && { printf '%s' "$line"; return 0; }
  done <<<"$types"
  return 1
}

save_clipboard_state() {
  CLIPBOARD_SAVED=0
  CLIPBOARD_SAVE_KIND=""
  CLIPBOARD_SAVE_TYPE=""
  [[ "$CLIPBOARD_RESTORE" == "1" ]] || return 0
  [[ "$PASTE_KEY" != "shift_insert" ]] || return 0
  command -v wl-copy >/dev/null 2>&1 || return 0
  command -v wl-paste >/dev/null 2>&1 || return 0

  local types selected
  types="$(timeout 0.2s wl-paste --list-types 2>/dev/null || true)"
  mkdir -p "$CLIPBOARD_SNAPSHOT_DIR"
  if [[ -z "$types" ]]; then
    CLIPBOARD_SAVED=1
    CLIPBOARD_SAVE_KIND="empty"
    debug_log "clipboard_snapshot kind=empty"
    return 0
  fi

  selected="$(pick_clipboard_type "$types" || true)"
  [[ -n "$selected" ]] || return 0
  if timeout 0.4s wl-paste --type "$selected" >"$CLIPBOARD_SNAPSHOT_DATA" 2>/dev/null; then
    CLIPBOARD_SAVED=1
    CLIPBOARD_SAVE_KIND="data"
    CLIPBOARD_SAVE_TYPE="$selected"
    debug_log "clipboard_snapshot kind=data type=${selected}"
  fi
}

restore_clipboard_state() {
  [[ "$CLIPBOARD_RESTORE" == "1" ]] || return 0
  [[ "$PASTE_KEY" != "shift_insert" ]] || return 0
  [[ "$CLIPBOARD_SAVED" == "1" ]] || return 0
  command -v wl-copy >/dev/null 2>&1 || return 0
  command -v wl-paste >/dev/null 2>&1 || return 0

  local current_text=""
  current_text="$(timeout 0.2s wl-paste --no-newline 2>/dev/null || true)"
  if [[ -n "$LAST_INJECTED_CLIPBOARD_TEXT" && "$current_text" != "$LAST_INJECTED_CLIPBOARD_TEXT" ]]; then
    debug_log "clipboard_restore skip=current_changed"
    return 0
  fi

  sleep "$CLIPBOARD_RESTORE_DELAY_SEC"
  if [[ "$CLIPBOARD_SAVE_KIND" == "empty" ]]; then
    wl-copy --clear >/dev/null 2>&1 || true
    debug_log "clipboard_restore kind=empty"
    return 0
  fi

  if [[ "$CLIPBOARD_SAVE_KIND" == "data" && -n "$CLIPBOARD_SAVE_TYPE" && -f "$CLIPBOARD_SNAPSHOT_DATA" ]]; then
    nohup bash -c 'cat "$1" | wl-copy --type "$2"' _ "$CLIPBOARD_SNAPSHOT_DATA" "$CLIPBOARD_SAVE_TYPE" >/dev/null 2>&1 &
    debug_log "clipboard_restore kind=data type=${CLIPBOARD_SAVE_TYPE}"
  fi
}

set_clipboard_text() {
  local text="$1"
  local got_clip got_primary
  local wl_mode="clipboard"
  local use_timeout=0
  if [[ "$PASTE_KEY" == "shift_insert" ]]; then
    wl_mode="primary"
  fi
  if command -v timeout >/dev/null 2>&1; then
    use_timeout=1
  fi

  wl_paste_safe() {
    local mode="$1"
    if [[ "$mode" == "primary" ]]; then
      if [[ "$use_timeout" -eq 1 ]]; then
        timeout 0.15s wl-paste --primary --no-newline 2>/dev/null || true
      else
        wl-paste --primary --no-newline 2>/dev/null || true
      fi
    else
      if [[ "$use_timeout" -eq 1 ]]; then
        timeout 0.15s wl-paste --no-newline 2>/dev/null || true
      else
        wl-paste --no-newline 2>/dev/null || true
      fi
    fi
  }

  if command -v wl-copy >/dev/null 2>&1; then
    local i
    for i in $(seq 1 5); do
      if [[ "$wl_mode" == "primary" ]]; then
        printf '%s' "$text" | wl-copy --primary --type text/plain;charset=utf-8
      else
        printf '%s' "$text" | wl-copy --type text/plain;charset=utf-8
      fi
      sleep "$CLIPBOARD_SETTLE_SEC"
      if [[ "$CLIPBOARD_VERIFY" == "1" ]] && command -v wl-paste >/dev/null 2>&1; then
        if [[ "$wl_mode" == "primary" ]]; then
          got_primary="$(wl_paste_safe primary)"
          if [[ "$got_primary" == "$text" ]]; then
            debug_log "clipboard=wl-copy(primary) verified try=${i} bytes=${#text}"
            return 0
          fi
          debug_log "clipboard=wl-copy(primary) mismatch try=${i} primary=${got_primary}"
        else
          got_clip="$(wl_paste_safe clipboard)"
          if [[ "$got_clip" == "$text" ]]; then
            debug_log "clipboard=wl-copy(clipboard) verified try=${i} bytes=${#text}"
            return 0
          fi
          debug_log "clipboard=wl-copy(clipboard) mismatch try=${i} clip=${got_clip}"
        fi
      else
        debug_log "clipboard=wl-copy set bytes=${#text} verify=${CLIPBOARD_VERIFY}"
        return 0
      fi
    done
    # Verification failed/timed out; keep non-blocking behavior by accepting last set.
    debug_log "clipboard=wl-copy fallback_accept bytes=${#text}"
    return 0
  fi
  if command -v xclip >/dev/null 2>&1; then
    if [[ "$wl_mode" == "primary" ]]; then
      debug_log "clipboard=xclip(primary) bytes=${#text}"
      printf '%s' "$text" | xclip -selection primary
    else
      debug_log "clipboard=xclip(clipboard) bytes=${#text}"
      printf '%s' "$text" | xclip -selection clipboard
    fi
    return 0
  fi
  if command -v xsel >/dev/null 2>&1; then
    if [[ "$wl_mode" == "primary" ]]; then
      debug_log "clipboard=xsel(primary) bytes=${#text}"
      printf '%s' "$text" | xsel --primary --input
    else
      debug_log "clipboard=xsel(clipboard) bytes=${#text}"
      printf '%s' "$text" | xsel --clipboard --input
    fi
    return 0
  fi
  debug_log "clipboard=none"
  echo "no clipboard tool found (need wl-copy/xclip/xsel)" >&2
  return 2
}

send_ctrl_u() {
  if [[ "$USE_WTYPE" -eq 1 ]]; then
    if wtype -M ctrl -k u -m ctrl >/dev/null 2>&1; then
      return 0
    fi
    USE_WTYPE=0
  fi
  ensure_ydotool_backend
  yd_key "ctrl+u"
}

send_type() {
  local text="$1"
  [[ -n "$text" ]] || return 0
  if [[ "$USE_WTYPE" -eq 1 ]]; then
    if wtype -- "$text" >/dev/null 2>&1; then
      return 0
    fi
    USE_WTYPE=0
  fi
  ensure_ydotool_backend
  # Always paste via clipboard under ydotool backend to avoid layout/IME
  # keymap issues (e.g. corrupted text like repeated digits).
  save_clipboard_state
  set_clipboard_text "$text"
  LAST_INJECTED_CLIPBOARD_TEXT="$text"
  sleep "$PASTE_PRE_DELAY_SEC"
  if [[ "$PASTE_KEY" == "shift_insert" ]]; then
    yd_key "shift+insert"
  elif [[ "$PASTE_KEY" == "ctrl_v" ]]; then
    yd_key "ctrl+v"
  else
    yd_key "ctrl+shift+v"
  fi
  restore_clipboard_state
}

send_enter() {
  if [[ "$USE_WTYPE" -eq 1 ]]; then
    if wtype -k Return >/dev/null 2>&1; then
      return 0
    fi
    USE_WTYPE=0
  fi
  ensure_ydotool_backend
  yd_key "enter"
}

if [[ "$USE_WTYPE" -eq 0 ]]; then
  ensure_ydotool_backend
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

replace_line_with_text() {
  local text="$1"
  if [[ "$CLEAR_BEFORE_REPLACE" == "1" ]]; then
    send_ctrl_u
  fi
  send_type "$text"
}

apply_partial_text() {
  local text="$1"
  [[ -n "$text" ]] || return 0
  if [[ "$text" == "$LAST_TEXT" ]]; then
    return 0
  fi
  if [[ -n "$LAST_TEXT" && "$text" == "$LAST_TEXT"* ]]; then
    local delta="${text#"$LAST_TEXT"}"
    if [[ -n "$delta" ]]; then
      debug_log "apply mode=append delta=${delta}"
      send_type "$delta"
    fi
    LAST_TEXT="$text"
    return 0
  fi
  debug_log "apply mode=replace text=${text}"
  replace_line_with_text "$text"
  LAST_TEXT="$text"
}

while IFS= read -r line; do
  [[ -z "$line" ]] && continue

  if [[ "$line" == CTRL$'\t'RESET ]]; then
    CURRENT_ACK_LABEL="CTRL"
    reset_text_state
    debug_log "recv mode=CTRL action=RESET"
    emit_ok "$CURRENT_ACK_LABEL"
    continue
  fi

  if [[ "$line" == SEG$'\t'* ]]; then
    CURRENT_ACK_LABEL="SEG"
    new_seg="${line#SEG$'\t'}"
    if [[ -n "$CURRENT_SEG" && "$new_seg" != "$CURRENT_SEG" ]]; then
      commit_segment_text
    fi
    CURRENT_SEG="$new_seg"
    LAST_TEXT="$COMMITTED_TEXT"
    debug_log "recv mode=SEG id=${CURRENT_SEG}"
    emit_ok "$CURRENT_ACK_LABEL"
    continue
  fi

  if [[ "$line" == PARTIAL$'\t'* ]]; then
    CURRENT_ACK_LABEL="PARTIAL"
    text="${line#PARTIAL$'\t'}"
    debug_log "recv mode=PARTIAL text=${text}"
    SEGMENT_TEXT="$text"
    full_text="$(join_text "$COMMITTED_TEXT" "$SEGMENT_TEXT")"
    apply_partial_text "$full_text"
    emit_ok "$CURRENT_ACK_LABEL"
    continue
  fi

  if [[ "$line" == FINAL$'\t'* ]]; then
    CURRENT_ACK_LABEL="FINAL"
    text="${line#FINAL$'\t'}"
    debug_log "recv mode=FINAL text=${text}"
    SEGMENT_TEXT="$text"
    commit_segment_text
    apply_partial_text "$COMMITTED_TEXT"
    send_enter
    reset_text_state
    emit_ok "$CURRENT_ACK_LABEL"
    continue
  fi

  CURRENT_ACK_LABEL="RAW"
  debug_log "recv mode=RAW text=${line}"
  send_type "$line"
  send_enter
  reset_text_state
  emit_ok "$CURRENT_ACK_LABEL"
done
