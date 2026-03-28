# SenseVoice 语音输入系统

基于 FunASR SenseVoice 的实时语音输入系统，支持 IBus 直接注入、LLM 后处理润色、声纹门禁。

## 架构

```
麦克风 → VAD(webrtcvad) → ASR(SenseVoice) → 声纹验证(ECAPA-TDNN)
  → 置信度路由 → LLM润色(DeepSeek) → IBus commit_text → 输入框
```

## 模块结构

```
├── stream_vad_realtime.py          # 核心管线：VAD + ASR + 声纹 + LLM后处理
├── ibus-sensevoice/                # IBus 注入引擎
│   ├── sensevoice_engine.py        #   socket → commit_text
│   ├── sensevoice-voice.xml        #   IBus 组件注册
│   └── test_inject.py              #   注入测试
├── toggle_resident_f8.sh           # F8 热键控制（常驻进程 + 信号切换）
├── configure_f8.sh                 # GNOME 快捷键配置
├── enroll_speaker_f8.sh            # 声纹模板采集
├── send_to_focus_ydotool.sh        # 剪贴板注入后端（兼容模式）
├── install_ibus_engine.sh          # IBus 引擎安装
├── install_autostart_service.sh    # systemd 自启服务安装
├── config/
│   ├── llm.env.example             # 配置模板（不含密钥）
│   └── sensevoice-vibe.service.example  # systemd 服务模板
├── hotwords_coding_zh.txt          # 编程术语热词表
└── requirements.txt                # Python 依赖
```

## 安装

```bash
# 1. 创建虚拟环境并安装依赖
./setup.sh
# 或手动：
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 下载 ASR 模型（首次自动下载到 ~/.cache/modelscope/）
# 或手动指定本地路径

# 3. 安装 IBus 引擎（ibus 注入模式需要）
./install_ibus_engine.sh

# 4. 复制配置文件并填入 API 密钥
cp config/llm.env.example ~/.config/sensevoice-vibe/llm.env
# 编辑 llm.env 填入 POST_LLM_BASE_URL 和 API_KEY

# 5. 配置 F8 快捷键
./configure_f8.sh

# 6.（可选）采集声纹模板
./enroll_speaker_f8.sh

# 7.（可选）安装开机自启服务
./install_autostart_service.sh
```

## 配置

**所有配置集中在一个文件：`~/.config/sensevoice-vibe/llm.env`**

F8 快捷键和 toggle 脚本均从此文件读取，修改后重启服务即可生效。

关键配置项：

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `SENSEVOICE_INJECT_MODE` | 注入模式 (ibus/clipboard) | ibus |
| `SENSEVOICE_STREAM_ENDPOINT_MS` | 停顿断句阈值 (ms) | 900 |
| `SENSEVOICE_STREAM_MAX_SEGMENT_MS` | 单段最大时长 (ms) | 20000 |
| `SENSEVOICE_SPK_ENABLE` | 声纹门禁开关 | 1 |
| `SENSEVOICE_SPK_THRESHOLD` | 声纹相似度阈值 | 0.45 |
| `SENSEVOICE_POST_LLM_ENABLE` | LLM 润色开关 | 1 |
| `SENSEVOICE_POST_LLM_MODEL` | 润色模型 | DeepSeek-V3.2 |
| `SENSEVOICE_LANGUAGE` | 识别语言 (auto/zh/en) | auto |

## 使用

```bash
# F8 开启/关闭语音输入（GNOME 全局快捷键）
# 第一次 F8：开始监听
# 说话 → 自动断句 → 润色 → 注入到当前输入框
# 第二次 F8：停止监听

# 手动启动（调试用）
./toggle_resident_f8.sh on

# 查看状态
./toggle_resident_f8.sh status

# 查看实时日志
tail -f ~/.local/state/sensevoice-vibe/stream_vad.log
```

## 依赖

- Python 3.10+
- FunASR 1.3.1 (SenseVoice ASR)
- SpeechBrain 1.0.3 (ECAPA-TDNN 声纹验证)
- PyTorch 2.10+ (CPU 或 GPU)
- webrtcvad (VAD)
- IBus (GNOME 输入法框架)
- OpenAI 兼容 API (LLM 后处理)
