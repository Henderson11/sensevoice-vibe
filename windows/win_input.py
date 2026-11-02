"""Windows 专用：全局热键 + 文字注入（纯 ctypes，不依赖第三方库）。

- HotkeyListener: RegisterHotKey 注册系统级热键。热键被系统吞掉，不会再传给前台程序，
  所以在 Guacamole/RDP 浏览器窗口里按 F8，F8 只会触发本地语音，不会被转发到远端。
- type_text: SendInput + KEYEVENTF_UNICODE，逐字符发送 Unicode 键盘事件，不占用剪贴板。
- paste_text: 剪贴板 + Ctrl+V 备选（SendInput 在某些应用/远程桌面里被忽略时用）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Callable, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_V = 0x56

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT

user32.OpenClipboard.argtypes = (wt.HWND,)
user32.OpenClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = (wt.UINT, wt.HANDLE)
user32.SetClipboardData.restype = wt.HANDLE
user32.GetClipboardData.argtypes = (wt.UINT,)
user32.GetClipboardData.restype = wt.HANDLE
kernel32.GlobalAlloc.argtypes = (wt.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = (wt.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wt.HGLOBAL,)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send(inputs: list) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def type_text(text: str, chunk: int = 24, delay_sec: float = 0.004) -> bool:
    """逐字符发 Unicode 键盘事件。非 BMP 字符按 UTF-16 代理对拆成两个单元发送。"""
    units = []
    raw = text.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        units.append(int.from_bytes(raw[i:i + 2], "little"))
    ok = True
    for i in range(0, len(units), chunk):
        batch = []
        for u in units[i:i + chunk]:
            batch.append(_key_input(0, u, KEYEVENTF_UNICODE))
            batch.append(_key_input(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if _send(batch) != len(batch):
            ok = False
        time.sleep(delay_sec)
    return ok


def _get_clipboard_text() -> Optional[str]:
    if not user32.OpenClipboard(None):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> bool:
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):  # 剪贴板可能被别的程序短暂占用
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        user32.CloseClipboard()


def paste_text(text: str, restore: bool = True) -> bool:
    old = _get_clipboard_text() if restore else None
    if not _set_clipboard_text(text):
        return False
    time.sleep(0.05)
    ok = _send([
        _key_input(VK_CONTROL), _key_input(VK_V),
        _key_input(VK_V, 0, KEYEVENTF_KEYUP), _key_input(VK_CONTROL, 0, KEYEVENTF_KEYUP),
    ]) == 4
    if restore and old is not None:
        time.sleep(0.25)  # 等目标程序读完剪贴板再还原
        _set_clipboard_text(old)
    return ok


# ---------------------------------------------------------------- 全局热键

_MODS = {"ALT": 0x0001, "CTRL": 0x0002, "CONTROL": 0x0002, "SHIFT": 0x0004, "WIN": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def parse_hotkey(spec: str):
    """'F8' / 'CTRL+F8' / 'ALT+SHIFT+V' → (modifiers, vk)"""
    mods = 0
    vk = 0
    for part in [p.strip().upper() for p in spec.split("+") if p.strip()]:
        if part in _MODS:
            mods |= _MODS[part]
        elif part.startswith("F") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x6F + int(part[1:])  # VK_F1=0x70
        elif len(part) == 1 and part.isalnum():
            vk = ord(part)
        else:
            raise ValueError(f"不支持的热键写法: {part}")
    if not vk:
        raise ValueError(f"热键缺少主键: {spec}")
    return mods, vk


class HotkeyListener(threading.Thread):
    def __init__(self, spec: str, on_press: Callable[[], None]):
        super().__init__(daemon=True, name="hotkey")
        self.mods, self.vk = parse_hotkey(spec)
        self.spec = spec
        self.on_press = on_press
        self.error = ""
        self.ready = threading.Event()
        self._tid = 0

    def run(self):
        self._tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk):
            self.error = f"RegisterHotKey({self.spec}) 失败, winerror={ctypes.get_last_error()}（热键可能被别的程序占用）"
            self.ready.set()
            return
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                try:
                    self.on_press()
                except Exception:
                    pass
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


# ---------------------------------------------------------------- 单击/双击修饰键热键（低级键盘钩子）
#
# 为什么不用 RegisterHotKey：它表达不了"只按一下 Ctrl"这种纯修饰键手势；
# 笔记本的 F1~F12 常被固件当功能键（飞行模式/静音…）占用，也不适合做热键。
# 规则（业界做法）：
#   - 单击: 目标键按下→很快松开，期间没按过别的键、没点过鼠标 → 触发
#   - 双击: 两次这样的"干净单击"间隔 < double_gap → 触发
#   - 从不吞键（永远 CallNextHookEx），所以不影响别的程序的快捷键
#   - 忽略注入事件（LLKHF_INJECTED），自己 SendInput 打字/Ctrl+V 不会反向触发
#   - 回调里只做轻量状态判断，触发动作交给 on_press（只是置个 Event）
#   - 钩子会漏抬起事件（见 _prune_held），"按着的键"以系统物理键状态为准，不能只信自己记的
#   - 钩子会被系统静默摘掉（见 _watchdog），每秒自检一次，发现就重装

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
_MOUSE_BREAK_MSGS = {0x0201, 0x0204, 0x0207, 0x020B, 0x020A}  # L/R/M/X 键按下、滚轮
LLKHF_INJECTED = 0x10
KEYEVENTF_EXTENDEDKEY = 0x0001

_TAP_KEYS = {
    "CTRL": {0xA2, 0xA3}, "LCTRL": {0xA2}, "RCTRL": {0xA3},
    "ALT": {0xA4, 0xA5}, "LALT": {0xA4}, "RALT": {0xA5},
    "SHIFT": {0xA0, 0xA1}, "LSHIFT": {0xA0}, "RSHIFT": {0xA1},
    "WIN": {0x5B, 0x5C}, "LWIN": {0x5B}, "RWIN": {0x5C},
    "MENU": {0x5D}, "PAUSE": {0x13}, "SCROLLLOCK": {0x91},
}

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wt.HMODULE
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetTickCount.restype = wt.DWORD
user32.SetTimer.argtypes = (wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p)
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = (wt.HWND, ctypes.c_size_t)

# Raw Input（watchdog 的"真值"：键盘到底有没有输入）
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT), ("dwFlags", wt.DWORD),
                ("hwndTarget", wt.HWND)]


user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = (wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = (wt.HWND,)
user32.RegisterRawInputDevices.argtypes = (ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT)
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wt.MSG),)

WM_TIMER = 0x0113
WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x00000100
HWND_MESSAGE = wt.HWND(-3)

_REPEAT_GAP_MAX = 1.2  # 自动重复间隔上限（系统首次重复延迟最长 1s），超过就不是自动重复
_HELD_TTL = 10.0       # 兜底：记着按下却 10s 没再收到它的按下/重复事件，视为抬起已丢
_WATCHDOG_MS = 1000    # 钩子存活自检周期
_MISSED_INPUT_MS = 1000  # Raw Input 收到的键盘输入比键盘钩子最后一次被调用还新出这么多 → 判定钩子已被摘


def parse_tap_spec(spec: str):
    """'TAP:RCTRL' / 'DOUBLETAP:CTRL' → (mode, vk 集合)"""
    mode, _, key = spec.strip().upper().partition(":")
    if mode not in ("TAP", "DOUBLETAP") or key not in _TAP_KEYS:
        raise ValueError(f"不支持的热键写法: {spec}（可用: TAP:/DOUBLETAP: + {', '.join(_TAP_KEYS)}）")
    return mode, _TAP_KEYS[key]


def is_tap_spec(spec: str) -> bool:
    return spec.strip().upper().startswith(("TAP:", "DOUBLETAP:"))


class TapHotkeyListener(threading.Thread):
    def __init__(self, spec: str, on_press: Callable[[], None], tap_max_sec: float = 0.45,
                 double_gap_sec: float = 0.40, accept_injected: bool = False,
                 logger: Optional[Callable[[str], None]] = None):
        super().__init__(daemon=True, name="taphotkey")
        self.spec = spec
        self.mode, self.vks = parse_tap_spec(spec)
        self.on_press = on_press
        self.tap_max = tap_max_sec
        self.double_gap = double_gap_sec
        self.accept_injected = accept_injected  # 仅自测用：允许合成按键触发
        self.logger = logger
        self.error = ""
        self.ready = threading.Event()
        self._tid = 0
        self._kb_hook = None
        self._ms_hook = None
        self._held = {}          # vk -> 最近一次按下/自动重复的时间
        self._down = False
        self._down_t = 0.0
        self._down_seen_t = 0.0  # 目标键最近一次按下/自动重复的时间
        self._clean = False
        self._last_tap_t = 0.0
        self._last_kb_cb_tick = 0  # 键盘钩子最后一次被调用（GetTickCount）
        self._last_raw_tick = 0    # Raw Input 最后一次收到键盘输入（GetTickCount）
        self._raw_hwnd = None
        self.rehooks = 0
        # 回调对象必须持有引用，否则被 GC 后钩子会崩
        self._kb_proc = HOOKPROC(self._kb_cb)
        self._ms_proc = HOOKPROC(self._ms_cb)
        self._wnd_proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))

    def _kb_cb(self, n_code, w_param, l_param):
        self._last_kb_cb_tick = kernel32.GetTickCount()
        try:
            if n_code == 0:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if self.accept_injected or not (kb.flags & LLKHF_INJECTED):
                    self._on_key(kb.vkCode, w_param in (WM_KEYDOWN, WM_SYSKEYDOWN))
        except Exception:
            pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)  # 永不吞键

    def _ms_cb(self, n_code, w_param, l_param):
        try:
            if n_code == 0 and w_param in _MOUSE_BREAK_MSGS:
                self._clean = False   # Ctrl+点击 / Ctrl+滚轮 不算单击
                self._last_tap_t = 0.0
        except Exception:
            pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _on_key(self, vk: int, is_down: bool):
        now = time.monotonic()
        if vk in self.vks:
            if is_down:
                if self._down:
                    if now - self._down_seen_t < _REPEAT_GAP_MAX:
                        self._down_seen_t = now
                        return  # 自动重复，忽略
                    # 隔了很久又来"按下"：上次的抬起丢了（如 Ctrl+Alt+Del），按新的一次按下处理
                    self._log(f"HOTKEY_LOST_KEYUP vk={vk:#x}")
                self._down = True
                self._down_t = self._down_seen_t = now
                self._prune_held(now)
                self._clean = not (self._held.keys() - self.vks)  # 已有别的键按着 → 不干净
                self._held[vk] = now
            else:
                self._held.pop(vk, None)
                if not self._down:
                    return
                self._down = False
                if self._clean and (now - self._down_t) <= self.tap_max:
                    self._register_tap(now)
                else:
                    self._last_tap_t = 0.0
            return
        # 其它键：按下 → 打断当前手势
        if is_down:
            self._held[vk] = now
            self._clean = False
            self._last_tap_t = 0.0
        else:
            self._held.pop(vk, None)

    def _prune_held(self, now: float):
        """丢掉"记着按下、其实早已松开"的键。
        LL 钩子会漏抬起：Win+L / Ctrl+Alt+Del / UAC 切到安全桌面时，抬起发生在安全桌面上收不到，
        该键就永远留在 _held 里 → 之后每次按 Ctrl 都判"不干净"，双击 Ctrl 永久失效，
        直到碰巧再按一次那个键（10-20 实测：进程、钩子、主循环全活着，就是不触发）。
        以系统物理键状态为准；_HELD_TTL 兜底连系统都以为没松开的幽灵键。"""
        stale = [k for k, t in self._held.items() if k not in self.vks and
                 (not (user32.GetAsyncKeyState(k) & 0x8000) or now - t > _HELD_TTL)]
        for k in stale:
            del self._held[k]
        if stale:
            self._log("HOTKEY_STALE_KEYS pruned=" + ",".join(f"{k:#x}" for k in stale))

    def _log(self, msg: str):
        if self.logger:  # 写文件放到别的线程，钩子回调要尽快返回（超时会被系统摘掉）
            threading.Thread(target=self.logger, args=(msg,), daemon=True).start()

    def _register_tap(self, now: float):
        if self.mode == "TAP":
            self.on_press()
            return
        if self._last_tap_t and (now - self._last_tap_t) <= self.double_gap:
            self._last_tap_t = 0.0
            self.on_press()
        else:
            self._last_tap_t = now

    def _install_hooks(self) -> bool:
        hmod = kernel32.GetModuleHandleW(None)
        self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
        if not self._kb_hook:
            return False
        self._ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
        self._last_kb_cb_tick = kernel32.GetTickCount()
        return True

    def _remove_hooks(self):
        for h in (self._kb_hook, self._ms_hook):
            if h:
                user32.UnhookWindowsHookEx(h)  # 已被系统摘掉的句柄会返回失败，无害
        self._kb_hook = self._ms_hook = None

    def _start_raw_input(self) -> str:
        """消息专用窗口 + RIDEV_INPUTSINK 键盘 Raw Input：前台是谁都能收到每个键盘事件（含合成的）。
        走消息队列，不像钩子那样会因超时被摘，所以拿它当"键盘到底有没有输入"的真值。成功返回空串。"""
        hinst = kernel32.GetModuleHandleW(None)
        cls = f"WinVoiceRawKbd{id(self):x}"
        wc = WNDCLASSW(lpfnWndProc=self._wnd_proc, hInstance=hinst, lpszClassName=cls)
        if not user32.RegisterClassW(ctypes.byref(wc)):
            return f"RegisterClass winerror={ctypes.get_last_error()}"
        self._raw_hwnd = user32.CreateWindowExW(0, cls, None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
        if not self._raw_hwnd:
            return f"CreateWindow winerror={ctypes.get_last_error()}"
        rid = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, self._raw_hwnd)  # Generic Desktop / Keyboard
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
            return f"RegisterRawInputDevices winerror={ctypes.get_last_error()}"
        return ""

    def _watchdog(self):
        """钩子存活自检（钩子线程里每 _WATCHDOG_MS 跑一次）。
        LL 钩子回调超时会被系统静默摘掉：不通知、不报错，表现就是进程活着、热键没反应。
        WinVoice 空闲时工作集会被换出到只剩十几 MB（10-20 实测，提交 1.7GB），首个事件的回调要先缺页换入，
        计划任务又给了低于正常的优先级，正是超时的高发条件。
        判据：Raw Input 收到的键盘输入比键盘钩子最后一次被调用还新出 _MISSED_INPUT_MS → 键盘钩子已死 → 两个都重装。
        不用 GetLastInputInfo：0 位移的合成鼠标事件（防休眠"抖鼠标"工具常这么做）会刷新它，
        却根本不调用 LL 鼠标钩子（10-20 实测），会每秒误判。鼠标钩子单独死掉判不出，只影响"Ctrl+点击打断手势"。"""
        missed = (self._last_raw_tick - self._last_kb_cb_tick) & 0xFFFFFFFF  # tick 是 32 位，49.7 天回绕
        if not (_MISSED_INPUT_MS < missed < 0x80000000):
            return
        self._remove_hooks()
        ok = self._install_hooks()
        self._last_kb_cb_tick = kernel32.GetTickCount()  # 装失败也别每秒重试刷屏：等下一段漏掉的输入再试
        self._held.clear()  # 摘除期间的事件全丢了，手势状态作废
        self._down = self._clean = False
        self._last_tap_t = 0.0
        self.rehooks += 1
        self._log(f"HOTKEY_REHOOK n={self.rehooks} missed_kb_ms={missed} ok={int(ok)}")

    def run(self):
        self._tid = kernel32.GetCurrentThreadId()
        if not self._install_hooks():
            self.error = f"安装键盘钩子失败, winerror={ctypes.get_last_error()}"
            self.ready.set()
            return
        raw_err = self._start_raw_input()
        timer = 0
        if raw_err:  # 拿不到真值就不做自检，热键本身照常工作
            self._log(f"HOTKEY_WATCHDOG disabled: {raw_err}")
        else:
            timer = user32.SetTimer(None, 0, _WATCHDOG_MS, None)  # 线程定时器：WM_TIMER 投递到本线程队列
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_INPUT:
                self._last_raw_tick = kernel32.GetTickCount()
            elif msg.message == WM_TIMER and not msg.hWnd:
                try:
                    self._watchdog()
                except Exception:
                    pass
            if msg.hWnd:
                user32.DispatchMessageW(ctypes.byref(msg))  # WM_INPUT 须经 DefWindowProc 让系统回收
        if timer:
            user32.KillTimer(None, timer)
        if self._raw_hwnd:
            user32.DestroyWindow(self._raw_hwnd)
        self._remove_hooks()

    def stop(self):
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


def make_hotkey_listener(spec: str, on_press: Callable[[], None],
                         logger: Optional[Callable[[str], None]] = None):
    """按写法自动选实现：'TAP:RCTRL'/'DOUBLETAP:CTRL' 走钩子；'F9'/'CTRL+ALT+H' 走 RegisterHotKey。"""
    if is_tap_spec(spec):
        return TapHotkeyListener(spec, on_press, logger=logger)
    return HotkeyListener(spec, on_press)


def synth_key(vk: int, up: bool = False) -> None:
    """合成一次按键（自测用）。右 Ctrl/Alt 需要 EXTENDEDKEY 标志。"""
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in (0xA3, 0xA5, 0x5C, 0x5D):
        flags |= KEYEVENTF_EXTENDEDKEY
    _send([_key_input(vk, 0, flags)])
