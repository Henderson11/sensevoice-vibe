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
| `SENSEVOICE_STREAM_ENDPOINT_MS` | 停顿断句阈值 (ms) | 1500 |
| `SENSEVOICE_STREAM_MAX_SEGMENT_MS` | 单段最大时长 (ms) | 20000 |
| `SENSEVOICE_SPK_ENABLE` | 声纹门禁开关 | 1 |
| `SENSEVOICE_SPK_THRESHOLD` | 声纹相似度阈值 | 0.45 |
| `SENSEVOICE_POST_LLM_ENABLE` | LLM 润色开关 | 1 |
| `SENSEVOICE_POST_LLM_MODEL` | 润色模型 | DeepSeek-V3.2 |
| `SENSEVOICE_LANGUAGE` | 识别语言 (auto/zh/en) | auto |

## LLM 后处理润色配置（内网 GPU 代理 - 部署 Runbook）

> 本节是给 **AI Agent 或新用户**的可执行部署手册。按顺序执行下面 6 步，每步都有验证命令和期望输出。任何一步验证失败立即停止，不要继续往下走。

ASR 输出会经过一次 LLM 润色（修错别字、补标点、规范术语），由 OpenAI 兼容协议调用。本节配置的是**内网 SpiritX 代理**（DeepSeek-V3.2 / GLM-5.1-FP8），并用公网 DeepSeek 官方 API 作为 fallback。

### 前置条件

- 已经按上节"安装"完成 1~3 步（venv、ASR 模型、IBus 引擎）
- 能访问内网 `<INTERNAL_LLM_HOST>:31091`（公司内网或 VPN 已连接）
- 已经从管理员处拿到 SpiritX 代理的 API key（`sk-` 开头）和 DeepSeek 官方 API key

---

### Step 1：创建配置目录

```bash
mkdir -p ~/.config/sensevoice-vibe
```

**验证**：
```bash
test -d ~/.config/sensevoice-vibe && echo OK
# 期望输出：OK
```

---

### Step 2：写入 `llm.env` 配置文件

把下面整段命令直接复制到终端执行。**先把 `<INTERNAL_KEY>` 和 `<DEEPSEEK_KEY>` 替换为真实 key**，然后执行：

```bash
INTERNAL_KEY='<INTERNAL_KEY>'         # SpiritX 代理 key，向管理员申请
DEEPSEEK_KEY='<DEEPSEEK_KEY>'         # 公网 DeepSeek 官方 key，作为 fallback

cat > ~/.config/sensevoice-vibe/llm.env <<EOF
# === LLM 后处理：主链路（内网 SpiritX 代理）===
SENSEVOICE_POST_LLM_ENABLE=1
SENSEVOICE_POST_LLM_BASE_URL=http://<INTERNAL_LLM_HOST>:31091/<YOUR_LLM_PROXY_PATH>/v1
SENSEVOICE_POST_LLM_API_KEY=$INTERNAL_KEY
SENSEVOICE_POST_LLM_MODEL=DeepSeek-V3.2
# 当前内网仅这两个模型可选: DeepSeek-V3.2 | GLM-5.1-FP8

# === LLM 后处理：fallback 链路（公网 DeepSeek 官方 API）===
SENSEVOICE_POST_LLM_FALLBACK_BASE_URL=https://api.deepseek.com/v1
SENSEVOICE_POST_LLM_FALLBACK_API_KEY=$DEEPSEEK_KEY
SENSEVOICE_POST_LLM_FALLBACK_MODEL=deepseek-chat

# === 润色行为（短语境编程模式）===
SENSEVOICE_POST_LLM_MODE=polish_coding_aggressive
SENSEVOICE_POST_LLM_TIMEOUT_MS=1800
SENSEVOICE_POST_LLM_MAX_TOKENS=72
SENSEVOICE_POST_LLM_TEMPERATURE=0
SENSEVOICE_POST_LLM_MIN_CHARS=5
SENSEVOICE_POST_LLM_DYNAMIC_MAX_TOKENS=1
SENSEVOICE_POST_LLM_OUTPUT_TOKEN_FACTOR=0.7

# === 熔断与缓存 ===
SENSEVOICE_POST_LLM_CIRCUIT_MAX_FAILS=4
SENSEVOICE_POST_LLM_CIRCUIT_COOLDOWN_SEC=25
SENSEVOICE_POST_LLM_HARD_COOLDOWN_SEC=300
SENSEVOICE_POST_LLM_RETRY_ON_TIMEOUT=1
SENSEVOICE_POST_LLM_RETRY_BACKOFF_MS=80
SENSEVOICE_POST_LLM_CACHE_TTL_SEC=300
SENSEVOICE_POST_LLM_CACHE_MAX_ENTRIES=120

# === 置信度路由（高置信度仍走 LLM，因为是 aggressive 模式）===
SENSEVOICE_CONF_ROUTE_ENABLE=1
SENSEVOICE_CONF_ROUTE_HIGH=0.42
SENSEVOICE_CONF_ROUTE_LOW=0.30

# === 安全权限 ===
EOF
chmod 600 ~/.config/sensevoice-vibe/llm.env
```

**验证**（确认两个 key 都已写入且非空）：
```bash
grep -E "^SENSEVOICE_POST_LLM_(API_KEY|FALLBACK_API_KEY)=" ~/.config/sensevoice-vibe/llm.env \
  | awk -F= '{ if (length($2) < 20) print "FAIL: "$1" 长度异常 ("length($2)")"; else print "OK: "$1" 长度="length($2) }'
# 期望输出（两行 OK）：
#   OK: SENSEVOICE_POST_LLM_API_KEY 长度=51
#   OK: SENSEVOICE_POST_LLM_FALLBACK_API_KEY 长度=35
```

