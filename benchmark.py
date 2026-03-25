#!/usr/bin/env python3
import argparse
import json
import os
import re
import statistics
import subprocess
import time

from funasr import AutoModel


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark SenseVoice local inference")
    p.add_argument(
        "--model",
        default="iic/SenseVoiceSmall",
        help="ModelScope id or local model path",
    )
    p.add_argument(
        "--audio",
        action="append",
        required=True,
        help="Audio path (can be passed multiple times)",
    )
    p.add_argument("--device", default="cpu", help="cpu / cuda:0")
    p.add_argument("--trials", type=int, default=4, help="Trials per audio")
    p.add_argument("--warmup", action="store_true", help="Do one warmup run")
    p.add_argument("--disable-update", action="store_true", help="Disable funasr update check")
    return p.parse_args()


def get_duration_seconds(ffmpeg_bin: str, audio: str) -> float:
    p = subprocess.run([ffmpeg_bin, "-i", audio], capture_output=True, text=True)
    msg = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", msg)
    if not m:
        raise RuntimeError(f"Cannot parse duration from ffmpeg output: {audio}")
    h, minute, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + minute * 60 + sec


def infer_once(model: AutoModel, audio: str) -> None:
    _ = model.generate(
        input=audio,
        cache={},
        language="auto",
        use_itn=True,
        batch_size_s=60,
        merge_vad=True,
        merge_length_s=15,
    )


def main() -> int:
    args = parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    venv_bin = os.path.join(root, ".venv", "bin")
    os.environ["PATH"] = f"{venv_bin}:" + os.environ.get("PATH", "")
    ffmpeg_bin = os.path.join(venv_bin, "ffmpeg")

    load_t0 = time.perf_counter()
    model = AutoModel(
        model=args.model,
        trust_remote_code=True,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
        device=args.device,
        disable_update=args.disable_update,
    )
    load_t1 = time.perf_counter()

    if args.warmup:
        infer_once(model, args.audio[0])

    results = []
    for audio in args.audio:
        dur = get_duration_seconds(ffmpeg_bin, audio)
        timings = []
        for _ in range(args.trials):
            t0 = time.perf_counter()
            infer_once(model, audio)
            t1 = time.perf_counter()
            timings.append(t1 - t0)
        avg = statistics.mean(timings)
        p50 = statistics.median(timings)
        p90 = sorted(timings)[max(0, int(len(timings) * 0.9) - 1)]
        results.append(
            {
                "audio": audio,
                "audio_duration_s": round(dur, 3),
                "trials": args.trials,
                "avg_infer_s": round(avg, 3),
                "p50_infer_s": round(p50, 3),
                "p90_infer_s": round(p90, 3),
                "avg_rtf": round(avg / dur, 4),
            }
        )

    print(
        json.dumps(
            {
                "model": args.model,
                "device": args.device,
                "cold_start_model_load_s": round(load_t1 - load_t0, 3),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
