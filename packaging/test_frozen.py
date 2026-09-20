"""Opt-in packaged-model smoke test with synthetic audio and no input devices."""
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import wave
import mido


def main():
    engine = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="xianxu-model-smoke-") as directory:
        root = Path(directory)
        audio = root / "synthetic.wav"
        rate = 22050
        with wave.open(str(audio), "wb") as target:
            target.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            samples = []
            for index in range(rate * 4):
                time_s = index / rate
                frequency = (261.63, 329.63, 392.0, 523.25)[int(time_s)]
                envelope = min(1, (time_s % 1) * 20) * max(0, 1 - (time_s % 1))
                samples.append(int(15000 * envelope * math.sin(2 * math.pi * frequency * time_s)))
            target.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        with (root / "stderr.log").open("w+", encoding="utf-8") as error_log:
            process = subprocess.Popen(
                [str(engine), "--no-hotkeys", "--data-dir", str(root / "data")],
                cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=error_log, text=True, encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            def rpc(method, **params):
                process.stdin.write(json.dumps({"method": method, "params": params}) + "\n")
                process.stdin.flush()
                line = process.stdout.readline()
                if not line:
                    error_log.seek(0)
                    raise RuntimeError(error_log.read())
                response = json.loads(line)
                if not response["ok"]:
                    raise RuntimeError(response)
                return response["result"]

            try:
                midi_path = root / "contour.mid"
                midi = mido.MidiFile()
                track = mido.MidiTrack()
                midi.tracks.append(track)
                for pitch in (73, 82, 77, 77, 75, 73, 70):
                    track.append(mido.Message("note_on", note=pitch, velocity=80, time=48))
                    track.append(mido.Message("note_off", note=pitch, time=144))
                midi.save(midi_path)
                item = rpc("import_score", path=str(midi_path))
                detail = rpc("get_score", id=item["id"])
                notes = detail["notes"]
                assert len(notes) == 7 and all(n["key"] for n in notes)
                assert notes[1]["pitch"] > notes[0]["pitch"]
                assert notes[1]["pitch"] > notes[2]["pitch"]
                assert notes[2]["pitch"] == notes[3]["pitch"]
                print("Packaged contour import: OK (peak, descent, repeated notes)")
                rpc("transcribe", path=str(audio), engine="instrument")
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    state = rpc("get_state")
                    job = state["job"]
                    if job["state"] == "error":
                        raise RuntimeError(job)
                    if job["state"] == "done":
                        print("Packaged ONNX transcription: OK", job["message"])
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("Packaged transcription timed out")
            finally:
                rpc("shutdown")
                process.wait(timeout=15)
                process.stdin.close()
                process.stdout.close()
            if process.returncode:
                raise RuntimeError(f"Engine exited with {process.returncode}")


if __name__ == "__main__":
    main()
