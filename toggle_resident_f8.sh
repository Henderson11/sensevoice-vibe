#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
mkdir -p "$STATE_DIR"

PID_FILE="$STATE_DIR/resident.pid"
STATUS_FILE="$STATE_DIR/resident.status"
DAEMON_LOG="$STATE_DIR/resident.stdout.log"
LOCK_FILE="$STATE_DIR/resident_toggle.lock"
TOGGLE_LOG="$STATE_DIR/toggle_resident.log"
LOCK_WAIT_SEC="${SENSEVOICE_TOGGLE_LOCK_WAIT_SEC:-0.8}"
STATUS_WAIT_SEC="${SENSEVOICE_TOGGLE_STATUS_WAIT_SEC:-3}"
TOGGLE_DEBOUNCE_MS="${SENSEVOICE_TOGGLE_DEBOUNCE_MS:-450}"
LAST_TOGGLE_MS_FILE="$STATE_DIR/last_toggle_ms"

MODEL_PATH_DEFAULT="$ROOT_DIR/models/sensevoice-small"

log_toggle() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$TOGGLE_LOG"
}

run_housekeep() {
  [[ -f "$ROOT_DIR/retention_housekeep.sh" ]] || return 0
  SENSEVOICE_RETENTION_KEEP_RECENT="${SENSEVOICE_RETENTION_KEEP_RECENT:-20}" \
  SENSEVOICE_LOG_KEEP_LINES="${SENSEVOICE_LOG_KEEP_LINES:-120}" \
  SENSEVOICE_RETENTION_PROTECT_WAV_CSV="${SENSEVOICE_RETENTION_PROTECT_WAV_CSV:-}" \
    /bin/bash "$ROOT_DIR/retention_housekeep.sh" >/dev/null 2>&1 || true
}

notify_msg() {
  local msg="$1"
  if [[ "${SENSEVOICE_SESSION_NOTIFY:-0}" != "1" ]]; then
    return 0
  fi
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -a "SenseVoice VAD" -u low -t 900 "$msg"
  fi
}

read_active() {
  if [[ -f "$STATUS_FILE" ]]; then
    awk -F= '/^active=/{print $2; found=1} END{if(!found) print 0}' "$STATUS_FILE"
  else
    echo 0
  fi
}

wait_for_active() {
  local target="$1"
  local loops
  loops="$(awk "BEGIN{print int((${STATUS_WAIT_SEC}*1000)/40)}")"
  [[ "$loops" -lt 1 ]] && loops=1
  for _ in $(seq 1 "$loops"); do
    if [[ "$(read_active)" == "$target" ]]; then
      return 0
    fi
    sleep 0.04
  done
  return 1
}

read_ready() {
  if [[ -f "$STATUS_FILE" ]]; then
    awk -F= '/^ready=/{print $2; found=1} END{if(!found) print 0}' "$STATUS_FILE"
  else
    echo 0
  fi
}

daemon_pid() {
  cat "$PID_FILE" 2>/dev/null || true
}

