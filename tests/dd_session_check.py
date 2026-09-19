"""Opt-in real-DD lifecycle check. Uses empty scores and never sends key presses."""

from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import customtkinter as ctk

from main import DDInputBackend, USER32
from music_score import PlaybackPlan
from music_player import MusicPlayerPanel


def main():
    root = ctk.CTk()
    root.withdraw()
    created = []

    def create_backend(mode):
        backend = DDInputBackend()
        created.append(backend)
        return backend

    panel = MusicPlayerPanel(root, create_backend, USER32, listen=False)
    panel.target = SimpleNamespace(state=lambda: "ready")
    panel.make_plan = lambda: PlaybackPlan((), 0.01, 0, 0, ())
    panel.fields["delay"].delete(0, "end")
    panel.fields["delay"].insert(0, "0")
    try:
        previous_thread = None
        for index in range(5):
            panel.start()
            thread = panel.player._thread
            if thread is None or thread is previous_thread:
                raise RuntimeError(panel.status.cget("text"))
            previous_thread = thread
            thread.join(2)
            if thread.is_alive() or panel.player.snapshot().state != "finished":
                raise RuntimeError(str(panel.player.snapshot()))
            assert len(created) == 1
            assert not panel.player._sink.held
            print(f"DD session {index + 1}/5: OK (same initialized backend, no input sent)", flush=True)
        print("Native DD lifecycle check passed.", flush=True)
    finally:
        panel.player.stop()
        panel.closed = True
        for timer in root.tk.call("after", "info"):
            root.tk.call("after", "cancel", timer)
        root.destroy()


if __name__ == "__main__":
    main()
