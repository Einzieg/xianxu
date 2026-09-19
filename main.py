import ctypes
import os
import threading
import time
from pathlib import Path
from ctypes import wintypes

import customtkinter as ctk
from pynput import keyboard

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

USER32 = ctypes.windll.user32
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

ACTION_MOUSE = "鼠标左键"
ACTION_KEYBOARD = "键盘按键"

SEND_MODE_WIN32 = "Win32 兼容"
SEND_MODE_DD = "DD 驱动"
SEND_MODE_DD_TARGET = "DD 驱动(目标窗口)"
SEND_MODE_WINDOW_MESSAGE = "目标窗口消息"

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
TARGET_WINDOW_CAPTURE_DELAY_MS = 3000

APP_DIR = Path(__file__).resolve().parent
DD_DRIVER_ENV = "DD_DLL_PATH"
DD_HOLD_MS_ENV = "DD_HOLD_MS"
DD_DRIVER_CANDIDATES = (
    Path("DD") / "64" / "ddx64.64.dll",
    Path("DD") / "64" / "ddx64.32.dll",
    Path("DD") / "32" / "ddx32.dll",
    Path("2026.DD.EV.HVCI.63xxx") / "2.hid" / "ddhid.63340.dll",
    Path("2026.DD.EV.HVCI.63xxx") / "1.simple" / "dd63330.dll",
)
DD_DRIVER_GLOB_DIRS = (
    APP_DIR,
    APP_DIR / "DD",
    APP_DIR / "DD" / "64",
    APP_DIR / "DD" / "32",
    APP_DIR / "drivers",
    APP_DIR / "2026.DD.EV.HVCI.63xxx",
    APP_DIR / "2026.DD.EV.HVCI.63xxx" / "1.simple",
    APP_DIR / "2026.DD.EV.HVCI.63xxx" / "2.hid",
)
DD_DRIVER_GLOBS = ("dd*.dll", "DD*.dll")
DEFAULT_DD_HOLD_MS = 20

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

FONT_TITLE = ("Microsoft YaHei UI", 20, "bold")
FONT_TEXT = ("Microsoft YaHei UI", 13)
FONT_BOLD = ("Microsoft YaHei UI", 13, "bold")
FONT_BUTTON = ("Microsoft YaHei UI", 15, "bold")
FONT_STATUS = ("Microsoft YaHei UI", 11)

WINDOW_WIDTH = 400
WINDOW_HEIGHT = 610
SETTINGS_VALUE_WIDTH = 170
SETTINGS_HINT_WRAP = 320
STATUS_WRAP = 350


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("value", INPUT_UNION),
    ]


SPECIAL_KEY_TO_VK = {
    keyboard.Key.alt: 0x12,
    keyboard.Key.alt_l: 0xA4,
    keyboard.Key.alt_r: 0xA5,
    keyboard.Key.backspace: 0x08,
    keyboard.Key.caps_lock: 0x14,
    keyboard.Key.cmd: 0x5B,
    keyboard.Key.cmd_l: 0x5B,
    keyboard.Key.cmd_r: 0x5C,
    keyboard.Key.ctrl: 0x11,
    keyboard.Key.ctrl_l: 0xA2,
    keyboard.Key.ctrl_r: 0xA3,
    keyboard.Key.delete: 0x2E,
    keyboard.Key.down: 0x28,
    keyboard.Key.end: 0x23,
    keyboard.Key.enter: 0x0D,
    keyboard.Key.esc: 0x1B,
    keyboard.Key.home: 0x24,
    keyboard.Key.insert: 0x2D,
    keyboard.Key.left: 0x25,
    keyboard.Key.menu: 0x5D,
    keyboard.Key.num_lock: 0x90,
    keyboard.Key.page_down: 0x22,
    keyboard.Key.page_up: 0x21,
    keyboard.Key.pause: 0x13,
    keyboard.Key.print_screen: 0x2C,
    keyboard.Key.right: 0x27,
    keyboard.Key.scroll_lock: 0x91,
    keyboard.Key.shift: 0x10,
    keyboard.Key.shift_l: 0xA0,
    keyboard.Key.shift_r: 0xA1,
    keyboard.Key.space: 0x20,
    keyboard.Key.tab: 0x09,
    keyboard.Key.up: 0x26,
}

for index in range(1, 25):
    SPECIAL_KEY_TO_VK[getattr(keyboard.Key, f"f{index}")] = 0x6F + index

EXTENDED_KEY_VKS = {
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2D,
    0x2E,
    0x5B,
    0x5C,
    0x5D,
    0xA3,
    0xA5,
}

DISPLAY_NAME_BY_CHAR = {
    " ": "SPACE",
}


class InputBackendError(RuntimeError):
    pass


def resolve_driver_path(raw_path):
    driver_path = Path(raw_path).expanduser()
    if not driver_path.is_absolute():
        driver_path = APP_DIR / driver_path
    return driver_path.resolve()


