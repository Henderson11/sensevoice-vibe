#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
mkdir -p "$STATE_DIR"

PID_FILE="$STATE_DIR/recording.pid"
WAV_FILE="$STATE_DIR/recording.wav"
TARGET_FILE="$STATE_DIR/target_pane"
LOG_FILE="$STATE_DIR/history.log"
LOCK_FILE="$STATE_DIR/toggle.lock"
LAST_TS_FILE="$STATE_DIR/last_toggle_ms"
START_TS_FILE="$STATE_DIR/recording.started_ms"
OUTPUT_MODE="${SENSEVOICE_OUTPUT_MODE:-auto}" # auto | tmux | focus
LANGUAGE="${SENSEVOICE_LANGUAGE:-auto}"       # auto | zn | en | yue | ja | ko
DEBOUNCE_MS="${SENSEVOICE_DEBOUNCE_MS:-150}"
MIN_RECORD_MS="${SENSEVOICE_MIN_RECORD_MS:-700}"
ENABLE_NOTIFY="${SENSEVOICE_NOTIFY:-0}"

# Prevent concurrent starts/stops when the same hotkey is bound in multiple layers
# (e.g. GNOME global + tmux) or when key-repeat emits near-duplicate triggers.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

now_ms="$(date +%s%3N 2>/dev/null || true)"
if [[ -n "$now_ms" ]]; then
  last_ms="0"
  if [[ -f "$LAST_TS_FILE" ]]; then
    last_ms="$(cat "$LAST_TS_FILE" 2>/dev/null || echo 0)"
  fi
  if [[ "$last_ms" =~ ^[0-9]+$ ]]; then
    delta_ms=$((now_ms - last_ms))
    if (( delta_ms >= 0 && delta_ms < DEBOUNCE_MS )); then
      exit 0
    fi
  fi
  echo "$now_ms" >"$LAST_TS_FILE"
fi

notify() {
  if [[ "$ENABLE_NOTIFY" == "1" ]] && command -v notify-send >/dev/null 2>&1; then
    notify-send "SenseVoice Vibe" "$1"
  fi
}

die() {
  notify "$1"
  echo "$1" >&2
  exit 2
}

resolve_target_pane() {
  local explicit="${1:-}"
  if [[ -n "$explicit" ]]; then
    echo "$explicit"
    return 0
  fi

  if [[ "$OUTPUT_MODE" == "focus" ]]; then
    echo ""
    return 0
  fi

  if [[ -n "${TMUX:-}" ]] && command -v tmux >/dev/null 2>&1; then
    tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}'
    return 0
  fi

  if [[ -f "$TARGET_FILE" ]]; then
    cat "$TARGET_FILE"
    return 0
  fi

  echo ""
}

pane_exists() {
  local pane="$1"
  tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null | grep -Fxq "$pane"
}

start_recording() {
  local target="$1"
  [[ -x "$VENV_DIR/bin/python" ]] || die "Virtual env not found: $VENV_DIR"
  command -v arecord >/dev/null 2>&1 || die "arecord not found; install ALSA utils first"

  : >"$WAV_FILE"
  if [[ -n "$target" ]]; then
    echo "$target" >"$TARGET_FILE"
  fi

  # Start recorder as a detached background job:
  # 1) close lock fd (9) only for arecord
  # 2) detach stdio so parent shell can exit immediately
  # 3) disown to avoid bash waiting on exit
  arecord -q -f S16_LE -r 16000 -c 1 "$WAV_FILE" 9>&- </dev/null >/dev/null 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$PID_FILE"
  date +%s%3N >"$START_TS_FILE" 2>/dev/null || true
  sleep 0.2
  if ! kill -0 "$pid" 2>/dev/null; then
    : >"$PID_FILE"
    die "Recording failed: no microphone or recording device unavailable"
  fi

  notify "F8 已开始录音（再次按 F8 结束并填入文本）"
  echo "START $(date '+%F %T') target=${target:-none}" >>"$LOG_FILE"
}

