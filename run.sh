#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Virtual env not found. Run ./setup.sh first." >&2
  exit 2
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: ./run.sh /path/to/audio.(wav|mp3|m4a) [extra-args...]" >&2
  exit 2
fi

# Ensure helper binaries installed in venv (e.g. ffmpeg shim) are visible.
export PATH="$VENV_DIR/bin:$PATH"

"$VENV_DIR/bin/python" "$ROOT_DIR/transcribe.py" "$@"