def find_dd_driver_dll():
    env_driver_path = os.getenv(DD_DRIVER_ENV)
    if env_driver_path:
        candidate = resolve_driver_path(env_driver_path)
        if candidate.is_file():
            return candidate

    for relative_path in DD_DRIVER_CANDIDATES:
        candidate = (APP_DIR / relative_path).resolve()
        if candidate.is_file():
            return candidate

    for search_dir in DD_DRIVER_GLOB_DIRS:
        if not search_dir.is_dir():
            continue
        for pattern in DD_DRIVER_GLOBS:
            matches = sorted(search_dir.glob(pattern))
            if matches:
                return matches[0].resolve()

    return None


def get_dd_hold_seconds():
    raw_value = os.getenv(DD_HOLD_MS_ENV, str(DEFAULT_DD_HOLD_MS))
    try:
        hold_ms = max(1, int(raw_value))
    except ValueError:
        hold_ms = DEFAULT_DD_HOLD_MS
    return hold_ms / 1000.0


def get_key_char(key):
    try:
        key_char = key.char
    except AttributeError:
        return None
    if key_char and len(key_char) == 1:
        return key_char
    return None


class Win32InputBackend:
    def __init__(self, user32):
        self.mouse_event = user32.mouse_event
        self.send_input = user32.SendInput
        self.map_virtual_key = user32.MapVirtualKeyW

        self.send_input.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
        self.send_input.restype = ctypes.c_uint
        self.map_virtual_key.argtypes = (ctypes.c_uint, ctypes.c_uint)
        self.map_virtual_key.restype = ctypes.c_uint

    def ensure_ready(self):
        return None

    def describe(self):
        return SEND_MODE_WIN32

    def prepare_keyboard_action(self, vk_code, key_char=None):
        if vk_code is None:
            return None

        scan_code = self.map_virtual_key(vk_code, MAPVK_VK_TO_VSC)
        if scan_code == 0:
            return None

        flags = KEYEVENTF_SCANCODE
        if vk_code in EXTENDED_KEY_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY

        return {"scan_code": scan_code, "flags": flags}

    def send_keyboard_action(self, prepared_key_action, target_hwnd=None):
        inputs = (INPUT * 2)()
        inputs[0] = INPUT(
            type=INPUT_KEYBOARD,
            value=INPUT_UNION(
                ki=KEYBDINPUT(
                    wVk=0,
                    wScan=prepared_key_action["scan_code"],
                    dwFlags=prepared_key_action["flags"],
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        inputs[1] = INPUT(
            type=INPUT_KEYBOARD,
            value=INPUT_UNION(
                ki=KEYBDINPUT(
                    wVk=0,
                    wScan=prepared_key_action["scan_code"],
                    dwFlags=prepared_key_action["flags"] | KEYEVENTF_KEYUP,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        return self.send_input(2, inputs, ctypes.sizeof(INPUT)) == 2

    def click_left(self):
        self.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def release_left(self):
        self.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def press_keyboard_action(self, prepared_key_action):
        input_item = INPUT(
            type=INPUT_KEYBOARD,
            value=INPUT_UNION(ki=KEYBDINPUT(
                wVk=0, wScan=prepared_key_action["scan_code"],
                dwFlags=prepared_key_action["flags"], time=0, dwExtraInfo=0,
            )),
        )
        return self.send_input(1, ctypes.byref(input_item), ctypes.sizeof(INPUT)) == 1

    def release_keyboard_action(self, prepared_key_action, target_hwnd=None):
        input_item = INPUT(
            type=INPUT_KEYBOARD,
            value=INPUT_UNION(
                ki=KEYBDINPUT(
                    wVk=0,
                    wScan=prepared_key_action["scan_code"],
                    dwFlags=prepared_key_action["flags"] | KEYEVENTF_KEYUP,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        return self.send_input(1, ctypes.byref(input_item), ctypes.sizeof(INPUT)) == 1


class DDInputBackend:
    def __init__(self):
        self.dll = None
        self.dll_path = None
        self.dd_btn = None
        self.dd_key = None
        self.dd_todc = None
        self.hold_seconds = get_dd_hold_seconds()

    def candidate_path(self):
        if self.dll_path and self.dll_path.is_file():
            return self.dll_path

        self.dll_path = find_dd_driver_dll()
        return self.dll_path

    def describe(self):
        candidate = self.candidate_path()
        if candidate is None:
            return SEND_MODE_DD
        return f"{SEND_MODE_DD} [{candidate.name}]"

    def ensure_ready(self):
        if self.dll is not None:
            return None

        driver_path = self.candidate_path()
        if driver_path is None:
            raise InputBackendError(
                f"未找到 DD 驱动 DLL，请将 DLL 放到程序目录、DD 子目录，或设置环境变量 {DD_DRIVER_ENV}"
            )

        try:
            self.dll = ctypes.WinDLL(str(driver_path))
            self.dd_btn = self.dll.DD_btn
            self.dd_key = self.dll.DD_key
            self.dd_todc = self.dll.DD_todc
        except AttributeError as exc:
            self.dll = None
            raise InputBackendError(f"DD DLL 缺少必要导出: {exc}") from exc
        except OSError as exc:
            self.dll = None
            raise InputBackendError(f"加载 DD 驱动失败: {exc}") from exc

        self.dd_btn.argtypes = (ctypes.c_int,)
        self.dd_btn.restype = ctypes.c_int
        self.dd_key.argtypes = (ctypes.c_int, ctypes.c_int)
        self.dd_key.restype = ctypes.c_int
        self.dd_todc.argtypes = (ctypes.c_int,)
        self.dd_todc.restype = ctypes.c_int

        status = self.dd_btn(0)
        if status != 1:
            # The bundled DD example waits for the DLL's asynchronous initialization.
            time.sleep(2.0)
            status = self.dd_btn(0)
        if status != 1:
            self.dll = None
            raise InputBackendError(f"DD 初始化失败，返回值: {status}")
        return None

    def prepare_keyboard_action(self, vk_code, key_char=None):
        self.ensure_ready()
        if vk_code is None:
            return None

        dd_code = self.dd_todc(vk_code)
        if dd_code <= 0:
            return None

        return {"dd_code": dd_code}

    def send_keyboard_action(self, prepared_key_action, target_hwnd=None):
        self.ensure_ready()
        self.dd_key(prepared_key_action["dd_code"], 1)
        time.sleep(self.hold_seconds)
        self.dd_key(prepared_key_action["dd_code"], 2)
        return True

    def press_keyboard_action(self, prepared_key_action):
        self.ensure_ready()
        return self.dd_key(prepared_key_action["dd_code"], 1) == 1

    def click_left(self):
        self.ensure_ready()
        self.dd_btn(1)
        time.sleep(self.hold_seconds)
        self.dd_btn(2)

    def release_left(self):
        self.ensure_ready()
        self.dd_btn(2)

    def release_keyboard_action(self, prepared_key_action, target_hwnd=None):
        self.ensure_ready()
        return self.dd_key(prepared_key_action["dd_code"], 2) == 1


class WindowMessageInputBackend:
    def __init__(self, user32):
        self.post_message = user32.PostMessageW
        self.map_virtual_key = user32.MapVirtualKeyW
        self.is_window = user32.IsWindow

        self.post_message.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.post_message.restype = wintypes.BOOL
        self.map_virtual_key.argtypes = (ctypes.c_uint, ctypes.c_uint)
        self.map_virtual_key.restype = ctypes.c_uint
        self.is_window.argtypes = (wintypes.HWND,)
        self.is_window.restype = wintypes.BOOL

    def ensure_ready(self):
        return None

    def describe(self):
        return SEND_MODE_WINDOW_MESSAGE

    def build_lparam(self, scan_code, is_key_up, is_extended):
        lparam = 1 | (scan_code << 16)
        if is_extended:
            lparam |= 1 << 24
        if is_key_up:
            lparam |= 1 << 30
            lparam |= 1 << 31
        return lparam

    def prepare_keyboard_action(self, vk_code, key_char=None):
        if vk_code is None:
            return None

        scan_code = self.map_virtual_key(vk_code, MAPVK_VK_TO_VSC)
        if scan_code == 0:
            return None

        is_extended = vk_code in EXTENDED_KEY_VKS
        char_code = None
        if key_char:
            char_code = ord(key_char)
        elif vk_code == 0x20:
            char_code = 0x20

        return {
            "vk_code": vk_code,
            "char_code": char_code,
            "keydown_lparam": self.build_lparam(scan_code, False, is_extended),
            "keyup_lparam": self.build_lparam(scan_code, True, is_extended),
        }

    def validate_target(self, target_hwnd):
        if not target_hwnd or not self.is_window(target_hwnd):
            raise InputBackendError("目标窗口无效，请重新捕获目标窗口")

    def send_keyboard_action(self, prepared_key_action, target_hwnd=None):
        self.validate_target(target_hwnd)
        self.post_message(
            target_hwnd,
            WM_KEYDOWN,
            prepared_key_action["vk_code"],
            prepared_key_action["keydown_lparam"],
        )
        if prepared_key_action["char_code"] is not None:
            self.post_message(
                target_hwnd,
                WM_CHAR,
                prepared_key_action["char_code"],
                prepared_key_action["keydown_lparam"],
            )
        self.post_message(
            target_hwnd,
            WM_KEYUP,
            prepared_key_action["vk_code"],
            prepared_key_action["keyup_lparam"],
        )
        return True

    def release_keyboard_action(self, prepared_key_action, target_hwnd=None):
        self.validate_target(target_hwnd)
        self.post_message(
            target_hwnd,
            WM_KEYUP,
            prepared_key_action["vk_code"],
            prepared_key_action["keyup_lparam"],
        )
        return True


class AutoClickerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("极速连点器 Pro")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.resizable(False, False)

        self.is_running = False
        self.music_window = None
        self.capture_mode = None
        self.click_thread = None
        self.click_stop_event = threading.Event()
        self.click_session_id = 0
        self.hotkey_pressed = False
        self.interval = 0.01
        self.prepared_key_action = None
        self.active_prepared_key_action = None
        self.active_vk_code = None
        self.active_backend = None
        self.active_send_mode = SEND_MODE_WIN32
        self.active_action_type = ACTION_MOUSE
        self.active_target_key = keyboard.Key.space
        self.active_target_key_name = "SPACE"

        self.keyboard_controller = keyboard.Controller()
        self.virtual_key_scan = USER32.VkKeyScanW
        self.virtual_key_scan.argtypes = (ctypes.c_wchar,)
        self.virtual_key_scan.restype = ctypes.c_short
        self.get_foreground_window = USER32.GetForegroundWindow
        self.get_window_text_length = USER32.GetWindowTextLengthW
        self.get_window_text = USER32.GetWindowTextW
        self.is_window = USER32.IsWindow

        self.get_foreground_window.restype = wintypes.HWND
        self.get_window_text_length.argtypes = (wintypes.HWND,)
        self.get_window_text_length.restype = ctypes.c_int
        self.get_window_text.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.get_window_text.restype = ctypes.c_int
        self.is_window.argtypes = (wintypes.HWND,)
        self.is_window.restype = wintypes.BOOL

        self.win32_backend = Win32InputBackend(USER32)
        self.dd_backend = DDInputBackend()
        self.window_message_backend = WindowMessageInputBackend(USER32)

        self.current_hotkey = keyboard.Key.f8
        self.hotkey_name = "F8"
        self.action_type = ACTION_MOUSE
        self.target_key = keyboard.Key.space
        self.target_key_name = "SPACE"
        self.send_mode = SEND_MODE_WIN32
        self.target_window_handle = None
        self.target_window_name = "未选择"
        self.active_target_window_handle = None

        self.setup_ui()

        self.listener = keyboard.Listener(on_press=self.on_key_press, on_release=self.on_key_release)
        self.listener.start()

    def setup_ui(self):
        self.label_title = ctk.CTkLabel(self, text="AutoClicker Fast", font=FONT_TITLE)
        self.label_title.pack(pady=(18, 8), padx=20)

        self.frame_settings = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_settings.pack(pady=8, padx=24, fill="x")
        self.frame_settings.grid_columnconfigure(0, minsize=110)
        self.frame_settings.grid_columnconfigure(1, weight=1)

        self.label_interval = ctk.CTkLabel(self.frame_settings, text="触发间隔 (毫秒):", font=FONT_TEXT)
        self.label_interval.grid(row=0, column=0, padx=10, pady=8, sticky="e")

        self.entry_interval = ctk.CTkEntry(
            self.frame_settings,
            width=SETTINGS_VALUE_WIDTH,
            font=FONT_TEXT,
            placeholder_text="10",
        )
        self.entry_interval.insert(0, "10")
        self.entry_interval.grid(row=0, column=1, padx=5, pady=8, sticky="w")

        self.label_hotkey = ctk.CTkLabel(self.frame_settings, text="启动热键:", font=FONT_TEXT)
        self.label_hotkey.grid(row=1, column=0, padx=10, pady=8, sticky="e")

        self.btn_set_hotkey = ctk.CTkButton(
            self.frame_settings,
            text=f"[{self.hotkey_name}]",
            width=SETTINGS_VALUE_WIDTH,
            font=FONT_BOLD,
            fg_color="#555555",
            hover_color="#333333",
            command=self.enable_hotkey_setting,
        )
        self.btn_set_hotkey.grid(row=1, column=1, padx=5, pady=8, sticky="w")

        self.label_action_type = ctk.CTkLabel(self.frame_settings, text="动作类型:", font=FONT_TEXT)
        self.label_action_type.grid(row=2, column=0, padx=10, pady=8, sticky="e")

        self.option_action_type = ctk.CTkOptionMenu(
            self.frame_settings,
            width=SETTINGS_VALUE_WIDTH,
            font=FONT_TEXT,
            dropdown_font=FONT_TEXT,
            values=[ACTION_MOUSE, ACTION_KEYBOARD],
            command=self.on_action_type_change,
        )
        self.option_action_type.grid(row=2, column=1, padx=5, pady=8, sticky="w")

        self.label_action_key = ctk.CTkLabel(self.frame_settings, text="目标按键:", font=FONT_TEXT)
        self.label_action_key.grid(row=3, column=0, padx=10, pady=8, sticky="e")

        self.btn_set_action_key = ctk.CTkButton(
            self.frame_settings,
            text=f"[{self.target_key_name}]",
            width=SETTINGS_VALUE_WIDTH,
            font=FONT_BOLD,
            fg_color="#555555",
            hover_color="#333333",
            command=self.enable_action_key_setting,
        )
        self.btn_set_action_key.grid(row=3, column=1, padx=5, pady=8, sticky="w")

        self.label_send_mode = ctk.CTkLabel(self.frame_settings, text="发送模式:", font=FONT_TEXT)
        self.label_send_mode.grid(row=4, column=0, padx=10, pady=8, sticky="e")

        self.option_send_mode = ctk.CTkOptionMenu(
            self.frame_settings,
            width=SETTINGS_VALUE_WIDTH,
            font=FONT_TEXT,
            dropdown_font=FONT_TEXT,
            values=[SEND_MODE_WIN32, SEND_MODE_DD, SEND_MODE_DD_TARGET, SEND_MODE_WINDOW_MESSAGE],
            command=self.on_send_mode_change,
        )
        self.option_send_mode.grid(row=4, column=1, padx=5, pady=8, sticky="w")

        self.label_target_window = ctk.CTkLabel(self.frame_settings, text="目标窗口:", font=FONT_TEXT)
        self.label_target_window.grid(row=5, column=0, padx=10, pady=8, sticky="e")

        self.btn_capture_window = ctk.CTkButton(
            self.frame_settings,
            text=f"[{self.target_window_name}]",
            width=SETTINGS_VALUE_WIDTH,
            font=FONT_BOLD,
            fg_color="#555555",
            hover_color="#333333",
            command=self.enable_target_window_capture,
        )
        self.btn_capture_window.grid(row=5, column=1, padx=5, pady=8, sticky="w")

        self.label_mode_hint = ctk.CTkLabel(
            self.frame_settings,
            text="",
            font=FONT_STATUS,
            text_color="gray",
            anchor="w",
            justify="left",
            wraplength=SETTINGS_HINT_WRAP,
        )
        self.label_mode_hint.grid(row=6, column=0, columnspan=2, padx=10, pady=(0, 6), sticky="w")

        self.switch_topmost = ctk.CTkSwitch(
            self,
            text="窗口始终置顶",
            command=self.toggle_topmost_window,
            font=FONT_TEXT,
            progress_color="#1f6aa5",
        )
        self.switch_topmost.pack(pady=(4, 16), padx=30, anchor="w")

        self.btn_start = ctk.CTkButton(
            self,
            text=self.get_start_button_text(),
            command=self.toggle_clicking,
            font=FONT_BUTTON,
            height=50,
            fg_color="#1f6aa5",
            hover_color="#144870",
        )
        self.btn_start.pack(pady=0, padx=30, fill="x")

        self.btn_music = ctk.CTkButton(self, text="打开乐谱演奏器", command=self.open_music_player, font=FONT_TEXT)
        self.btn_music.pack(pady=(12, 0), padx=30, fill="x")

        self.label_status = ctk.CTkLabel(
            self,
            text="状态: 就绪",
            font=FONT_STATUS,
            text_color="gray",
            wraplength=STATUS_WRAP,
            justify="center",
        )
        self.label_status.pack(pady=(14, 14), padx=20, fill="x")

        self.option_action_type.set(self.action_type)
        self.option_send_mode.set(self.send_mode)
        self.refresh_action_key_button()
        self.refresh_target_window_button()
        self.refresh_mode_hint()
        self.refresh_idle_status()

    def get_start_button_text(self):
        return f"开始运行 ({self.hotkey_name})"

    def get_current_action_text(self):
        if self.action_type == ACTION_MOUSE:
            return ACTION_MOUSE
        return f"{ACTION_KEYBOARD} [{self.target_key_name}]"

    def get_send_mode_text(self):
        if self.send_mode == SEND_MODE_WIN32:
            return self.win32_backend.describe()
        if self.send_mode == SEND_MODE_DD_TARGET:
            if self.target_window_handle and self.is_window(self.target_window_handle):
                return f"{SEND_MODE_DD_TARGET} [{self.target_window_name}]"
            return SEND_MODE_DD_TARGET
        if self.send_mode == SEND_MODE_WINDOW_MESSAGE:
            if self.target_window_handle and self.is_window(self.target_window_handle):
                return f"{self.window_message_backend.describe()} [{self.target_window_name}]"
            return self.window_message_backend.describe()
        return self.dd_backend.describe()

    def refresh_idle_status(self):
        if self.is_running:
            return

        text = f"当前动作: {self.get_current_action_text()} | 发送: {self.get_send_mode_text()}"
        color = "gray"
        if self.send_mode in (SEND_MODE_DD, SEND_MODE_DD_TARGET) and self.dd_backend.candidate_path() is None:
            color = "#d35400"
        self.label_status.configure(text=text, text_color=color)

    def shorten_window_name(self, name):
        if len(name) <= 18:
            return name
        return f"{name[:15]}..."

    def get_target_window_button_text(self):
        return f"[{self.shorten_window_name(self.target_window_name)}]"

    def refresh_mode_hint(self):
        if self.send_mode == SEND_MODE_DD:
            hint = (
                "DD 模式会自动识别当前目录 / DD 子目录中的 DLL。\n"
                f"按下保持默认 {DEFAULT_DD_HOLD_MS}ms，可用 {DD_HOLD_MS_ENV} 覆盖。"
            )
        elif self.send_mode == SEND_MODE_DD_TARGET:
            hint = (
                "DD 目标窗口模式仍使用 DD 驱动发键，但只有目标窗口在前台时才发送。\n"
                "这样不会影响其他前台程序；本质仍是全局 HID 注入，不是进程内定向投递。"
            )
        elif self.send_mode == SEND_MODE_WINDOW_MESSAGE:
            hint = (
                "窗口消息模式只向选中的窗口投递按键，不影响其他程序。\n"
                "点“目标窗口”后，3 秒内切到目标程序；部分 Raw Input / 游戏程序不响应。"
            )
        else:
            hint = "Win32 兼容模式会把按键发送给当前前台程序。"
        self.label_mode_hint.configure(text=hint)

    def refresh_target_window_button(self):
        self.btn_capture_window.configure(text=self.get_target_window_button_text())
        if self.send_mode in (SEND_MODE_WINDOW_MESSAGE, SEND_MODE_DD_TARGET) and self.action_type == ACTION_KEYBOARD:
            self.btn_capture_window.configure(state="normal", fg_color="#555555", hover_color="#333333")
        else:
            self.btn_capture_window.configure(state="disabled", fg_color="#777777", hover_color="#777777")

    def get_window_title_by_handle(self, hwnd):
        if not hwnd or not self.is_window(hwnd):
            return ""
        text_length = self.get_window_text_length(hwnd)
        if text_length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(text_length + 1)
        self.get_window_text(hwnd, buffer, len(buffer))
        return buffer.value.strip()

    def enable_target_window_capture(self):
        if self.send_mode not in (SEND_MODE_WINDOW_MESSAGE, SEND_MODE_DD_TARGET) or self.action_type != ACTION_KEYBOARD:
            return

        self.btn_capture_window.configure(text="[3秒内切换窗口]", state="disabled", fg_color="#d35400")
        self.label_status.configure(text="请在 3 秒内切换到目标窗口", text_color="#d35400")
        self.after(TARGET_WINDOW_CAPTURE_DELAY_MS, self.capture_target_window)

    def capture_target_window(self):
        hwnd = self.get_foreground_window()
        current_app_hwnd = self.winfo_id()
        if not hwnd or hwnd == current_app_hwnd:
            self.label_status.configure(text="错误: 未捕获到目标窗口，请重试", text_color="red")
        else:
            title = self.get_window_title_by_handle(hwnd) or f"句柄 {int(hwnd):#x}"
            self.target_window_handle = hwnd
            self.target_window_name = title
            self.label_status.configure(text=f"已捕获目标窗口: {title}", text_color="gray")

        self.refresh_target_window_button()
        self.refresh_mode_hint()
        self.refresh_idle_status()

    def toggle_topmost_window(self):
        if self.switch_topmost.get() == 1:
            self.attributes("-topmost", True)
            self.label_status.configure(text="窗口已置顶", text_color="#3498db")
        else:
            self.attributes("-topmost", False)
            self.label_status.configure(text="已取消置顶", text_color="gray")

    def enable_hotkey_setting(self):
        self.capture_mode = "hotkey"
        self.btn_set_hotkey.configure(text="按下按键...", fg_color="#d35400")
        self.focus_set()

    def enable_action_key_setting(self):
        if self.action_type != ACTION_KEYBOARD:
            return

        self.capture_mode = "action_key"
        self.btn_set_action_key.configure(text="按下按键...", fg_color="#d35400")
        self.focus_set()

    def format_key_name(self, key):
        try:
            if key.char:
                return DISPLAY_NAME_BY_CHAR.get(key.char, key.char.upper())
        except AttributeError:
            pass
        return str(key).replace("Key.", "").upper()

    def keys_match(self, first_key, second_key):
        return self.format_key_name(first_key) == self.format_key_name(second_key)

    def on_key_press(self, key):
        if self.music_window is not None:
            return
        if self.capture_mode == "hotkey":
            self.current_hotkey = key
            self.hotkey_name = self.format_key_name(key)
            self.capture_mode = None
            self.hotkey_pressed = False
            self.after(0, self.update_hotkey_ui)
            return

        if self.capture_mode == "action_key":
            self.target_key = key
            self.target_key_name = self.format_key_name(key)
            self.capture_mode = None
            self.after(0, self.refresh_action_key_button)
            return

        if self.keys_match(key, self.current_hotkey):
            if self.hotkey_pressed:
                return
            self.hotkey_pressed = True
            self.after(0, self.toggle_clicking)

    def on_key_release(self, key):
        if self.keys_match(key, self.current_hotkey):
            self.hotkey_pressed = False

    def update_hotkey_ui(self):
        self.btn_set_hotkey.configure(text=f"[{self.hotkey_name}]", fg_color="#555555")
        if not self.is_running:
            self.btn_start.configure(text=self.get_start_button_text())
        self.refresh_idle_status()

    def refresh_action_key_button(self):
        self.btn_set_action_key.configure(text=f"[{self.target_key_name}]")
        if self.action_type == ACTION_KEYBOARD:
            self.btn_set_action_key.configure(state="normal", fg_color="#555555", hover_color="#333333")
        else:
            self.btn_set_action_key.configure(state="disabled", fg_color="#777777", hover_color="#777777")
        self.refresh_target_window_button()
        self.refresh_idle_status()

    def on_action_type_change(self, value):
        self.action_type = value
        self.refresh_action_key_button()
        self.refresh_mode_hint()

    def on_send_mode_change(self, value):
        self.send_mode = value
        self.refresh_target_window_button()
        self.refresh_mode_hint()
        self.refresh_idle_status()

    def resolve_virtual_key(self, key):
        try:
            if key.char:
                vk_code = self.virtual_key_scan(key.char)
                if vk_code == -1:
                    return None
                return vk_code & 0xFF
        except AttributeError:
            pass
        return SPECIAL_KEY_TO_VK.get(key)

    def get_selected_backend(self):
        if self.send_mode in (SEND_MODE_DD, SEND_MODE_DD_TARGET):
            return self.dd_backend
        if self.send_mode == SEND_MODE_WINDOW_MESSAGE:
            return self.window_message_backend
        return self.win32_backend

    def send_key_with_fallback(self):
        try:
            action_key = (
                self.active_target_key.char if getattr(self.active_target_key, "char", None) else self.active_target_key
            )
        except AttributeError:
            action_key = self.active_target_key

        self.keyboard_controller.press(action_key)
        self.keyboard_controller.release(action_key)

    def send_win32_keyup(self, vk_code):
        scan_code = self.win32_backend.map_virtual_key(vk_code, MAPVK_VK_TO_VSC)
        if scan_code == 0:
            return False

        flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
        if vk_code in EXTENDED_KEY_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        input_item = INPUT(
            type=INPUT_KEYBOARD,
            value=INPUT_UNION(
                ki=KEYBDINPUT(
                    wVk=0,
                    wScan=scan_code,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        return self.win32_backend.send_input(1, ctypes.byref(input_item), ctypes.sizeof(INPUT)) == 1

    def is_active_target_window_ready(self):
        if not self.active_target_window_handle:
            return False
        if not self.is_window(self.active_target_window_handle):
            return False
        return self.get_foreground_window() == self.active_target_window_handle

    def toggle_clicking(self):
        if not self.is_running:
            self.start_clicking()
        else:
            self.stop_clicking()

    def start_clicking(self):
        if self.music_window is not None:
            self.label_status.configure(text="请先关闭乐谱演奏器，再使用连点功能", text_color="#d35400")
            return
        try:
            ms = int(self.entry_interval.get())
            if ms < 0:
                raise ValueError
            self.interval = ms / 1000.0
        except ValueError:
            self.label_status.configure(text="错误: 请输入有效的毫秒数", text_color="red")
            return

        backend = self.get_selected_backend()
        self.prepared_key_action = None

        try:
            backend.ensure_ready()
            if self.send_mode in (SEND_MODE_WINDOW_MESSAGE, SEND_MODE_DD_TARGET):
                if self.action_type != ACTION_KEYBOARD:
                    self.label_status.configure(text="错误: 目标窗口相关模式仅支持键盘按键", text_color="red")
                    return
                if not self.target_window_handle or not self.is_window(self.target_window_handle):
                    self.label_status.configure(text="错误: 请先捕获有效的目标窗口", text_color="red")
                    return

            if self.action_type == ACTION_KEYBOARD:
                if self.keys_match(self.current_hotkey, self.target_key):
                    self.label_status.configure(text="错误: 热键不能与目标按键相同", text_color="red")
                    return

                vk_code = self.resolve_virtual_key(self.target_key)
                if vk_code is None:
                    self.label_status.configure(text="错误: 当前按键无法转换为虚拟键码", text_color="red")
                    return

                self.prepared_key_action = backend.prepare_keyboard_action(vk_code, get_key_char(self.target_key))
                if self.prepared_key_action is None:
                    self.label_status.configure(
                        text=f"错误: 当前按键暂不支持通过 {backend.describe()} 发送",
                        text_color="red",
                    )
                    return
        except InputBackendError as exc:
            self.label_status.configure(text=f"错误: {exc}", text_color="red")
            return

        self.active_backend = backend
        self.active_send_mode = self.send_mode
        self.active_action_type = self.action_type
        self.active_target_key = self.target_key
        self.active_target_key_name = self.target_key_name
        self.active_prepared_key_action = self.prepared_key_action
        self.active_vk_code = self.resolve_virtual_key(self.target_key) if self.action_type == ACTION_KEYBOARD else None
        self.active_target_window_handle = (
            self.target_window_handle if self.send_mode in (SEND_MODE_WINDOW_MESSAGE, SEND_MODE_DD_TARGET) else None
        )
        self.click_session_id += 1
        session_id = self.click_session_id
        self.click_stop_event.clear()
        self.is_running = True
        self.btn_start.configure(text=f"运行中... ({self.hotkey_name})", fg_color="#c0392b", hover_color="#962d22")

        if self.active_action_type == ACTION_MOUSE:
            status_text = f"状态: 正在使用 {backend.describe()} 执行鼠标左键点击"
        else:
            status_text = f"状态: 正在使用 {backend.describe()} 按下键盘按键 [{self.active_target_key_name}]"
            if self.send_mode in (SEND_MODE_WINDOW_MESSAGE, SEND_MODE_DD_TARGET):
                status_text += f" -> {self.target_window_name}"
        self.label_status.configure(text=status_text, text_color="#e74c3c")

        self.click_thread = threading.Thread(target=self.clicking_process, args=(session_id,), daemon=True)
        self.click_thread.start()

    def wait_for_click_thread(self):
        thread = self.click_thread
        if thread is None or thread is threading.current_thread():
            return
        if not thread.is_alive():
            self.click_thread = None
            return

        timeout = max(0.1, self.interval + getattr(self.active_backend, "hold_seconds", 0.0) + 0.05)
        thread.join(timeout)
        if not thread.is_alive():
            self.click_thread = None

    def release_active_input(self):
        if self.active_backend is None:
            USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return

        try:
            if self.active_action_type == ACTION_MOUSE:
                for _ in range(3):
                    self.active_backend.release_left()
                    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    time.sleep(0.01)
            elif self.active_prepared_key_action is not None:
                for _ in range(5):
                    self.active_backend.release_keyboard_action(
                        self.active_prepared_key_action,
                        self.active_target_window_handle,
                    )
                    if (
                        self.active_backend is not self.window_message_backend
                        and self.active_send_mode != SEND_MODE_DD_TARGET
                        and self.active_vk_code is not None
                    ):
                        self.send_win32_keyup(self.active_vk_code)
                    time.sleep(0.01)
        except InputBackendError:
            pass
        finally:
            if not (
                self.active_action_type == ACTION_KEYBOARD
                and self.active_send_mode in (SEND_MODE_WINDOW_MESSAGE, SEND_MODE_DD_TARGET)
            ):
                USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            self.active_prepared_key_action = None
            self.active_vk_code = None
            self.active_target_window_handle = None

    def stop_clicking(self):
        self.click_session_id += 1
        self.is_running = False
        self.click_stop_event.set()
        self.wait_for_click_thread()
        self.release_active_input()
        self.btn_start.configure(text=self.get_start_button_text(), fg_color="#1f6aa5", hover_color="#144870")
        self.label_status.configure(text="状态: 已停止", text_color="gray")

    def handle_runtime_error(self, message):
        self.click_session_id += 1
        self.is_running = False
        self.click_stop_event.set()
        self.wait_for_click_thread()
        self.release_active_input()
        self.btn_start.configure(text=self.get_start_button_text(), fg_color="#1f6aa5", hover_color="#144870")
        self.label_status.configure(text=f"错误: {message}", text_color="red")

    def execute_action(self):
        if self.active_action_type == ACTION_MOUSE:
            self.active_backend.click_left()
            return

        if self.active_send_mode == SEND_MODE_DD_TARGET and not self.is_active_target_window_ready():
            return

        if not self.active_backend.send_keyboard_action(
            self.active_prepared_key_action,
            self.active_target_window_handle,
        ):
            if self.active_backend is self.win32_backend:
                self.send_key_with_fallback()
                return
            raise InputBackendError(f"{self.active_backend.describe()} 发送失败")

    def clicking_process(self, session_id):
        try:
            while (
                self.is_running
                and session_id == self.click_session_id
                and not self.click_stop_event.is_set()
            ):
                self.execute_action()
                if self.interval > 0 and self.click_stop_event.wait(self.interval):
                    break
        except InputBackendError as exc:
            self.after(0, self.handle_runtime_error, str(exc))

    def open_music_player(self):
        if self.music_window is not None:
            self.music_window.lift()
            return
        if self.is_running:
            self.stop_clicking()
        self.capture_mode = None
        self.hotkey_pressed = False
        from music_player import MusicPlayerWindow

        def on_closed():
            self.music_window = None

        self.music_window = MusicPlayerWindow(
            self,
            lambda mode: self.dd_backend if mode == SEND_MODE_DD else self.win32_backend,
            USER32,
            on_closed=on_closed,
        )

    def on_closing(self):
        if self.music_window is not None:
            self.music_window.panel.shutdown(self._finish_closing)
            return
        self._finish_closing()

    def _finish_closing(self):
        self.stop_clicking()
        self.listener.stop()
        self.destroy()


if __name__ == "__main__":
    app = AutoClickerApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
