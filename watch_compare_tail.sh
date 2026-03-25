#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sensevoice-vibe"
FILE="${1:-$STATE_DIR/post_compare.jsonl}"
TAIL_LINES="${SENSEVOICE_COMPARE_WATCH_TAIL_LINES:-80}"

mkdir -p "$(dirname "$FILE")"
touch "$FILE"

tail -n "$TAIL_LINES" -F "$FILE" | python3 -u -c '
import difflib
import json
import shutil
import sys
import textwrap
from collections import deque

MAX_ITEMS = 14
UNCHANGED_ITEMS = 4

def width() -> int:
    cols = shutil.get_terminal_size((84, 24)).columns
    return max(46, cols - 2)

def short(text: str, limit: int) -> str:
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)] + "..."

def wrap_block(prefix: str, text: str, total_width: int):
    available = max(20, total_width - len(prefix))
    body = text or "-"
    chunks = textwrap.wrap(body, width=available, replace_whitespace=False, drop_whitespace=False) or [body]
    lines = [prefix + chunks[0]]
    pad = " " * len(prefix)
    lines.extend(pad + chunk for chunk in chunks[1:])
    return lines

def change_stats(src: str, dst: str):
    left = (src or "").strip()
    right = (dst or "").strip()
    if not left and not right:
        return 0, 0.0
    ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
    changed = int(round((1.0 - ratio) * max(len(left), len(right))))
    return changed, max(0.0, min(1.0, 1.0 - ratio))

def changed_spans(src: str, dst: str):
    sm = difflib.SequenceMatcher(None, src or "", dst or "", autojunk=False)
    pieces = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        left = (src or "")[i1:i2].strip()
        right = (dst or "")[j1:j2].strip()
        if tag == "replace" and left and right:
            pieces.append(f"{left} -> {right}")
        elif tag == "delete" and left:
            pieces.append(f"- {left}")
        elif tag == "insert" and right:
            pieces.append(f"+ {right}")
    return pieces[:3]

def is_hidden_noise(obj: dict) -> bool:
    drop = str(obj.get("drop_reason", "") or "")
    raw = str(obj.get("raw_asr", "") or "").strip()
    final = str(obj.get("final_injected", "") or "").strip()
    if drop == "sanitize_drop":
        return True
    if not raw and not final:
        return True
    if raw in {".", "。", "Yeah.", "I."} and not final:
        return True
    return False

def is_unchanged_passthrough(obj: dict) -> bool:
    drop = str(obj.get("drop_reason", "") or "")
    raw = str(obj.get("raw_asr", "") or "").strip()
    final = str(obj.get("final_injected", "") or "").strip()
    llm = str(obj.get("llm_action", "") or "")
    return bool(raw) and raw == final and not drop and llm in {"pass", "skip_high_conf", "none"}

def stage_summary(obj: dict) -> str:
    raw = str(obj.get("raw_asr", "") or "").strip()
    san = str(obj.get("after_sanitize", "") or "").strip()
    mem = str(obj.get("after_memory", "") or "").strip()
    after_llm = str(obj.get("after_llm", "") or "").strip()
    lex = str(obj.get("after_lexicon", "") or "").strip()
    final = str(obj.get("final_injected", "") or "").strip()
    llm = str(obj.get("llm_action", "") or "-")
    parts = []
    if raw and san and raw != san:
        parts.append("sanitize")
    if san and mem and san != mem:
        parts.append("memory")
    if llm not in {"-", "", "none"}:
        parts.append(f"llm:{llm}")
    if after_llm and lex and after_llm != lex:
        parts.append("lexicon")
    if final and not parts:
        parts.append("inject")
    return " -> ".join(parts) if parts else "inject"

def low_token_summary(obj: dict) -> str:
    toks = obj.get("conf_low_tokens") or []
    parts = []
    for item in toks:
        if not isinstance(item, dict):
            continue
        tok = str(item.get("token", "") or "").strip()
        score = item.get("score")
        if not tok or score is None:
            continue
        try:
            parts.append(f"{tok}:{float(score):.2f}")
        except Exception:
            continue
    return ", ".join(parts[:4])

def render(records, unchanged_records):
    cols = width()
    print("\033[2J\033[H", end="")
    print(short(" SenseVoice Compare  RAW -> FINAL ", cols).center(cols, "="))
    print(short("Changed/blocked segments first, then recent stable passthrough", cols))
    print("=" * cols)
    if not records and not unchanged_records:
        print("Waiting for new voice segments...", flush=True)
        return
    if records:
        print("Changed or blocked:")
    for obj in records:
        ts = str(obj.get("ts", "-"))
        seg = obj.get("seg_id", "-")
        route = str(obj.get("conf_route", "-") or "-")
        conf = obj.get("conf_score")
        conf_src = str(obj.get("conf_source", "-") or "-")
        llm = str(obj.get("llm_action", "-") or "-")
        raw = str(obj.get("raw_asr", "") or "").strip()
        final = str(obj.get("final_injected", "") or "").strip()
        drop = str(obj.get("drop_reason", "") or "").strip()
        changed_n, changed_ratio = change_stats(raw, final)
        conf_str = f"{float(conf):.3f}" if conf is not None else "-"
        header = f"[{ts}] seg={seg} route={route} conf={conf_str} src={conf_src} llm={llm} delta={changed_n} ({changed_ratio*100:.1f}%)"
        print(short(header, cols))
        print(short(f"PATH {stage_summary(obj)}", cols))
        low = low_token_summary(obj)
        if low:
            for line in wrap_block("LOW  ", low, cols):
                print(line)
        for line in wrap_block("RAW  ", raw or "-", cols):
            print(line)
        for line in wrap_block("OUT  ", final or "-", cols):
            print(line)
        spans = changed_spans(raw, final)
        if spans:
            for line in wrap_block("DIFF ", " | ".join(spans), cols):
                print(line)
        elif raw == final and final:
            print("DIFF no change")
        if drop:
            for line in wrap_block("DROP ", drop, cols):
                print(line)
        print("-" * cols)
    if unchanged_records:
        print("Recent stable passthrough:")
        for obj in unchanged_records:
            ts = str(obj.get("ts", "-"))
            seg = obj.get("seg_id", "-")
            route = str(obj.get("conf_route", "-") or "-")
            conf = obj.get("conf_score")
            conf_src = str(obj.get("conf_source", "-") or "-")
            llm = str(obj.get("llm_action", "-") or "-")
            raw = str(obj.get("raw_asr", "") or "").strip()
            conf_str = f"{float(conf):.3f}" if conf is not None else "-"
            print(short(f"[{ts}] seg={seg} route={route} conf={conf_str} src={conf_src} llm={llm}", cols))
            low = low_token_summary(obj)
            if low:
                for line in wrap_block("LOW  ", low, cols):
                    print(line)
            for line in wrap_block("TEXT ", raw or "-", cols):
                print(line)
            print("-" * cols)
    print(flush=True)

records = deque(maxlen=MAX_ITEMS)
unchanged_records = deque(maxlen=UNCHANGED_ITEMS)
for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    if is_hidden_noise(obj):
        continue
    if is_unchanged_passthrough(obj):
        unchanged_records.append(obj)
    else:
        records.append(obj)
    render(records, unchanged_records)
'
