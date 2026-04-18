#!/usr/bin/env bash
# 模型一键下载脚本
# 优先 ModelScope（国内快），失败回退 HuggingFace
#
# 用法：
#   ./download_models.sh              # 下载全部 3 个模型到 ./models/
#   ./download_models.sh asr          # 仅下载 ASR 模型
#   ./download_models.sh speaker      # 仅下载声纹模型（eres2netv2 + campplus）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="$ROOT_DIR/models"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

mkdir -p "$MODELS_DIR"

declare -A MODEL_MS=(
  [asr]="iic/SenseVoiceSmall"
  [speaker_eres2netv2]="iic/speech_eres2netv2_sv_zh-cn_16k-common"
  [speaker_campplus]="iic/speech_campplus_sv_zh-cn_16k-common"
)

declare -A MODEL_HF=(
  [asr]="FunAudioLLM/SenseVoiceSmall"
  [speaker_eres2netv2]="alibaba-damo/speech_eres2netv2_sv_zh-cn_16k-common"
  [speaker_campplus]="alibaba-damo/speech_campplus_sv_zh-cn_16k-common"
)

declare -A LOCAL_DIR=(
  [asr]="$MODELS_DIR/sensevoice-small"
  [speaker_eres2netv2]="$MODELS_DIR/eres2netv2"
  [speaker_campplus]="$MODELS_DIR/campplus"
)

download_one() {
  local key="$1"
  local target="${LOCAL_DIR[$key]}"
  local ms_repo="${MODEL_MS[$key]}"
  local hf_repo="${MODEL_HF[$key]}"

  if [[ -f "$target/configuration.json" ]] && \
     { [[ -f "$target/model.pt" ]] || [[ -f "$target/model_quant.onnx" ]] || \
       [[ -f "$target/campplus_cn_common.bin" ]]; }; then
    echo "  [skip] $key 已存在: $target"
    return 0
  fi

  echo "  [pull] $key from ModelScope ($ms_repo)"
  if "$PYTHON_BIN" -c "
import sys
from modelscope.hub.snapshot_download import snapshot_download
try:
    p = snapshot_download(model_id='$ms_repo', cache_dir=r'$MODELS_DIR/.modelscope_cache')
    import shutil, os
    target = r'$target'
    os.makedirs(target, exist_ok=True)
    for f in os.listdir(p):
        s = os.path.join(p, f); d = os.path.join(target, f)
        if os.path.isfile(s) and not os.path.exists(d):
            shutil.copy2(s, d)
    print(f'  [ok] copied to {target}')
except Exception as e:
    print(f'  [ms-fail] {type(e).__name__}: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1; then
    return 0
  fi

  echo "  [pull] $key from HuggingFace fallback ($hf_repo)"
  if ! "$PYTHON_BIN" -c "import huggingface_hub" 2>/dev/null; then
    "$PYTHON_BIN" -m pip install -q huggingface_hub
  fi
  "$PYTHON_BIN" -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='$hf_repo', local_dir=r'$target')
print('  [ok] downloaded to $target')
"
}

case "${1:-all}" in
  asr)
    download_one asr ;;
  speaker)
    download_one speaker_eres2netv2
    download_one speaker_campplus ;;
  all|"")
    echo "==> 下载 ASR 模型 (SenseVoice-Small, ~1.1GB)"
    download_one asr
    echo ""
    echo "==> 下载声纹模型 (ERes2NetV2, ~70MB)"
    download_one speaker_eres2netv2
    echo ""
    echo "==> 下载声纹模型 (CAM++, ~28MB, 备用)"
    download_one speaker_campplus ;;
  *)
    echo "用法: $0 [all|asr|speaker]" >&2
    exit 1 ;;
esac

echo ""
echo "==> 验证模型文件"
for key in asr speaker_eres2netv2 speaker_campplus; do
  d="${LOCAL_DIR[$key]}"
  if [[ -d "$d" ]]; then
    sz=$(du -sh "$d" | awk '{print $1}')
    n=$(find "$d" -maxdepth 1 -type f | wc -l)
    echo "  [ok] $key  size=$sz  files=$n  path=$d"
  fi
done

echo ""
echo "全部模型就绪，可继续: ./setup.sh"
