#!/usr/bin/env bash
# F8 控制脚本 —— 极简版（v2，2026-05）。
#
# daemon 生命周期：systemd user unit `sensevoice-vibe.service` 永久管理（Restart=always）。
# 本脚本只做用户意图传达：发命令到 daemon 的 control socket，读 ack 显示通知。
# 不再有 PID/status 文件、SIGHUP/USR2 信号、env mtime 检测、cold_start 等待逻辑——
# 所有曾经的 race（启停竞争、status 残留、信号在 import 阶段被默认 handler 终止）
# 都因为通信通道单一化而消失。

set -u

SOCKET="${XDG_RUNTIME_DIR:-/run/user/$UID}/sensevoice-ctl.sock"
SERVICE="sensevoice-vibe.service"
ACTION="${1:-toggle}"

# UI 通知（无 notify-send 静默 no-op）
notify() {
  command -v notify-send >/dev/null 2>&1 \
    && notify-send -t 1500 "SenseVoice" "$1" 2>/dev/null || true
}

# 同步 RPC：连 unix socket 发命令，最多等 2 秒拿到 ack。
# 用 python 而不是 nc/socat —— python3 是系统标配，nc 的 -U 不通用。
send_rpc() {
  python3 - "$SOCKET" "$1" <<'PY'
import socket, sys
sock_path, cmd = sys.argv[1], sys.argv[2]
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(sock_path)
    s.sendall((cmd + "\n").encode())
    print(s.recv(256).decode().strip())
except FileNotFoundError:
    print("ERR\tsocket_missing")
except ConnectionRefusedError:
    print("ERR\tconn_refused")
except socket.timeout:
    print("ERR\ttimeout")
except Exception as e:
    print(f"ERR\t{type(e).__name__}:{e}")
PY
}

# 发命令，若 socket 不存在/拒接（daemon 没起），尝试 systemctl start 一次再重发
rpc_with_autostart() {
  local cmd="$1" ack
  ack="$(send_rpc "$cmd")"
  if [[ "$ack" == "ERR"$'\t'"socket_missing" || "$ack" == "ERR"$'\t'"conn_refused" ]]; then
    notify "Starting daemon..."
    systemctl --user start "$SERVICE" 2>/dev/null || true
    # daemon 加载模型可能要几秒；socket 一就绪就重试
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
      sleep 0.5
      if [[ -S "$SOCKET" ]]; then
        ack="$(send_rpc "$cmd")"
        [[ "$ack" == "ERR"$'\t'"conn_refused" ]] || break
      fi
    done
  fi
  echo "$ack"
}

case "$ACTION" in
  --status|status)
    echo "$(send_rpc STATUS)"
    exit 0
    ;;
  --restart|restart)
    systemctl --user restart "$SERVICE"
    notify "ASR restarted"
    exit $?
    ;;
  --stop)
    # 只下线 daemon（systemctl stop），不仅仅是 deactivate；用于运维
    systemctl --user stop "$SERVICE"
    notify "ASR daemon stopped"
    exit $?
    ;;
  --on|on)         CMD="ACTIVATE" ;;
  --off|off)       CMD="DEACTIVATE" ;;
  --toggle|toggle|"") CMD="TOGGLE" ;;
  *)
    echo "用法: $0 [on|off|toggle|status|restart|--stop]" >&2
    exit 2
    ;;
esac

ACK="$(rpc_with_autostart "$CMD")"

if [[ "$ACK" == OK* ]]; then
  active="$(echo "$ACK" | grep -oP 'active=\K[01]' | head -1)"
  ready="$(echo "$ACK" | grep -oP 'ready=\K[01]' | head -1)"
  if [[ "$active" == "1" ]]; then
    if [[ "$ready" == "1" ]]; then
      notify "Listening ON"
    else
      notify "Listening ON (loading...)"
    fi
  else
    notify "Listening OFF"
  fi
else
  notify "ASR error: ${ACK#ERR	}"
  exit 2
fi
