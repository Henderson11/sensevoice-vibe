#!/usr/bin/env python3
"""
端到端测试: 切换到语音引擎 → 通过 socket 注入文本 → 切回 rime

用法:
    /usr/bin/python3 test_inject.py

测试流程:
  1. 3 秒倒计时 (请点击目标输入框)
  2. 切换 IBus 引擎到 sensevoice-voice (IBus daemon 自动启动引擎进程)
  3. 等待 socket 就绪
  4. 通过 socket 发送测试文本
  5. 切回 rime

验证:
  如果目标输入框出现 "你好世界_IBus语音测试" → 方案可行!
"""
import os
import socket
import subprocess
import sys
import time

ENGINE_NAME = "sensevoice-voice"
SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "sensevoice-ibus.sock",
)
RESTORE_ENGINE = "rime"


def switch_engine(name, timeout_sec=5):
    """切换 IBus 全局引擎"""
    subprocess.run(
        ["ibus", "engine", name],
        capture_output=True, text=True, timeout=timeout_sec,
    )
    # ibus engine 命令 rc 不可靠，用 ibus engine 查询验证
    time.sleep(0.3)
    check = subprocess.run(
        ["ibus", "engine"],
        capture_output=True, text=True, timeout=2,
    )
    actual = check.stdout.strip()
    return actual == name


def wait_socket(path, timeout_sec=5.0):
    """等待 socket 文件出现并可连接"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if os.path.exists(path):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(path)
                s.close()
                return True
            except OSError:
                pass
        time.sleep(0.2)
    return False


def send_text(sock_path, protocol_line):
    """通过 socket 发送一行文本，返回 ack"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.settimeout(2.0)
    s.sendall((protocol_line + "\n").encode())
    try:
        ack = s.recv(256).decode().strip()
    except socket.timeout:
        ack = "(no ack)"
    s.close()
    return ack


def main():
    # Step 1: 倒计时
    print("=== IBus 语音注入端到端测试 ===")
    for i in range(3, 0, -1):
        print(f"  {i} 秒后注入文本，请点击目标输入框...")
        time.sleep(1)

    # Step 2: 切换到语音引擎 (IBus daemon 自动启动引擎进程)
    print("切换到 sensevoice-voice...")
    if not switch_engine(ENGINE_NAME):
        print(f"FAIL: 无法切换到 {ENGINE_NAME}")
        sys.exit(1)
    print(f"  当前引擎: {ENGINE_NAME}")

    # Step 3: 等待 socket
    print("等待 socket 就绪...")
    if not wait_socket(SOCKET_PATH):
        print(f"FAIL: socket {SOCKET_PATH} 未就绪")
        switch_engine(RESTORE_ENGINE)
        sys.exit(1)
    print(f"  socket 就绪")

    # Step 4: 发送测试文本
    test_text = "你好世界_IBus语音测试"
    print(f"发送: COMMIT\\t{test_text}")
    time.sleep(0.3)  # 等引擎 focus_in
    ack = send_text(SOCKET_PATH, f"COMMIT\t{test_text}")
    print(f"  ack: {ack}")

    # Step 5: 切回 rime
    time.sleep(0.5)
    print("切回 rime...")
    switch_engine(RESTORE_ENGINE)
    print(f"  当前引擎: {RESTORE_ENGINE}")

    print()
    print("=" * 50)
    print(f"请检查输入框中是否出现: {test_text}")
    print("  出现了 → IBus 语音引擎方案可行!")
    print("  没出现 → 需要进一步调试")
    print("=" * 50)


if __name__ == "__main__":
    main()
