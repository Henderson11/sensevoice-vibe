#!/usr/bin/env python3
"""最近语音面板（Shift+F8）：列出最近的语音段——时间、时长、结果、声纹分数、文字，
选一条可以 重新识别并输入 / 播放录音 / 复制文字 / 打开所在文件夹。
说了话却没出字时，用它确认那段话有没有被录到，并直接重试，不用再说一遍。

  recent_voice.py           弹 zenity 面板
  recent_voice.py --print   在终端打印同样的列表
"""

import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sensevoice.recent import STATUS_LABELS, default_root, load_entries  # noqa: E402

ROOT = default_root()
SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "sensevoice-ctl.sock")
MAX_ROWS = 20


def rows():
    out = []
    live = [e for e in load_entries(ROOT) if os.path.exists(os.path.join(ROOT, e.get("wav", "")))]
    for e in reversed(live[-MAX_ROWS:]):
        score = e.get("spk_score")
        out.append({
            "id": e["id"], "time": e.get("time", ""), "dur": f"{e.get('seg_ms', 0) / 1000:.1f}s",
            "status": STATUS_LABELS.get(e.get("status", ""), e.get("status", "")),
            "score": "-" if score is None else f"{score:.2f}",
            "text": (e.get("text") or "").replace("\n", " ")[:60], "entry": e,
        })
    return out


def send_retry(eid: str) -> str:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(SOCKET_PATH)
        s.sendall(f"RETRY {eid}\n".encode())
        return s.recv(256).decode().strip()
    except OSError as e:
        return f"ERR\t{type(e).__name__}:{e}"


def notify(msg: str) -> None:
    subprocess.run(["notify-send", "-a", "SenseVoice", "-t", "3000", "SenseVoice", msg],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def zenity(*argv) -> str:
    r = subprocess.run(["zenity", *argv], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def act_on(row) -> None:
    e = row["entry"]
    wav = os.path.join(ROOT, e["wav"])
    head = f"{row['time']}　{row['dur']}　{row['status']}　声纹 {row['score']}"
    while True:
        choice = zenity("--list", "--title=最近语音", f"--text={head}\n{e.get('text') or '（没有文字）'}",
                        "--column=操作", "--width=520", "--height=320",
                        "重新识别并输入", "播放录音", "复制文字", "打开所在文件夹")
        if choice == "重新识别并输入":
            ack = send_retry(e["id"])
            notify("已提交重试，结果会输入到当前光标处" if ack.startswith("OK") else f"重试失败：{ack}")
            return
        if choice == "播放录音":
            subprocess.run(["paplay", wav], stderr=subprocess.DEVNULL)
            continue  # 听完回到操作菜单，方便接着重试
        if choice == "复制文字":
            if e.get("text"):
                subprocess.run(["wl-copy", e["text"]])
                notify("文字已复制")
            else:
                notify("这条没有文字，可以先重新识别")
            return
        if choice == "打开所在文件夹":
            subprocess.Popen(["xdg-open", ROOT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        return


def main() -> int:
    rs = rows()
    if "--print" in sys.argv:
        print(f"{'时间':16}{'时长':>8}  {'结果':10}{'声纹':>6}  文字   （录音目录 {ROOT}）")
        for r in rs:
            print(f"{r['time']:16}{r['dur']:>8}  {r['status']:10}{r['score']:>6}  {r['text']}")
        return 0
    if not rs:
        notify("还没有最近语音记录")
        return 0
    argv = ["--list", "--title=最近语音（选一条可重试/播放）", "--width=1000", "--height=560",
            "--column=ID", "--column=时间(结束)", "--column=时长", "--column=结果", "--column=声纹", "--column=文字",
            "--hide-column=1", "--print-column=1"]
    for r in rs:
        argv += [r["id"], r["time"], r["dur"], r["status"], r["score"], r["text"] or "—"]
    picked = zenity(*argv).split("|")[0]
    for r in rs:
        if r["id"] == picked:
            act_on(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