is_running() {
  local pid
  pid="$(daemon_pid)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

start_daemon() {
  [[ -x "$VENV_DIR/bin/python" ]] || exit 2
  local llm_env_file="$HOME/.config/sensevoice-vibe/llm.env"
  if [[ -f "$llm_env_file" ]]; then
    set -a
    # shellcheck source=/dev/null
    . "$llm_env_file"
    set +a
  fi
  run_housekeep
  notify_msg "ASR daemon starting..."
  log_toggle "START_DAEMON_REQUEST"
  nohup env \
    PYTHONPATH="$ROOT_DIR" \
    SENSEVOICE_RESIDENT=1 \
    SENSEVOICE_STREAM_ACTIVE_ON_START=1 \
    SENSEVOICE_MODEL="${SENSEVOICE_MODEL:-$MODEL_PATH_DEFAULT}" \
    SENSEVOICE_LANGUAGE="${SENSEVOICE_LANGUAGE:-auto}" \
    SENSEVOICE_FILTER_FILLERS="${SENSEVOICE_FILTER_FILLERS:-1}" \
    SENSEVOICE_SPK_ENABLE="${SENSEVOICE_SPK_ENABLE:-1}" \
    SENSEVOICE_SPK_ENROLL_WAV="${SENSEVOICE_SPK_ENROLL_WAV:-$STATE_DIR/speaker_enroll.d}" \
    SENSEVOICE_SPK_THRESHOLD="${SENSEVOICE_SPK_THRESHOLD:-0.52}" \
    SENSEVOICE_SPK_MIN_MS="${SENSEVOICE_SPK_MIN_MS:-900}" \
    SENSEVOICE_SPK_MODEL="${SENSEVOICE_SPK_MODEL:-speechbrain/spkrec-ecapa-voxceleb}" \
    SENSEVOICE_SPK_CACHE_DIR="${SENSEVOICE_SPK_CACHE_DIR:-$HOME/.cache/sensevoice-vibe/spkrec}" \
    SENSEVOICE_SPK_AGG="${SENSEVOICE_SPK_AGG:-topk_mean}" \
    SENSEVOICE_SPK_TOPK="${SENSEVOICE_SPK_TOPK:-1}" \
    SENSEVOICE_SPK_AUTO_ENROLL="${SENSEVOICE_SPK_AUTO_ENROLL:-0}" \
    SENSEVOICE_SPK_AUTO_ENROLL_DIR="${SENSEVOICE_SPK_AUTO_ENROLL_DIR:-$STATE_DIR/speaker_enroll.d}" \
    SENSEVOICE_SPK_AUTO_ENROLL_MIN_SCORE="${SENSEVOICE_SPK_AUTO_ENROLL_MIN_SCORE:-0.70}" \
    SENSEVOICE_SPK_AUTO_ENROLL_MIN_MS="${SENSEVOICE_SPK_AUTO_ENROLL_MIN_MS:-2200}" \
    SENSEVOICE_SPK_AUTO_ENROLL_COOLDOWN_SEC="${SENSEVOICE_SPK_AUTO_ENROLL_COOLDOWN_SEC:-90}" \
    SENSEVOICE_SPK_AUTO_ENROLL_MAX_TEMPLATES="${SENSEVOICE_SPK_AUTO_ENROLL_MAX_TEMPLATES:-20}" \
    SENSEVOICE_SPK_ADAPTIVE="${SENSEVOICE_SPK_ADAPTIVE:-1}" \
    SENSEVOICE_SPK_ADAPTIVE_WINDOW="${SENSEVOICE_SPK_ADAPTIVE_WINDOW:-80}" \
    SENSEVOICE_SPK_ADAPTIVE_MIN_SAMPLES="${SENSEVOICE_SPK_ADAPTIVE_MIN_SAMPLES:-10}" \
    SENSEVOICE_SPK_ADAPTIVE_FLOOR="${SENSEVOICE_SPK_ADAPTIVE_FLOOR:-0.52}" \
    SENSEVOICE_SPK_ADAPTIVE_MARGIN="${SENSEVOICE_SPK_ADAPTIVE_MARGIN:-0.04}" \
    SENSEVOICE_SPK_PRUNE_OUTLIERS="${SENSEVOICE_SPK_PRUNE_OUTLIERS:-1}" \
    SENSEVOICE_SPK_PRUNE_KEEP="${SENSEVOICE_SPK_PRUNE_KEEP:-10}" \
    SENSEVOICE_PARTIAL_STRATEGY="${SENSEVOICE_PARTIAL_STRATEGY:-stable2}" \
    SENSEVOICE_EMIT_PARTIAL="${SENSEVOICE_EMIT_PARTIAL:-0}" \
    SENSEVOICE_AUTO_ENTER="${SENSEVOICE_AUTO_ENTER:-0}" \
    SENSEVOICE_INJECT_MODE="${SENSEVOICE_INJECT_MODE:-clipboard}" \
    SENSEVOICE_PREFER_WTYPE="${SENSEVOICE_PREFER_WTYPE:-0}" \
    SENSEVOICE_PASTE_KEY="${SENSEVOICE_PASTE_KEY:-ctrl_v}" \
    SENSEVOICE_YDOTOOL_KEY_DELAY_MS="${SENSEVOICE_YDOTOOL_KEY_DELAY_MS:-35}" \
    SENSEVOICE_CLIPBOARD_SETTLE_SEC="${SENSEVOICE_CLIPBOARD_SETTLE_SEC:-0.08}" \
    SENSEVOICE_PASTE_PRE_DELAY_SEC="${SENSEVOICE_PASTE_PRE_DELAY_SEC:-0.08}" \
    SENSEVOICE_CLIPBOARD_RESTORE="${SENSEVOICE_CLIPBOARD_RESTORE:-1}" \
    SENSEVOICE_CLIPBOARD_RESTORE_DELAY_SEC="${SENSEVOICE_CLIPBOARD_RESTORE_DELAY_SEC:-0.12}" \
    SENSEVOICE_CLIPBOARD_VERIFY="${SENSEVOICE_CLIPBOARD_VERIFY:-1}" \
    SENSEVOICE_INJECT_ACK_TIMEOUT_SEC="${SENSEVOICE_INJECT_ACK_TIMEOUT_SEC:-1.2}" \
    SENSEVOICE_DEBUG_INJECT="${SENSEVOICE_DEBUG_INJECT:-1}" \
    SENSEVOICE_CLEAR_BEFORE_REPLACE="${SENSEVOICE_CLEAR_BEFORE_REPLACE:-0}" \
    SENSEVOICE_STREAM_INDICATOR="${SENSEVOICE_STREAM_INDICATOR:-none}" \
    SENSEVOICE_STREAM_VAD_AGGRESSIVENESS="${SENSEVOICE_STREAM_VAD_AGGRESSIVENESS:-3}" \
    SENSEVOICE_STREAM_START_MS="${SENSEVOICE_STREAM_START_MS:-240}" \
    SENSEVOICE_STREAM_FRAME_MS="${SENSEVOICE_STREAM_FRAME_MS:-20}" \
    SENSEVOICE_STREAM_PRE_ROLL_MS="${SENSEVOICE_STREAM_PRE_ROLL_MS:-700}" \
    SENSEVOICE_STREAM_ENDPOINT_MS="${SENSEVOICE_STREAM_ENDPOINT_MS:-1200}" \
    SENSEVOICE_STREAM_MAX_SEGMENT_MS="${SENSEVOICE_STREAM_MAX_SEGMENT_MS:-30000}" \
    SENSEVOICE_STREAM_MIN_SEGMENT_MS="${SENSEVOICE_STREAM_MIN_SEGMENT_MS:-850}" \
    SENSEVOICE_STREAM_PARTIAL_INTERVAL_MS="${SENSEVOICE_STREAM_PARTIAL_INTERVAL_MS:-280}" \
    SENSEVOICE_STREAM_MIN_PARTIAL_MS="${SENSEVOICE_STREAM_MIN_PARTIAL_MS:-1300}" \
    SENSEVOICE_RETENTION_KEEP_RECENT="${SENSEVOICE_RETENTION_KEEP_RECENT:-20}" \
    SENSEVOICE_LOG_KEEP_LINES="${SENSEVOICE_LOG_KEEP_LINES:-120}" \
    SENSEVOICE_POST_LLM_ENABLE="${SENSEVOICE_POST_LLM_ENABLE:-0}" \
    SENSEVOICE_POST_LLM_BASE_URL="${SENSEVOICE_POST_LLM_BASE_URL:-${OPENAI_BASE_URL:-}}" \
    SENSEVOICE_POST_LLM_API_KEY="${SENSEVOICE_POST_LLM_API_KEY:-${OPENAI_API_KEY:-}}" \
    SENSEVOICE_POST_LLM_MODEL="${SENSEVOICE_POST_LLM_MODEL:-DeepSeek-V3.1-Terminus}" \
    SENSEVOICE_POST_LLM_FALLBACK_MODEL="${SENSEVOICE_POST_LLM_FALLBACK_MODEL:-DeepSeek-V3.1-Terminus}" \
    SENSEVOICE_POST_LLM_MODE="${SENSEVOICE_POST_LLM_MODE:-polish_coding_aggressive}" \
    SENSEVOICE_POST_LLM_TIMEOUT_MS="${SENSEVOICE_POST_LLM_TIMEOUT_MS:-1800}" \
    SENSEVOICE_POST_LLM_MAX_TOKENS="${SENSEVOICE_POST_LLM_MAX_TOKENS:-96}" \
    SENSEVOICE_POST_LLM_TEMPERATURE="${SENSEVOICE_POST_LLM_TEMPERATURE:-0}" \
    SENSEVOICE_POST_LLM_STRICT="${SENSEVOICE_POST_LLM_STRICT:-0}" \
    SENSEVOICE_POST_LLM_CIRCUIT_MAX_FAILS="${SENSEVOICE_POST_LLM_CIRCUIT_MAX_FAILS:-4}" \
    SENSEVOICE_POST_LLM_CIRCUIT_COOLDOWN_SEC="${SENSEVOICE_POST_LLM_CIRCUIT_COOLDOWN_SEC:-25}" \
    SENSEVOICE_POST_LLM_HARD_COOLDOWN_SEC="${SENSEVOICE_POST_LLM_HARD_COOLDOWN_SEC:-300}" \
    SENSEVOICE_POST_LLM_RETRY_ON_TIMEOUT="${SENSEVOICE_POST_LLM_RETRY_ON_TIMEOUT:-1}" \
    SENSEVOICE_POST_LLM_RETRY_BACKOFF_MS="${SENSEVOICE_POST_LLM_RETRY_BACKOFF_MS:-80}" \
    SENSEVOICE_POST_LLM_MODEL_AUTO="${SENSEVOICE_POST_LLM_MODEL_AUTO:-1}" \
    SENSEVOICE_POST_LLM_MODEL_PROBE_TIMEOUT_MS="${SENSEVOICE_POST_LLM_MODEL_PROBE_TIMEOUT_MS:-450}" \
    SENSEVOICE_POST_LLM_MIN_CHARS="${SENSEVOICE_POST_LLM_MIN_CHARS:-5}" \
    SENSEVOICE_POST_LLM_CACHE_TTL_SEC="${SENSEVOICE_POST_LLM_CACHE_TTL_SEC:-300}" \
    SENSEVOICE_POST_LLM_CACHE_MAX_ENTRIES="${SENSEVOICE_POST_LLM_CACHE_MAX_ENTRIES:-120}" \
    SENSEVOICE_POST_LLM_DYNAMIC_MAX_TOKENS="${SENSEVOICE_POST_LLM_DYNAMIC_MAX_TOKENS:-1}" \
    SENSEVOICE_POST_LLM_OUTPUT_TOKEN_FACTOR="${SENSEVOICE_POST_LLM_OUTPUT_TOKEN_FACTOR:-0.7}" \
    SENSEVOICE_PROJECT_LEXICON_ENABLE="${SENSEVOICE_PROJECT_LEXICON_ENABLE:-1}" \
    SENSEVOICE_PROJECT_ROOT="${SENSEVOICE_PROJECT_ROOT:-$HOME/mosim_workspace}" \
    SENSEVOICE_PROJECT_LEXICON_MAX_TERMS="${SENSEVOICE_PROJECT_LEXICON_MAX_TERMS:-2500}" \
    SENSEVOICE_PROJECT_LEXICON_HINT_LIMIT="${SENSEVOICE_PROJECT_LEXICON_HINT_LIMIT:-16}" \
    SENSEVOICE_PROJECT_LEXICON_MIN_TERM_LEN="${SENSEVOICE_PROJECT_LEXICON_MIN_TERM_LEN:-3}" \
    SENSEVOICE_PROJECT_LEXICON_EXTRA_FILE="${SENSEVOICE_PROJECT_LEXICON_EXTRA_FILE:-$ROOT_DIR/hotwords_coding_zh.txt}" \
    SENSEVOICE_CONF_ROUTE_ENABLE="${SENSEVOICE_CONF_ROUTE_ENABLE:-1}" \
    SENSEVOICE_CONF_ROUTE_HIGH="${SENSEVOICE_CONF_ROUTE_HIGH:-0.42}" \
    SENSEVOICE_CONF_ROUTE_LOW="${SENSEVOICE_CONF_ROUTE_LOW:-0.30}" \
    SENSEVOICE_LEARN_ENABLE="${SENSEVOICE_LEARN_ENABLE:-1}" \
    SENSEVOICE_LEARN_STORE="${SENSEVOICE_LEARN_STORE:-$STATE_DIR/correction_memory.json}" \
    SENSEVOICE_LEARN_MIN_HITS="${SENSEVOICE_LEARN_MIN_HITS:-2}" \
    SENSEVOICE_LEARN_MAX_RULES="${SENSEVOICE_LEARN_MAX_RULES:-320}" \
    SENSEVOICE_COMPARE_LOG_ENABLE="${SENSEVOICE_COMPARE_LOG_ENABLE:-1}" \
    SENSEVOICE_COMPARE_LOG_FILE="${SENSEVOICE_COMPARE_LOG_FILE:-$STATE_DIR/post_compare.jsonl}" \
    SENSEVOICE_COMPARE_LOG_KEEP_LINES="${SENSEVOICE_COMPARE_LOG_KEEP_LINES:-300}" \
    SENSEVOICE_STATE_LOG="${SENSEVOICE_STATE_LOG:-$STATE_DIR/stream_vad.log}" \
    "$VENV_DIR/bin/python" "$ROOT_DIR/stream_vad_realtime.py" \
    --resident \
    --language "${SENSEVOICE_LANGUAGE:-auto}" \
    --indicator "${SENSEVOICE_STREAM_INDICATOR:-none}" \
    >>"$DAEMON_LOG" 2>&1 </dev/null &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$PID_FILE"
  log_toggle "START_DAEMON pid=$pid"
}

ensure_running() {
  if is_running; then
    return 0
  fi
  start_daemon
  for _ in $(seq 1 100); do
    if is_running; then
      return 0
    fi
    sleep 0.05
  done
  return 1
}

send_signal() {
  local sig="$1"
  local pid
  pid="$(daemon_pid)"
  [[ -n "$pid" ]] || return 1
  kill "-$sig" "$pid" 2>/dev/null || return 1
}

now_ms() {
  date +%s%3N
}

should_drop_by_debounce() {
  local action="$1"
  # Debounce only toggle action to suppress duplicated hotkey events.
  if [[ "$action" != "toggle" && "$action" != "--toggle" && -n "$action" ]]; then
    return 1
  fi
  local now last
  now="$(now_ms)"
  last="$(cat "$LAST_TOGGLE_MS_FILE" 2>/dev/null || echo 0)"
  if [[ "$last" =~ ^[0-9]+$ ]]; then
    if (( now - last < TOGGLE_DEBOUNCE_MS )); then
      log_toggle "DEBOUNCE_DROP delta_ms=$((now-last))"
      return 0
    fi
  fi
  echo "$now" >"$LAST_TOGGLE_MS_FILE"
  return 1
}

# 提前加载 llm.env 以获取 SENSEVOICE_INJECT_MODE 等配置
_llm_env_file="$HOME/.config/sensevoice-vibe/llm.env"
if [[ -f "$_llm_env_file" ]]; then
  set -a; . "$_llm_env_file"; set +a
fi

INJECT_MODE="${SENSEVOICE_INJECT_MODE:-clipboard}"
IBUS_VOICE_ENGINE="sensevoice-voice"
IBUS_RESTORE_ENGINE="${SENSEVOICE_IBUS_RESTORE_ENGINE:-rime}"

switch_ibus_engine() {
  local target="$1"
  if [[ "$INJECT_MODE" != "ibus" ]]; then
    return 0
  fi
  timeout 3 ibus engine "$target" >/dev/null 2>&1 || true
  log_toggle "IBUS_SWITCH target=$target"
}

set_active_state() {
  local target="$1" # 1:on 0:off
  # IBus 引擎切换由 IBusInjector 按需微切换，F8 只控制监听状态
  if [[ "$target" == "1" ]]; then
    send_signal USR2
  else
    send_signal HUP
  fi
  if wait_for_active "$target"; then
    return 0
  fi
  # Retry once for race windows (status file lag / transient signal miss)
  if [[ "$target" == "1" ]]; then
    send_signal USR2
  else
    send_signal HUP
  fi
  wait_for_active "$target"
}

action="${1:-toggle}"
exec 9>"$LOCK_FILE"
if ! flock -w "$LOCK_WAIT_SEC" 9; then
  exit 0
fi

run_housekeep

if should_drop_by_debounce "$action"; then
  exit 0
fi

case "$action" in
  --status|status)
    if is_running; then
      echo "running=1 ready=$(read_ready) active=$(read_active) pid=$(daemon_pid)"
    else
      echo "running=0 ready=0 active=0 pid="
    fi
    exit 0
    ;;
  --on|on)
    if ! ensure_running; then
      notify_msg "ASR daemon start failed"
      exit 2
    fi
    if [[ "$(read_active)" == "1" ]]; then
      log_toggle "CONTROL_ON noop"
      notify_msg "Listening ON"
    elif set_active_state 1; then
      log_toggle "CONTROL_ON ok"
      notify_msg "Listening ON"
    else
      log_toggle "CONTROL_ON timeout"
      notify_msg "Listening ON failed"
      exit 2
    fi
    ;;
  --off|off)
    if is_running; then
      if [[ "$(read_active)" == "0" ]]; then
        log_toggle "CONTROL_OFF noop"
        notify_msg "Listening OFF"
      elif set_active_state 0; then
        log_toggle "CONTROL_OFF ok"
        notify_msg "Listening OFF"
      else
        log_toggle "CONTROL_OFF timeout"
        notify_msg "Listening OFF failed"
        exit 2
      fi
    fi
    ;;
  --toggle|toggle|"")
    if ! ensure_running; then
      notify_msg "ASR daemon start failed"
      exit 2
    fi
    current_active="$(read_active)"
    if [[ "$current_active" == "1" ]]; then
      if set_active_state 0; then
        log_toggle "CONTROL_TOGGLE new=0"
        notify_msg "Listening OFF"
      else
        log_toggle "CONTROL_TOGGLE fail target=0"
        notify_msg "Listening OFF failed"
        exit 2
      fi
    else
      if set_active_state 1; then
        log_toggle "CONTROL_TOGGLE new=1"
        notify_msg "Listening ON"
      else
        log_toggle "CONTROL_TOGGLE fail target=1"
        notify_msg "Listening ON failed"
        exit 2
      fi
    fi
    ;;
  *)
    echo "Usage: $0 [toggle|on|off|status]" >&2
    exit 2
    ;;
esac
