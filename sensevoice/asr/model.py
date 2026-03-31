# sensevoice/asr/model.py
# ASR model loading and inference: model initialization, basic transcription,
# confidence-scored transcription, and native CTC forced-alignment confidence.

import argparse
import itertools
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
from funasr import AutoModel
from funasr.models.sense_voice.utils.ctc_alignment import ctc_forced_align
from funasr.utils.load_utils import extract_fbank, load_audio_text_image_video
from funasr.utils.postprocess_utils import rich_transcription_postprocess

from sensevoice.asr.confidence import _aggregate_display_conf_scores
from sensevoice.text.constants import EMOJI_ARTIFACTS


def load_model(args: argparse.Namespace):
    """加载 ASR 模型。优先使用 ONNX INT8（更快更小），回退到 PyTorch。"""
    model_dir = args.model
    use_onnx = getattr(args, "asr_onnx", False)

    # 自动检测：如果模型目录下有 model_quant.onnx 且未显式禁用，使用 ONNX
    import os
    if not use_onnx and os.path.isfile(os.path.join(model_dir, "model_quant.onnx")):
        use_onnx = True

    if use_onnx:
        from funasr_onnx import SenseVoiceSmall
        return SenseVoiceSmall(model_dir, batch_size=1, quantize=True)

    return AutoModel(
        model=model_dir,
        trust_remote_code=False,
        device=args.device,
        disable_update=True,
        disable_log=True,
    )


def _native_ctc_confidence(
    model: AutoModel,
    samples: np.ndarray,
    language: str,
) -> Tuple[str, Optional[float], str, List[Dict[str, float]]]:
    if samples.size == 0:
        return "", None, "empty", []
    inner = getattr(model, "model", None)
    tokenizer = model.kwargs.get("tokenizer")
    frontend = model.kwargs.get("frontend")
    if inner is None or tokenizer is None or frontend is None:
        return "", None, "missing_components", []

    device = model.kwargs.get("device", "cpu")
    data_type = model.kwargs.get("data_type", "sound")
    audio_fs = model.kwargs.get("fs", 16000)
    use_itn = True
    textnorm = "withitn" if use_itn else "woitn"
    lang_key = language if language in getattr(inner, "lid_dict", {}) else "auto"

    try:
        inner.eval()
        with torch.no_grad():
            audio_sample_list = load_audio_text_image_video(
                samples,
                fs=frontend.fs,
                audio_fs=audio_fs,
                data_type=data_type,
                tokenizer=tokenizer,
            )
            speech, speech_lengths = extract_fbank(
                audio_sample_list,
                data_type=data_type,
                frontend=frontend,
            )
            speech = speech.to(device=device)
            speech_lengths = speech_lengths.to(device=device)

            language_query = inner.embed(
                torch.LongTensor([[inner.lid_dict[lang_key] if lang_key in inner.lid_dict else 0]]).to(speech.device)
            ).repeat(speech.size(0), 1, 1)
            textnorm_query = inner.embed(
                torch.LongTensor([[inner.textnorm_dict[textnorm]]]).to(speech.device)
            ).repeat(speech.size(0), 1, 1)
            speech = torch.cat((textnorm_query, speech), dim=1)
            speech_lengths += 1

            event_emo_query = inner.embed(torch.LongTensor([[1, 2]]).to(speech.device)).repeat(
                speech.size(0), 1, 1
            )
            input_query = torch.cat((language_query, event_emo_query), dim=1)
            speech = torch.cat((input_query, speech), dim=1)
            speech_lengths += 3

            encoder_out, encoder_out_lens = inner.encoder(speech, speech_lengths)
            if isinstance(encoder_out, tuple):
                encoder_out = encoder_out[0]

            ctc_log_probs = inner.ctc.log_softmax(encoder_out)
            x = ctc_log_probs[0, : encoder_out_lens[0].item(), :]
            yseq = x.argmax(dim=-1)
            yseq = torch.unique_consecutive(yseq, dim=-1)
            mask = yseq != inner.blank_id
            token_int = yseq[mask].tolist()
            raw_text = tokenizer.decode(token_int)
            processed_text = rich_transcription_postprocess(raw_text).strip()
            processed_text = processed_text.translate(str.maketrans("", "", EMOJI_ARTIFACTS)).strip()

            tokens = tokenizer.text2tokens(raw_text)[4:]
            token_back_to_id = tokenizer.tokens2ids(tokens)
            token_ids: List[int] = []
            for tok_ls in token_back_to_id:
                if tok_ls:
                    token_ids.extend(tok_ls)
                else:
                    token_ids.append(124)
            if not token_ids:
                return processed_text, None, "empty_token_ids", []

            speech_probs = inner.ctc.softmax(encoder_out)[0, 4 : encoder_out_lens[0].item(), :]
            pred = speech_probs.argmax(-1)
            speech_probs[pred == inner.blank_id, inner.blank_id] = 0
            align = ctc_forced_align(
                speech_probs.unsqueeze(0).float(),
                torch.tensor(token_ids, device=speech_probs.device).unsqueeze(0).long(),
                (encoder_out_lens[0] - 4).reshape(1).long(),
                torch.tensor([len(token_ids)], device=speech_probs.device).long(),
                ignore_id=inner.ignore_id,
            )

            token_scores: List[float] = []
            display_token_scores: List[Dict[str, float]] = []
            align_seq = align[0, : int((encoder_out_lens[0] - 4).item())].tolist()
            start = 0
            for token_id, group in itertools.groupby(align_seq):
                group_len = len(list(group))
                end = start + group_len
                if token_id != inner.blank_id and group_len > 0:
                    probs = speech_probs[start:end, int(token_id)]
                    if probs.numel() > 0:
                        token_scores.append(float(probs.mean().item()))
                start = end
            per_id_scores = token_scores[:]
            if per_id_scores:
                pos = 0
                for tok, tok_ids in zip(tokens, token_back_to_id):
                    ids = tok_ids if tok_ids else [124]
                    n_ids = len(ids)
                    if pos >= len(per_id_scores):
                        break
                    seg = per_id_scores[pos : pos + n_ids]
                    pos += n_ids
                    if not seg:
                        continue
                    disp = tok.lstrip("▁").strip()
                    if not disp:
                        continue
                    display_token_scores.append(
                        {"token": disp, "score": round(float(np.mean(seg)), 4)}
                    )
            if not token_scores:
                return processed_text, None, "empty_token_scores", []
            utter_conf = _aggregate_display_conf_scores(display_token_scores)
            if utter_conf is None:
                utter_conf = float(np.clip(np.mean(token_scores), 0.0, 1.0))
            return processed_text, utter_conf, "native_ctc", display_token_scores
    except Exception as e:
        return "", None, f"native_err:{type(e).__name__}", []


