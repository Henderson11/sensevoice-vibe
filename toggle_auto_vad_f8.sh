#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
mkdir -p "$STATE_DIR"

PID_FILE="$STATE_DIR/auto_vad.pid"
LOG_FILE="$STATE_DIR/auto_vad.stdout.log"
LOCK_FILE="$STATE_DIR/auto_vad.lock"
TOGGLE_LOG="$STATE_DIR/toggle_auto_vad.log"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

is_running() {
  [[ -s "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

log_toggle() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$TOGGLE_LOG"
}

notify_starting() {
  if [[ "${SENSEVOICE_SESSION_NOTIFY:-1}" != "1" ]]; then
    return 0
  fi
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -a "SenseVoice VAD" -u low -t 900 "Listening session starting..."
  fi
}

start_stream() {
  [[ -x "$VENV_DIR/bin/python" ]] || exit 2
  notify_starting
  log_toggle "START_REQUEST"
  local cmd=(
    "$VENV_DIR/bin/python" "$ROOT_DIR/stream_vad_realtime.py"
    "--language" "${SENSEVOICE_LANGUAGE:-zn}"
    "--indicator" "${SENSEVOICE_STREAM_INDICATOR:-notify}"
  )
  if [[ "${SENSEVOICE_AUTO_ENTER:-0}" == "1" ]]; then
    cmd+=("--auto-enter")
  fi
  if [[ -n "${SENSEVOICE_ARECORD_DEVICE:-}" ]]; then
    cmd+=("--input-device" "${SENSEVOICE_ARECORD_DEVICE}")
  fi

  nohup "${cmd[@]}" 9>&- >>"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$PID_FILE"
  log_toggle "STARTED pid=$pid"
}

stop_stream() {
  log_toggle "STOP_REQUEST"
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$pid" ]]; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
  fi
  : >"$PID_FILE"
  log_toggle "STOPPED"
}

if is_running; then
  stop_stream
else
  start_stream
fi
