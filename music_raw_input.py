"""Background Raw Input transport-key receiver. Never injects or suppresses keys."""

import ctypes
from ctypes import wintypes
import threading


class RawInputDevice(ctypes.Structure):
    _fields_ = [("page", wintypes.USHORT), ("usage", wintypes.USHORT),
                ("flags", wintypes.DWORD), ("target", wintypes.HWND)]


class RawInputHeader(ctypes.Structure):
    _fields_ = [("kind", wintypes.DWORD), ("size", wintypes.DWORD),
                ("device", wintypes.HANDLE), ("wparam", ctypes.c_size_t)]


class RawKeyboard(ctypes.Structure):
    _fields_ = [("scan", wintypes.USHORT), ("flags", wintypes.USHORT),
                ("reserved", wintypes.USHORT), ("vk", wintypes.USHORT),
                ("message", wintypes.UINT), ("extra", wintypes.ULONG)]


TRANSPORT_KEYS = frozenset((0x77, 0x78, 0x79))
WM_INPUT = 0x00FF
WM_INPUT_DEVICE_CHANGE = 0x00FE
WM_STOP = 0x8001
RID_INPUT = 0x10000003


def decode_transport_packet(packet):
    header_size = ctypes.sizeof(RawInputHeader)
    if len(packet) < header_size + ctypes.sizeof(RawKeyboard):
        return None
    header = RawInputHeader.from_buffer_copy(packet)
    if header.kind != 1 or not header_size + ctypes.sizeof(RawKeyboard) <= header.size <= len(packet):
        return None
    key = RawKeyboard.from_buffer_copy(packet, header_size)
    if key.vk not in TRANSPORT_KEYS:
        return None
    return key.vk, not bool(key.flags & 1), header.device or 0


class RawKeyboardListener:
    def __init__(self, callback, removed, report):
        self.callback = callback
        self.removed = removed
        self.report = report
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.error = None
        self.thread = None
        self.hwnd = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="raw-transport-keys", daemon=True)
        self.thread.start()
        if not self.ready.wait(3):
            self.stop()
            raise RuntimeError("Raw Input startup timed out")
        if self.error:
            raise RuntimeError(self.error)

    def stop(self):
        self.closed.set()
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_STOP, 0, 0)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(2)

    def _run(self):
        registered = False
        try:
            u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            u.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                         wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                         wintypes.HINSTANCE, wintypes.LPVOID)
            u.CreateWindowExW.restype = wintypes.HWND
            u.RegisterRawInputDevices.argtypes = (ctypes.POINTER(RawInputDevice), wintypes.UINT, wintypes.UINT)
            u.RegisterRawInputDevices.restype = wintypes.BOOL
            u.GetRawInputData.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                         ctypes.POINTER(wintypes.UINT), wintypes.UINT)
            u.GetRawInputData.restype = wintypes.UINT
            u.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
            u.GetMessageW.restype = ctypes.c_int
            u.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
            u.DispatchMessageW.restype = ctypes.c_ssize_t
            u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            u.PostMessageW.restype = wintypes.BOOL
            u.DestroyWindow.argtypes = (wintypes.HWND,)
            u.DestroyWindow.restype = wintypes.BOOL

            # Message-only STATIC window: no visible UI, activation or keyboard grab.
            self.hwnd = u.CreateWindowExW(0, "STATIC", "Transport keys", 0, 0, 0, 0, 0,
                                          wintypes.HWND(-3), None, None, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            device = RawInputDevice(1, 6, 0x100 | 0x2000, self.hwnd)  # INPUTSINK | DEVNOTIFY
            if not u.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            self.ready.set()
            message = wintypes.MSG()
            while not self.closed.is_set():
                result = u.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0 or message.message == WM_STOP:
                    break
                if message.message == WM_INPUT:
                    try:
                        event = self._read(message.lParam)
                        if event is not None and not self.closed.is_set():
                            self.callback(*event)
                    finally:
                        u.DispatchMessageW(ctypes.byref(message))
                else:
                    if message.message == WM_INPUT_DEVICE_CHANGE and message.wParam == 2:
                        self.removed(message.lParam)
                    u.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.error = str(exc)
            self.report(f"Raw Input unavailable: {exc}")
        finally:
            if registered:
                device = RawInputDevice(1, 6, 1, None)  # RIDEV_REMOVE
                self.user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device))
            if self.hwnd:
                self.user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            self.ready.set()

    def _read(self, handle):
        size = wintypes.UINT()
        header_size = ctypes.sizeof(RawInputHeader)
        u = self.user32
        if u.GetRawInputData(handle, RID_INPUT, None, ctypes.byref(size), header_size) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if not header_size <= size.value <= 4096:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        received = u.GetRawInputData(handle, RID_INPUT, buffer, ctypes.byref(size), header_size)
        if received == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return decode_transport_packet(buffer.raw[:received])
