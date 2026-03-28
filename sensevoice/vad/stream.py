# sensevoice/vad/stream.py
# VAD audio stream utilities: microphone recording process creation.

import argparse
import subprocess


def make_arecord_process(args: argparse.Namespace) -> subprocess.Popen:
    cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(args.sample_rate), "-c", "1", "-t", "raw"]
    if args.input_device:
        cmd.extend(["-D", args.input_device])
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