stop_recording_and_send() {
  [[ -f "$PID_FILE" ]] || die "No active recording"
  local pid
  pid="$(cat "$PID_FILE")"
  [[ -n "$pid" ]] || die "No active recording"
  local now_ms start_ms duration_ms
  now_ms="$(date +%s%3N 2>/dev/null || true)"
  start_ms="$(cat "$START_TS_FILE" 2>/dev/null || echo 0)"
  if [[ -n "$now_ms" ]] && [[ "$start_ms" =~ ^[0-9]+$ ]] && [[ "$now_ms" =~ ^[0-9]+$ ]]; then
    duration_ms=$((now_ms - start_ms))
    # Ignore accidental second trigger (hotkey repeat / dual-binding race)
    # that arrives too soon after start.
    if (( duration_ms >= 0 && duration_ms < MIN_RECORD_MS )); then
      exit 0
    fi
  fi

  if kill -0 "$pid" 2>/dev/null; then
    kill -INT "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
  fi
  : >"$PID_FILE"

  if [[ ! -s "$WAV_FILE" ]]; then
    die "Recorded file is empty"
  fi

  notify "正在转录..."
  local out clean_out text
  # Ensure venv helper binaries (notably ffmpeg shim) are visible in PATH.
  out="$(PATH="$VENV_DIR/bin:$PATH" "$VENV_DIR/bin/python" "$ROOT_DIR/transcribe_text.py" "$WAV_FILE" --language "$LANGUAGE" --disable-update 2>&1 || true)"
  # tqdm/progress output may inject CR/ANSI sequences; normalize before parsing.
  clean_out="$(printf '%s\n' "$out" | tr '\r' '\n' | sed -E 's/\x1B\[[0-9;]*[A-Za-z]//g')"
  text="$(printf '%s\n' "$clean_out" | awk -F'\t' 'index($0, "TEXT_RESULT\t")==1 {print substr($0, 13)}' | tail -n1)"

  if [[ -z "${text// }" ]]; then
    # Distinguish between true empty speech and backend failure (e.g. ffmpeg path).
    if ! printf '%s\n' "$clean_out" | grep -q 'TEXT_RESULT'; then
      notify "转录失败（后端错误，见日志）"
      echo "ERROR $(date '+%F %T') out=$(printf '%s' "$clean_out" | tr '\n' ' ' | cut -c1-400)" >>"$LOG_FILE"
      exit 1
    fi
    notify "转录为空（请重试）"
    echo "EMPTY $(date '+%F %T')" >>"$LOG_FILE"
    exit 1
  fi

  local target=""
  local can_focus_send=0
  if [[ -f "$TARGET_FILE" ]]; then
    target="$(cat "$TARGET_FILE")"
  fi
  if [[ "$OUTPUT_MODE" != "tmux" ]] && [[ -x "$ROOT_DIR/send_to_focus_ydotool.sh" ]]; then
    if command -v wtype >/dev/null 2>&1 || { command -v ydotool >/dev/null 2>&1 && command -v ydotoold >/dev/null 2>&1; }; then
      can_focus_send=1
    fi
  fi

  if [[ -n "$target" ]] && command -v tmux >/dev/null 2>&1 && pane_exists "$target"; then
    printf 'PARTIAL\t%s\n' "$text" | "$ROOT_DIR/send_to_cli_tmux.sh" "$target"
    notify "已填入到 $target（请手动按回车发送）"
    echo "FILL $(date '+%F %T') target=$target text=$text" >>"$LOG_FILE"
  elif (( can_focus_send )); then
    if printf 'PARTIAL\t%s\n' "$text" | "$ROOT_DIR/send_to_focus_ydotool.sh"; then
      notify "已填入到当前焦点窗口（请手动按回车发送）"
      echo "FILL_FOCUS $(date '+%F %T') text=$text" >>"$LOG_FILE"
    else
      if command -v wl-copy >/dev/null 2>&1; then
        printf '%s' "$text" | wl-copy
        notify "焦点填入失败，结果已复制到剪贴板"
      else
        notify "焦点填入失败：$text"
      fi
      echo "FOCUS_FAIL_COPY $(date '+%F %T') text=$text" >>"$LOG_FILE"
    fi
  else
    if command -v wl-copy >/dev/null 2>&1; then
      printf '%s' "$text" | wl-copy
      notify "未找到 tmux 目标 pane，结果已复制到剪贴板"
    else
      notify "未找到 tmux 目标 pane：$text"
    fi
    echo "COPY $(date '+%F %T') text=$text" >>"$LOG_FILE"
  fi
}

main() {
  local explicit_target="${1:-}"
  local target
  target="$(resolve_target_pane "$explicit_target")"

  if [[ -f "$PID_FILE" ]] && [[ -s "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      stop_recording_and_send
      return 0
    fi
  fi

  # stale/empty/no pid marker: start a fresh recording
  : >"$PID_FILE"
  start_recording "$target"
}

main "${1:-}"
