#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
OUT_WAV="${1:-$STATE_DIR/speaker_enroll.wav}"
DURATION_SEC="${SENSEVOICE_SPK_ENROLL_SEC:-20}"

mkdir -p "$STATE_DIR"

if ! command -v arecord >/dev/null 2>&1; then
  echo "arecord not found. Install ALSA tools first." >&2
  exit 2
fi

status_before="$("$ROOT_DIR/toggle_resident_f8.sh" status 2>/dev/null || true)"
was_active=0
if [[ "$status_before" == *"active=1"* ]]; then
  was_active=1
fi

# Avoid device-busy conflicts with the always-on stream capture process.
"$ROOT_DIR/toggle_resident_f8.sh" off >/dev/null 2>&1 || true
sleep 0.25

echo "Recording speaker enrollment sample to: $OUT_WAV"
echo "Duration: ${DURATION_SEC}s"
echo "Speak naturally in your normal coding voice."
arecord -q -f S16_LE -r 16000 -c 1 -d "$DURATION_SEC" "$OUT_WAV"

if [[ "$was_active" == "1" ]]; then
  "$ROOT_DIR/toggle_resident_f8.sh" on >/dev/null 2>&1 || true
fi

echo "Done. Enrollment sample saved:"
echo "  $OUT_WAV"
