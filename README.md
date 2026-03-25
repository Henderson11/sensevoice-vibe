# SenseVoice Local Deployment

This folder provides a local deployment for SenseVoice using FunASR.

## Note on "SenseVoice Large 2.0"
As of 2026-02-21, public downloadable checkpoints on ModelScope are `iic/SenseVoiceSmall` and `iic/SenseVoiceSmall-onnx`.
If you have a private/local `SenseVoice Large 2.0` checkpoint, pass it with `--model /path/to/model_dir`.

## 1) Setup

```bash
cd /home/dell/mosim_workspace/work/sensevoice-local-small
./setup.sh
```

## 1.5) Configure F8 Resident Toggle

This config is ready even before microphone arrives.

```bash
cd /home/dell/mosim_workspace/work/sensevoice-local-small
./configure_f8.sh
```

What it configures:
- GNOME global hotkey: `F8` -> `toggle_resident_f8.sh toggle`
  - `F8` toggles listening `ON/OFF`
  - ASR model is resident (no reload on each F8)
  - while listening: speech start/end is detected automatically
  - default safety mode: inject only endpoint final text (no mid-sentence rewrite)

State files:
- `${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe`
- includes `resident.pid`, `resident.status`, `stream_vad.log`

## 1.6) Auto Start Resident Daemon (recommended)

Enable user-level autostart after desktop login:

```bash
cd /home/dell/mosim_workspace/work/sensevoice-local-small
./install_autostart_service.sh
```

Check service status:

```bash
systemctl --user status sensevoice-vibe.service
```

## 1.7) Enable Ydotool Focus Mode (one-time root setup)

`ydotool` needs `/dev/uinput` access. Run these once in your normal terminal:

```bash
sudo apt-get update
sudo apt-get install -y ydotool
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
echo 'KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/80-ydotool-uinput.rules
sudo usermod -aG input "$USER"
```

Then re-login (or reboot) so group changes take effect.

For this machine, recommended focus injection is `ydotool + clipboard paste`.
Do not rely on `ydotool type` for Chinese input scenarios.

## 1.8) Parallel A/B: Sherpa-onnx Runtime (recommended for latency test)

Install sherpa runtime and download a Chinese streaming int8 model:

```bash
cd /home/dell/mosim_workspace/work/sensevoice-local-small
./setup_sherpa.sh
```

Install sherpa resident user service:

```bash
./install_sherpa_autostart_service.sh
systemctl --user status sherpa-vibe.service
```

Optional hotkey for parallel testing (keep existing F8 unchanged):

```bash
./configure_f7_sherpa.sh
```

What it configures:
- GNOME global hotkey: `F7` -> `toggle_sherpa_resident.sh toggle`
- `F8` remains FunASR/SenseVoice resident path
- `F7` becomes sherpa-onnx resident path

Sherpa model path (default):
- `models/sherpa-onnx/sherpa-onnx-streaming-zipformer-zh-xlarge-int8-2025-06-30`
- Punctuation model (default):
- `models/sherpa-onnx/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx`

## 2) Run

```bash
./run.sh /path/to/audio.wav
```

Optional args:

```bash
./run.sh /path/to/audio.wav --device cpu --language auto --model iic/SenseVoiceSmall
```

If you have a private/local SenseVoice Large 2.0 checkpoint:

```bash
./run.sh /path/to/audio.wav --model /path/to/SenseVoice-Large-2.0
```

## 3) Output
`transcribe.py` prints JSON with:
- `text`: post-processed transcript
- `raw`: original model output

## 4) Vibe Coding Workflow (Chinese Voice Command)

Record one short command from microphone (6 seconds):

```bash
arecord -q -f S16_LE -r 16000 -c 1 -d 6 /tmp/vibe_cmd.wav
```

Transcribe it:

```bash
./run.sh /tmp/vibe_cmd.wav --language auto --disable-update
```

Extract only recognized text:

```bash
./run.sh /tmp/vibe_cmd.wav --language auto --disable-update \
  | python3 -c 'import sys,json; s=sys.stdin.read(); i=s.rfind("{"); print(json.loads(s[i:])["text"])'
```

Pass recognized command to coding tool (Codex wrapper):

```bash
CMD="$(./run.sh /tmp/vibe_cmd.wav --language auto --disable-update \
  | python3 -c 'import sys,json; s=sys.stdin.read(); i=s.rfind("{"); print(json.loads(s[i:])["text"])')"
/home/dell/mosim_workspace/scripts/code-with-codex.sh "$CMD" /home/dell/mosim_workspace
```

If default microphone is not correct, list devices:

```bash
arecord -l
```

## 5) Direct Output To CLI Chat Box (No Clipboard)

Recommended on your machine (Wayland): run coding CLI in `tmux`, then inject text via `tmux send-keys`.

Start CLI in tmux:

