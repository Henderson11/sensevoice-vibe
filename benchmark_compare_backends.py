#!/usr/bin/env python3
import argparse
import os
import time
import wave

import numpy as np
import sherpa_onnx
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare FunASR(SenseVoice) vs sherpa-onnx on one wav")
    p.add_argument("audio", help="Path to wav file (mono PCM16 preferred)")
    p.add_argument(
        "--sensevoice-model",
        default=os.environ.get("SENSEVOICE_MODEL", "iic/SenseVoiceSmall"),
    )
    p.add_argument(
        "--sherpa-model-dir",
        default=os.environ.get(
            "SENSEVOICE_SHERPA_MODEL_DIR",
            os.path.join(
                os.path.dirname(__file__),
                "models",
                "sherpa-onnx",
                "sherpa-onnx-streaming-zipformer-zh-xlarge-int8-2025-06-30",
            ),
        ),
    )
    p.add_argument("--language", default="zn")
    p.add_argument("--threads", type=int, default=4)
    return p.parse_args()


def decode_sensevoice(audio: str, model_path: str, language: str):
    with wave.open(audio, "rb") as wf:
        sr = wf.getframerate()
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError("Require mono PCM16 wav for this quick benchmark")
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if sr != 16000:
        raise ValueError(f"Expect 16k wav for quick benchmark, got {sr}")

    t0 = time.perf_counter()
    model = AutoModel(
        model=model_path,
        trust_remote_code=True,
        device="cpu",
        disable_update=True,
        disable_log=True,
    )
    t1 = time.perf_counter()
    res = model.generate(
        input=samples,
        cache={},
        language=language,
        use_itn=True,
        batch_size=1,
        disable_pbar=True,
        disable_log=True,
    )
    t2 = time.perf_counter()
    text = ""
    if res and isinstance(res, list) and isinstance(res[0], dict):
        text = rich_transcription_postprocess(res[0].get("text", ""))
    return text, t1 - t0, t2 - t1


def decode_sherpa_streaming_style(audio: str, model_dir: str, threads: int, endpoint_ms: int = 500):
    tokens = os.path.join(model_dir, "tokens.txt")
    encoder = os.path.join(model_dir, "encoder.int8.onnx")
    decoder = os.path.join(model_dir, "decoder.onnx")
    joiner = os.path.join(model_dir, "joiner.int8.onnx")
    for p in [tokens, encoder, decoder, joiner]:
        if not os.path.isfile(p):
            raise FileNotFoundError(p)

    with wave.open(audio, "rb") as wf:
        sr = wf.getframerate()
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError("Require mono PCM16 wav for this quick benchmark")
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    t0 = time.perf_counter()
    rec = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=tokens,
        encoder=encoder,
        decoder=decoder,
        joiner=joiner,
        num_threads=max(1, threads),
        sample_rate=16000,
        feature_dim=80,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=max(0.2, endpoint_ms / 1000.0),
        rule3_min_utterance_length=300,
        decoding_method="greedy_search",
        provider="cpu",
    )
    t1 = time.perf_counter()

    stream = rec.create_stream()
    chunk = int(sr * 0.1)
    for i in range(0, len(samples), chunk):
        stream.accept_waveform(sr, samples[i : i + chunk])
        while rec.is_ready(stream):
            rec.decode_stream(stream)
    stream.input_finished()
    while rec.is_ready(stream):
        rec.decode_stream(stream)
    text = rec.get_result(stream)
    t2 = time.perf_counter()
    return text, t1 - t0, t2 - t1


def main() -> int:
    args = parse_args()
    if not os.path.isfile(args.audio):
        raise FileNotFoundError(args.audio)

    s_text, s_load, s_decode = decode_sensevoice(args.audio, args.sensevoice_model, args.language)
    h_text, h_load, h_decode = decode_sherpa_streaming_style(args.audio, args.sherpa_model_dir, args.threads)

    print(f"FUNASR_LOAD_SEC\t{s_load:.3f}")
    print(f"FUNASR_DECODE_SEC\t{s_decode:.3f}")
    print(f"FUNASR_TEXT\t{s_text}")
    print(f"SHERPA_LOAD_SEC\t{h_load:.3f}")
    print(f"SHERPA_DECODE_SEC\t{h_decode:.3f}")
    print(f"SHERPA_TEXT\t{h_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
