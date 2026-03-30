"""Focus-based injector -- inject text via a subprocess helper script."""

import os
import select
import subprocess
import time
from typing import Optional


class FocusInjector:
    def __init__(self, script_path: str):
        self.script_path = script_path
        self.proc: Optional[subprocess.Popen] = None
        self.last_error = ""
        self.fail_streak = 0
        self.ack_timeout_sec = max(0.2, float(os.environ.get("SENSEVOICE_INJECT_ACK_TIMEOUT_SEC", "1.2")))

    def _ensure(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        # Terminate stale process (exited but not yet cleaned up) before
        # creating a new one, preventing leaked zombie/orphan processes.
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.proc = subprocess.Popen(
            [self.script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def _restart(self) -> None:
        self.close()
        self._ensure()

    def send(self, mode: str, text: str) -> bool:
        if not text.strip():
            return True
        line = f"{mode}\t{text}\n"
        for _ in range(2):
            try:
                self._ensure()
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                if self._await_ack():
                    self.last_error = ""
                    self.fail_streak = 0
                    return True
            except BrokenPipeError:
                self.last_error = "broken_pipe"
            except Exception as e:
                self.last_error = f"{type(e).__name__}"
            self._restart()
        self.fail_streak += 1
        return False

    def _await_ack(self) -> bool:
        if self.proc is None or self.proc.stdout is None:
            self.last_error = "no_stdout"
            return False
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + self.ack_timeout_sec
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                self.last_error = "ack_timeout"
                return False
            ready, _, _ = select.select([fd], [], [], remain)
            if not ready:
                self.last_error = "ack_timeout"
                return False
            ack = self.proc.stdout.readline()
            if ack == "":
                self.last_error = "injector_exit"
                return False
            ack = ack.strip()
            if not ack:
                continue
            if ack.startswith("OK\t"):
                return True
            if ack.startswith("ERR\t"):
                self.last_error = ack[4:] or "inject_error"
                return False
            self.last_error = f"bad_ack:{ack[:80]}"
            return False

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        self.proc = None
