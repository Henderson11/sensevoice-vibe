#!/usr/bin/env python3
"""最近语音面板（Shift+F8）：列出最近的语音段——时间、时长、结果、声纹分数、文字，
选一条可以 重新识别并输入 / 播放录音 / 复制文字 / 打开所在文件夹。
说了话却没出字时，用它确认那段话有没有被录到，并直接重试，不用再说一遍。

  recent_voice.py           弹 zenity 面板
  recent_voice.py --print   在终端打印同样的列表
  recent_voice.py --tips    表达复盘：改进重点排序 + 最近的 原话 → 改写 → 表达建议（润色为 pyramid 模式时记录）
  recent_voice.py --review  同上，弹 zenity 窗口（面板里的"表达复盘"按钮也是它）
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


# 表达建议按问题类型归类，统计出现次数排出"先改哪一条"；按顺序匹配，先命中的为准
TIP_CATEGORIES = [
    ("区分事实与猜测", r"而非事实|当成事实|是猜测|是担忧", "把猜测、担忧标明是猜测，别当成结论说"),
    ("一次只说一件事", r"分开|拆开|一次只|分别", "多个诉求分开说，或先说\"有两件事\"再逐一展开"),
    ("删去重复绕圈", r"重复|绕圈|啰嗦", "同一个意思只说一次"),
    ("问题先行", r"^先(问|抛出|说要问)", "提问时第一句就是问题本身，现象和原因放后面"),
    ("结论先行", r"^先(给结论|说结论|点明|亮出|说明)", "先亮出判断或观点，再给理由"),
    ("诉求先行", r"^先说(要|核心|明要)", "第一句先说要对方做什么，背景、依据、约束放后面"),
]


def review_text() -> str:
    import json
    import re
    from collections import Counter
    path = os.path.join(os.path.dirname(ROOT), "expression_journal.jsonl")
    try:
        items = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    except FileNotFoundError:
        items = []
    if not items:
        return "还没有表达建议记录（润色模式为 pyramid、且一段话不短于 40 字时才会记录）"
    counts = Counter()
    for it in items:
        cat = next((name for name, rx, _ in TIP_CATEGORIES if re.search(rx, it["tip"])), "其他")
        counts[cat] += 1
    advice = {name: tip for name, _, tip in TIP_CATEGORIES}
    lines = [f"表达复盘：共 {len(items)} 条（{items[0]['ts'][:16]} ~ {items[-1]['ts'][:16]}）", "",
             "改进重点（按出现次数排序，先改排在前面的）："]
    for i, (cat, n) in enumerate(counts.most_common(), 1):
        lines.append(f"  {i}. {cat} ×{n}" + (f" —— {advice[cat]}" if cat in advice else ""))
    lines += ["", "逐条记录（新的在前）：", ""]
    for it in reversed(items[-30:]):
        lines += [f"[{it['ts'][5:16]}] 建议：{it['tip']}", f"  原话：{it['raw']}", f"  改写：{it['polished']}", ""]
    return "\n".join(lines)


def show_review(head: str = "") -> None:
    subprocess.run(["zenity", "--text-info", "--title=表达复盘", "--width=960", "--height=720", "--font=Monospace 11"],
                   input=head + review_text(), text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    if "--tips" in sys.argv:
        print(review_text())
        return 0
    if "--review" in sys.argv:
        show_review()
        return 0
    rs = rows()
    if "--print" in sys.argv:
        print(f"{'时间':16}{'时长':>8}  {'结果':10}{'声纹':>6}  文字   （录音目录 {ROOT}）")
        for r in rs:
            print(f"{r['time']:16}{r['dur']:>8}  {r['status']:10}{r['score']:>6}  {r['text']}")
        return 0
    if not rs:
        show_review("还没有最近语音记录。\n\n")  # 没有最近语音时直接打开表达复盘
        return 0
    argv = ["--list", "--title=最近语音（选一条可重试/播放）", "--width=1000", "--height=560", "--extra-button=表达复盘",
            "--column=ID", "--column=时间(结束)", "--column=时长", "--column=结果", "--column=声纹", "--column=文字",
            "--hide-column=1", "--print-column=1"]
    for r in rs:
        argv += [r["id"], r["time"], r["dur"], r["status"], r["score"], r["text"] or "—"]
    res = subprocess.run(["zenity", *argv], capture_output=True, text=True)
    if "表达复盘" in res.stdout.splitlines():  # zenity 的额外按钮：退出码 1，stdout 是按钮文字
        show_review()
        return 0
    if res.returncode != 0:
        return 0
    picked = res.stdout.strip().split("|")[0]
    for r in rs:
        if r["id"] == picked:
            act_on(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