def _is_onnx_model(model) -> bool:
    return type(model).__name__ == "SenseVoiceSmall" and hasattr(model, "infer")


def transcribe_array(model, samples: np.ndarray, language: str) -> str:
    if samples.size == 0:
        return ""
    if _is_onnx_model(model):
        res = model(samples, language=language, textnorm="withitn")
        if res and isinstance(res, list) and res[0]:
            raw = res[0] if isinstance(res[0], str) else res[0].get("text", "")
            text = rich_transcription_postprocess(raw).strip()
            return text.translate(str.maketrans("", "", EMOJI_ARTIFACTS)).strip()
        return ""
    else:
        res = model.generate(
            input=samples, cache={}, language=language, use_itn=True,
            batch_size=1, disable_pbar=True, disable_log=True,
        )
        if res and isinstance(res, list) and isinstance(res[0], dict):
            text = rich_transcription_postprocess(res[0].get("text", "")).strip()
            return text.translate(str.maketrans("", "", EMOJI_ARTIFACTS)).strip()
        return ""


def transcribe_array_with_conf(
    model,
    samples: np.ndarray,
    language: str,
) -> Tuple[str, Optional[float], str, List[Dict[str, float]]]:
    # ONNX 模式下无法做 CTC 置信度计算，直接返回文本
    if _is_onnx_model(model):
        text = transcribe_array(model, samples, language)
        return text, None, "onnx", []
    text, conf, source, token_scores = _native_ctc_confidence(model, samples, language)
    if text:
        return text, conf, source, token_scores
    return transcribe_array(model, samples, language), None, source, []
