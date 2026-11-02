#!/usr/bin/env bash
# 把 WinVoice 部署到 winpc（Windows 笔记本）。幂等，可重复执行用于更新代码/配置。
#   ./deploy_winpc.sh            # 完整部署（首次会安装私有 Python）
#   ./deploy_winpc.sh --sync     # 只同步代码+配置（不动 Python/依赖）
#   ./deploy_winpc.sh --spk      # 同步 + 声纹门禁（torch/funasr/补丁/模型/注册模板）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
LLM_ENV="${HOME}/.config/sensevoice-vibe/llm.env"
REMOTE_DIR_WIN='D:\claudeproject\winvoice'
REMOTE_DIR_SCP='/D:/claudeproject/winvoice'
PY_VER="3.12.8"
SSH_OPTS=(-o ConnectTimeout=20 -o RequestTTY=no -o RemoteCommand=none)
SYNC_ONLY=0; WITH_SPK=1
case "${1:-}" in
  --sync) SYNC_ONLY=1; WITH_SPK=0 ;;   # 只同步代码+配置
  --spk)  SYNC_ONLY=1; WITH_SPK=1 ;;   # 同步 + 声纹（依赖/补丁/模型/注册模板）
esac
SPK_ENROLL_SRC="${HOME}/.local/state/sensevoice-vibe/speaker_enroll.d"

