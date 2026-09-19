"""Raw Input transport keys, with state polling as a reported fallback."""

import ctypes
import sys
import threading
import time


def report_hotkey(message):
    print(f"[hotkeys {time.time():.3f}] {message}", file=sys.stderr, flush=True)


class TransportHotkeys:
    KEYS = (("stop", 0x79), ("pause", 0x78), ("play", 0x77))

    def __init__(self, callback, *, read_key=None, interval=.015, raw_factory=None, report=report_hotkey):
        if read_key is None and raw_factory is None:
            from music_raw_input import RawKeyboardListener
            raw_factory = RawKeyboardListener
        if read_key is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
            user32.GetAsyncKeyState.restype = ctypes.c_short
            read_key = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
        self.read_key = read_key
        self.callback = callback
        self.interval = interval
        self.closed = threading.Event()
        self.thread = None
        self.held = None
        self.raw_factory = raw_factory
        self.raw = None
        self.raw_held = set()
        self.startup_held = set()
        self.report = report
        self.mode = "stopped"

    def poll(self):
        down = {name for name, vk in self.KEYS if self.read_key(vk)}
        self._update(down)

    def _update(self, down):
        # Ignore keys held during startup; require a new physical press.
        rising = down - self.held if self.held is not None else set()
        self.held = down
        if self.closed.is_set():
            return
        if "stop" in down:
            if "stop" in rising:
                self._dispatch("stop")
            return
        for name, _ in self.KEYS:
            if name in rising:
                self._dispatch(name)
                break

    def _dispatch(self, action):
        self.callback(action)
        self.report(f"source={self.mode} action={action}")

    def raw_event(self, vk, down, device=0):
        names = dict((code, name) for name, code in self.KEYS)
        if vk not in names or self.closed.is_set():
            return
        if down:
            self.raw_held.add((device, vk))
        else:
            self.raw_held.discard((device, vk))
            self.startup_held.discard(names[vk])
        self._update(self.startup_held | {names[code] for _, code in self.raw_held})

    def device_removed(self, device):
        self.raw_held = {(source, code) for source, code in self.raw_held if source != device}
        self._update(self.startup_held | {name for name, code in self.KEYS
                                        if any(vk == code for _, vk in self.raw_held)})

    def start(self):
        self.poll()
        self.startup_held = set(self.held)
        if self.raw_factory is not None:
            self.raw = self.raw_factory(self.raw_event, self.device_removed, self.report)
            try:
                self.mode = "raw-input"
                self.raw.start()
                self.report("Raw Input ready: F8/F9/F10 (background INPUTSINK)")
            except Exception as exc:
                self.raw.stop()
                self.raw = None
                self.report(f"Falling back to key-state polling: {exc}")
        if self.raw is None:
            self.mode = "polling"
        self.thread = threading.Thread(target=self._run, name="transport-hotkeys", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.closed.wait(self.interval):
            if self.raw is not None:
                if self.raw.thread.is_alive():
                    continue
                self.report("Raw Input listener ended; falling back to key-state polling")
                self.raw = None
                self.mode = "polling"
                self.held = None
            self.poll()

    def stop(self):
        self.closed.set()
        if self.raw is not None:
            self.raw.stop()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(.5)
