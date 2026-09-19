import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import mido
import numpy as np
import soundfile as sf

from music_audio import AudioCancelled, estimate_pitch, export_midi, measure_note, transcribe_audio
from music_playback import ScorePlayer
from music_input import KeyboardSink
from music_score import (DEFAULT_MAPPING, DEMO_SCORE, KeyEvent, PlaybackPlan, ScoreError,
                         compile_score, load_midi, parse_pitch, parse_text, score_to_text)


class ScoreTests(unittest.TestCase):
    def test_text_chords_rests_extensions_and_octaves(self):
        score = parse_text("6, [3 5]:1/2 - 0:2 | 1'", bpm=120)
        self.assertEqual([note.pitch for note in score.notes], [57, 64, 67, 72])
        self.assertAlmostEqual(score.notes[1].duration, 0.75)
        self.assertAlmostEqual(score.notes[-1].start, 2.25)
        self.assertAlmostEqual(score.duration, 2.75)

    def test_invalid_token_reports_line(self):
        with self.assertRaisesRegex(ScoreError, "第 2 行"):
            parse_text("3\nwrong")
        for text in ("- 3", "3:0", "3:1/0", "123", "0 0", "[3 5"):
            with self.subTest(text=text), self.assertRaises(ScoreError):
                parse_text(text)

    def test_strict_missing_and_explicit_skip(self):
        score = parse_text("1 3 2")
        with self.assertRaisesRegex(ScoreError, "2 个音符"):
            compile_score(score)
        plan = compile_score(score, skip_missing=True)
        self.assertEqual(plan.skipped, 2)
        self.assertEqual(plan.note_count, 1)
        self.assertEqual(plan.events[0].time, 0.5)
        self.assertEqual(plan.duration, 1.5)

    def test_demo_fits_screenshot_mapping(self):
        plan = compile_score(parse_text(DEMO_SCORE))
        self.assertEqual(plan.skipped, 0)
        self.assertGreater(plan.note_count, 30)
        self.assertTrue({event.key for event in plan.events} <= set(DEFAULT_MAPPING.values()))

    def test_demo_fits_measured_octave(self):
        mapping = {pitch - 12: key for pitch, key in DEFAULT_MAPPING.items()}
        self.assertEqual(compile_score(parse_text(DEMO_SCORE, octave=3), mapping).skipped, 0)
        original = parse_text("6, 3 1'", octave=3)
        self.assertEqual(original.notes, parse_text(score_to_text(original, octave=3), octave=3).notes)

    def test_speed_transpose_and_short_repeated_notes(self):
        plan = compile_score(parse_text("1:1/16 1:1/16", bpm=120), transpose=4, speed=2)
        self.assertEqual(plan.events[0].key, "F")
        self.assertFalse(plan.events[1].down)
        self.assertLess(plan.events[1].time, plan.events[2].time)
        self.assertAlmostEqual(plan.duration, 0.03125)

    def test_mapping_validation(self):
        self.assertEqual(parse_pitch("Bb3"), 58)
        with self.assertRaises(ScoreError):
            compile_score(parse_text("3"), {64: "F", 65: "f"})
        with self.assertRaises(ScoreError):
            compile_score(parse_text("3"), speed=float("nan"))

    def test_midi_multitrack_tempo_and_drums(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("set_tempo", tempo=500_000),
            mido.MetaMessage("set_tempo", tempo=1_000_000, time=480),
        ]))
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("track_name", name="Melody"),
            mido.Message("note_on", note=64, velocity=90),
            mido.Message("note_off", note=64, time=960),
        ]))
        midi.tracks.append(mido.MidiTrack([
            mido.Message("note_on", note=36, channel=9, velocity=90),
            mido.Message("note_off", note=36, channel=9, time=480),
        ]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tempo.mid"
            midi.save(path)
            score = load_midi(path)
        self.assertEqual(len(score.notes), 1)
        self.assertEqual(score.notes[0].track, 1)
        self.assertAlmostEqual(score.notes[0].duration, 1.5)
        self.assertEqual(score.tracks, ((1, "Melody"),))

    def test_midi_round_trip(self):
        original = parse_text("3 0 [5 7]:2 1'", bpm=120)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "score.mid"
            export_midi(original, path)
            imported = load_midi(path)
        self.assertEqual([(n.pitch, n.start, n.duration) for n in original.notes],
                         [(n.pitch, n.start, n.duration) for n in imported.notes])

    def test_editable_monophonic_transcription(self):
        original = parse_text("0:1/2 #4 7:2 1' 6,", bpm=120)
        reparsed = parse_text(score_to_text(original), bpm=120)
        self.assertEqual(original.notes, reparsed.notes)


class AudioTests(unittest.TestCase):
    def test_sine_pitch_and_stereo_calibration(self):
        for frequency in (110, 220, 329.6276, 440, 1318.5102):
            with self.subTest(frequency=frequency):
                signal = np.sin(2 * np.pi * frequency * np.arange(2048) / 22050)
                detected, confidence = estimate_pitch(signal)
                self.assertLess(abs(1200 * np.log2(detected / frequency)), 5)
                self.assertGreater(confidence, 0.95)
        signal = np.sin(2 * np.pi * 440 * np.arange(24000) / 48000) * 0.1
        result = measure_note(np.column_stack((signal, -signal)), 48000, "J")
        self.assertEqual(result.pitch, 69)
        self.assertLess(abs(result.cents), 5)

    def test_silence_is_not_a_note(self):
        self.assertIsNone(estimate_pitch(np.zeros(2048))[0])
        with self.assertRaises(ScoreError):
            measure_note(np.zeros(48000), 48000, "B")

    def test_transcription_and_cancellation(self):
        sample_rate = 22050
        signal = []
        for pitch in (67, 72, 76):
            signal.append(np.zeros(4400))
            frequency = 440 * 2 ** ((pitch - 69) / 12)
            signal.append(0.2 * np.sin(2 * np.pi * frequency * np.arange(11025) / sample_rate))
        signal.append(np.zeros(4400))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solo.wav"
            sf.write(path, np.concatenate(signal), sample_rate)
            score = transcribe_audio(path)
            self.assertEqual([note.pitch for note in score.notes], [67, 72, 76])
            self.assertAlmostEqual(score.notes[0].start, 0.2, delta=0.1)
            stop = threading.Event()
            stop.set()
            with self.assertRaises(AudioCancelled):
                transcribe_audio(path, stop)


class InputAdapterTests(unittest.TestCase):
    def test_failed_release_is_retained_for_emergency_retry(self):
        backend = Mock()
        backend.prepare_keyboard_action.return_value = {"code": 1}
        backend.press_keyboard_action.return_value = True
        backend.release_keyboard_action.return_value = False
        sink = KeyboardSink(backend, ["F"])
        sink.key_down("F")
        with self.assertRaises(RuntimeError):
            sink.release_all()
        self.assertEqual(sink.held, {"F"})
        backend.release_keyboard_action.return_value = True
        sink.release_all()
        self.assertEqual(sink.held, set())

    def test_dd_initialization_retries_before_sending(self):
        from main import DDInputBackend

        dll = Mock()
        dll.DD_btn.side_effect = [0, 1]
        backend = DDInputBackend()
        with patch("main.ctypes.WinDLL", return_value=dll), patch("main.time.sleep") as sleep:
            backend.ensure_ready()
        self.assertEqual(dll.DD_btn.call_count, 2)
        sleep.assert_called_once_with(2.0)
        dll.DD_key.assert_not_called()


class FakeSink:
    def __init__(self, fail_down=None, fail_up=None):
        self.events = []
        self.active = set()
        self.fail_down = fail_down
        self.fail_up = fail_up

    def key_down(self, key):
        self.events.append(("down", key))
        self.active.add(key)
        if key == self.fail_down:
            raise RuntimeError("simulated down failure")

    def key_up(self, key):
        self.events.append(("up", key))
        if key == self.fail_up:
            raise RuntimeError("simulated up failure")
        self.active.discard(key)


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("Timed out waiting for playback state")


def test_plan():
    return PlaybackPlan((KeyEvent(0, "F", True), KeyEvent(0.12, "F", False),
                         KeyEvent(0.24, "H", True), KeyEvent(0.30, "H", False)), 0.35, 2, 0, ())


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.player = ScorePlayer()

    def tearDown(self):
        self.player.stop()

    def test_stop_releases_entire_simultaneous_chord_without_restarting(self):
        sink = FakeSink()
        plan = compile_score(parse_text("[3 5 7]:2"), DEFAULT_MAPPING, hold_ms=150)
        self.player.start(plan, sink, delay=0)
        keys = {DEFAULT_MAPPING[p] for p in (64, 67, 71)}
        wait_until(lambda: keys <= sink.active)
        self.assertTrue(self.player.stop())
        self.assertFalse(sink.active)
        events = list(sink.events)
        time.sleep(.05)
        self.assertEqual(events, sink.events)
        for key in keys:
            self.assertEqual(sink.events.count(("down", key)), 1)
            self.assertEqual(sink.events.count(("up", key)), 1)

    def test_stop_interrupts_countdown_without_input(self):
        sink = FakeSink()
        self.player.start(test_plan(), sink, delay=3)
        self.assertTrue(self.player.stop())
        self.assertEqual(sink.events, [])
        self.assertEqual(self.player.snapshot().state, "stopped")

    def test_stop_releases_held_key_and_does_not_resume(self):
        sink = FakeSink()
        self.player.start(test_plan(), sink, delay=0)
        wait_until(lambda: "F" in sink.active)
        self.assertTrue(self.player.stop())
        events = list(sink.events)
        time.sleep(0.05)
        self.assertFalse(sink.active)
        self.assertEqual(sink.events, events)
        self.assertNotIn(("down", "H"), sink.events)

    def test_focus_loss_pauses_and_requires_explicit_resume(self):
        sink = FakeSink()
        focused = threading.Event()
        focused.set()
        self.player.start(test_plan(), sink, lambda: "ready" if focused.is_set() else "background", delay=0)
        wait_until(lambda: "F" in sink.active)
        focused.clear()
        wait_until(lambda: self.player.snapshot().state == "paused")
        position = self.player.snapshot().position
        self.assertFalse(sink.active)
        focused.set()
        time.sleep(0.06)
        self.assertEqual(self.player.snapshot().state, "paused")
        self.assertAlmostEqual(self.player.snapshot().position, position)
        self.player.toggle_pause()
        wait_until(lambda: not self.player.running)
        self.assertEqual(self.player.snapshot().state, "finished")
        self.assertEqual(sink.events.count(("down", "F")), 1)
        self.assertEqual(sink.events.count(("down", "H")), 1)
        self.assertFalse(sink.active)

    def test_failed_down_still_releases_all_chord_keys(self):
        sink = FakeSink(fail_down="H")
        plan = PlaybackPlan((KeyEvent(0, "F", True), KeyEvent(0, "H", True)), 0.1, 2, 0, ())
        self.player.start(plan, sink, delay=0)
        wait_until(lambda: not self.player.running)
        self.assertEqual(self.player.snapshot().state, "error")
        self.assertFalse(sink.active)

    def test_release_failure_does_not_skip_other_keys_or_allow_restart(self):
        sink = FakeSink(fail_down="H", fail_up="F")
        plan = PlaybackPlan((KeyEvent(0, "F", True), KeyEvent(0, "H", True)), 0.1, 2, 0, ())
        self.player.start(plan, sink, delay=0)
        wait_until(lambda: not self.player.running)
        self.assertNotIn("H", sink.active)
        self.assertFalse(self.player.stop())
        with self.assertRaises(RuntimeError):
            self.player.start(test_plan(), FakeSink(), delay=0)
        sink.fail_up = None
        self.assertTrue(self.player.stop())
        self.assertFalse(sink.active)

    def test_closed_target_never_receives_input(self):
        sink = FakeSink()
        self.player.start(test_plan(), sink, lambda: "closed", delay=0)
        wait_until(lambda: not self.player.running)
        self.assertEqual(self.player.snapshot().state, "error")
        self.assertEqual(sink.events, [])


if __name__ == "__main__":
    unittest.main()
