"""IBus engine Unix socket injector -- inject text without clipboard."""

import os
import subprocess
import time


class IBusInjector:
    """通过 IBus engine 的 Unix socket 注入文本 (不使用剪贴板)。

    每次注入时做毫秒级微切换:
      rime → sensevoice-voice → commit_text → rime
    rime 始终保持活跃，打字不受影响。
    """

    VOICE_ENGINE = "sensevoice-voice"
    RESTORE_ENGINE = "rime"

    def __init__(self):
        import socket as _socket
        self._socket_mod = _socket
        self.last_error = ""
        self.fail_streak = 0
        self.ack_timeout_sec = max(
            0.2, float(os.environ.get("SENSEVOICE_INJECT_ACK_TIMEOUT_SEC", "1.2"))
        )
        self._sock_path = os.path.join(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
            "sensevoice-ibus.sock",
        )
        self.RESTORE_ENGINE = os.environ.get(
            "SENSEVOICE_IBUS_RESTORE_ENGINE", "rime"
        )
        self._active = False

    def _switch_engine(self, name: str) -> bool:
        try:
            subprocess.run(
                ["ibus", "engine", name],
                capture_output=True, timeout=3,
            )
            return True
        except Exception:
            return False

    def _wait_socket(self, timeout: float = 3.0) -> bool:
        """等待 socket 可连接 (引擎进程可能需要 IBus daemon 自动启动)"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self._sock_path):
                try:
                    s = self._socket_mod.socket(
                        self._socket_mod.AF_UNIX, self._socket_mod.SOCK_STREAM
                    )
                    s.settimeout(0.3)
                    s.connect(self._sock_path)
                    s.close()
                    return True
                except OSError:
                    pass
            time.sleep(0.1)
        return False

    def send(self, mode: str, text: str) -> bool:
        if not text.strip():
            return True

        # COMMIT/FINAL/PARTIAL 都需要注入（PARTIAL 是 auto_enter=0 时的最终文本）
        is_text_inject = mode in ("COMMIT", "FINAL", "PARTIAL")
        if not is_text_inject:
            self._try_socket_send(f"{mode}\t{text}\n")
            return True

        # 微切换 rime → voice → commit → rime
        self._switch_engine(self.VOICE_ENGINE)
        time.sleep(0.15)

        if not self._wait_socket(timeout=3.0):
            self.last_error = "socket_not_ready"
            self._switch_engine(self.RESTORE_ENGINE)
            self.fail_streak += 1
            return False

        ok = self._try_socket_send(f"{mode}\t{text}\n")

        time.sleep(0.05)
        self._switch_engine(self.RESTORE_ENGINE)

        if ok:
            self.fail_streak = 0
        else:
            self.fail_streak += 1
        return ok

    def _try_socket_send(self, line: str) -> bool:
        for _ in range(2):
            try:
                s = self._socket_mod.socket(
                    self._socket_mod.AF_UNIX, self._socket_mod.SOCK_STREAM
                )
                s.settimeout(self.ack_timeout_sec)
                s.connect(self._sock_path)
                s.sendall(line.encode("utf-8"))
                ack = s.recv(256).decode("utf-8", errors="replace").strip()
                s.close()
                if ack.startswith("OK\t"):
                    self.last_error = ""
                    return True
                if ack.startswith("ERR\t"):
                    self.last_error = ack[4:] or "inject_error"
                else:
                    self.last_error = f"bad_ack:{ack[:80]}"
            except Exception as e:
                self.last_error = f"{type(e).__name__}:{e}"
                try:
                    s.close()  # type: ignore[possibly-undefined]
                except Exception:
                    pass
        return False

    def close(self) -> None:
        pass
