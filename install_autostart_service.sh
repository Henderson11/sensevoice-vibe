#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/sensevoice-vibe.service"

mkdir -p "$UNIT_DIR"

cat >"$UNIT_FILE" <<EOF
[Unit]
Description=SenseVoice Resident VAD Daemon
After=default.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment=SENSEVOICE_RESIDENT=1
Environment=SENSEVOICE_STREAM_ACTIVE_ON_START=0
Environment=SENSEVOICE_MODEL=%h/.cache/modelscope/hub/models/iic/SenseVoiceSmall
Environment=SENSEVOICE_LANGUAGE=zn
Environment=SENSEVOICE_FILTER_FILLERS=1
Environment=SENSEVOICE_PARTIAL_STRATEGY=stable2
Environment=SENSEVOICE_EMIT_PARTIAL=0
Environment=SENSEVOICE_AUTO_ENTER=0
Environment=SENSEVOICE_PREFER_WTYPE=0
Environment=SENSEVOICE_PASTE_KEY=shift_insert
Environment=SENSEVOICE_YDOTOOL_KEY_DELAY_MS=20
Environment=SENSEVOICE_CLIPBOARD_SETTLE_SEC=0.03
Environment=SENSEVOICE_CLIPBOARD_VERIFY=0
Environment=SENSEVOICE_CLEAR_BEFORE_REPLACE=0
Environment=SENSEVOICE_STREAM_INDICATOR=notify_once
Environment=SENSEVOICE_STREAM_VAD_AGGRESSIVENESS=1
Environment=SENSEVOICE_STREAM_START_MS=80
Environment=SENSEVOICE_STREAM_FRAME_MS=20
Environment=SENSEVOICE_STREAM_ENDPOINT_MS=500
Environment=SENSEVOICE_STREAM_MAX_SEGMENT_MS=8000
Environment=SENSEVOICE_STREAM_PARTIAL_INTERVAL_MS=280
Environment=SENSEVOICE_STREAM_MIN_PARTIAL_MS=700
ExecStart=$VENV_DIR/bin/python $ROOT_DIR/stream_vad_realtime.py --resident --language zn --indicator notify_once
Restart=always
RestartSec=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now sensevoice-vibe.service

echo "Installed: $UNIT_FILE"
systemctl --user --no-pager --full status sensevoice-vibe.service | sed -n '1,20p'
