# sensevoice/speaker/verifier.py
# Speaker verification gate: compares live audio embeddings against enrolled
# speaker templates using cosine similarity, with adaptive threshold, outlier
# pruning, and automatic enrolment of high-confidence segments.

import collections
import os
import re
import time
import wave
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np


class SpeakerVerifierGate:
    def __init__(
        self,
        enabled: bool,
        enroll_wav: str,
        threshold: float,
        min_ms: int,
        model_id: str,
        cache_dir: str,
        device: str,
        score_agg: str,
        score_topk: int,
        auto_enroll: bool,
        auto_enroll_dir: str,
        auto_enroll_min_score: float,
        auto_enroll_min_ms: int,
        auto_enroll_cooldown_sec: int,
        auto_enroll_max_templates: int,
        adaptive_enable: bool,
        adaptive_window: int,
        adaptive_min_samples: int,
        adaptive_floor: float,
        adaptive_margin: float,
        prune_outliers: bool,
        prune_keep: int,
    ):
        self.requested = enabled
        self.enabled = False
        self.threshold = threshold
        self.min_ms = min_ms
        self.score_agg = score_agg
        self.score_topk = max(1, int(score_topk))
        self.reason = "disabled"
        self.enroll_count = 0
        self._enroll_paths: List[str] = []
        self.pruned_count = 0

        self.auto_enroll_requested = bool(auto_enroll)
        self.auto_enroll_enabled = False
        self.auto_enroll_dir = ""
        self.auto_enroll_min_score = float(auto_enroll_min_score)
        self.auto_enroll_min_ms = max(300, int(auto_enroll_min_ms))
        self.auto_enroll_cooldown_sec = max(0, int(auto_enroll_cooldown_sec))
        self.auto_enroll_max_templates = max(1, int(auto_enroll_max_templates))
        self._last_auto_enroll_ts = 0.0

        # Adaptive threshold: lower gate slightly when the user's recent valid scores drift down.
        self.adaptive_enable = bool(adaptive_enable)
        self.adaptive_window = max(10, int(adaptive_window))
        self.adaptive_min_samples = max(5, int(adaptive_min_samples))
        self.adaptive_floor = float(adaptive_floor)
        self.adaptive_margin = float(adaptive_margin)
        self._recent_good_scores: Deque[float] = collections.deque(maxlen=self.adaptive_window)
        self._last_threshold_used = self.threshold

        # Outlier pruning: keep the most central templates when template bank is noisy/too large.
        self.prune_outliers = bool(prune_outliers)
        self.prune_keep = max(2, int(prune_keep))

        self._torch = None
        self._F = None
        self._classifier = None
        self._enroll_embeddings = None

        if not enabled:
            return

        enroll_paths = self._resolve_enroll_paths(enroll_wav or "")
        if not enroll_paths:
            self.reason = f"enroll_missing:{(enroll_wav or '').strip() or '-'}"
            return

        try:
            import torchaudio  # type: ignore

            # speechbrain still expects these legacy torchaudio helpers in some versions.
            if not hasattr(torchaudio, "list_audio_backends"):
                torchaudio.list_audio_backends = lambda: ["soundfile"]  # type: ignore[attr-defined]
            if not hasattr(torchaudio, "set_audio_backend"):
                torchaudio.set_audio_backend = lambda backend: None  # type: ignore[attr-defined]
            if not hasattr(torchaudio, "get_audio_backend"):
                torchaudio.get_audio_backend = lambda: "soundfile"  # type: ignore[attr-defined]

            import torch  # type: ignore
            import torch.nn.functional as F  # type: ignore
            from speechbrain.inference.speaker import SpeakerRecognition  # type: ignore

            self._torch = torch
            self._F = F

            model_cache = os.path.expanduser(cache_dir or "~/.cache/sensevoice-vibe/spkrec")
            os.makedirs(model_cache, exist_ok=True)
            self._classifier = SpeakerRecognition.from_hparams(
                source=model_id,
                savedir=model_cache,
                run_opts={"device": device},
            )

            emb_list = []
            for enroll_path in enroll_paths:
                enroll_samples, sr = self._load_wav_mono_float32(enroll_path)
                if sr != 16000:
                    enroll_samples = self._resample_linear(enroll_samples, sr, 16000)
                if enroll_samples.size < 16000:
                    # Ignore enrollment clips shorter than 1s; they are unstable for speaker verification.
                    continue
                wav = torch.from_numpy(enroll_samples).float().unsqueeze(0)
                with torch.no_grad():
                    emb = self._classifier.encode_batch(wav).reshape(1, -1)
                emb_list.append(emb)
                self._enroll_paths.append(enroll_path)

            if not emb_list:
                self.reason = "enroll_invalid:no_valid_wav"
                return

            self._enroll_embeddings = torch.cat(emb_list, dim=0)
            self.enroll_count = int(self._enroll_embeddings.shape[0])
            self._configure_auto_enroll(enroll_wav, auto_enroll_dir)
            self._apply_in_memory_prune()

            self.enabled = True
            self.reason = (
                f"ready:n={self.enroll_count},agg={self.score_agg},k={self.score_topk},"
                f"adapt={int(self.adaptive_enable)},pruned={self.pruned_count}"
            )
        except Exception as e:
            import sys
            print(f"[sensevoice] warning: speaker verifier init failed: {type(e).__name__}: {e}", file=sys.stderr)
            self.reason = f"init_error:{type(e).__name__}"
            self.enabled = False

    @staticmethod
    def _resolve_enroll_paths(spec: str) -> List[str]:
        raw = os.path.expanduser((spec or "").strip())
        if not raw:
            return []

        parts = [p.strip() for p in re.split(r"[;,]", raw) if p.strip()]
        paths: List[str] = []
        if len(parts) > 1:
            paths = [os.path.expanduser(p) for p in parts]
        elif os.path.isdir(raw):
            for name in sorted(os.listdir(raw)):
                if name.lower().endswith(".wav"):
                    paths.append(os.path.join(raw, name))
        else:
            paths = [raw]

        out: List[str] = []
        seen = set()
        for p in paths:
            p2 = os.path.abspath(os.path.expanduser(p))
            if not os.path.isfile(p2):
                continue
            if p2 in seen:
                continue
            seen.add(p2)
            out.append(p2)
        return out

    def _configure_auto_enroll(self, enroll_spec: str, auto_enroll_dir: str) -> None:
        if not self.auto_enroll_requested:
            return
        chosen = os.path.expanduser((auto_enroll_dir or "").strip())
        if not chosen:
            spec_raw = os.path.expanduser((enroll_spec or "").strip())
            if spec_raw and os.path.isdir(spec_raw):
                chosen = spec_raw
        if not chosen:
            return
        os.makedirs(chosen, exist_ok=True)
        self.auto_enroll_dir = os.path.abspath(chosen)
        self.auto_enroll_enabled = True

    def _apply_in_memory_prune(self) -> None:
        if not self.prune_outliers:
            return
        if self._enroll_embeddings is None or self._F is None or self._torch is None:
            return

        n = int(self._enroll_embeddings.shape[0])
        if n < 4 or n <= self.prune_keep:
            return

        F = self._F
        torch = self._torch
        emb = self._enroll_embeddings

        # Keep templates that are most similar to their nearest neighbors (central samples).
        norm = F.normalize(emb, dim=1)
        sim = torch.matmul(norm, norm.transpose(0, 1))
        sim.fill_diagonal_(-1.0)
        k = min(3, n - 1)
        neigh_mean = sim.topk(k=k, dim=1).values.mean(dim=1)
        keep_n = min(self.prune_keep, n)
        keep_idx = torch.topk(neigh_mean, k=keep_n).indices
        keep_idx = torch.sort(keep_idx).values.tolist()

        self._enroll_embeddings = emb[keep_idx]
        if self._enroll_paths:
            self._enroll_paths = [self._enroll_paths[i] for i in keep_idx if i < len(self._enroll_paths)]
        self.pruned_count = n - keep_n
        self.enroll_count = int(self._enroll_embeddings.shape[0])

    def _effective_threshold(self) -> float:
        if not self.adaptive_enable:
            return self.threshold
        if len(self._recent_good_scores) < self.adaptive_min_samples:
            return self.threshold
        q25 = float(np.percentile(np.array(self._recent_good_scores, dtype=np.float32), 25))
        adaptive = max(self.adaptive_floor, q25 - self.adaptive_margin)
        # Never make the gate stricter than configured base threshold.
        return min(self.threshold, adaptive)

    @staticmethod
    def _load_wav_mono_float32(path: str) -> Tuple[np.ndarray, int]:
        with wave.open(path, "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            sample_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

        if sampwidth == 2:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 1:
            # 8-bit PCM in WAV is typically unsigned
            x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sampwidth == 4:
            x = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise RuntimeError(f"unsupported_sample_width:{sampwidth}")

        if channels > 1:
            x = x.reshape(-1, channels).mean(axis=1)
        return x.astype(np.float32, copy=False), sample_rate

    @staticmethod
    def _resample_linear(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        if src_sr == dst_sr or samples.size == 0:
            return samples
        duration = samples.shape[0] / float(src_sr)
        n_dst = max(1, int(round(duration * dst_sr)))
        src_x = np.linspace(0.0, duration, num=samples.shape[0], endpoint=False, dtype=np.float64)
        dst_x = np.linspace(0.0, duration, num=n_dst, endpoint=False, dtype=np.float64)
        y = np.interp(dst_x, src_x, samples.astype(np.float64, copy=False))
        return y.astype(np.float32, copy=False)

    @staticmethod
    def _save_wav_mono_float32(path: str, samples: np.ndarray, sample_rate: int) -> None:
        x = np.clip(samples, -1.0, 1.0)
        pcm = (x * 32767.0).astype(np.int16)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm.tobytes())

    def verify(self, samples: np.ndarray, sample_rate: int, seg_ms: float) -> Tuple[bool, float, str, float]:
        if not self.enabled:
            # Fail-open when verifier is unavailable.
            return True, 1.0, self.reason, self.threshold
        if seg_ms < self.min_ms:
            return False, 0.0, "too_short", self.threshold

        assert self._torch is not None
        assert self._F is not None
        assert self._classifier is not None
        assert self._enroll_embeddings is not None

        torch = self._torch
        F = self._F

        x = samples.astype(np.float32, copy=False)
        if sample_rate != 16000:
            x = self._resample_linear(x, sample_rate, 16000)
        wav = torch.from_numpy(x).float().unsqueeze(0)

        with torch.no_grad():
            emb = self._classifier.encode_batch(wav)
        emb = emb.reshape(1, -1)

        # Compare against all enrollment templates.
        scores = F.cosine_similarity(self._enroll_embeddings, emb, dim=1)
        if self.score_agg == "max":
            score = float(scores.max().item())
        elif self.score_agg == "mean":
            score = float(scores.mean().item())
        else:
            k = min(self.score_topk, int(scores.numel()))
            score = float(scores.topk(k=k).values.mean().item())
        eff_thr = self._effective_threshold()
        self._last_threshold_used = eff_thr
        if score >= eff_thr:
            reason = "pass" if score >= self.threshold else "pass_adapt"
            return True, score, reason, eff_thr
        return False, score, "below_threshold", eff_thr

    def on_success(self, score: float, seg_ms: float, text: str) -> None:
        if not self.adaptive_enable:
            return
        if seg_ms < self.min_ms:
            return
        if len((text or "").strip()) < 4:
            return
        self._recent_good_scores.append(float(score))

    def maybe_auto_enroll(
        self,
        samples: np.ndarray,
        sample_rate: int,
        seg_ms: float,
        score: float,
    ) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        if not self.auto_enroll_enabled:
            return False, "auto_off"
        if seg_ms < self.auto_enroll_min_ms:
            return False, "too_short"
        if score < self.auto_enroll_min_score:
            return False, "score_low"
        if self.enroll_count >= self.auto_enroll_max_templates:
            return False, "max_templates"
        now = time.time()
        if now - self._last_auto_enroll_ts < self.auto_enroll_cooldown_sec:
            return False, "cooldown"

        assert self._torch is not None
        assert self._classifier is not None
        assert self._enroll_embeddings is not None

        x = samples.astype(np.float32, copy=False)
        sr = sample_rate
        if sr != 16000:
            x = self._resample_linear(x, sr, 16000)
            sr = 16000
        if x.size < 16000:
            return False, "too_short"

        torch = self._torch
        wav = torch.from_numpy(x).float().unsqueeze(0)
        with torch.no_grad():
            emb = self._classifier.encode_batch(wav).reshape(1, -1)

        ts = time.strftime("%Y%m%d_%H%M%S")
        ms = int((now - int(now)) * 1000)
        base = f"auto_{ts}_{ms:03d}_{int(score*1000):03d}"
        out = os.path.join(self.auto_enroll_dir, f"{base}.wav")
        if os.path.exists(out):
            for i in range(1, 100):
                candidate = os.path.join(self.auto_enroll_dir, f"{base}_{i:02d}.wav")
                if not os.path.exists(candidate):
                    out = candidate
                    break
        self._save_wav_mono_float32(out, x, sr)

        self._enroll_embeddings = torch.cat([self._enroll_embeddings, emb], dim=0)
        self._enroll_paths.append(out)
        self.enroll_count = int(self._enroll_embeddings.shape[0])
        self._last_auto_enroll_ts = now
        return True, out