```bash
tmux new -s vibe
# in tmux pane:
codex
```

Find target pane id:

```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}'
```

Send one command line directly (as Enter):

```bash
printf '%s\n' '帮我在 utils/date.py 修复时区解析并补测试' \
  | ./send_to_cli_tmux.sh vibe:0.0
```

Realtime update protocol:
- `PARTIAL<TAB>...` only updates current input line
- `FINAL<TAB>...` updates current line and presses Enter

Example:

```bash
printf 'PARTIAL\t修复 utils 日期\nFINAL\t修复 utils.py 日期解析并补测试\n' \
  | ./send_to_cli_tmux.sh vibe:0.0
```

## 6) F8 Runtime Behavior

With microphone connected:
- Global `F8` (GNOME): resident daemon control mode
  - first `F8`: listening ON
  - endpoint detected: final text is written to current focus input (default no Enter)
  - second `F8`: listening OFF (daemon keeps running)

Then press `Enter` manually when you confirm the text.

If focus injection or tmux target is unavailable, text falls back to clipboard (`wl-copy`) when available.

Useful runtime overrides:

```bash
SENSEVOICE_RESIDENT=1
SENSEVOICE_STREAM_ACTIVE_ON_START=0|1
SENSEVOICE_MODEL=/home/dell/.cache/modelscope/hub/models/iic/SenseVoiceSmall
SENSEVOICE_LANGUAGE=zn|auto|en|yue|ja|ko
SENSEVOICE_FILTER_FILLERS=1|0
SENSEVOICE_PARTIAL_STRATEGY=stable2|raw
SENSEVOICE_EMIT_PARTIAL=0|1
SENSEVOICE_SESSION_NOTIFY=1|0
SENSEVOICE_TOGGLE_DEBOUNCE_MS=450
SENSEVOICE_PREFER_WTYPE=1|0
SENSEVOICE_PASTE_KEY=shift_insert|ctrl_shift_v
SENSEVOICE_YDOTOOL_KEY_DELAY_MS=20
SENSEVOICE_CLIPBOARD_SETTLE_SEC=0.03
SENSEVOICE_CLIPBOARD_VERIFY=0|1
SENSEVOICE_CLEAR_BEFORE_REPLACE=0|1
SENSEVOICE_STREAM_INDICATOR=notify_once|notify|none
SENSEVOICE_STREAM_VAD_AGGRESSIVENESS=1
SENSEVOICE_AUTO_ENTER=0|1
SENSEVOICE_ARECORD_DEVICE=hw:Microphone,0
SENSEVOICE_STREAM_START_MS=80
SENSEVOICE_STREAM_FRAME_MS=20
SENSEVOICE_STREAM_ENDPOINT_MS=500
SENSEVOICE_STREAM_MAX_SEGMENT_MS=8000
SENSEVOICE_STREAM_PARTIAL_INTERVAL_MS=280
SENSEVOICE_STREAM_MIN_PARTIAL_MS=700
SENSEVOICE_SHERPA_MODEL_DIR=/home/dell/mosim_workspace/work/sensevoice-local-small/models/sherpa-onnx/sherpa-onnx-streaming-zipformer-zh-xlarge-int8-2025-06-30
SENSEVOICE_SHERPA_THREADS=4
SENSEVOICE_SHERPA_DECODING=modified_beam_search|greedy_search
SENSEVOICE_SHERPA_MAX_ACTIVE_PATHS=8
SENSEVOICE_SHERPA_HOTWORDS_FILE=  # optional, default empty (disabled)
SENSEVOICE_SHERPA_HOTWORDS_SCORE=1.8
SENSEVOICE_SHERPA_BLANK_PENALTY=0.0
SENSEVOICE_SHERPA_READ_MS=100
SENSEVOICE_SHERPA_MAX_UTTERANCE_MS=12000
SENSEVOICE_SHERPA_ENABLE_PUNC=1|0
SENSEVOICE_SHERPA_PUNC_MODEL=/home/dell/mosim_workspace/work/sensevoice-local-small/models/sherpa-onnx/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx
SENSEVOICE_SHERPA_PUNC_THREADS=1
```

Quick A/B benchmark on one WAV:

```bash
./.venv/bin/python benchmark_compare_backends.py /path/to/audio.wav \
  --sensevoice-model /home/dell/.cache/modelscope/hub/models/iic/SenseVoiceSmall \
  --language zn
```

Manual fallback (old push-to-talk):

```bash
SENSEVOICE_OUTPUT_MODE=focus /home/dell/mosim_workspace/work/sensevoice-local-small/toggle_talk_f8.sh
```

## Verified on this machine
Validated on 2026-02-21 with:

```bash
./run.sh /home/dell/.cache/modelscope/hub/models/iic/SenseVoiceSmall/example/en.mp3 --disable-update
```
