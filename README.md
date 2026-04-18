<div align="center">

# 🎤 SenseVoice Vibe

**给 Linux 程序员的本地语音输入系统 — 按 F8 开口说，文字直接出现在光标处**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![ASR](https://img.shields.io/badge/ASR-SenseVoice--Small-FF6B6B)](https://www.modelscope.cn/models/iic/SenseVoiceSmall)
[![Speaker](https://img.shields.io/badge/Speaker-ERes2NetV2-4ECDC4)](https://www.modelscope.cn/models/iic/speech_eres2netv2_sv_zh-cn_16k-common)
[![Platform](https://img.shields.io/badge/Platform-Linux--IBus-FCC419?logo=linux&logoColor=black)](https://github.com/ibus/ibus)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/Henderson11/sensevoice-vibe/pulls)

**[特性](#-特性) · [60 秒上手](#-60-秒上手) · [完整安装](#-完整安装-runbook) · [配置](#-配置) · [故障排查](#-故障排查) · [架构](#-架构)**

</div>

---

## 💡 这是什么

写代码 / 写文档 / 写消息时，敲键盘累。这个项目让你：

> **按一下 F8 → 对着麦克风说话 → 屏幕里的光标处直接出现文字**

跟一般"语音转文字"工具的差别：

| | 一般方案 | **SenseVoice Vibe** |
|---|---|---|
| **识别引擎** | 走云 API（百度/讯飞/Azure） | **本地 ASR**（SenseVoice INT8，~500ms/句） |
| **隐私** | 音频上传服务器 | **音频不出本机** |
| **旁人说话** | 全部识别注入 | **声纹门禁** — 只识别你自己 |
| **错别字** | 写错就错 | **LLM 润色**（DeepSeek/GLM/Qwen，可换） |
| **注入方式** | 复制粘贴（污染剪贴板） | **IBus commit_text** — 直接发到光标 |
| **热词** | 手工维护 | **自动从你代码目录提取**编程术语 |
| **专业术语** | "组网" | **"整网"**（你项目的固有词被保留） |

适合：在 Linux 写代码的人、信息安全要求高的场景、不愿意每月花钱订阅云 ASR 的人。

---

## ✨ 特性

- 🎤 **本地 ASR**：FunASR SenseVoice-Small，ONNX INT8 推理 ~500ms/句
- 🔐 **声纹门禁**：ERes2NetV2 验证只识别你自己（避免周围人说话被打字）
- 🧠 **LLM 后处理润色**：OpenAI 兼容 API，自动修错别字补标点（DeepSeek-V3.2 / GLM-5.1 / 任何兼容服务）
- ⚡ **熔断 + 缓存 + 公网 fallback**：内网 LLM 故障时自动切到公网 API，重复句命中缓存
- ⌨️ **IBus 直接注入**：不污染剪贴板，不模拟键盘（`commit_text` 协议）
- 🔥 **F8 一键热键**：常驻进程模式，模型只加载一次
- 📚 **项目术语表**：自动扫描你的代码目录提取标识符当热词（"FlashAttention"、"output_gen_pf" 不会被识别成 "闪光注意力"、"输出根据 PF"）
- 🔄 **配置漂移自动重启**：改 `llm.env` 后按 F8，脚本自动检测并重启
- 📊 **完整可观测性**：所有阶段（VAD / 声纹 / ASR / LLM / 注入）都有结构化日志

---

## 🏗 架构

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ 麦克风    │────►│ WebRTC   │────►│ 声纹验证  │────►│ SenseVoice│
│ PulseAudio│     │ VAD      │     │ ERes2NetV2│     │ ASR INT8 │
└──────────┘     └──────────┘     └──────────┘     └────┬─────┘
                                       │                 │
                                       │ ✗ 不是你→丢弃    ▼
                                       │            ┌──────────┐
                                       │            │置信度路由 │
                                       │            └────┬─────┘
                                       │                 │
                                       │            ┌────▼─────┐  熔断+缓存
                                       │            │ LLM 润色  │◄──── DeepSeek/GLM
                                       │            └────┬─────┘
                                       │                 │
                                       │            ┌────▼─────┐
                                       │            │ 项目术语表│
                                       │            │ 标准化   │
                                       │            └────┬─────┘
                                       │                 │
                                       └────────────────►│
                                                         ▼
                                                   ┌──────────┐
                                                   │ IBus 注入 │
                                                   │commit_text│
                                                   └────┬─────┘
                                                        ▼
                                                  💻 当前光标处
```

---

## ⚡ 60 秒上手

```bash
git clone https://github.com/Henderson11/sensevoice-vibe.git
cd sensevoice-vibe
./setup.sh                  # 建 venv + pip install
./download_models.sh        # 拉 1.2GB 模型（ModelScope，国内快）
./install_ibus_engine.sh    # 装 IBus 引擎

# 配置 LLM API key（必填一项即可，没有也能跑只是没润色）
mkdir -p ~/.config/sensevoice-vibe
cp config/llm.env.example ~/.config/sensevoice-vibe/llm.env
$EDITOR ~/.config/sensevoice-vibe/llm.env  # 填 BASE_URL 和 API_KEY

./enroll_speaker_f8.sh      # 录 10 秒注册你的声纹
./configure_f8.sh           # 注册 GNOME F8 热键
./toggle_resident_f8.sh on  # 启动常驻服务
# 现在按 F8，对麦克风说话，文字直接出现在你光标处
```

**首次启动**模型加载约 15 秒。之后按 F8 是即时的。

---

## 📋 完整安装 (Runbook)

> 以下步骤是**确定性可执行**的——AI Agent 或新用户按顺序执行，每一步都有**验证命令**和**期望输出**。任何一步验证失败立即停止，不要继续。

### 前置依赖（操作系统层）

Ubuntu / Debian：
```bash
sudo apt-get update && sudo apt-get install -y \
    python3-venv python3-pip git \
    ibus ibus-gtk3 ibus-gtk4 \
    python3-gi gir1.2-ibus-1.0 \
    portaudio19-dev libsndfile1 \
    alsa-utils pulseaudio-utils
```

> ⚠️ `python3-gi` + `gir1.2-ibus-1.0` 是必需的——IBus engine 用 `/usr/bin/python3`（不是 venv）调 GObject Introspection。

**验证**：
```bash
python3 -c "import sys; assert sys.version_info >= (3,10), '需要 Python 3.10+'; print('python OK')"
/usr/bin/python3 -c "from gi.repository import IBus; print('gi+IBus OK')"
ibus --version | head -1
arecord --version | head -1
# 期望：四行 OK / 版本号，无错误
```

> 💡 **不想分步装？** 直接跑 `./install.sh` 一键完成 Step 1~7，自动检查系统依赖、装 venv、拉模型、注册 IBus、绑定 F8。

### Step 1：克隆仓库

```bash
git clone https://github.com/Henderson11/sensevoice-vibe.git
cd sensevoice-vibe
```

**验证**：
```bash
test -f download_models.sh && test -f setup.sh && test -d sensevoice/ && echo "OK"
# 期望：OK
```

### Step 2：建 venv + 装 Python 依赖 + 注册 funasr patch

```bash
./setup.sh
```

`setup.sh` 内部做了：建 venv → 装 PyTorch（自动检测 GPU）→ 装 requirements.txt 全部依赖 → 把 ERes2NetV2 patch 到 FunASR → 验证关键 import → 检查 `/usr/bin/python3 + gi`。

**验证**（setup.sh 末尾会自动跑这个，应全部 `ok`）：
```bash
.venv/bin/python -c "
import funasr, funasr_onnx, modelscope, huggingface_hub, webrtcvad, soundfile, torch
print('all imports OK')
print('funasr', funasr.__version__, '/ torch', torch.__version__)
"
# 期望：all imports OK + 版本号
```

### Step 3：下载模型

```bash
./download_models.sh
```

约 1.2GB，下载时间取决于带宽（国内 ModelScope 通常 30 秒~2 分钟）。

**验证**：
```bash
ls -lh models/sensevoice-small/model.pt models/eres2netv2/model.pt
# 期望：两个文件都存在，分别约 936M 和 70M
```

如果 ModelScope 拉失败，脚本会自动回退到 HuggingFace（需要科学上网）。

### Step 4：装 IBus 引擎

```bash
./install_ibus_engine.sh
```

**验证**：
```bash
ibus list-engine 2>/dev/null | grep -i sensevoice
# 期望：sensevoice-voice - SenseVoice Voice Input
```

如果 ibus 没列出来，重启 IBus：
```bash
ibus restart
```

### Step 5：配置 LLM API key

```bash
mkdir -p ~/.config/sensevoice-vibe
cp config/llm.env.example ~/.config/sensevoice-vibe/llm.env
chmod 600 ~/.config/sensevoice-vibe/llm.env
```

编辑 `~/.config/sensevoice-vibe/llm.env`，把这两行填实际值：

```bash
SENSEVOICE_POST_LLM_BASE_URL=<YOUR_LLM_BASE_URL>      # 例如 https://api.deepseek.com/v1
SENSEVOICE_POST_LLM_API_KEY=<YOUR_LLM_API_KEY>        # 例如 sk-xxxxxxxx
SENSEVOICE_POST_LLM_MODEL=DeepSeek-V3.2               # 你的服务支持的 model id
```

**验证**（确认 API 可达）：
```bash
set -a; source ~/.config/sensevoice-vibe/llm.env; set +a
curl -sS -m 5 -H "Authorization: Bearer $SENSEVOICE_POST_LLM_API_KEY" \
  "$SENSEVOICE_POST_LLM_BASE_URL/models" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('models:', [m['id'] for m in d['data']][:5])"
# 期望：models: ['DeepSeek-V3.2', ...] 类似列表
```

> **不想用 LLM 润色？** 把 `SENSEVOICE_POST_LLM_ENABLE=0` 即可，仍能正常 ASR。

### Step 6：注册你的声纹（声纹门禁需要）

```bash
./enroll_speaker_f8.sh
```

按提示对麦克风说话 10 秒（**说什么内容都行**，重要的是声音特征）。

**验证**：
```bash
ls -lh ~/.local/state/sensevoice-vibe/speaker_enroll.d/
# 期望：至少一个 *.wav 文件，约 320KB ~ 1MB
```

> **不想用声纹门禁？**（单人独占的电脑可以关）：编辑 `llm.env` 把 `SENSEVOICE_SPK_ENABLE=0`。

### Step 7：注册 F8 热键 + 启动常驻服务

```bash
./configure_f8.sh                # 注册 GNOME 全局快捷键 F8
./toggle_resident_f8.sh on       # 冷启动常驻服务
```

**验证**：
```bash
sleep 10 && ./toggle_resident_f8.sh status
# 期望：running=1 ready=1 active=1 pid=<某个数字>

grep -E "MODEL_READY|POST_LLM enabled" ~/.local/state/sensevoice-vibe/stream_vad.log | tail -3
# 期望（关键字）：
#   MODEL_READY
#   POST_LLM enabled=1 model=DeepSeek-V3.2 reason=ready
```

### Step 8：端到端验证

打开任意输入框（浏览器地址栏 / 终端 / 编辑器），按 **F8**，对麦克风说一句话，比如：
> "现在是测试语音输入的功能"

应该看到文字直接出现在光标处。

查看日志确认：
```bash
tail -3 ~/.local/state/sensevoice-vibe/stream_vad.log | grep "FINAL mode"
# 期望：FINAL mode=PARTIAL text=现在是测试语音输入的功能
```

🎉 **如果看到 `FINAL mode=...` 行 = 部署成功**。

---

## 🎛 配置

所有配置集中在 `~/.config/sensevoice-vibe/llm.env`。修改后必须重启服务：

```bash
./toggle_resident_f8.sh restart
```

> 💡 也可以按 F8——脚本会自动检测 `llm.env` 比进程新，自动重启。

### 关键配置项

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `SENSEVOICE_POST_LLM_ENABLE` | LLM 润色总开关 | 1 |
| `SENSEVOICE_POST_LLM_BASE_URL` | LLM API URL（OpenAI 兼容） | 必填 |
| `SENSEVOICE_POST_LLM_API_KEY` | API key | 必填 |
| `SENSEVOICE_POST_LLM_MODEL` | 主模型 id | DeepSeek-V3.2 |
| `SENSEVOICE_POST_LLM_MODE` | 润色策略：`polish_coding_aggressive`/`polish_coding`/`polish` | aggressive |
| `SENSEVOICE_SPK_ENABLE` | 声纹门禁开关 | 1 |
| `SENSEVOICE_SPK_THRESHOLD` | 相似度阈值，高=严 | 0.60 |
| `SENSEVOICE_STREAM_ENDPOINT_MS` | 静音判句尾阈值 (ms) | 1500 |
| `SENSEVOICE_STREAM_MAX_SEGMENT_MS` | 单段最大时长 (ms) | 30000 |
| `SENSEVOICE_PROJECT_ROOT` | 项目术语表扫描目录 | $HOME |
| `SENSEVOICE_INJECT_MODE` | `ibus`（推荐）/ `clipboard`（兼容） | ibus |
| `SENSEVOICE_LANGUAGE` | `auto`/`zh`/`en`/`yue`/`ja`/`ko` | auto |

完整列表见 [`config/llm.env.example`](config/llm.env.example)。

### 切换模型

```bash
sed -i 's/^SENSEVOICE_POST_LLM_MODEL=.*/SENSEVOICE_POST_LLM_MODEL=GLM-5.1-FP8/' \
    ~/.config/sensevoice-vibe/llm.env
./toggle_resident_f8.sh restart
```

### 不要 LLM 润色（纯本地）

```bash
sed -i 's/^SENSEVOICE_POST_LLM_ENABLE=.*/SENSEVOICE_POST_LLM_ENABLE=0/' \
    ~/.config/sensevoice-vibe/llm.env
./toggle_resident_f8.sh restart
```

---

## 📊 性能与延迟

实测（i7-13700H CPU 单核 ONNX INT8，无 GPU）：

| 阶段 | 时间 |
|------|------|
| ASR 推理（一句 5 秒音频） | ~500 ms |
| 声纹验证 | ~200 ms |
| LLM 润色（DeepSeek 内网） | 1000~2000 ms |
| IBus 注入 | ~50 ms |
| **尾延迟**（说完最后一个字 → 出字） | **~3.5 秒**（含 1.5s VAD endpoint 等待） |

**模型大小**：

| 模型 | 大小 | 参数量 |
|------|------|--------|
| SenseVoice-Small (PT) | 936 MB | 234M |
| SenseVoice-Small (ONNX INT8) | 232 MB | 234M (量化) |
| ERes2NetV2 (声纹) | 70 MB | - |
| CAM++ (声纹备用) | 28 MB | - |

---

## 🩺 故障排查

| 症状 | 原因 | 修复 |
|------|------|------|
| F8 没反应 | 热键被其他程序占用 / GNOME 快捷键未注册 | `./configure_f8.sh` 重注册；或 GNOME Settings > Keyboard 检查 |
| `running=0` | 常驻进程没起来 | `cat ~/.local/state/sensevoice-vibe/resident.stdout.log` 看错误 |
| `ready=0` 卡住 | 模型加载失败 | 检查 `models/sensevoice-small/model.pt` 是否完整 |
| 说话没字出来 | 声纹被门禁挡了 | 看日志 `DROP_FINAL_SPK score=...`；如果 score 低→重新 enroll，如果 score ≥ 0.5→把阈值调低 |
| 字出来了但全是错别字 | LLM 润色没生效 | `grep "POST_LLM enabled" ~/.local/state/sensevoice-vibe/stream_vad.log`，看 reason= |
| LLM 调用超时 | 网络慢 / 服务过载 | `POST_LLM_TIMEOUT_MS` 加大；或换 fallback URL |
| 改了 `llm.env` 不生效 | 进程没重启 | `./toggle_resident_f8.sh restart`（**F8 默认只切换录音状态，不重启进程**——但脚本会自动检测 mtime 漂移触发 restart） |
| 说话半截就出字 | VAD endpoint 太短 | `SENSEVOICE_STREAM_ENDPOINT_MS` 加大（默认 1500） |
| 长句被切两段 | 段长超过 MAX_SEGMENT | `SENSEVOICE_STREAM_MAX_SEGMENT_MS` 加大（默认 30000） |
| 旁人说话也被识别 | 声纹门禁阈值太低 | `SENSEVOICE_SPK_THRESHOLD` 提高（如 0.6 → 0.7） |

### 实时观测日志

```bash
tail -f ~/.local/state/sensevoice-vibe/stream_vad.log
```

关键日志关键字：

```
SEG id=N            ← 新段开始
SPEECH_START        ← 检测到说话
SPK_PASS  score=X   ← 声纹通过
DROP_FINAL_SPK      ← 声纹拒绝（不是你）
CONF_SCORE score=X  ← ASR 置信度
POST_LLM_APPLY      ← LLM 改了文本
POST_LLM_PASS       ← LLM 觉得不用改
FINAL mode=...      ← 最终注入
```

---

## 🛣 路线图

- [ ] 流式 ASR 输出（VAD 切完前就开始 partial 注入，缩短尾延迟）
- [ ] Wayland 原生支持（当前依赖 IBus + X11/XWayland）
- [ ] Web UI 实时监控（VAD 波形 / 置信度 / 模型状态）
- [ ] 多人多声纹（家庭/团队场景，按声纹分用户）
- [ ] 命令模式（识别"打开浏览器"等指令而非转文字）
- [ ] macOS / Windows 移植

---

## 🤝 贡献

欢迎 PR！流程：

1. Fork → 建个特性分支 `feat/xxx`
2. 改代码，跑一下 `./toggle_resident_f8.sh restart` 自测
3. 提交 PR，描述里写"为什么"+"怎么验证"

调试时建议把 `SENSEVOICE_DEBUG_INJECT=1` 打开（默认开），日志最详细。

---

## 📜 许可证 & 鸣谢

本项目代码：**MIT License**

依赖的开源项目（许可证遵循各自原协议）：

- [FunASR](https://github.com/modelscope/FunASR) — 阿里达摩院 / 通义实验室，SenseVoice ASR 模型
- [3D-Speaker](https://github.com/modelscope/3D-Speaker) — 阿里通义实验室，ERes2NetV2 / CAM++ 声纹模型
- [IBus](https://github.com/ibus/ibus) — Linux 输入法框架
- [WebRTC VAD](https://github.com/wiseman/py-webrtcvad) — Google WebRTC 项目的 VAD
- [DeepSeek API](https://platform.deepseek.com/) — LLM 后处理（其他 OpenAI 兼容 API 也可）

特别感谢 [SenseVoice 论文](https://arxiv.org/abs/2407.04051) 给了一个值得本地部署的高质量多语 ASR 模型。

---

<div align="center">

**如果这个项目帮到你，给个 ⭐ Star 让更多人发现它**

[Issues](https://github.com/Henderson11/sensevoice-vibe/issues) · [Discussions](https://github.com/Henderson11/sensevoice-vibe/discussions) · [PRs](https://github.com/Henderson11/sensevoice-vibe/pulls)

</div>
