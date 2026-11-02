"""最近语音备份：把"可能是本人说的"语音段落盘，没出字时可以从通知或面板重试，不用再说一遍。

存什么（由 pipeline 决定）：声纹没过但分数接近阈值的段、识别失败/为空/注入失败的段、
最近若干条正常输入的段。分数很低的段（几乎都是旁人说话）不存。
目录结构：<state_dir>/recent_audio/{index.jsonl, <id>.wav}，index 每行一条记录，新的在后。
"""

from __future__ import annotations

import json
import os
import threading
import time
import wave
from typing import Dict, List, Optional

import numpy as np

STATUS_LABELS = {
    "ok": "已输入",
    "spk_reject": "声纹未通过",
    "asr_failed": "识别失败",
    "asr_empty": "没识别出文字",
    "inject_failed": "已识别但没能输入",
    "retry_ok": "重试后已输入",
    "retry_asr_failed": "重试仍识别失败",
    "retry_asr_empty": "重试仍没识别出文字",
    "retry_inject_failed": "重试已识别但没能输入",
}


def default_root() -> str:
    state_home = os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local/state"))
    return os.path.join(state_home, "sensevoice-vibe", "recent_audio")


def load_entries(root: str) -> List[Dict]:
    path = os.path.join(root, "index.jsonl")
    out: List[Dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return out


class RecentStore:
    def __init__(self, root: str, max_total: int = 50, max_ok: int = 20, max_days: float = 3.0):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.index_path = os.path.join(root, "index.jsonl")
        self.max_total = max(1, int(max_total))
        self.max_ok = max(0, int(max_ok))
        self.max_age_sec = max(0.0, float(max_days)) * 86400.0
        self._lock = threading.Lock()

    def _write(self, entries: List[Dict]) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, self.index_path)

    def _prune(self, entries: List[Dict], now: float) -> List[Dict]:
        keep = [e for e in entries if now - float(e.get("ts", 0)) <= self.max_age_sec]
        ok_ids = [e["id"] for e in keep if e.get("status") == "ok"]
        if len(ok_ids) > self.max_ok:
            drop = set(ok_ids[: len(ok_ids) - self.max_ok])  # 正常输入的只留最近 max_ok 条，不挤掉失败记录
            keep = [e for e in keep if e["id"] not in drop]
        keep = keep[-self.max_total:]
        referenced = {e.get("wav") for e in keep}
        for name in os.listdir(self.root):
            if name.endswith(".wav") and name not in referenced:
                try:
                    os.remove(os.path.join(self.root, name))
                except OSError:
                    pass
        return keep

    def save(self, samples: np.ndarray, sample_rate: int, seg_id: int, seg_ms: float, status: str,
             spk_score: Optional[float] = None, text: str = "") -> str:
        now = time.time()
        eid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(seg_id)}"
        wav_name = f"{eid}.wav"
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(os.path.join(self.root, wav_name), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm.tobytes())
        entry = {
            "id": eid, "ts": round(now, 3), "time": time.strftime("%m-%d %H:%M:%S", time.localtime(now)),
            "seg_ms": int(seg_ms), "status": status,
            "spk_score": None if spk_score is None else round(float(spk_score), 4),
            "text": text or "", "wav": wav_name,
        }
        with self._lock:
            entries = load_entries(self.root)
            entries.append(entry)
            self._write(self._prune(entries, now))
        return eid

    def get(self, eid: str) -> Optional[Dict]:
        with self._lock:
            for e in load_entries(self.root):
                if e.get("id") == eid:
                    return e
        return None

    def update(self, eid: str, **fields) -> None:
        with self._lock:
            entries = load_entries(self.root)
            for e in entries:
                if e.get("id") == eid:
                    e.update(fields)
            self._write(entries)

    def wav_path(self, entry: Dict) -> str:
        return os.path.join(self.root, entry["wav"])

    def load_samples(self, entry: Dict) -> np.ndarray:
        with wave.open(self.wav_path(entry), "rb") as wf:
            return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
