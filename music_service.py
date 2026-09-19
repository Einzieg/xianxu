"""Long-lived JSON-lines engine for the Tauri player. Stdout is protocol only."""

import argparse
from dataclasses import asdict
import ctypes
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time

from music_audio import AudioCancelled, calibrate, export_midi, transcribe_audio
from music_audition import audition
from music_input import KeyboardSink, PreviewSink, TargetWindow
from music_library import MODES, PlaylistNavigator, ScoreLibrary, decode_score, mapping_from_settings
from music_playback import ScorePlayer
from music_score import ScoreError, compile_score, load_midi, parse_text


ROOT = Path(__file__).resolve().parent


def make_backend(mode):
    # Import lazily: browsing the library must not initialize DD or a Tk window.
    from main import DDInputBackend, USER32, Win32InputBackend
    return DDInputBackend() if mode == "DD" else Win32InputBackend(USER32)


class MusicService:
    def __init__(self, directory, *, backend_factory=make_backend, parent_pid=None, monitor=True):
        self.lock = threading.RLock()
        self.library = ScoreLibrary(directory, ROOT / "music_settings.json")
        self.navigator = PlaylistNavigator()
        self.player = ScorePlayer()
        self.backends = {}
        self.backend_factory = backend_factory
        self.parent_pid = parent_pid
        self.target = None
        self.preview = False
        self.stop_sequence = 0
        self.queue_active = False
        self.abort_queue = threading.Event()
        self.closed = threading.Event()
        self.closing = False
        self.job_stop = threading.Event()
        self.job = None
        self.job_thread = None
        self.calibration_sink = None
        self.notice = ""
        self.listener = None
        self.actions = queue.Queue()
        self.monitor_thread = None
        if monitor:
            self.monitor_thread = threading.Thread(target=self._monitor, name="playlist", daemon=True)
            self.monitor_thread.start()

    def _ids(self):
        return [item["id"] for item in self.library.items]

    def _busy(self):
        return self.job_thread is not None and self.job_thread.is_alive()

    def _editable(self):
        if self.player.running or self.queue_active:
            raise ScoreError("请先停止演奏")
        if self._busy():
            raise ScoreError("请等待当前任务完成，或取消任务")
        if self.calibration_sink is not None:
            raise ScoreError("校准按键尚未释放，请先再次停止")

    def _backend(self):
        mode = self.library.settings["backend"]
        if mode not in self.backends:
            self.backends[mode] = self.backend_factory(mode)
        return self.backends[mode]

    def _arranged(self, item_id):
        item = self.library.get(item_id)
        if item["mapping"] != self.library.settings["mapping"]:
            updated = self.library.arrange(decode_score(item["original"]), item["source"],
                                           texture=item.get("texture", "melody"),
                                           melody_track=item.get("melody_track"),
                                           sparse_config=item.get("sparse_config"))
            updated.update(id=item["id"], created_at=item["created_at"])
            item.update(updated)
            self.library.save()
        return item

    def _snapshot(self):
        status = asdict(self.player.snapshot())
        status["pressed"] = sorted(status["pressed"])
        if self.notice:
            status["message"] = self.notice
        return {"library": [self.library.describe(item) for item in self.library.items],
                "current_id": self.library.current_id, "mode": self.library.mode, "preview": self.preview,
                "settings": self.library.settings,
                "target": {"title": self.target.title, "state": self.target.state()} if self.target else None,
                "playback": status, "job": dict(self.job) if self.job else None, "queue_active": self.queue_active,
                "stop_sequence": self.stop_sequence}

    def handle(self, method, params=None):
        params = params or {}
        if method == "shutdown":
            return self.shutdown()
        if method in ("stop", "cancel_job"):
            self.job_stop.set()
        if method == "stop":
            self.abort_queue.set()
            self.player.request_stop()
        with self.lock:
            if self.closed.is_set() or (self.closing and method not in ("get_state", "stop", "cancel_job")):
                raise RuntimeError("引擎正在关闭")
            if method in ("play", "resume", "next", "previous") and "stop_sequence" in params:
                if params["stop_sequence"] != self.stop_sequence:
                    raise ScoreError("已收到停止操作，本次播放请求已取消；请重新点击播放")
            if method == "get_state":
                return self._snapshot()
            if method == "get_process_id":
                return {"pid": os.getpid()}
            if method == "get_score":
                item = self._arranged(params["id"])
                mapping = mapping_from_settings(self.library.settings)
                notes = [{**asdict(n), "key": mapping.get(n.pitch, "")} for n in decode_score(item["score"]).notes]
                return {"item": self.library.describe(item), "notes": notes,
                        "original_notes": [asdict(n) for n in decode_score(item["original"]).notes],
                        "mapping": self.library.settings["mapping"]}
            if method == "audition":
                self._editable()
                return audition(self._arranged(params["id"]), params["kind"],
                                params.get("start", 0), params.get("duration", 15))
            if method == "select":
                self._editable()
                self.library.get(params["id"])
                self.library.current_id = params["id"]
                self.navigator.reset()
                self.library.save()
            elif method == "remove":
                self._editable()
                self.library.remove(params["id"])
                self.navigator.reset()
            elif method == "set_mode":
                if params["mode"] not in MODES:
                    raise ScoreError("未知播放模式")
                self.library.mode = params["mode"]
                self.navigator.reset()
                self.library.save()
            elif method == "set_preview":
                self._editable()
                if type(params.get("preview")) is not bool:
                    raise ScoreError("预览模式必须为布尔值")
                self.preview = params["preview"]
            elif method == "import_score":
                self._editable()
                path = Path(params["path"])
                if path.suffix.lower() in (".mid", ".midi"):
                    source = load_midi(path)
                elif path.suffix.lower() == ".txt":
                    if path.stat().st_size > 4 * 1024 * 1024:
                        raise ScoreError("文本简谱不能超过4 MB")
                    source = parse_text(path.read_text(encoding="utf-8-sig"), self.library.settings["bpm"], path.stem,
                                        self.library.settings["octave"])
                else:
                    raise ScoreError("请导入 TXT / MID / MIDI 文件")
                return self.library.add(self.library.arrange(source, str(path), texture="chords"))
            elif method == "add_text":
                self._editable()
                if len(params["text"]) > 4 * 1024 * 1024:
                    raise ScoreError("文本简谱不能超过4 MB")
                source = parse_text(params["text"], self.library.settings["bpm"],
                                    params.get("title", "文本简谱").strip() or "文本简谱", self.library.settings["octave"])
                return self.library.add(self.library.arrange(source, "文本简谱", texture="chords"))
            elif method == "export":
                item = self._arranged(params["id"])
                path = Path(params["path"])
                if path.suffix.lower() not in (".mid", ".midi"):
                    raise ScoreError("导出文件名请使用 .mid 或 .midi")
                export_midi(decode_score(item["score"]), path)
                return {"path": str(path)}
            elif method == "set_settings":
                self._editable()
                self._settings(params)
            elif method == "play":
                if self._busy():
                    raise ScoreError("请先完成或取消当前任务")
                if self.player.running:
                    raise ScoreError("已在演奏，请暂停/继续或先停止")
                self._play(params.get("id") or self.library.current_id, preview=bool(params.get("preview", False)),
                           activate_target=params.get("activate_target") is True)
            elif method == "pause":
                self.notice = ""
                if "paused" in params:
                    if params["paused"] is not True:
                        raise ScoreError("继续演奏请使用 resume 操作")
                    self.player.set_paused(True)
                elif self.player.snapshot().state == "paused":
                    self._resume(activate_target=False)
                else:
                    self.player.set_paused(True)
            elif method == "resume":
                self._resume(activate_target=params.get("activate_target") is True)
            elif method == "stop":
                self.stop_sequence += 1
                self.queue_active = False
                if not self.player.stop():
                    raise RuntimeError("按键尚未释放，请再次停止；不要强杀进程")
                if self.calibration_sink is not None and not self._busy():
                    self.calibration_sink.release_all()
                    self.calibration_sink = None
                self.notice = "已停止，播放队列不会继续"
            elif method in ("next", "previous"):
                if self._busy():
                    raise ScoreError("请先完成或取消当前任务")
                active = self.queue_active and not self.abort_queue.is_set()
                self.queue_active = False
                if not self.player.stop():
                    raise RuntimeError("上一曲按键尚未释放，无法切换")
                if method == "next":
                    item_id = self.navigator.next(self._ids(), self.library.current_id, self.library.mode, automatic=False)
                else:
                    item_id = self.navigator.previous(self._ids(), self.library.current_id, self.library.mode)
                if item_id is not None:
                    self.library.current_id = item_id
                    self.library.save()
                    if active:
                        self._play(item_id, preview=self.preview, activate_target=params.get("activate_target") is True)
                else:
                    self.notice = "已到列表末尾"
            elif method == "transcribe":
                self._editable()
                path = Path(params["path"])
                engine = params.get("engine", "instrument")
                if engine not in ("instrument", "melody", "yin"):
                    raise ScoreError("未知转谱引擎")
                register = params.get("register", "auto")
                if register not in ("auto", "high", "mid", "low"):
                    raise ScoreError("未知主奏音区")
                texture = params.get("texture", "melody")
                if texture not in ("melody", "chords") or (texture == "chords" and engine != "instrument"):
                    raise ScoreError("保留和弦模式仅支持乐器音符模型")
                if not path.is_file() or path.suffix.lower() not in (".mp3", ".wav", ".flac", ".ogg"):
                    raise ScoreError("请选择本地 MP3 / WAV / FLAC / OGG")
                self._start_job("transcribe", lambda: self._transcribe(path, engine, register, texture))
            elif method == "capture":
                self._editable()
                delay = self._number(params.get("delay", 3), 0, 30, "捕获倒计时")
                self._start_job("capture", lambda: self._capture(delay))
            elif method == "calibrate":
                self._editable()
                if self.target is None or self.target.state() == "closed":
                    raise ScoreError("请先捕获游戏窗口")
                self._start_job("calibrate", self._calibrate)
            elif method == "cancel_job":
                pass
            else:
                raise ScoreError(f"未知操作：{method}")
            return self._snapshot()

    @staticmethod
    def _number(value, lower, upper, label):
        result = float(value)
        if not math.isfinite(result) or not lower <= result <= upper:
            raise ScoreError(f"{label} 必须在 {lower}～{upper} 之间")
        return result

    def _settings(self, params):
        settings = dict(self.library.settings)
        for name, lower, upper in (("speed", 0.25, 4), ("hold", 10, 150), ("delay", 0, 30), ("bpm", 20, 300)):
            if name in params:
                settings[name] = self._number(params[name], lower, upper, name)
        if "octave" in params:
            value = self._number(params["octave"], 0, 8, "简谱八度")
            if not value.is_integer():
                raise ScoreError("简谱八度必须是整数")
            settings["octave"] = int(value)
        if "backend" in params:
            if params["backend"] not in ("DD", "Win32"):
                raise ScoreError("未知输入后端")
            settings["backend"] = params["backend"]
        if "mapping" in params:
            settings["mapping"] = params["mapping"]
            mapping_from_settings(settings)
        self.library.settings = settings
        self.library.save()

    def _focus_target(self):
        if self.target is None or self.target.state() == "closed":
            raise ScoreError("目标窗口已关闭或未捕获，请重新捕获游戏窗口")
        if self.target.state() != "ready":
            self.target.activate()
            # Cross-thread foreground activation may complete after the Win32 call returns.
            deadline = time.monotonic() + .75
            while self.target.state() == "background" and time.monotonic() < deadline:
                if self.abort_queue.wait(.01) or self.closing:
                    return
            print(f"[focus {time.time():.3f}] target={self.target.hwnd} pid={self.target.pid} "
                  f"state={self.target.state()}", file=sys.stderr, flush=True)
        # Windows can reject foreground activation. Never treat a request as success.
        if self.target.state() != "ready":
            raise ScoreError("无法切换到游戏窗口，未发送按键；请切回游戏，按 F8 开始或 F9 继续已有演奏")

    def _resume(self, *, activate_target=False):
        if not self.player.running or self.abort_queue.is_set():
            raise ScoreError("当前没有可继续的演奏，请重新点击播放")
        if not self.preview:
            if activate_target:
                self._focus_target()
            elif self.target is None or self.target.state() != "ready":
                raise ScoreError("请先切回游戏窗口，再按 F9 继续")
        if self.abort_queue.is_set() or self.closing:
            return
        self.notice = ""
        self.player.set_paused(False)

    def _play(self, item_id, *, preview=False, continuing=False, activate_target=False):
        if self.calibration_sink is not None or self.player.snapshot().pressed:
            raise ScoreError("上一轮按键尚未释放，请先再次停止")
        if item_id is None:
            raise ScoreError("谱库为空，请先导入或转谱")
        if not continuing:
            self.abort_queue.clear()
        item = self._arranged(item_id)
        settings = self.library.settings
        mapping = mapping_from_settings(settings)
        plan = compile_score(decode_score(item["score"]), mapping, speed=settings["speed"], hold_ms=settings["hold"])
        if preview:
            sink, target_guard, delay = PreviewSink(), lambda: "ready", 0
        else:
            if self.target is None or self.target.state() == "closed":
                raise ScoreError("请先捕获游戏窗口")
            if continuing and self.target.state() != "ready":
                raise ScoreError("目标窗口失焦，队列已停止；回到游戏后重新开始")
            sink = KeyboardSink(self._backend(), mapping.values())
            target_guard, delay = self.target.state, 0 if continuing else settings["delay"]
            if activate_target and not self.abort_queue.is_set():
                self._focus_target()
        # F10 can arrive while DD initializes or between tracks. Keep its latch
        # in the per-event guard; ScorePlayer.start clears only its own stop flag.
        def guard():
            if self.abort_queue.is_set() or self.closing:
                return "closed"
            return target_guard()

        if self.abort_queue.is_set() or self.closing:
            return
        self.library.current_id = item_id
        self.library.save()
        self.notice = ""
        self.preview = preview
        self.player.start(plan, sink, guard, delay)
        self.queue_active = True

    def _tick(self):
        with self.lock:
            if self.closed.is_set() or self.closing or not self.queue_active or self.player.running:
                return
            self.queue_active = False
            if self.abort_queue.is_set() or self.player.snapshot().state != "finished":
                return
            item_id = self.navigator.next(self._ids(), self.library.current_id, self.library.mode)
            if item_id is None:
                self.notice = "列表播放完毕，按键已释放"
                return
            try:
                self._play(item_id, preview=self.preview, continuing=True)
            except Exception as exc:
                self.notice = f"队列已停止：{exc}"

    def _monitor(self):
        while not self.closed.wait(0.05):
            try:
                while not self.actions.empty():
                    method, params = self.actions.get_nowait()
                    self.handle(method, params)
                    from music_hotkeys import report_hotkey
                    report_hotkey(f"handled={method} state={self.player.snapshot().state}")
                self._tick()
            except Exception as exc:
                from music_hotkeys import report_hotkey
                report_hotkey(f"action failed: {exc}")
                with self.lock:
                    self.notice = str(exc)

    def _progress(self, value, message):
        with self.lock:
            self.job.update(progress=max(0, min(1, value)), message=message)

    def _start_job(self, kind, operation):
        self.job_stop.clear()
        self.job = {"id": str(time.time_ns()), "kind": kind, "state": "running", "progress": 0, "message": "准备中"}

        def run():
            try:
                operation()
                with self.lock:
                    if self.job_stop.is_set():
                        self.job.update(state="cancelled", message="已取消")
                    else:
                        self.job.update(state="done", progress=1)
            except Exception as exc:
                with self.lock:
                    cancelled = self.job_stop.is_set() or isinstance(exc, AudioCancelled)
                    self.job.update(state="cancelled" if cancelled else "error", message="已取消" if cancelled else str(exc),
                                    error="" if cancelled else str(exc))

        self.job_thread = threading.Thread(target=run, name=f"music-{kind}", daemon=True)
        self.job_thread.start()

    def _transcribe(self, path, engine, register="auto", texture="melody"):
        source = transcribe_audio(path, stop=self.job_stop, engine=engine,
                                  register=register, texture=texture,
                                  progress=lambda v: self._progress(v * 0.85, "识别旋律与音符起止"))
        self._progress(0.88, "自动编配到9键，保留原始识别谱")
        item = self.library.arrange(source, str(path), self.job_stop, texture=texture)
        item["transcription"] = {"engine": engine, "register": register, "texture": texture}
        if engine == "instrument":
            from music_neural import TRANSCRIPTION_REVISION, CHORD_TRANSCRIPTION_REVISION
            item["transcription"]["revision"] = (CHORD_TRANSCRIPTION_REVISION if texture == "chords"
                                                   else TRANSCRIPTION_REVISION)
        with self.lock:
            if not self.job_stop.is_set() and not self.closing:
                self.library.add(item)
                self.job.update(message=f"已入库：{item['title']} · {item['note_count']} 音 · 请试听校对")

    def _capture(self, delay):
        self._progress(0, "请切到游戏窗口")
        if self.job_stop.wait(delay):
            return
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        target = TargetWindow(user32, user32.GetForegroundWindow())
        if target.pid == self.parent_pid:
            raise ScoreError("请捕获游戏窗口，不能选择演奏器自身")
        with self.lock:
            if not self.job_stop.is_set() and not self.closing:
                self.target = target
                self.job.update(message=f"已捕获：{target.title}")

    def _calibrate(self):
        keys = list(mapping_from_settings(self.library.settings).values())
        sink = KeyboardSink(self._backend(), keys)
        self.calibration_sink = sink
        try:
            folder = self.library.directory / "calibration" / time.strftime("%Y%m%d-%H%M%S")
            results = calibrate(keys, sink, self.target.state, folder, stop=self.job_stop,
                                progress=lambda message: self._progress(0.2, message))
            with self.lock:
                if not self.job_stop.is_set() and not self.closing:
                    self._settings({"mapping": [{"pitch": note.pitch, "key": note.key} for note in results]})
                    self.job.update(message="校准完成，已保存实测键位；谱子将按新键位自动编配")
        finally:
            sink.release_all()
            self.calibration_sink = None

    def listen_hotkeys(self):
        from music_hotkeys import TransportHotkeys

        def pressed(action):
            if action == "stop":
                self.abort_queue.set()
                self.player.request_stop()
                self.job_stop.set()
                self.actions.put(("stop", {}))
            elif action == "pause":
                self.actions.put(("pause", {}))
            elif action == "play":
                self.actions.put(("play", {"preview": self.preview}))

        self.listener = TransportHotkeys(pressed)
        self.listener.start()

    def shutdown(self):
        self.abort_queue.set()
        self.job_stop.set()
        self.player.request_stop()
        with self.lock:
            self.closing = True
            self.queue_active = False
        if not self.player.stop(timeout=3):
            raise RuntimeError("关闭取消：仍有按键未释放，请再次停止")
        thread = self.job_thread
        if thread and thread is not threading.current_thread():
            thread.join(10)
            if thread.is_alive():
                raise RuntimeError("关闭取消：后台任务仍在结束，请稍后重试")
        if self.calibration_sink:
            self.calibration_sink.release_all()
            self.calibration_sink = None
        self.closed.set()
        if self.listener:
            self.listener.stop()
        return {"closed": True}


def watch_parent(service, pid):
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    try:
        while not service.closed.wait(0.1):
            if not handle or kernel32.WaitForSingleObject(handle, 0) == 0:
                service.shutdown()
                return
    finally:
        if handle:
            kernel32.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "artifacts" / "player-data")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--no-hotkeys", action="store_true")
    args = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    service = MusicService(args.data_dir, parent_pid=args.parent_pid)
    if not args.no_hotkeys:
        service.listen_hotkeys()
    if args.parent_pid:
        threading.Thread(target=watch_parent, args=(service, args.parent_pid), daemon=True).start()
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                result = service.handle(request["method"], request.get("params", {}))
                response = {"ok": True, "result": result}
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            print(json.dumps(response, ensure_ascii=True, allow_nan=False), flush=True)
            if service.closed.is_set():
                break
    finally:
        service.shutdown()


if __name__ == "__main__":
    main()
