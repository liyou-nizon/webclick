"""全局热键：焦点在浏览器里也能停下录制。

实现：后台线程 RegisterHotKey + GetMessage 消息循环（事件驱动，不会漏按），
注册失败时退回 GetAsyncKeyState 轮询。轮询/监听都不需要管理员权限。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8

_VK = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B,
    "SPACE": 0x20, "PAUSE": 0x13, "CAPSLOCK": 0x14,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "NUM0": 0x60, "NUM1": 0x61, "NUM2": 0x62, "NUM3": 0x63, "NUM4": 0x64,
    "NUM5": 0x65, "NUM6": 0x66, "NUM7": 0x67, "NUM8": 0x68, "NUM9": 0x69,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
_MOD = {"CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL, "ALT": MOD_ALT,
        "SHIFT": MOD_SHIFT, "WIN": MOD_WIN, "META": MOD_WIN}


def _vk_for(name: str) -> int | None:
    n = name.strip().upper()
    if n in _VK:
        return _VK[n]
    if len(n) == 1 and ("A" <= n <= "Z" or "0" <= n <= "9"):
        return ord(n)
    if n.startswith("F") and n[1:].isdigit() and 1 <= int(n[1:]) <= 24:
        return 0x6F + int(n[1:])
    return None


class Hotkey:
    """解析 "F9" / "ctrl+shift+q"，并把它注册成系统级热键。"""

    def __init__(self, spec: str | None = "F9", log=print):
        self.spec = (spec or "").strip()
        self.log = log
        self.ok = False
        self.error = ""
        self.vk = 0
        self.mods = 0
        self._fired = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._mode = ""
        self._ready = threading.Event()
        if not self.spec:
            self.error = "未启用"
            return
        if os.name != "nt":
            self.error = "仅 Windows 支持全局热键"
            return
        parts = [p for p in self.spec.replace(" ", "").split("+") if p]
        if not parts:
            self.error = "热键为空"
            return
        vk = _vk_for(parts[-1])
        if vk is None:
            self.error = f"不认识的键: {parts[-1]}"
            return
        for m in parts[:-1]:
            mv = _MOD.get(m.upper())
            if mv is None:
                self.error = f"不认识的修饰键: {m}"
                return
            self.mods |= mv
        self.vk = vk
        self.user32 = ctypes.windll.user32
        self._thread = threading.Thread(target=self._listen, name="webclick-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        if not self.ok:
            self._start_polling()

    # ---------- 事件驱动 ----------
    def _listen(self) -> None:
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            ok = self.user32.RegisterHotKey(None, 1, self.mods, self.vk)
        except Exception as exc:  # noqa: BLE001
            self.error = f"RegisterHotKey 异常: {exc}"
            self._ready.set()
            return
        if not ok:
            self.error = "热键被占用或注册失败"
            self._ready.set()
            return
        self.ok = True
        self._mode = "hotkey"
        self._ready.set()
        msg = wt.MSG()
        try:
            while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._fired.set()
        finally:
            with _suppress():
                self.user32.UnregisterHotKey(None, 1)

    # ---------- 轮询兜底 ----------
    def _start_polling(self) -> None:
        if not hasattr(self, "user32"):
            return
        self.ok = True
        self._mode = "poll"
        self._prev = False
        self.error = ""

    def _poll_once(self) -> None:
        def down(vk: int) -> bool:
            return bool(self.user32.GetAsyncKeyState(vk) & 0x8000)

        mods_ok = True
        for bit, vk in ((MOD_CONTROL, 0x11), (MOD_ALT, 0x12), (MOD_SHIFT, 0x10), (MOD_WIN, 0x5B)):
            if self.mods & bit and not down(vk):
                mods_ok = False
                break
        active = down(self.vk) and mods_ok
        if active and not self._prev:
            self._fired.set()
        self._prev = active

    # ---------- 对外 ----------
    def poll(self) -> bool:
        """有触发返回一次 True 并清标记。"""
        if not self.ok:
            return False
        if self._mode == "poll":
            self._poll_once()
        if self._fired.is_set():
            self._fired.clear()
            return True
        return False

    def describe(self) -> str:
        if not self.ok:
            return f"(不可用: {self.error})"
        txt = self.spec.upper()
        for a, b in (("CTRL", "Ctrl"), ("SHIFT", "Shift"), ("ALT", "Alt"), ("WIN", "Win")):
            txt = txt.replace(a, b)
        return txt + ("  [轮询]" if self._mode == "poll" else "")

    def close(self) -> None:
        if self._thread_id:
            with _suppress():
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


# ---------------- 测试用：真的发一次按键 ----------------
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


def send_key(vk: int, up: bool = False) -> None:
    """SendInput 注入按键（会触发 RegisterHotKey 注册的热键）。"""
    flags = 0x0002 if up else 0
    inp = _INPUT(type=1)
    inp.union.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
