# sensevoice/cli/args.py
# Command-line argument parsing for SenseVoice streaming VAD realtime transcription.

import argparse
import os


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Continuous auto-VAD realtime transcription")
    p.add_argument(
        "--model",
        default=os.environ.get("SENSEVOICE_MODEL", "iic/SenseVoiceSmall"),
        help="Model id or local model path",
    )
    p.add_argument("--device", default=os.environ.get("SENSEVOICE_DEVICE", "cpu"))
    p.add_argument("--language", default=os.environ.get("SENSEVOICE_LANGUAGE", "auto"))
    p.add_argument("--sample-rate", type=int, default=16000)
    p.add_argument(
        "--frame-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_FRAME_MS", "20")),
        choices=[10, 20, 30],
    )
    p.add_argument(
        "--vad-aggressiveness",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_VAD_AGGRESSIVENESS", "2")),
        choices=[0, 1, 2, 3],
    )
    p.add_argument("--start-ms", type=int, default=int(os.environ.get("SENSEVOICE_STREAM_START_MS", "240")))
    p.add_argument(
        "--endpoint-silence-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_ENDPOINT_MS", "1500")),
    )
    p.add_argument(
        "--max-segment-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_MAX_SEGMENT_MS", "8000")),
    )
    p.add_argument(
        "--pre-roll-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_PRE_ROLL_MS", "300")),
    )
    p.add_argument(
        "--min-segment-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_MIN_SEGMENT_MS", "400")),
    )
    p.add_argument(
        "--partial-interval-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_PARTIAL_INTERVAL_MS", "350")),
    )
    p.add_argument(
        "--min-partial-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_STREAM_MIN_PARTIAL_MS", "900")),
    )
    p.add_argument(
        "--auto-enter",
        action="store_true",
        default=os.environ.get("SENSEVOICE_AUTO_ENTER", "0") == "1",
        help="Send FINAL (Enter) when endpoint is detected",
    )
    p.add_argument(
        "--indicator",
        choices=["none", "notify", "notify_once"],
        default=os.environ.get("SENSEVOICE_STREAM_INDICATOR", "notify_once"),
        help="Non-intrusive speaking indicator",
    )
    p.add_argument(
        "--input-device",
        default=os.environ.get("SENSEVOICE_ARECORD_DEVICE", ""),
        help="Optional arecord device, e.g. hw:Microphone,0",
    )
    p.add_argument(
        "--inject-mode",
        default=os.environ.get("SENSEVOICE_INJECT_MODE", "clipboard"),
        choices=["ibus", "clipboard"],
        help="Text injection backend: ibus (recommended) or clipboard",
    )
    p.add_argument(
        "--inject-script",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "send_to_focus_ydotool.sh"),
    )
    p.add_argument(
        "--post-llm-fallback-base-url",
        default=os.environ.get("SENSEVOICE_POST_LLM_FALLBACK_BASE_URL", ""),
        help="Fallback LLM API base URL (e.g. official DeepSeek API)",
    )
    p.add_argument(
        "--post-llm-fallback-api-key",
        default=os.environ.get("SENSEVOICE_POST_LLM_FALLBACK_API_KEY", ""),
    )
    p.add_argument(
        "--state-log",
        default=os.path.join(
            os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local/state")),
            "sensevoice-vibe",
            "stream_vad.log",
        ),
    )
    p.add_argument(
        "--filter-fillers",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_FILTER_FILLERS", "1")),
        help="Drop short filler/interjection outputs in Chinese mode",
    )
    p.add_argument(
        "--partial-strategy",
        choices=["stable2", "raw"],
        default=os.environ.get("SENSEVOICE_PARTIAL_STRATEGY", "stable2"),
        help="stable2: only emit stable prefix agreed by 2 consecutive partial hypotheses",
    )
    p.add_argument(
        "--emit-partial",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_EMIT_PARTIAL", "1")),
        help="Whether to emit interim PARTIAL updates",
    )
    p.add_argument(
        "--resident",
        action="store_true",
        default=os.environ.get("SENSEVOICE_RESIDENT", "0") == "1",
        help="Keep process resident and toggle active listening via SIGUSR1",
    )
    p.add_argument(
        "--active-on-start",
        action="store_true",
        default=os.environ.get("SENSEVOICE_STREAM_ACTIVE_ON_START", "0") == "1",
        help="Start listening immediately in resident mode",
    )
    p.add_argument(
        "--wake-words",
        default=os.environ.get("SENSEVOICE_WAKE_WORDS", ""),
        help="Optional wake words (comma-separated). If set, only matched utterances are injected.",
    )
    p.add_argument(
        "--wake-strategy",
        choices=["prefix", "contains"],
        default=os.environ.get("SENSEVOICE_WAKE_STRATEGY", "prefix"),
        help="Wake word match mode: prefix (recommended) or contains",
    )
    p.add_argument(
        "--wake-strip",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_WAKE_STRIP", "1")),
        help="If enabled, strip wake word token from injected text",
    )
    p.add_argument(
        "--speaker-verify",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_SPK_ENABLE", "0")),
        help="Enable speaker verification gate before text injection",
    )
    p.add_argument(
        "--speaker-enroll-wav",
        default=os.environ.get(
            "SENSEVOICE_SPK_ENROLL_WAV",
            os.path.join(
                os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local/state")),
                "sensevoice-vibe",
                "speaker_enroll.wav",
            ),
        ),
        help="Enrollment source: WAV file, directory with *.wav, or comma-separated WAV paths",
    )
    p.add_argument(
        "--speaker-threshold",
        type=float,
        default=float(os.environ.get("SENSEVOICE_SPK_THRESHOLD", "0.80")),
        help="Cosine similarity threshold for speaker verification",
    )
    p.add_argument(
        "--speaker-min-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_MIN_MS", "900")),
        help="Minimum segment duration required before speaker verification",
    )
    p.add_argument(
        "--speaker-model",
        default=os.environ.get("SENSEVOICE_SPK_MODEL", "campplus"),
        help="Speaker verification model id",
    )
    p.add_argument(
        "--speaker-cache-dir",
        default=os.environ.get(
            "SENSEVOICE_SPK_CACHE_DIR",
            os.path.join(os.path.expanduser("~"), ".cache", "sensevoice-vibe", "spkrec"),
        ),
        help="Cache directory for speaker verification model",
    )
    p.add_argument(
        "--speaker-score-agg",
        choices=["max", "mean", "topk_mean"],
        default=os.environ.get("SENSEVOICE_SPK_AGG", "topk_mean"),
        help="Aggregation over multiple enrollment templates",
    )
    p.add_argument(
        "--speaker-score-topk",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_TOPK", "3")),
        help="Top-k used when --speaker-score-agg=topk_mean",
    )
    p.add_argument(
        "--speaker-auto-enroll",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL", "0")),
        help="Auto-add high-confidence passed segments into speaker template set",
    )
    p.add_argument(
        "--speaker-auto-enroll-dir",
        default=os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_DIR", ""),
        help="Directory for auto-enrolled speaker templates (default: enrollment dir if it is a directory)",
    )
    p.add_argument(
        "--speaker-auto-enroll-min-score",
        type=float,
        default=float(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_MIN_SCORE", "0.74")),
        help="Minimum speaker score required to auto-enroll a segment",
    )
    p.add_argument(
        "--speaker-auto-enroll-min-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_MIN_MS", "2200")),
        help="Minimum segment duration required to auto-enroll",
    )
    p.add_argument(
        "--speaker-auto-enroll-cooldown-sec",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_COOLDOWN_SEC", "180")),
        help="Cooldown between auto-enroll operations",
    )
    p.add_argument(
        "--speaker-auto-enroll-max-templates",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_AUTO_ENROLL_MAX_TEMPLATES", "12")),
        help="Maximum number of templates kept in memory for speaker matching",
    )
    p.add_argument(
        "--speaker-adaptive",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_SPK_ADAPTIVE", "1")),
        help="Enable adaptive speaker threshold from recent valid scores",
    )
    p.add_argument(
        "--speaker-adaptive-window",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_ADAPTIVE_WINDOW", "80")),
        help="History window size for adaptive threshold",
    )
    p.add_argument(
        "--speaker-adaptive-min-samples",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_ADAPTIVE_MIN_SAMPLES", "10")),
        help="Minimum number of recent valid samples before adaptive threshold activates",
    )
    p.add_argument(
        "--speaker-adaptive-floor",
        type=float,
        default=float(os.environ.get("SENSEVOICE_SPK_ADAPTIVE_FLOOR", "0.52")),
        help="Lower bound of adaptive threshold",
    )
    p.add_argument(
        "--speaker-adaptive-margin",
        type=float,
        default=float(os.environ.get("SENSEVOICE_SPK_ADAPTIVE_MARGIN", "0.04")),
        help="Adaptive threshold = max(floor, q25 - margin)",
    )
    p.add_argument(
        "--speaker-prune-outliers",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_SPK_PRUNE_OUTLIERS", "1")),
        help="Prune outlier templates in memory at startup",
    )
    p.add_argument(
        "--speaker-prune-keep",
        type=int,
        default=int(os.environ.get("SENSEVOICE_SPK_PRUNE_KEEP", "10")),
        help="Number of central templates to keep after pruning",
    )
    p.add_argument(
        "--post-llm",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_ENABLE", "0")),
        help="Enable OpenAI-compatible LLM post-processing for final text",
    )
    p.add_argument(
        "--post-llm-base-url",
        default=os.environ.get("SENSEVOICE_POST_LLM_BASE_URL", os.environ.get("OPENAI_BASE_URL", "")),
        help="OpenAI-compatible base URL, e.g. http://host:port/v1",
    )
    p.add_argument(
        "--post-llm-api-key",
        default=os.environ.get("SENSEVOICE_POST_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        help="API key for OpenAI-compatible endpoint",
    )
    p.add_argument(
        "--post-llm-model",
        default=os.environ.get("SENSEVOICE_POST_LLM_MODEL", "DeepSeek-V3.1-Terminus"),
        help="Model name for LLM post-processing",
    )
    p.add_argument(
        "--post-llm-fallback-model",
        default=os.environ.get("SENSEVOICE_POST_LLM_FALLBACK_MODEL", ""),
        help="Optional fallback model if primary returns reasoning-only or invalid output",
    )
    p.add_argument(
        "--post-llm-mode",
        choices=["correct", "polish_light", "polish_coding", "polish_coding_aggressive"],
        default=os.environ.get("SENSEVOICE_POST_LLM_MODE", "correct"),
        help="LLM post mode: strict correction or light semantic polish",
    )
    p.add_argument(
        "--post-llm-timeout-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_TIMEOUT_MS", "900")),
        help="Timeout for LLM post-processing HTTP call",
    )
    p.add_argument(
        "--post-llm-max-tokens",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_MAX_TOKENS", "192")),
        help="Max tokens for LLM post-processing output",
    )
    p.add_argument(
        "--post-llm-temperature",
        type=float,
        default=float(os.environ.get("SENSEVOICE_POST_LLM_TEMPERATURE", "0.1")),
        help="Sampling temperature for LLM post-processing",
    )
    p.add_argument(
        "--post-llm-strict",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_STRICT", "0")),
        help="If enabled, block text injection when post-LLM fails",
    )
    p.add_argument(
        "--post-llm-circuit-max-fails",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_CIRCUIT_MAX_FAILS", "2")),
        help="Open post-LLM circuit after this many consecutive failures",
    )
    p.add_argument(
        "--post-llm-circuit-cooldown-sec",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_CIRCUIT_COOLDOWN_SEC", "120")),
        help="Cooldown while post-LLM circuit is open",
    )
    p.add_argument(
        "--post-llm-hard-cooldown-sec",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_HARD_COOLDOWN_SEC", "1800")),
        help="Long cooldown for hard failures (model/auth missing)",
    )
    p.add_argument(
        "--post-llm-retry-on-timeout",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_RETRY_ON_TIMEOUT", "1")),
        help="Retry once when post-LLM times out",
    )
    p.add_argument(
        "--post-llm-retry-backoff-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_RETRY_BACKOFF_MS", "80")),
        help="Backoff before post-LLM timeout retry",
    )
    p.add_argument(
        "--post-llm-model-auto",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_MODEL_AUTO", "1")),
        help="Auto-probe /models and select available post-LLM model",
    )
    p.add_argument(
        "--post-llm-model-probe-timeout-ms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_MODEL_PROBE_TIMEOUT_MS", "450")),
        help="Timeout for /models probe",
    )
    p.add_argument(
        "--post-llm-min-chars",
        type=int,
        default=int(os.environ.get("SENSEVOICE_POST_LLM_MIN_CHARS", "5")),
        help="Skip LLM post-processing for too-short texts",
    )
    p.add_argument(
        "--post-llm-dynamic-max-tokens",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_POST_LLM_DYNAMIC_MAX_TOKENS", "1")),
        help="Use dynamic per-utterance max_tokens for one-shot rewrite",
    )
    p.add_argument(
        "--post-llm-output-token-factor",
        type=float,
        default=float(os.environ.get("SENSEVOICE_POST_LLM_OUTPUT_TOKEN_FACTOR", "0.7")),
        help="Dynamic max_tokens factor by input length",
    )
    p.add_argument(
        "--project-lexicon",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_PROJECT_LEXICON_ENABLE", "1")),
        help="Enable project lexicon constraints for coding terms",
    )
    p.add_argument(
        "--project-root",
        default=os.environ.get("SENSEVOICE_PROJECT_ROOT", os.path.expanduser("~/mosim_workspace")),
        help="Project root used to build coding lexicon",
    )
    p.add_argument(
        "--project-lexicon-max-terms",
        type=int,
        default=int(os.environ.get("SENSEVOICE_PROJECT_LEXICON_MAX_TERMS", "2500")),
        help="Maximum number of lexicon terms extracted from project",
    )
    p.add_argument(
        "--project-lexicon-hint-limit",
        type=int,
        default=int(os.environ.get("SENSEVOICE_PROJECT_LEXICON_HINT_LIMIT", "16")),
        help="Max lexicon hints injected into post-LLM prompt",
    )
    p.add_argument(
        "--project-lexicon-min-term-len",
        type=int,
        default=int(os.environ.get("SENSEVOICE_PROJECT_LEXICON_MIN_TERM_LEN", "3")),
        help="Minimum identifier length for lexicon extraction",
    )
    p.add_argument(
        "--project-lexicon-extra-file",
        default=os.environ.get(
            "SENSEVOICE_PROJECT_LEXICON_EXTRA_FILE",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "hotwords_coding_zh.txt"),
        ),
        help="Optional extra lexicon file (one term per line)",
    )
    p.add_argument(
        "--conf-route",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_CONF_ROUTE_ENABLE", "1")),
        help="Enable confidence-based routing for post-processing",
    )
    p.add_argument(
        "--conf-high",
        type=float,
        default=float(os.environ.get("SENSEVOICE_CONF_ROUTE_HIGH", "0.78")),
        help="High-confidence threshold (skip heavy post-LLM)",
    )
    p.add_argument(
        "--conf-low",
        type=float,
        default=float(os.environ.get("SENSEVOICE_CONF_ROUTE_LOW", "0.52")),
        help="Low-confidence threshold (stronger post-LLM correction)",
    )
    p.add_argument(
        "--compare-log",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("SENSEVOICE_COMPARE_LOG_ENABLE", "1")),
        help="Write stage-by-stage compare records (raw/clean/llm/final) to JSONL",
    )
    p.add_argument(
        "--compare-log-file",
        default=os.environ.get(
            "SENSEVOICE_COMPARE_LOG_FILE",
            os.path.join(
                os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local/state")),
                "sensevoice-vibe",
                "post_compare.jsonl",
            ),
        ),
        help="JSONL file for before/after post-processing comparison",
    )
    p.add_argument(
        "--compare-log-keep-lines",
        type=int,
        default=int(os.environ.get("SENSEVOICE_COMPARE_LOG_KEEP_LINES", "300")),
        help="Maximum number of compare JSONL lines to keep",
    )
    return p.parse_args()
