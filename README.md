<div align="center">

# 🎤 SenseVoice Vibe

**给程序员的语音输入：按 F8 开口说，润色好的文字直接出现在光标处**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![ASR](https://img.shields.io/badge/ASR-Qwen--Audio_3.1-FF6B6B)](https://help.aliyun.com/zh/model-studio/qwen-audio-3-1-asr-flash-message)
[![Speaker](https://img.shields.io/badge/Speaker-ERes2NetV2-4ECDC4)](https://www.modelscope.cn/models/iic/speech_eres2netv2_sv_zh-cn_16k-common)
[![Platform](https://img.shields.io/badge/Platform-Linux_IBus_|_Windows-FCC419)](#windows-版winvoice)

</div>

## 是什么

常驻后台的语音输入：麦克风一直在听，只有**你本人**说的话会被识别（声纹门禁），经阿里云百炼语音识别转成文字，再由大模型修正同音字、术语和标点，最后通过 IBus 直接输入到当前光标处，不占用剪贴板。

面向长时间口述代码、文档、技术问答的场景，办公室里同事说话不会被误输入。

> 项目名沿用早期"本地 SenseVoice 识别"方案；现在识别走百炼云端，本地只跑声纹模型。

## 特性

- **云端识别 + 自动兜底**：主模型 `qwen-audio-3.1-asr-flash-message`（带前文上下文）或 `qwen-audio-3.1-asr-flash`；主模型慢了就并发请求备用模型，谁先返回用谁，`./switch_asr.sh` 一键切换
- **前文上下文**：把最近几句已输入的话传给识别模型，跨句的专有名词、术语更容易认对
- **声纹门禁**：本地 ERes2NetV2，说话过程中每 5 秒后台比对一次，说完几乎不用再等；旁人的语音不会上传云端
- **长口述**：单段最长 3 分钟，快到上限时在短停顿处收尾，不会在词中间截断
- **LLM 润色**：OpenAI 兼容接口，编程语境下修正同音字、术语、口误和标点；主链路失败自动切备用，熔断保护
- **项目术语表**：从你的代码目录提取标识符，作为润色时的术语提示
- **没出字兜底**：可能是你说的、但没出字的语音会自动备份；弹出带"重新识别并输入"按钮的通知，`Shift+F8` 查看最近语音（时间、时长、结果），可重试、播放、复制，不用再说一遍
- **IBus 原生输入**：`commit_text` 直接写入焦点应用，剪贴板和键盘状态不受影响
- **Windows 版**：`windows/` 下的 WinVoice，同一套识别、润色、声纹逻辑

## 工作流程

```
麦克风 ─ arecord ─▶ 采集线程 ─▶ webrtcvad 分段 ──────────────▶ 一段话结束（停顿 1.5s / 上限前软切）
                                 │ 每满 5s                         │
                                 ▼                                 ▼
                          后台声纹比对（ERes2NetV2）─────▶ 声纹判定 ──✗──▶ 丢弃（接近阈值的存入最近语音，可重试）
                                                                   │✓
                                                                   ▼
                                         百炼云端识别（主模型 + 并发兜底，带前文与热词）
                                                                   ▼
                                         LLM 润色（项目术语提示）→ 术语标准化
                                                                   ▼
                                         IBus commit_text → 当前光标处
```

## 安装（Linux）

**前置**：GNOME/KDE 等使用 IBus 的桌面、一个可用的麦克风、[阿里云百炼](https://bailian.console.aliyun.com/) API Key、一个 OpenAI 兼容的 LLM 接口（润色用，也可以只用百炼的兼容模式）。

```bash
git clone https://github.com/Henderson11/sensevoice-vibe.git
cd sensevoice-vibe
./install.sh                     # 检查系统依赖 → .venv → 声纹模型 → 配置 → IBus 引擎 → 快捷键
$EDITOR ~/.config/sensevoice-vibe/llm.env   # 填 SENSEVOICE_BAILIAN_API_KEY 与 SENSEVOICE_POST_LLM_*
./enroll_speaker_f8.sh           # 录 20 秒声纹，正常说话即可
./install_autostart_service.sh   # 安装 systemd 用户服务并启动（开机自启）
```

`install.sh` 缺系统包时会给出 `apt install` 命令：`python3-venv alsa-utils ibus python3-gi gir1.2-ibus-1.0 libnotify-bin zenity`（可选 `wl-clipboard`）。

**验证**：

```bash
systemctl --user status sensevoice-vibe
tail -f ~/.local/state/sensevoice-vibe/stream_vad.log
# 启动时应看到 SPK_GATE enabled=1 ... enroll_n=1、MODEL_READY、DAEMON_START
# 说一句话后应看到 SPK_PASS → ASR_USAGE → POST_LLM_APPLY/PASS → FINAL mode=COMMIT text=...
```

## 使用

| 操作 | 作用 |
|---|---|
| `F8` | 开关收音（daemon 常驻，开关只是暂停/恢复） |
| `Shift+F8` | 最近语音面板：看最近 20 条的时间、时长、结果，选一条可重新识别并输入 / 播放 / 复制 |
| 通知里的"重新识别并输入" | 声纹没过或识别失败时弹出，点一下跳过声纹重新识别，输入到当前光标 |
| `./switch_asr.sh message\|standard` | 切换 Message 版 / 普通版识别模型并重启 |
| `./enroll_speaker_f8.sh 名字` | 再录一段声纹模板（如远距离、不同语气），录完自动重启生效 |
| `./toggle_resident_f8.sh status\|restart` | 查看状态 / 重启 daemon |

## 配置

全部配置在 `~/.config/sensevoice-vibe/llm.env`，模板见 [config/llm.env.example](config/llm.env.example)，改完 `systemctl --user restart sensevoice-vibe` 生效。

| 分组 | 主要配置项 | 说明 |
|---|---|---|
| 识别 | `SENSEVOICE_BAILIAN_MODEL` / `_HEDGE_MODELS` | 主模型与兜底模型；超时和兜底阈值随音频时长自动放大 |
| 前文 | `SENSEVOICE_BAILIAN_CONTEXT_TURNS` | Message 版带最近几句作前文，0 关闭 |
| 分段 | `SENSEVOICE_STREAM_ENDPOINT_MS` / `_MAX_SEGMENT_MS` | 停顿多久算说完 / 单段上限（默认 1.5s / 3 分钟） |
| 声纹 | `SENSEVOICE_SPK_THRESHOLD` / `_WINDOW_MS` | 阈值（默认 0.50）/ 分窗长度，0 表示说完再整段判定 |
| 兜底 | `SENSEVOICE_RECENT_*` | 备份条数、天数、存盘与弹通知的门槛 |
| 润色 | `SENSEVOICE_POST_LLM_*` | 接口、模型、模式、备用链路、熔断 |
| 术语 | `SENSEVOICE_PROJECT_ROOT` | 提取术语的代码目录 |

**热词表**（可选）：`config/hotwords.yaml`，含个人/项目私有词汇，已加入 `.gitignore`，不会被提交。格式：

```yaml
common_errors:            # 经常被听错的词（最高权重）
  terms: [group_mask, PyTorch]
apis:
  terms: [commit_text, notify-send]
projects:
  terms: [WinVoice]
frameworks:
  terms: [FunASR, IBus]
```

修改后下一句即生效，无需重启。

**费用参考**（百炼北京地域原价）：Message 版输入 ¥6 / 输出 ¥4.5 每百万 token，普通版 ¥0.8 / ¥2.7；音频按 12.5 token/秒计。30 秒语音约 ¥0.0034（Message）/ ¥0.0006（普通版）。

## Windows 版（WinVoice）

`windows/` 是同一套逻辑的 Windows 移植：双击 Ctrl 开关、Unicode 键盘事件输入、Toast 通知、声纹门禁，识别客户端与润色模块和 Linux 共用源码。

`windows/deploy_winpc.sh` 通过 ssh 把代码和配置同步到一台 Windows 机器（ssh 别名与目标目录见脚本开头），配置从 Linux 的 `llm.env` 摘取生成：

```bash
./windows/deploy_winpc.sh            # 首次：安装私有 Python 3.12、依赖、声纹模型
./windows/deploy_winpc.sh --sync     # 只同步代码与配置
./windows/deploy_winpc.sh --deps     # 同步并补装 requirements 里新增的依赖
```

部署前会比对上次部署的文件哈希，发现有人直接在 Windows 上改过就中止（`--force` 才覆盖）。完整部署会把 Linux 的声纹模板拷过去；Windows 上单独录过模板（笔记本麦克风声学不同时建议录）后，补依赖请用 `--deps`，不要再跑完整部署。

## 日志与排查

| 位置 | 内容 |
|---|---|
| `~/.local/state/sensevoice-vibe/stream_vad.log` | 主日志：分段、声纹（`SPK_WINDOWS` / `SPK_TIME`）、识别（`ASR_USAGE` / `ASR_WIN` / `ASR_FAILED`）、润色、输入 |
| `~/.local/state/sensevoice-vibe/post_compare.jsonl` | 逐段记录：识别原文、润色结果、最终输入、丢弃原因 |
| `~/.local/state/sensevoice-vibe/recent_audio/` | 最近语音备份（`recent_voice.py --print` 在终端查看） |

| 症状 | 原因 | 处理 |
|---|---|---|
| 说话没反应 | 收音被关 / 声纹没过 / 识别失败 | 按 `Shift+F8` 看最近语音的结果；`toggle_resident_f8.sh status` 看是否 `active=1` |
| 本人说话常被拒 | 麦克风或距离与注册时差别大 | 用当前麦克风再录一段：`./enroll_speaker_f8.sh near_02` |
| daemon 启动即退出，日志 `no enroll wav found` | `speaker_enroll.d/` 里没有模板 | 先运行 `./enroll_speaker_f8.sh`，再 `systemctl --user reset-failed sensevoice-vibe && systemctl --user start sensevoice-vibe` |
| 出字慢 | 主模型服务端慢 | 看 `ASR_WIN` 是否频繁由兜底模型胜出；`./switch_asr.sh standard` 换普通版 |
| 文字没打到光标处 | 焦点不在输入框 / IBus 引擎未注册 | 通知里点"复制文字"；`ibus list-engine \| grep sensevoice` 确认引擎 |

## 目录结构

```
sensevoice/            daemon 主体：pipeline.py（主循环）、asr/bailian.py（百炼客户端）、speaker/（声纹）、
                       llm/（润色）、recent.py（最近语音）、inject/ibus.py、control_socket.py
ibus-sensevoice/       IBus 引擎（接收文字并 commit_text）
windows/               WinVoice（Windows 版）与部署脚本
config/                配置模板、systemd 单元模板、通用编程词表
install.sh             一键安装        download_models.sh   下载声纹模型
enroll_speaker_f8.sh   录声纹模板      switch_asr.sh        切换识别模型
toggle_resident_f8.sh  F8 开关         recent_voice.py      Shift+F8 最近语音面板
daemon_launch.sh       systemd 启动入口（加载 llm.env）   retention_housekeep.sh  启动前清理旧日志
```

## 许可证与鸣谢

本项目代码：**MIT License**。依赖的服务与开源项目遵循各自的协议：

- [阿里云百炼](https://help.aliyun.com/zh/model-studio/) — Qwen-Audio / Fun-ASR 语音识别
- [3D-Speaker](https://github.com/modelscope/3D-Speaker) — ERes2NetV2 声纹模型
- [FunASR](https://github.com/modelscope/FunASR) — 声纹模型加载
- [IBus](https://github.com/ibus/ibus) — Linux 输入法框架
- [WebRTC VAD](https://github.com/wiseman/py-webrtcvad) — 语音活动检测

<div align="center">

[Issues](https://github.com/Henderson11/sensevoice-vibe/issues) · [Pull Requests](https://github.com/Henderson11/sensevoice-vibe/pulls)

</div>
