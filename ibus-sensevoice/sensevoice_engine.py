#!/usr/bin/env python3
"""
最小 IBus 语音引擎 —— 接收外部 ASR 文本并通过 commit_text 注入焦点输入框。

架构:
  stream_vad_realtime.py ──(Unix socket)──▶ sensevoice_engine.py
                                                 │
                                            commit_text()
                                                 │
                                                 ▼
                                           IBus daemon ──▶ 应用输入框

IPC 协议 (Unix socket, 行分隔):
  COMMIT\t<text>      → 直接 commit_text
  PREEDIT\t<text>     → 显示预编辑文本 (输入预览)
  PREEDIT_CLEAR       → 清除预编辑
  FINAL\t<text>       → commit_text (兼容现有协议)
  PARTIAL\t<text>     → 更新预编辑 (兼容现有协议)
  CTRL\tRESET         → 清除状态
  其他文本行          → 当作 COMMIT 处理

使用方式:
  1. 启动引擎进程: /usr/bin/python3 sensevoice_engine.py
     (socket 立即可用，无需等待引擎切换)
  2. 切换 IBus 引擎: ibus engine sensevoice-voice
  3. 通过 socket 发送文本
  4. 切回原引擎: ibus engine rime
"""
import os
import socket
import sys
import threading

import gi
gi.require_version("IBus", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, IBus

SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "sensevoice-ibus.sock",
)

ENGINE_NAME = "sensevoice-voice"

# 全局引用，供 socket 线程调度到 GLib 主循环
_engine_instance = None
_engine_lock = threading.Lock()


class SenseVoiceEngine(IBus.Engine):
    """最小语音引擎: 不拦截键盘事件，只通过 socket 接收文本并 commit。"""

    __gtype_name__ = "SenseVoiceEngine"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._has_focus = False
        self._preedit_text = ""
        global _engine_instance
        with _engine_lock:
            _engine_instance = self

    # ── IBus Engine 回调 ─────────────────────────────────────────

    def do_process_key_event(self, keyval, keycode, state):
        # 不拦截任何按键，全部透传给应用
        return False

    def do_focus_in(self):
        self._has_focus = True

    def do_focus_out(self):
        self._has_focus = False
        self._clear_preedit()

    def do_enable(self):
        self._has_focus = True

    def do_disable(self):
        self._has_focus = False
        self._clear_preedit()

    def do_destroy(self):
        global _engine_instance
        with _engine_lock:
            if _engine_instance is self:
                _engine_instance = None

    # ── 文本操作 ─────────────────────────────────────────────────

    def do_commit(self, text):
        """提交文本到焦点输入框"""
        if not text:
            return
        self._clear_preedit()
        ibus_text = IBus.Text.new_from_string(text)
        self.commit_text(ibus_text)

    def do_preedit(self, text):
        """显示预编辑文本 (输入预览，尚未提交)"""
        if not text:
            self._clear_preedit()
            return
        self._preedit_text = text
        ibus_text = IBus.Text.new_from_string(text)
        attr_list = IBus.AttrList()
        attr_list.append(
            IBus.Attribute.new(
                IBus.AttrType.UNDERLINE,
                IBus.AttrUnderline.SINGLE,
                0,
                len(text),
            )
        )
        ibus_text.set_attributes(attr_list)
        self.update_preedit_text(ibus_text, len(text), True)

    def _clear_preedit(self):
        if self._preedit_text:
            self._preedit_text = ""
            self.hide_preedit_text()


# ── Socket IPC (进程级别，不依赖引擎实例) ────────────────────────


