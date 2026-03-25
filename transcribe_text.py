#!/usr/bin/env python3
import argparse
import os
import sys

from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SenseVoice transcription (text-only output)")
    p.add_argument("audio", help="Path to input audio file")
    p.add_argument("--model", default="iic/SenseVoiceSmall", help="ModelScope model id or local model path")
    p.add_argument("--device", default="cpu", help="inference device, e.g. cpu / cuda:0")
    p.add_argument("--language", default="auto", help="auto|zn|en|yue|ja|ko|nospeech")
    p.add_argument("--batch-size-s", type=int, default=60, help="Dynamic batch size in seconds")
    p.add_argument("--disable-update", action="store_true", help="Disable funasr update check")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not os.path.isfile(args.audio):
        print("TEXT_RESULT\t", end="")
        print(f"audio file not found: {args.audio}", file=sys.stderr)
        return 2

    model = AutoModel(
        model=args.model,
        trust_remote_code=True,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
        device=args.device,
        disable_update=args.disable_update,
    )

    res = model.generate(
        input=args.audio,
        cache={},
        language=args.language,
        use_itn=True,
        batch_size_s=args.batch_size_s,
        merge_vad=True,
        merge_length_s=15,
        disable_pbar=True,
        disable_log=True,
    )

    text = ""
    if res and isinstance(res, list) and isinstance(res[0], dict):
        text = rich_transcription_postprocess(res[0].get("text", ""))

    # Keep a machine-parsable sentinel line for shell pipeline parsing.
    print(f"TEXT_RESULT\t{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
