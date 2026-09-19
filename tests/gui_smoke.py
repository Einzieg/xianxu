"""Render and exercise the actual UI without ever creating an input backend."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import customtkinter as ctk
from PIL import ImageGrab

from main import USER32
from music_player import MusicPlayerPanel


def forbidden_backend(mode):
    raise AssertionError("GUI preview must not initialize a real input backend")


def main():
    ctk.set_appearance_mode("Dark")
    root = ctk.CTk()
    root.title("Music player UI verification")
    root.geometry("1060x780+50+50")
    root.attributes("-topmost", True)
    panel = MusicPlayerPanel(root, forbidden_backend, USER32, listen=False)
    panel.pack(fill="both", expand=True)
    errors = []
    root.report_callback_exception = lambda kind, error, tb: errors.append(str(error))

    def stop_preview():
        try:
            assert panel.player.snapshot().position > 0, panel.player.snapshot()
            panel.stop()
            assert panel.player.stop()
            assert not panel.player.snapshot().pressed
            root.geometry("880x620+50+50")
            root.after(150, check_small)
        except Exception as exc:
            errors.append(str(exc))
            panel.shutdown(root.destroy)

    def check_small():
        try:
            assert panel.stop_button.winfo_rooty() + panel.stop_button.winfo_height() <= root.winfo_rooty() + root.winfo_height()
            assert panel.status.winfo_rooty() + panel.status.winfo_height() <= root.winfo_rooty() + root.winfo_height()
            assert not errors, errors
            print("GUI smoke: calibrated demo compiles, preview advances, stop releases, minimum-size controls fit.", flush=True)
        except Exception as exc:
            errors.append(str(exc))
        panel.shutdown(root.destroy)

    def check():
        try:
            assert panel.make_plan().skipped == 0
            assert panel.fields["octave"].get() == "3"
            root.lift()
            root.update_idletasks()
            output = Path(__file__).resolve().parents[1] / "artifacts" / "ui"
            output.mkdir(parents=True, exist_ok=True)
            x, y = root.winfo_rootx(), root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(output / "music-player.png")
            panel.start(preview=True)
            root.after(250, stop_preview)
        except Exception as exc:
            errors.append(str(exc))
            panel.shutdown(root.destroy)

    root.after(1200, check)
    root.mainloop()
    if errors:
        raise AssertionError(errors)


if __name__ == "__main__":
    main()
