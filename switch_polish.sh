#!/usr/bin/env bash
# 切换 LLM 润色方式：改 llm.env 的 SENSEVOICE_POST_LLM_MODE，重启 daemon。
#   ./switch_polish.sh status              查看当前
#   ./switch_polish.sh pyramid [--winpc]   金字塔重组：结论先行、归类分组，较长的话附带表达建议
#   ./switch_polish.sh faithful [--winpc]  按原话顺序纠错（polish_coding_aggressive）
# --winpc：同时同步到 winpc（WinVoice 正在运行才重启它）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${HOME}/.config/sensevoice-vibe/llm.env"

case "${1:-status}" in
  pyramid)  MODE=pyramid ;;
  faithful) MODE=polish_coding_aggressive ;;
  status)   grep -E '^SENSEVOICE_POST_LLM_MODE=' "$ENV_FILE"; exit 0 ;;
  *)        sed -n '2,6p' "$0"; exit 2 ;;
esac

cp -p "$ENV_FILE" "$ENV_FILE.bak"
python3 - "$ENV_FILE" "$MODE" <<'PY'
import re, sys
path, mode = sys.argv[1:3]
s = open(path, encoding="utf-8").read()
pat = re.compile(r"^SENSEVOICE_POST_LLM_MODE=.*$", re.M)
s = pat.sub(f"SENSEVOICE_POST_LLM_MODE={mode}", s, count=1) if pat.search(s) else s.rstrip("\n") + f"\nSENSEVOICE_POST_LLM_MODE={mode}\n"
open(path, "w", encoding="utf-8").write(s)
PY
systemctl --user restart sensevoice-vibe.service
echo "润色已切到 $MODE，daemon 已重启；上一份配置在 $ENV_FILE.bak"

if [[ " $* " == *" --winpc "* ]]; then
  "$HERE/windows/deploy_winpc.sh" --sync
  SSH_OPTS=(-o ConnectTimeout=20 -o RequestTTY=no -o RemoteCommand=none)
  running="$(ssh "${SSH_OPTS[@]}" winpc "powershell -NoProfile -Command \"@(Get-CimInstance Win32_Process -Filter \\\"name='pythonw.exe'\\\" | Where-Object { \$_.CommandLine -like '*winvoice.py*' }).Count\"" 2>/dev/null | tr -dc '0-9')"
  if [[ "${running:-0}" -gt 0 ]]; then
    ssh "${SSH_OPTS[@]}" winpc 'cmd /c "D:\claudeproject\winvoice\stop.bat" >nul 2>&1 & schtasks /run /tn WinVoice' >/dev/null 2>&1 || true
    echo "winpc 已同步并重启 WinVoice"
  else
    echo "winpc 已同步；WinVoice 当前没在运行，下次启动生效"
  fi
fi
