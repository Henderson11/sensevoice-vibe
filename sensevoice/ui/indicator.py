"""Desktop notification indicator for speech detection."""

import subprocess
from shutil import which as _which


def _shutil_which(cmd: str) -> bool:
    return _which(cmd) is not None


class SpeechIndicator:
    def __init__(self, mode: str):
        self.mode = mode
        self.active = False
        self.notified_once = False
        self.sync_key = "sensevoice-stream-vad"

    def _notify(self, text: str, timeout_ms: int) -> None:
        if self.mode not in ("notify", "notify_once"):
            return
        if not _shutil_which("notify-send"):
            return
        subprocess.run(
            [
                "notify-send",
                "-a",
                "SenseVoice VAD",
                "-u",
                "low",
                "-t",
                str(timeout_ms),
                "-h",
                f"string:x-canonical-private-synchronous:{self.sync_key}",
                text,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def on_speech_start(self) -> None:
        if self.mode == "none":
            return
        if self.active:
            return
        if self.mode == "notify_once" and self.notified_once:
            return
        self.active = True
        self.notified_once = True
        self._notify("Listening: speech detected", 1200)

    def on_speech_end(self) -> None:
        if not self.active:
            return
        self.active = False

    def on_shutdown(self) -> None:
        if self.active:
            self.on_speech_end()

    def reset_session(self) -> None:
        self.active = False