---

### Step 3：验证内网代理 API 可达

```bash
set -a; source ~/.config/sensevoice-vibe/llm.env; set +a
curl -sS -m 5 -H "Authorization: Bearer $SENSEVOICE_POST_LLM_API_KEY" \
  "$SENSEVOICE_POST_LLM_BASE_URL/models" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ids = [m['id'] for m in d.get('data', [])]
print('available_models:', sorted(ids))
assert 'DeepSeek-V3.2' in ids, 'DeepSeek-V3.2 不在可用列表里'
print('OK: DeepSeek-V3.2 可用')
"
```

**期望输出**（包含且仅以 `OK:` 结尾即通过）：
```
available_models: ['DeepSeek-V3.2', 'GLM-5.1-FP8']
OK: DeepSeek-V3.2 可用
```

**失败处理**：
- `Could not resolve host` / `Connection refused` → 检查内网/VPN 连通性，`ping <INTERNAL_LLM_HOST>`
- `401 Unauthorized` → API key 错误，回到 Step 2 重写
- `available_models` 不含 `DeepSeek-V3.2` → 内网模型变更，把 Step 2 的 `SENSEVOICE_POST_LLM_MODEL` 改成实际可用的（如 `GLM-5.1-FP8`）

---

### Step 4：发一次 chat completion 实际测试

```bash
set -a; source ~/.config/sensevoice-vibe/llm.env; set +a
curl -sS -m 8 "$SENSEVOICE_POST_LLM_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $SENSEVOICE_POST_LLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$SENSEVOICE_POST_LLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"把下面这句话修一下错别字，只回结果不解释：缓存甚至算缓村\"}],\"max_tokens\":40,\"temperature\":0}" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
out = d['choices'][0]['message']['content'].strip()
print('LLM 返回:', repr(out))
assert '缓存' in out, '返回内容不含\"缓存\"，LLM 行为异常'
print('OK: 链路通且模型行为正常')
"
```

**期望输出**：
```
LLM 返回: '缓存甚至算缓存'
OK: 链路通且模型行为正常
```

---

### Step 5：重启常驻服务以应用新配置

```bash
./toggle_resident_f8.sh restart
```

**验证**（等模型加载约 5~15 秒后）：
```bash
sleep 10 && ./toggle_resident_f8.sh status
# 期望输出：running=1 ready=1 active=1 pid=<新PID>
```

**进一步验证**（确认 LLM 模块已就绪、模型名正确）：
```bash
grep -E "POST_LLM enabled" ~/.local/state/sensevoice-vibe/stream_vad.log | tail -1
# 期望输出（关键字段：enabled=1, model=DeepSeek-V3.2, reason=ready）：
# 2026-XX-XX HH:MM:SS [INFO] POST_LLM enabled=1 model=DeepSeek-V3.2 fallback=deepseek-chat reason=ready:model=DeepSeek-V3.2,fallback=deepseek-chat
```

---

### Step 6：端到端验证（说一句话，看 LLM 是否润色）

按 F8 开始录音，对着麦克风说一句**故意带错的话**，比如：
> "这个缓村的设计需要再优化"

然后看日志：
```bash
tail -5 ~/.local/state/sensevoice-vibe/stream_vad.log | grep POST_LLM_APPLY
# 期望看到一行：
# POST_LLM_APPLY src=这个缓村的设计需要再优化。 dst=这个缓存的设计需要再优化。 route=high
```

如果看到 `POST_LLM_APPLY` 且 `src` 和 `dst` 不同 → **部署成功**。

---

### 失败诊断速查表

| 日志关键字 | 含义 | 处理 |
|-----------|------|------|
| `POST_LLM enabled=0 reason=disabled` | 配置里 `ENABLE` 为 0 | 检查 `llm.env` 的 `SENSEVOICE_POST_LLM_ENABLE=1` |
| `POST_LLM enabled=0 reason=missing_*` | API key 或 base_url 缺失 | 回到 Step 2 重写 |
| `POST_LLM_SKIP reason=circuit_open` | 熔断中 | 等待 25 秒后自动恢复，或检查内网连通 |
| `POST_LLM_SKIP reason=text_too_short` | 文本短于 5 字 | 正常行为，无需处理 |
| 完全没有 `POST_LLM*` 行 | 服务没起来或没说话 | `./toggle_resident_f8.sh status` 看 ready/active |

---

### 切换模型（不需要重新部署）

要把润色模型从 DeepSeek 换成 GLM-5.1：
```bash
sed -i 's/^SENSEVOICE_POST_LLM_MODEL=.*/SENSEVOICE_POST_LLM_MODEL=GLM-5.1-FP8/' \
  ~/.config/sensevoice-vibe/llm.env
./toggle_resident_f8.sh restart
```

按 F8 时脚本也会**自动检测 `llm.env` 改动**并重启进程，无需手动 `restart`。

> ⚠️ **关键**：F8 的 `on/off/toggle` 默认只切换录音 active 位，**不重启进程**。修改 `llm.env` 后必须显式 `restart` 或让脚本自动检测漂移触发重启，否则改动不会生效（环境变量在进程启动时就固化在内存里了）。

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
