#!/usr/bin/env bash
# 下载声纹模型 ERes2NetV2（~70MB，ModelScope），并整理成 FunASR 能直接加载的目录结构。
# 语音识别走百炼云端，不需要本地 ASR 模型。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$ROOT_DIR/models/eres2netv2"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3)"

if [[ -f "$TARGET/model.pt" && -f "$TARGET/config.yaml" ]]; then
  echo "  声纹模型已就绪: $TARGET"
  exit 0
fi

mkdir -p "$TARGET"
echo "  下载 iic/speech_eres2netv2_sv_zh-cn_16k-common → $TARGET"
"$PYTHON_BIN" -m modelscope.cli.cli download --model "iic/speech_eres2netv2_sv_zh-cn_16k-common" --local_dir "$TARGET"

# ModelScope 下来是 pretrained_eres2netv2.ckpt + 自家 configuration.json；FunASR 要 model.pt + config.yaml
if [[ -f "$TARGET/pretrained_eres2netv2.ckpt" && ! -f "$TARGET/model.pt" ]]; then
  mv "$TARGET/pretrained_eres2netv2.ckpt" "$TARGET/model.pt"
fi
cat > "$TARGET/config.yaml" <<'YAML'
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
cat > "$TARGET/configuration.json" <<'JSON'
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

if [[ -f "$TARGET/model.pt" ]]; then
  echo "  [ok] $(du -h "$TARGET/model.pt" | awk '{print $1}')  $TARGET/model.pt"
else
  echo "  [MISSING] $TARGET/model.pt" >&2
  exit 1
fi
