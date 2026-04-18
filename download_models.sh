#!/usr/bin/env bash
# 模型一键下载脚本
# 用 ModelScope CLI 主拉，自动做 FunASR-ready 后处理
#
# 用法：
#   ./download_models.sh              # 下载全部 3 个模型
#   ./download_models.sh asr          # 仅 ASR
#   ./download_models.sh speaker      # 仅声纹（eres2netv2 + campplus）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="$ROOT_DIR/models"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

mkdir -p "$MODELS_DIR"

ms_download() {
  local repo="$1" target="$2"
  echo "  [pull] $repo  →  $target"
  "$PYTHON_BIN" -m modelscope.cli.cli download --model "$repo" --local_dir "$target"
}

hf_download() {
  local repo="$1" target="$2"
  echo "  [pull] $repo from HuggingFace fallback  →  $target"
  if ! "$PYTHON_BIN" -c "import huggingface_hub" 2>/dev/null; then
    "$PYTHON_BIN" -m pip install -q huggingface_hub
  fi
  HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}" \
    "$PYTHON_BIN" -c "
import os, sys
from huggingface_hub import snapshot_download
snapshot_download(repo_id='$repo', local_dir=r'$target')
print('  [ok] hf done:', '$target')
"
}

# ============ ASR (SenseVoice-Small) ============
download_asr() {
  local target="$MODELS_DIR/sensevoice-small"
  if [[ -f "$target/model.pt" && -f "$target/configuration.json" ]]; then
    echo "  [skip] ASR 已就绪: $target"
    return 0
  fi
  echo "==> 下载 ASR (SenseVoice-Small, ~1.1GB)"
  ms_download "iic/SenseVoiceSmall" "$target" || hf_download "FunAudioLLM/SenseVoiceSmall" "$target"
}

# ============ 声纹 ERes2NetV2 ============
download_eres2netv2() {
  local target="$MODELS_DIR/eres2netv2"
  if [[ -f "$target/model.pt" && -f "$target/config.yaml" ]]; then
    echo "  [skip] ERes2NetV2 已就绪: $target"
    return 0
  fi
  echo "==> 下载声纹 ERes2NetV2 (~70MB)"
  ms_download "iic/speech_eres2netv2_sv_zh-cn_16k-common" "$target"

  # === FunASR-ready 后处理 ===
  # ModelScope 下来文件名是 pretrained_eres2netv2.ckpt + 自家 configuration.json
  # FunASR 期望: model.pt + funasr 风格 config.yaml + configuration.json
  if [[ -f "$target/pretrained_eres2netv2.ckpt" && ! -f "$target/model.pt" ]]; then
    echo "  [post] 重命名 pretrained_eres2netv2.ckpt → model.pt"
    mv "$target/pretrained_eres2netv2.ckpt" "$target/model.pt"
  fi
  echo "  [post] 写 FunASR-ready config.yaml + configuration.json"
  cat > "$target/config.yaml" <<'YAML'
model: ERes2NetV2
model_conf:
    feat_dim: 80
    embedding_size: 192
    m_channels: 64
    baseWidth: 26
    scale: 2
    expansion: 2
    pooling_func: TSTP
    two_emb_layer: false

frontend: WavFrontend
frontend_conf:
    fs: 16000
YAML
  cat > "$target/configuration.json" <<'JSON'
{
    "framework": "pytorch",
    "task": "speaker-verification",
    "model": {"type": "funasr"},
    "file_path_metas": {
        "init_param": "model.pt",
        "config": "config.yaml"
    }
}
JSON
}

# ============ 声纹 CAM++（备用）============
download_campplus() {
  local target="$MODELS_DIR/campplus"
  if [[ -f "$target/campplus_cn_common.bin" && -f "$target/configuration.json" ]]; then
    echo "  [skip] CAM++ 已就绪: $target"
    return 0
  fi
  echo "==> 下载声纹 CAM++ (~28MB, 备用)"
  ms_download "iic/speech_campplus_sv_zh-cn_16k-common" "$target"
}

case "${1:-all}" in
  asr) download_asr ;;
  speaker) download_eres2netv2; download_campplus ;;
  all|"") download_asr; download_eres2netv2; download_campplus ;;
  *) echo "用法: $0 [all|asr|speaker]" >&2; exit 1 ;;
esac

echo ""
echo "==> 验证模型"
err=0
for d in "$MODELS_DIR/sensevoice-small/model.pt" \
         "$MODELS_DIR/eres2netv2/model.pt" \
         "$MODELS_DIR/eres2netv2/config.yaml" \
         "$MODELS_DIR/campplus/campplus_cn_common.bin"; do
  if [[ -f "$d" ]]; then
    sz=$(du -h "$d" | awk '{print $1}')
    echo "  [ok] $sz  $d"
  else
    echo "  [MISSING] $d"
    err=1
  fi
done
[[ "$err" == "0" ]] && echo "" && echo "模型就绪，可继续: ./setup.sh"
exit "$err"
