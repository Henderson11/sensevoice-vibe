# sensevoice/logging.py
# Logging utilities: bounded state log appending and bounded JSONL appending.

import json
import os
import time


def append_state_log(path: str, text: str, level: str = "INFO") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = f"{time.strftime('%F %T')} [{level}] {text}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

    # Keep state log bounded in long-running sessions so disk usage does not grow.
    keep_lines_raw = os.environ.get("SENSEVOICE_LOG_KEEP_LINES", "120")
    try:
        keep_lines = int(keep_lines_raw)
    except Exception:
        keep_lines = 20
    if keep_lines < 1:
        keep_lines = 20

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= keep_lines:
            return

        kept = lines[-keep_lines:]
        with open(path, "w", encoding="utf-8") as wf:
            wf.writelines(kept)
    except Exception:
        pass



def append_jsonl_bounded(path: str, obj: dict, keep_lines: int = 400) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

    if keep_lines < 1:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= keep_lines:
            return
        with open(path, "w", encoding="utf-8") as wf:
            wf.writelines(lines[-keep_lines:])
    except Exception:
        return
