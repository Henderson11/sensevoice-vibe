"""Daemon 控制 socket — 替代 SIGHUP/USR2 + status 文件的简陋 IPC。

用 unix domain socket 提供同步 RPC：ACTIVATE/DEACTIVATE/TOGGLE/STATUS。
listener 在主循环启动前就 bind，所以模型加载期间命令也能被接收并入队，
模型 ready 后主循环立即消费——彻底消除 "import 阶段信号被默认 handler
吞掉" 这种竞态。
"""

from __future__ import annotations

import os
import socket
import threading
from typing import Callable, Optional

SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "sensevoice-ctl.sock",
)


class ControlServer:
    def __init__(
        self,
        get_state: Callable[[], dict],
        submit: Callable[[str], dict],
    ) -> None:
        self._get_state = get_state
        self._submit = submit
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(4)
        srv.settimeout(1.0)
        self._server = srv
        self._running = True
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="ctl-accept"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            if self._server is not None:
                self._server.close()
        except OSError:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

    def _accept_loop(self) -> None:
        while self._running:
            try:
                assert self._server is not None
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True, name="ctl-conn"
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2.0)
            buf = b""
            while self._running and b"\n" not in buf:
                chunk = conn.recv(256)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 1024:
                    return
            line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip().upper()
            ack = self._dispatch(line)
            try:
                conn.sendall((ack + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, cmd: str) -> str:
        if cmd == "STATUS":
            st = self._get_state()
            return f"OK\tactive={st['active']}\tready={st['ready']}\tpid={st['pid']}"
        if cmd in ("ACTIVATE", "DEACTIVATE", "TOGGLE"):
            r = self._submit(cmd)
            tail = "\tqueued=1" if r.get("queued") else ""
            return f"OK\tactive={r['active']}\tready={r['ready']}{tail}"
        return f"ERR\tunknown_cmd:{cmd[:32]}"