# 远程跑 PowerShell：用 -EncodedCommand 避开 cmd 的引号/管道吞字符问题
ps() {
  local enc
  enc=$(printf '%s' "\$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $1" \
        | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
  ssh "${SSH_OPTS[@]}" winpc "powershell -NoProfile -EncodedCommand $enc" 2>&1 | grep -v '^#< CLIXML' || true
}

echo "== 1. 连通性 =="
ps "hostname; whoami"

echo "== 2. 建目录 =="
ps "New-Item -ItemType Directory -Force '$REMOTE_DIR_WIN' | Out-Null; 'ok'"

if [[ $SYNC_ONLY -eq 0 ]]; then
  echo "== 3. 安装私有 Python $PY_VER（用户级，装到 $REMOTE_DIR_WIN\\py312，不碰系统环境）=="
  ps "
    \$py='$REMOTE_DIR_WIN\\py312\\python.exe'
    if (Test-Path \$py) { 'python already installed: ' + (& \$py --version) }
    else {
      \$exe=\"\$env:TEMP\\python-$PY_VER-amd64.exe\"
      Invoke-WebRequest 'https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-amd64.exe' -OutFile \$exe -UseBasicParsing
      \$p = Start-Process \$exe -ArgumentList '/quiet','InstallAllUsers=0','TargetDir=$REMOTE_DIR_WIN\\py312','Include_launcher=0','Include_test=0','Include_doc=0','PrependPath=0','Shortcuts=0' -Wait -PassThru
      'installer exit code: ' + \$p.ExitCode
      if (Test-Path \$py) { & \$py --version } else { 'PYTHON INSTALL FAILED' }
    }"
fi

echo "== 4. 生成 config.env（从 llm.env 摘出需要的项；含 API Key，仅放在 winpc 本机）=="
TMP_CFG="$(mktemp)"; trap 'rm -f "$TMP_CFG"' EXIT
python3 - "$LLM_ENV" "$HERE/config.example.env" > "$TMP_CFG" <<'PY'
import re, sys
llm, tmpl = sys.argv[1], sys.argv[2]
want = re.compile(r"^(SENSEVOICE_BAILIAN_|SENSEVOICE_POST_LLM_|SENSEVOICE_SPK_(?!ENROLL_WAV|MODEL|CACHE_DIR|AUTO_ENROLL)|SENSEVOICE_STREAM_(ENDPOINT_MS|MAX_SEGMENT_MS|SOFT_CUT_WINDOW_MS|SOFT_CUT_SILENCE_MS|MIN_SEGMENT_MS|VAD_AGGRESSIVENESS|START_MS|PRE_ROLL_MS))")
vals = {}
for line in open(llm, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if want.match(k):
        vals[k] = v.split("  #")[0].strip()
out = []
for line in open(tmpl, encoding="utf-8"):
    s = line.rstrip("\n")
    m = re.match(r"^([A-Z0-9_]+)=", s)
    if m and m.group(1) in vals:
        s = f"{m.group(1)}={vals.pop(m.group(1))}"
    out.append(s)
out.append("")
out.append("# --- 以下由部署脚本从 llm.env 同步 ---")
for k in sorted(vals):
    out.append(f"{k}={vals[k]}")
print("\n".join(out))
PY
grep -c "=" "$TMP_CFG" | xargs -I{} echo "config.env 共 {} 行配置"

echo "== 5. 同步文件 =="
scp "${SSH_OPTS[@]}" -q \
  "$HERE/winvoice.py" "$HERE/win_input.py" "$HERE/requirements.txt" \
  "$HERE/run.bat" "$HERE/run_console.bat" "$HERE/stop.bat" \
  "$ROOT/sensevoice/llm/postprocessor.py" "$ROOT/sensevoice/speaker/verifier.py" "$ROOT/config/hotwords.yaml" \
  "winpc:$REMOTE_DIR_SCP/"
scp "${SSH_OPTS[@]}" -q "$TMP_CFG" "winpc:$REMOTE_DIR_SCP/config.env"
# 共享的百炼 ASR 客户端（Linux/Windows 同一份源码，Windows 上叫 asr_bailian.py）
scp "${SSH_OPTS[@]}" -q "$ROOT/sensevoice/asr/bailian.py" "winpc:$REMOTE_DIR_SCP/asr_bailian.py"
ps "Get-ChildItem '$REMOTE_DIR_WIN' -File | Select-Object Name,Length | Format-Table -AutoSize | Out-String"

if [[ $SYNC_ONLY -eq 0 ]]; then
  echo "== 6. 安装依赖 =="
  ps "& '$REMOTE_DIR_WIN\\py312\\python.exe' -m pip install --disable-pip-version-check -q -r '$REMOTE_DIR_WIN\\requirements.txt'; 'pip exit: ' + \$LASTEXITCODE"
  echo "== 7. 导入自检 =="
  ps "& '$REMOTE_DIR_WIN\\py312\\python.exe' -c \"import numpy, requests, yaml, sounddevice, webrtcvad; print('imports ok'); import sounddevice as sd; print('portaudio', sd.get_portaudio_version()[1])\""
fi
if [[ $WITH_SPK -eq 1 ]]; then
  echo "== 6b. 声纹门禁：依赖（torch CPU + funasr，约 1GB，首次较慢）=="
  ps "& '$REMOTE_DIR_WIN\\py312\\python.exe' -m pip install --disable-pip-version-check -q --no-warn-script-location torch==2.5.1 torchaudio==2.5.1 funasr==1.3.1; 'pip exit: ' + \$LASTEXITCODE"

  echo "== 6c. 声纹门禁：ERes2NetV2 补丁（拷进 funasr 并注册，同 Linux 做法）=="
  scp "${SSH_OPTS[@]}" -q "$ROOT/sensevoice/speaker/funasr_patch/eres2netv2.py" \
      "$ROOT/sensevoice/speaker/funasr_patch/pooling_layers.py" "winpc:$REMOTE_DIR_SCP/"
  ps "
    \$d = (& '$REMOTE_DIR_WIN\\py312\\python.exe' -c \"import importlib.util as u;print(list(u.find_spec('funasr').submodule_search_locations)[0])\" | Select-Object -Last 1).Trim()
    \$t = Join-Path \$d 'models\\eres2net'
    Copy-Item '$REMOTE_DIR_WIN\\eres2netv2.py','$REMOTE_DIR_WIN\\pooling_layers.py' \$t -Force
    \$init = Join-Path \$t '__init__.py'
    \$line = 'from funasr.models.eres2net.eres2netv2 import ERes2NetV2  # noqa'
    if (-not (Select-String -Path \$init -SimpleMatch 'eres2netv2' -Quiet)) { Add-Content -Path \$init -Value \$line; 'patched __init__.py' } else { 'already patched' }
    Remove-Item '$REMOTE_DIR_WIN\\eres2netv2.py','$REMOTE_DIR_WIN\\pooling_layers.py'"

  echo "== 6d. 声纹模型（69MB）+ 注册模板（从 Linux 当前在用的 speaker_enroll.d 拷贝）=="
  ps "New-Item -ItemType Directory -Force '$REMOTE_DIR_WIN\\models\\eres2netv2','$REMOTE_DIR_WIN\\speaker_enroll.d' | Out-Null; 'ok'"
  scp "${SSH_OPTS[@]}" -q "$ROOT/models/eres2netv2/"* "winpc:$REMOTE_DIR_SCP/models/eres2netv2/"
  scp "${SSH_OPTS[@]}" -q "$SPK_ENROLL_SRC"/*.wav "winpc:$REMOTE_DIR_SCP/speaker_enroll.d/"
  ps "Get-ChildItem '$REMOTE_DIR_WIN\\models\\eres2netv2','$REMOTE_DIR_WIN\\speaker_enroll.d' -File | Select-Object Directory,Name,Length | Format-Table -AutoSize | Out-String -Width 200"
fi

echo "== 完成 =="
