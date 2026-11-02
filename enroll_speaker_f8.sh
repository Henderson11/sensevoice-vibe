#!/usr/bin/env bash
# 录一段声纹注册模板到 speaker_enroll.d/，录完重启 daemon 让新模板生效。
#   ./enroll_speaker_f8.sh [名字] [秒数]      例：./enroll_speaker_f8.sh 03_far 15
# 模板可以录多段（不同距离/语气）；声纹按与各模板的最高相似度判定。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENROLL_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe/speaker_enroll.d"
NAME="${1:-$(date +%Y%m%d_%H%M%S)}"
DURATION_SEC="${2:-${SENSEVOICE_SPK_ENROLL_SEC:-20}}"
OUT_WAV="$ENROLL_DIR/${NAME%.wav}.wav"

command -v arecord >/dev/null 2>&1 || { echo "缺少 arecord（sudo apt install alsa-utils）" >&2; exit 2; }
mkdir -p "$ENROLL_DIR"
if [[ -e "$OUT_WAV" ]]; then
  echo "已存在 $OUT_WAV，换个名字再录" >&2
  exit 2
fi

# 录音期间关掉收音，免得这段话被当成语音输入
was_active=0
[[ "$("$ROOT_DIR/toggle_resident_f8.sh" status 2>/dev/null || true)" == *"active=1"* ]] && was_active=1
"$ROOT_DIR/toggle_resident_f8.sh" off >/dev/null 2>&1 || true
sleep 0.3

echo "录音 ${DURATION_SEC}s → $OUT_WAV"
echo "用平时说话的音量和语气，正常说话即可。"
arecord -q -f S16_LE -r 16000 -c 1 -d "$DURATION_SEC" "$OUT_WAV"
echo "已保存。现有模板："
ls -1 "$ENROLL_DIR"

if systemctl --user is-active --quiet sensevoice-vibe.service; then
  systemctl --user restart sensevoice-vibe.service   # 模板在启动时加载
  echo "daemon 已重启，新模板生效"
elif [[ "$was_active" == "1" ]]; then
  "$ROOT_DIR/toggle_resident_f8.sh" on >/dev/null 2>&1 || true
fi