def _dispatch_to_engine(line, conn):
    """解析协议并在 GLib 主循环中分派到引擎实例"""
    with _engine_lock:
        engine = _engine_instance

    if engine is None:
        try:
            conn.sendall(b"ERR\tno_engine\n")
        except OSError:
            pass
        return

    ack = "OK"
    if line == "CTRL\tRESET":
        GLib.idle_add(engine._clear_preedit)
    elif line == "PREEDIT_CLEAR":
        GLib.idle_add(engine._clear_preedit)
    elif line.startswith("COMMIT\t"):
        GLib.idle_add(engine.do_commit, line[7:])
    elif line.startswith("FINAL\t"):
        GLib.idle_add(engine.do_commit, line[6:])
    elif line.startswith("PARTIAL\t"):
        # 微切换模式下 preedit 会在切回 rime 时被清除，所以直接 commit
        GLib.idle_add(engine.do_commit, line[8:])
    elif line.startswith("PREEDIT\t"):
        GLib.idle_add(engine.do_preedit, line[8:])
    elif line.startswith("SEG\t"):
        pass  # segment boundary
    else:
        GLib.idle_add(engine.do_commit, line)

    try:
        conn.sendall(f"{ack}\t{line[:20]}\n".encode())
    except OSError:
        pass


def _handle_client(conn, running_flag):
    """处理单个客户端连接"""
    buf = b""
    try:
        while running_flag[0]:
            data = conn.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                text = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
                if text:
                    _dispatch_to_engine(text, conn)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def start_socket_listener():
    """启动 Unix socket 监听线程 (进程级别)"""
    running = [True]

    def socket_loop():
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        server.listen(2)
        server.settimeout(1.0)

        while running[0]:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=_handle_client,
                args=(conn, running),
                daemon=True,
            ).start()

        try:
            server.close()
        except OSError:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

    t = threading.Thread(target=socket_loop, daemon=True)
    t.start()
    return running  # 调用方可设 running[0] = False 来停止


# ── IBus 引擎工厂和主循环 ────────────────────────────────────────


class SenseVoiceFactory(IBus.Factory):
    """创建 SenseVoiceEngine 实例"""

    _engine_count = 0

    def __init__(self, bus):
        super().__init__(
            object_path=IBus.PATH_FACTORY,
            connection=bus.get_connection(),
        )
        self._bus = bus

    def do_create_engine(self, engine_name):
        if engine_name == ENGINE_NAME:
            SenseVoiceFactory._engine_count += 1
            return SenseVoiceEngine(
                engine_name=ENGINE_NAME,
                object_path=f"/org/freedesktop/IBus/Engine/{SenseVoiceFactory._engine_count}",
                connection=self._bus.get_connection(),
            )
        return super().do_create_engine(engine_name)


def main(exec_by_ibus=False):
    IBus.init()
    bus = IBus.Bus()
    if not bus.is_connected():
        print("ERROR: 无法连接到 IBus 守护进程", file=sys.stderr)
        sys.exit(1)

    # 1. 先启动 socket 监听 (引擎实例还没有，但 socket 已可接受连接)
    running_flag = start_socket_listener()

    # 2. 创建引擎工厂
    factory = SenseVoiceFactory(bus)  # noqa: F841

    # 3. 仅手动启动时注册组件 (IBus daemon 启动时已知组件)
    if not exec_by_ibus:
        component = IBus.Component.new(
            "im.sensevoice.SenseVoice",
            "SenseVoice Voice Input Engine",
            "0.1", "MIT", "sensevoice", "", "", "",
        )
        engine_desc = IBus.EngineDesc.new(
            ENGINE_NAME, "SenseVoice Voice",
            "Voice input via SenseVoice ASR",
            "zh", "MIT", "sensevoice", "", "default",
        )
        component.add_engine(engine_desc)
        bus.register_component(component)

    bus.request_name("im.sensevoice.SenseVoice", 0)

    print(f"SenseVoice IBus engine started, socket: {SOCKET_PATH}", flush=True)
    mainloop = GLib.MainLoop()

    def on_disconnected(_bus):
        mainloop.quit()

    bus.connect("disconnected", on_disconnected)

    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass
    finally:
        running_flag[0] = False
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ibus", action="store_true",
                        help="Launched by IBus daemon")
    args = parser.parse_args()
    main(exec_by_ibus=args.ibus)
