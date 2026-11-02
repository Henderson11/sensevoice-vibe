#!/usr/bin/env bash
# 在百炼 3.1 普通版和 Message 版之间切换：改 llm.env 的主模型与兜底模型，重启 daemon。
#   ./switch_asr.sh status              查看当前用的哪个
#   ./switch_asr.sh message [--winpc]   切到 qwen-audio-3.1-asr-flash-message（带前文上下文；兜底 3.1 普通版 → fun-asr）
#   ./switch_asr.sh standard [--winpc]  切回 qwen-audio-3.1-asr-flash（兜底 fun-asr → qwen3-asr-flash）
# --winpc：同时同步到 winpc（deploy_winpc.sh --sync）；WinVoice 正在运行才重启它（两台同时开会各识别一次）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${HOME}/.config/sensevoice-vibe/llm.env"
SSH_OPTS=(-o ConnectTimeout=20 -o RequestTTY=no -o RemoteCommand=none)

case "${1:-status}" in
  message)  MODEL=qwen-audio-3.1-asr-flash-message; HEDGE=qwen-audio-3.1-asr-flash,fun-asr-flash-2026-06-15 ;;
  standard) MODEL=qwen-audio-3.1-asr-flash;         HEDGE=fun-asr-flash-2026-06-15,qwen3-asr-flash ;;
  status)   grep -E '^SENSEVOICE_BAILIAN_(MODEL|HEDGE_MODELS)=' "$ENV_FILE"; exit 0 ;;
  *)        sed -n '2,6p' "$0"; exit 2 ;;
esac

if [[ "$MODEL" == *-message ]] && ! "$HERE/.venv/bin/python" -c "import dashscope" 2>/dev/null; then
  echo "缺少 dashscope：先执行 $HERE/.venv/bin/pip install 'dashscope>=2.0.1'" >&2
  exit 3
fi

cp -p "$ENV_FILE" "$ENV_FILE.bak"   # 只留上一份，权限照旧（含 API Key）
python3 - "$ENV_FILE" "$MODEL" "$HEDGE" <<'PY'
import re, sys
path, model, hedge = sys.argv[1:4]
s = open(path, encoding="utf-8").read()
for key, val in (("SENSEVOICE_BAILIAN_MODEL", model), ("SENSEVOICE_BAILIAN_HEDGE_MODELS", hedge)):
    pat = re.compile(rf"^{key}=.*$", re.M)
    s = pat.sub(f"{key}={val}", s, count=1) if pat.search(s) else s.rstrip("\n") + f"\n{key}={val}\n"
open(path, "w", encoding="utf-8").write(s)
PY
systemctl --user restart sensevoice-vibe.service
echo "Linux 已切到 $MODEL（兜底 $HEDGE），daemon 已重启；上一份配置在 $ENV_FILE.bak"

if [[ " $* " == *" --winpc "* ]]; then
  "$HERE/windows/deploy_winpc.sh" --sync
  running="$(ssh "${SSH_OPTS[@]}" winpc "powershell -NoProfile -Command \"@(Get-CimInstance Win32_Process -Filter \\\"name='pythonw.exe'\\\" | Where-Object { \$_.CommandLine -like '*winvoice.py*' }).Count\"" 2>/dev/null | tr -dc '0-9')"
  if [[ "${running:-0}" -gt 0 ]]; then
    ssh "${SSH_OPTS[@]}" winpc 'cmd /c "D:\claudeproject\winvoice\stop.bat" >nul 2>&1 & schtasks /run /tn WinVoice' >/dev/null 2>&1 || true
    echo "winpc 已同步并重启 WinVoice"
  else
    echo "winpc 已同步；WinVoice 当前没在运行，下次启动生效"
  fi
fi
