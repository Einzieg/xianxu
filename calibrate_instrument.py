"""Explicit, foreground-guarded calibration of the selected instrument window."""

import argparse
from datetime import datetime
from pathlib import Path
import threading
import time

from pynput import keyboard

from main import DDInputBackend, USER32
from music_audio import calibrate
from music_input import KeyboardSink, TargetWindow
from music_score import DEFAULT_MAPPING, pitch_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hwnd", type=int, required=True)
    parser.add_argument("--focus", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--delay", type=int, default=3)
    parser.add_argument("--wait-foreground", type=float, default=0)
    args = parser.parse_args()
    target = TargetWindow(USER32, args.hwnd)
    sink = KeyboardSink(DDInputBackend(), DEFAULT_MAPPING.values())
    stop = threading.Event()
    listener = keyboard.Listener(on_press=lambda key: stop.set() if key == keyboard.Key.f10 else None)
    listener.start()
    try:
        if args.wait_foreground:
            print("等待游戏稳定处于前台 1 秒；F10 取消。", flush=True)
            deadline = time.monotonic() + args.wait_foreground
            ready_since = None
            while time.monotonic() < deadline:
                now = time.monotonic()
                ready_since = (ready_since or now) if target.state() == "ready" else None
                if ready_since is not None and now - ready_since >= 1:
                    break
                if stop.wait(0.05):
                    raise RuntimeError("已取消测量")
            else:
                raise RuntimeError("等待游戏前台超时，未发送按键")
        if args.focus:
            if not target.activate():
                raise RuntimeError("无法激活游戏窗口，请切回游戏后重试")
        output = args.output or Path(__file__).parent / "artifacts" / "calibration" / datetime.now().strftime("%Y%m%d-%H%M%S")
        print(f"Target: {target.title} (PID {target.pid}). F10 stops calibration.", flush=True)
        results = calibrate(list(DEFAULT_MAPPING.values()), sink, target.state, output,
                            stop=stop, progress=lambda text: print(text, flush=True), delay=args.delay)
        for result in results:
            print(f"{result.key}: {result.frequency:.2f} Hz -> {pitch_name(result.pitch)}, confidence={result.confidence:.3f}")
        print(f"Artifacts: {output}", flush=True)
    finally:
        listener.stop()
        sink.release_all()


if __name__ == "__main__":
    main()
