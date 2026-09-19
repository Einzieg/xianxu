"""Interruptible playback with one worker owning all key transitions."""

from dataclasses import dataclass
import math
import threading
import time


@dataclass(frozen=True)
class PlaybackStatus:
    state: str
    position: float
    duration: float
    message: str
    pressed: frozenset[str]


class ScorePlayer:
    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread = None
        self._sink = None
        self._pressed = set()
        self._status = PlaybackStatus("idle", 0, 0, "就绪", frozenset())

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self):
        with self._lock:
            return self._status

    def _publish(self, state, position, duration, message):
        with self._lock:
            self._status = PlaybackStatus(state, position, duration, message, frozenset(self._pressed))

    def start(self, plan, sink, target_state=lambda: "ready", delay=3.0):
        with self._lock:
            if self.running or self._pressed:
                raise RuntimeError("上一轮演奏尚未结束或仍有按键未释放")
            if not math.isfinite(delay) or not 0 <= delay <= 30:
                raise ValueError("倒计时必须在 0～30 秒之间")
            self._stop.clear()
            self._pause.clear()
            self._sink = sink
            self._publish("countdown", 0, plan.duration, "准备开始")
            self._thread = threading.Thread(target=self._run, args=(plan, target_state, delay),
                                            name="score-player", daemon=True)
            self._thread.start()

    def toggle_pause(self):
        with self._lock:
            self.set_paused(not self._pause.is_set())

    def set_paused(self, paused):
        with self._lock:
            if not self.running or self._stop.is_set():
                return
            if paused:
                self._pause.set()
            else:
                self._pause.clear()

    def request_stop(self):
        self._stop.set()

    def stop(self, timeout=2.0):
        self.request_stop()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        if self.running:
            return False
        with self._lock:
            error = self._release_all()
            if error:
                self._publish("error", self._status.position, self._status.duration, error)
            return not self._pressed

    def _release_all(self):
        failures = []
        for key in tuple(self._pressed):
            for _ in range(3):
                try:
                    self._sink.key_up(key)
                    self._pressed.remove(key)
                    break
                except Exception as exc:
                    error = str(exc)
            else:
                failures.append(f"{key}: {error}")
        return "释放按键失败: " + "; ".join(failures) if failures else ""

    def _run(self, plan, target_state, delay):
        position = 0.0
        index = 0
        last = time.perf_counter()
        final_state, final_message = "finished", "演奏完成，按键已释放"
        pause_message = "已暂停，按 F9 继续"
        try:
            while not self._stop.is_set():
                now = time.perf_counter()
                elapsed = now - last
                last = now
                if self._pause.is_set():
                    with self._lock:
                        error = self._release_all()
                    if error:
                        raise RuntimeError(error)
                    self._publish("paused", position, plan.duration, pause_message)
                    self._stop.wait(0.005)
                    continue
                if delay > 0:
                    delay = max(0, delay - elapsed)
                    self._publish("countdown", position, plan.duration, f"{math.ceil(delay)} 秒后开始，请切到游戏")
                    self._stop.wait(0.005)
                    continue
                target = target_state()
                if target == "closed":
                    raise RuntimeError("目标窗口已关闭，请重新捕获")
                if target != "ready":
                    pause_message = "目标窗口失去焦点，已暂停；回到游戏后按 F9 继续"
                    self._pause.set()
                    continue
                if elapsed > 0.25:
                    pause_message = "系统响应延迟，已暂停以避免集中补发；按 F9 继续"
                    self._pause.set()
                    continue
                position = min(plan.duration, position + elapsed)
                while index < len(plan.events) and plan.events[index].time <= position:
                    event = plan.events[index]
                    with self._lock:
                        if self._stop.is_set() or self._pause.is_set():
                            break
                        if event.down:
                            if target_state() != "ready":
                                pause_message = "目标窗口失去焦点，已暂停；回到游戏后按 F9 继续"
                                self._pause.set()
                                break
                            # Track a key before sending so a partial backend failure still releases it.
                            self._pressed.add(event.key)
                            self._sink.key_down(event.key)
                        elif event.key in self._pressed:
                            self._sink.key_up(event.key)
                            self._pressed.remove(event.key)
                        index += 1
                self._publish("playing", position, plan.duration, "演奏中 · F9 暂停 · F10 停止")
                if index == len(plan.events) and position >= plan.duration:
                    break
                pause_message = "已暂停，按 F9 继续" if not self._pause.is_set() else pause_message
                self._stop.wait(0.005)
            if self._stop.is_set():
                final_state, final_message = "stopped", "已停止，按键已释放"
        except Exception as exc:
            final_state, final_message = "error", str(exc)
        finally:
            with self._lock:
                error = self._release_all()
                if error:
                    final_state, final_message = "error", f"{final_message}；{error}"
                self._publish(final_state, position, plan.duration, final_message)
