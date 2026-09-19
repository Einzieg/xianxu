"""Small adapters around the existing DD/Win32 input backends."""

import ctypes
from ctypes import wintypes
import os
import threading


class KeyboardSink:
    def __init__(self, backend, keys):
        self.backend = backend
        self.held = set()
        self.lock = threading.RLock()
        backend.ensure_ready()
        self.actions = {}
        for key in set(keys):
            action = backend.prepare_keyboard_action(ord(key.upper()), key.lower())
            if action is None:
                raise RuntimeError(f"输入后端不支持按键 {key}")
            self.actions[key] = action

    def key_down(self, key):
        with self.lock:
            self.held.add(key)
            if not self.backend.press_keyboard_action(self.actions[key]):
                raise RuntimeError(f"按下 {key} 失败")

    def key_up(self, key):
        with self.lock:
            if not self.backend.release_keyboard_action(self.actions[key]):
                raise RuntimeError(f"释放 {key} 失败")
            self.held.discard(key)

    def release_all(self):
        with self.lock:
            errors = []
            for key in tuple(self.held):
                for _ in range(3):
                    try:
                        self.key_up(key)
                        break
                    except Exception as exc:
                        error = str(exc)
                else:
                    errors.append(error)
            if errors:
                raise RuntimeError("；".join(errors))


class PreviewSink:
    def key_down(self, key):
        pass

    def key_up(self, key):
        pass


class TargetWindow:
    def __init__(self, user32, hwnd):
        self.user32 = user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.hwnd = hwnd
        self.pid = self._pid()
        if not hwnd or not user32.IsWindow(hwnd) or not self.pid or self.pid == os.getpid():
            raise ValueError("请选择游戏窗口，不能选择演奏器自身")
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        self.title = title.value or f"窗口 {hwnd}"

    def _pid(self):
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
        return pid.value

    def state(self):
        if not self.user32.IsWindow(self.hwnd) or self._pid() != self.pid:
            return "closed"
        return "ready" if self.user32.GetForegroundWindow() == self.hwnd else "background"

    def activate(self):
        if self.state() == "closed":
            return False
        user32 = self.user32
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
        current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        attached = current_thread != foreground_thread and user32.AttachThreadInput(current_thread, foreground_thread, True)
        try:
            if user32.IsIconic(self.hwnd):
                user32.ShowWindow(self.hwnd, 9)
            user32.BringWindowToTop(self.hwnd)
            user32.SetForegroundWindow(self.hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        return self.state() == "ready"
