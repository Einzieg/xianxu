"""Headless ONNX/voice-selection regressions; never open UI or send keys.

Run: .venv/Scripts/python.exe -m unittest tests.test_music_neural -v
Optional real-song checks: set MUSIC_NEURAL_AUDIO to the primary audio path;
set MUSIC_NEURAL_SECONDARY for another file. Each run writes fresh candidate
and lead MIDI/JSON under artifacts/neural-check. Statistics are NOT accuracy.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

import music_neural as neural
from music_audio import AudioCancelled
from music_score import ScoreError, load_midi


RATE = neural.SAMPLE_RATE
ROOT = Path(__file__).resolve().parents[1]
HAS_MODEL = neural.MODEL_PATH.is_file() and importlib.util.find_spec("onnxruntime") is not None


def tone(pitch, duration, amplitude=0.15, rate=RATE, partials=(1, 0.45, 0.2)):
    t = np.arange(round(duration * rate)) / rate
    hz = 440 * 2 ** ((pitch - 69) / 12)
    signal = sum(weight * np.sin(2 * np.pi * hz * harmonic * t)
                 for harmonic, weight in enumerate(partials, 1))
    envelope = np.minimum(t / 0.01, 1) * np.minimum((duration - t) / 0.02, 1)
    return np.asarray(signal * envelope * amplitude, dtype=np.float32)


def add_tone(signal, start, pitch, duration, amplitude=0.15, rate=RATE):
    wave = tone(pitch, duration, amplitude, rate)
    begin = round(start * rate)
    signal[begin:begin + len(wave)] += wave


def vibrato(pitch, depth, speed, duration=4):
    t = np.arange(round((duration + 0.5) * RATE)) / RATE
    age = t - 0.25
    frequency = 440 * 2 ** ((pitch - 69 + depth / 100 * np.sin(2 * np.pi * speed * age)) / 12)
    phase = 2 * np.pi * np.cumsum(frequency) / RATE
    envelope = np.clip(age / 0.015, 0, 1) * np.clip((duration - age) / 0.035, 0, 1)
    return (0.16 * envelope * (np.sin(phase) + 0.4 * np.sin(2 * phase) + 0.2 * np.sin(3 * phase))).astype(np.float32)


def legato(release):
    pitches = (77, 79, 81, 79, 82, 84, 79, 81)
    starts = 0.25 + np.arange(len(pitches)) * 0.35
    t = np.arange(round((starts[-1] + 0.35 + max(1, release * 5)) * RATE)) / RATE
    data = np.zeros(len(t), dtype=np.float32)
    for start, pitch in zip(starts, pitches):
        age = t - start
        envelope = np.clip(age / 0.012, 0, 1) * np.exp(-np.maximum(age - 0.32, 0) / release)
        envelope[age < 0] = 0
        phase = 2 * np.pi * (440 * 2 ** ((pitch - 69) / 12)) * age
        data += (0.14 * envelope * (np.sin(phase) + 0.4 * np.sin(2 * phase) + 0.2 * np.sin(3 * phase))).astype(np.float32)
    delay = round(0.073 * RATE)
    data[delay:] += 0.18 * data[:-delay].copy()
    return data, pitches, starts


def candidate(start, duration, pitch, confidence=0.85, onset=0.9):
    return neural.NeuralNote(start, duration, pitch, confidence, onset, 1.0)


def select(candidates, duration=5, register="auto", stop=None):
    return neural._select_lead(tuple(sorted(candidates, key=lambda n: (n.start, n.pitch))),
                               duration, register, stop, lambda value: None)[0]


def stats(score):
    pitches = np.array([n.pitch for n in score.notes])
    durations = np.array([n.duration for n in score.notes])
    return {
        "duration": score.duration, "notes": len(score.notes),
        "first_onset": score.notes[0].start,
        "last_offset": score.notes[-1].start + score.notes[-1].duration,
        "coverage": float(durations.sum() / score.duration),
        "pitch_percentiles": np.percentile(pitches, [0, 25, 50, 75, 100]).tolist(),
        "short_below_150ms": int((durations < 0.15).sum()),
        "jumps_at_least_octave": int((np.abs(np.diff(pitches)) >= 12).sum()),
        "adjacent_repetitions": int((np.diff(pitches) == 0).sum()),
    }


class NeuralUnitTests(unittest.TestCase):
    def test_chord_selection_keeps_overlaps_but_rejects_weak_evidence(self):
        notes = [candidate(.2, .7, pitch) for pitch in (60, 64, 67)]
        notes += [candidate(.2, .7, 84, confidence=.5), candidate(.2, .7, 48, onset=.55),
                  neural.NeuralNote(.2, .7, 55, .9, .9, .6)]
        selected = neural._select_chords(notes)
        self.assertEqual([n.pitch for n in selected], [60, 64, 67])
        self.assertEqual([(n.start, n.duration) for n in selected], [(.2, .7)] * 3)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(AudioCancelled):
            neural._select_chords(notes, stop)

    def test_chord_mode_validation_does_not_load_model(self):
        from music_audio import transcribe_audio
        with patch.object(neural, "_load_model") as model:
            for engine in ("melody", "yin"):
                with self.assertRaises(ScoreError):
                    transcribe_audio("missing.wav", engine=engine, texture="chords")
            with self.assertRaises(ScoreError):
                neural.analyze_instrument("missing.wav", texture="bad")
        model.assert_not_called()

    def test_chords_preserve_quiet_lead_and_do_not_duplicate_its_rearticulations(self):
        from music_score import Note
        lead = (Note(.2, .3, 60), Note(.5, .3, 60), Note(1, .4, 62))
        candidates = [candidate(.2, .8, 60), candidate(.2, .6, 64),
                      candidate(1, .4, 62, confidence=.5), candidate(1, .4, 67)]
        selected = neural._select_chords(candidates, lead=lead)
        self.assertEqual(tuple(n for n in selected if n.track == 0), lead)
        self.assertEqual([n.pitch for n in selected if n.track == 1], [64, 67])

    def setUp(self):
        (ROOT / "artifacts").mkdir(exist_ok=True)

    def test_model_provenance_and_license(self):
        directory = ROOT / "models" / "basic-pitch"
        manifest = json.loads((directory / "SOURCES.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["license"], "Apache-2.0")
        for item in manifest["files"]:
            self.assertEqual(hashlib.sha256((directory / item["path"]).read_bytes()).hexdigest(),
                             item["sha256"])
        self.assertIn("Copyright 2022 Spotify AB", (directory / "LICENSE").read_text(encoding="utf-8"))

    def test_missing_and_corrupt_model_are_explicit(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as temporary:
            path = Path(temporary) / "missing.onnx"
            with patch.object(neural, "MODEL_PATH", path):
                with self.assertRaisesRegex(ScoreError, "model missing"):
                    neural.transcribe_instrument("unused.wav")
                path.write_bytes(b"not an ONNX model")
                with self.assertRaisesRegex(ScoreError, "SHA256 mismatch"):
                    neural.transcribe_instrument("unused.wav")

    def test_missing_runtime_is_explicit(self):
        with patch.dict("sys.modules", {"onnxruntime": None}):
            with self.assertRaisesRegex(ScoreError, "Missing onnxruntime"):
                neural._load_model()

    def test_invalid_register_and_pre_cancel(self):
        with self.assertRaisesRegex(ScoreError, "register"):
            neural.transcribe_instrument("unused.wav", register="highest")
        stop = threading.Event()
        stop.set()
        with patch.object(neural, "_load_model") as load:
            with self.assertRaises(AudioCancelled):
                neural.transcribe_instrument("unused.wav", stop)
            load.assert_not_called()

    def test_window_padding_unwrap_and_explicit_output_order(self):
        class Session:
            def __init__(self):
                self.windows = []

            def run(self, names, feed):
                self.windows.append(feed[neural.INPUT_NAME].copy())
                assert names == ["StatefulPartitionedCall:1", "StatefulPartitionedCall:2",
                                 "StatefulPartitionedCall:0"]
                offset = (len(self.windows) - 1) * 1000
                return [np.broadcast_to((np.arange(172) + offset)[None, :, None],
                                         (1, 172, width)).astype(np.float32)
                        for width in (88, 88, 264)]

        data = np.ones(4 * RATE, dtype=np.float32)
        session = Session()
        output = neural._infer(data, session, None, lambda value: None)
        self.assertEqual(len(session.windows), 3)
        self.assertTrue((session.windows[0][0, :3840, 0] == 0).all())
        self.assertTrue((session.windows[0][0, 3840:, 0] == 1).all())
        self.assertEqual(session.windows[-1][0, -1, 0], 0)
        self.assertEqual(output["note"].shape, (int(len(data) / 36164 * 142), 88))
        self.assertEqual(output["note"][0, 0], 15)
        self.assertEqual(output["note"][141, 0], 156)
        self.assertEqual(output["note"][142, 0], 1015)

    def test_time_alignment_matches_official_full_300_seconds(self):
        count = int(300 * RATE / 36164 * 142)
        times = neural._frame_times(count + 1)
        frame = np.arange(count + 1)
        expected = frame * 256 / 22050 - np.floor(frame / 172) * (
            256 / 22050 * (172 - 43844 / 256) + 0.0018)
        np.testing.assert_allclose(times, expected, atol=1e-12)
        self.assertTrue((np.diff(times) > 0).all())
        self.assertLess(abs(times[-1] - 300), 0.03)

    def test_decoder_requires_onsets_and_sustain_not_frameactive(self):
        output = {"note": np.full((200, 88), 0.55), "onset": np.full((200, 88), 0.2)}
        self.assertEqual(neural._decode_candidates(output, 3, None, lambda value: None), ())
        output["note"][:] = 0.1
        output["onset"][30, 48] = 0.95
        self.assertEqual(neural._decode_candidates(output, 3, None, lambda value: None), ())

    def test_decoder_exact_reattacks_and_release(self):
        frames = np.zeros((220, 88))
        onsets = np.zeros_like(frames)
        frames[10:180, 48] = 0.8
        onsets[[10, 65, 120], 48] = 0.9
        notes = neural._decode_candidates({"note": frames, "onset": onsets}, 3, None, lambda v: None)
        self.assertEqual([n.pitch for n in notes], [69, 69, 69])
        times = neural._frame_times(221)
        np.testing.assert_allclose([n.start for n in notes], times[[10, 65, 120]])
        np.testing.assert_allclose([n.start + n.duration for n in notes], times[[65, 120, 180]])

    def test_held_lead_not_spliced_with_loud_ornament(self):
        notes = [candidate(0.1, 3, 69), candidate(0.1, 3, 45, 0.95),
                 candidate(1.0, 0.14, 93, 0.99), candidate(2.0, 0.14, 91, 0.99)]
        lead = select(notes, register="high")
        self.assertEqual([n.pitch for n in lead], [69])

    def test_rest_not_filled_by_bass_or_low_confidence_notes(self):
        notes = [candidate(0.1, 0.6, 69), candidate(1.6, 0.6, 71),
                 candidate(3.1, 0.6, 69), candidate(0, 4.5, 40, 0.95),
                 candidate(0.8, 0.5, 76, 0.44), candidate(2.4, 0.15, 88, 0.6)]
        lead = select(notes)
        self.assertEqual([n.pitch for n in lead], [69, 71, 69])
        self.assertFalse(any(n.start < 1 < n.start + n.duration for n in lead))

    def test_chromatic_and_real_octave_leaps_are_legal(self):
        pitches = [66, 68, 80, 68, 70, 82, 70, 66]
        notes = [candidate(i * 0.5, 0.46, pitch, 0.98, 0.98) for i, pitch in enumerate(pitches)]
        lead = select(notes)
        self.assertEqual([n.pitch for n in lead], pitches)

    def test_register_preferences_do_not_mean_highest_note(self):
        notes = []
        for index in range(8):
            for offset in (0, 24, 36):
                notes.append(candidate(index * 0.5, 0.45, 40 + offset + index % 3))
        notes.append(candidate(1.12, 0.13, 105, 0.99, 0.99))
        medians = {register: np.median([n.pitch for n in select(notes, register=register)])
                   for register in neural.REGISTERS}
        self.assertLess(medians["low"], medians["mid"])
        self.assertLess(medians["mid"], medians["high"])
        self.assertLess(medians["high"], 90)

    def test_near_release_overlap_trimmed_and_repeated_notes_kept(self):
        notes = [candidate(i * 0.5, 0.54, 69, 0.95) for i in range(6)]
        lead = select(notes)
        self.assertEqual(len(lead), 6)
        for left, right in zip(lead, lead[1:]):
            self.assertLessEqual(left.start + left.duration, right.start + 1e-10)

    def test_validated_reattack_prefix_not_lost_before_next_pitch(self):
        notes = [candidate(0, 0.5, 69, 0.8),
                 candidate(0.5, 0.45, 69, 0.55),
                 candidate(0.6, 0.8, 71, 0.9)]
        lead = select(notes, duration=2)
        self.assertEqual([n.pitch for n in lead], [69, 69, 71])
        np.testing.assert_allclose([n.start for n in lead], [0, 0.5, 0.6])
        np.testing.assert_allclose([n.duration for n in lead], [0.5, 0.1, 0.8])

    def test_reattack_recovery_does_not_invent_notes_across_rests(self):
        for middle in (candidate(0.53, 0.45, 69, 0.55),
                       candidate(0.5, 0.45, 70, 0.55),
                       candidate(0.5, 0.45, 69, 0.44)):
            with self.subTest(middle=middle):
                notes = [candidate(0, 0.5, 69, 0.8), middle,
                         candidate(0.6, 0.8, 71, 0.9)]
                lead = select(notes, duration=2)
                self.assertEqual([n.pitch for n in lead], [69, 71])
                self.assertAlmostEqual(lead[0].start + lead[0].duration, 0.5)

    def test_reattack_recovery_requires_positive_prefix_evidence(self):
        times = neural._frame_times(173)
        output = {"note": np.zeros((172, 88), dtype=np.float32)}
        output["note"][times[:-1] < .5, 48] = .8
        output["note"][(times[:-1] >= .5) & (times[:-1] < .6), 48] = .2
        output["note"][(times[:-1] >= .6) & (times[:-1] < .95), 48] = .8
        output["note"][(times[:-1] >= .6) & (times[:-1] < 1.4), 50] = .95
        notes = [candidate(0, .5, 69, .8), candidate(.5, .45, 69, .55),
                 candidate(.6, .8, 71, .9)]
        lead, _ = neural._select_lead(notes, 2, "auto", None, lambda v: None,
                                      output=output, fixed_centers=(70.0,))
        self.assertEqual([n.pitch for n in lead], [69, 71])
        self.assertAlmostEqual(lead[0].duration, .5)

    def test_cancellation_in_decoder_and_tracker(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(AudioCancelled):
            select([candidate(0, 1, 69)], stop=stop)
        with self.assertRaises(AudioCancelled):
            neural._decode_candidates({"note": np.zeros((20, 88)), "onset": np.zeros((20, 88))},
                                      1, stop, lambda v: None)

    def test_revision_and_manifest_agree(self):
        manifest = json.loads((ROOT / "models/basic-pitch/SOURCES.json").read_text(encoding="utf-8"))
        self.assertEqual(neural.TRANSCRIPTION_REVISION, "legato-v2.1")
        self.assertEqual(manifest["transcription_revision"], neural.TRANSCRIPTION_REVISION)

    def test_cancellation_in_register_and_acoustic_stages(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(AudioCancelled):
            neural._register_curve([candidate(0, 1, 69)], 1, "auto", stop)
        with self.assertRaises(AudioCancelled):
            neural._pitch_envelope(np.zeros(RATE), 69, stop)
        output = {"note": np.zeros((86, 88)), "onset": np.zeros((86, 88))}
        with self.assertRaises(AudioCancelled):
            neural._acoustic_evidence(np.zeros(RATE), output, stop, lambda v: None)

    def test_sparse_integrals_allow_positive_prefix_with_negative_tail(self):
        times = neural._frame_times(261)
        output = {"note": np.zeros((260, 88), dtype=np.float32)}
        output["note"][:90, 48] = 0.95
        output["note"][90:200, 50] = 0.95
        notes = [candidate(0, float(times[200]), 69, 0.6),
                 candidate(float(times[90]), float(times[200] - times[90]), 71, 0.95)]
        lead, _ = neural._select_lead(notes, 3, "auto", None, lambda v: None,
                                      output=output, fixed_centers=(70.0,))
        self.assertEqual([n.pitch for n in lead], [69, 71])
        self.assertAlmostEqual(lead[0].duration, times[90])


@unittest.skipUnless(HAS_MODEL, "Official ONNX model and onnxruntime required")
class NeuralRuntimeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "artifacts").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "artifacts")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "fixture.wav"

    def write(self, data, rate=RATE):
        sf.write(self.path, data, rate, subtype="FLOAT")
        return self.path

    def test_real_onnx_chromatic_sequence_resampling_and_stereo(self):
        pitches = (66, 68, 69, 73, 71, 68)
        rate = 48000
        signal = np.zeros(round(4.2 * rate), dtype=np.float32)
        for index, pitch in enumerate(pitches):
            add_tone(signal, 0.2 + index * 0.6, pitch, 0.5, rate=rate)
        progress = []
        score = neural.transcribe_instrument(self.write(np.column_stack((signal, -signal)), rate),
                                             progress=progress.append)
        self.assertEqual([n.pitch for n in score.notes], list(pitches))
        np.testing.assert_allclose([n.start for n in score.notes], 0.2 + np.arange(6) * 0.6, atol=0.08)
        self.assertTrue(all(a <= b for a, b in zip(progress, progress[1:])))
        self.assertEqual(progress[-1], 1)
        self.assertAlmostEqual(score.duration, len(signal) / rate)

    def test_real_onnx_repeated_attacks(self):
        signal = np.zeros(round(3.5 * RATE), dtype=np.float32)
        for index in range(6):
            add_tone(signal, 0.1 + index * 0.55, 69, 0.44)
        score = neural.transcribe_instrument(self.write(signal))
        self.assertEqual([n.pitch for n in score.notes], [69] * 6)
        np.testing.assert_allclose([n.start for n in score.notes], 0.1 + np.arange(6) * 0.55, atol=0.08)

    def test_real_onnx_same_pitch_reattacks_without_silence(self):
        t = np.arange(round(3.3 * RATE)) / RATE
        phase = np.mod(t, 0.55)
        envelope = 0.14 + 0.86 * np.minimum(phase / 0.01, 1) * np.exp(-phase / 0.12)
        signal = 0.2 * envelope * (np.sin(2 * np.pi * 440 * t) + 0.45 * np.sin(2 * np.pi * 880 * t))
        score = neural.transcribe_instrument(self.write(signal))
        self.assertEqual([n.pitch for n in score.notes], [69] * 6)
        np.testing.assert_allclose([n.start for n in score.notes], np.arange(6) * 0.55, atol=0.05)
        for left, right in zip(score.notes, score.notes[1:]):
            self.assertAlmostEqual(left.start + left.duration, right.start)

    def test_existing_audio_bridge_matches_direct_interface(self):
        from music_audio import transcribe_audio
        path = self.write(tone(69, 1))
        direct = neural.transcribe_instrument(path, register="mid")
        bridged = transcribe_audio(path, engine="instrument", register="mid")
        self.assertEqual(bridged, direct)

    def test_real_onnx_polyphony_and_louder_bass(self):
        pitches = (64, 67, 69, 72, 71, 67, 64, 62)
        signal = np.zeros(round(5.3 * RATE), dtype=np.float32)
        for index, pitch in enumerate(pitches):
            start = 0.2 + index * 0.6
            add_tone(signal, start, pitch, 0.5, 0.13)
            if index % 2 == 0:
                add_tone(signal, start, (40, 43, 45, 41)[index // 2], 1.1, 0.32)
        result = neural.analyze_instrument(self.write(signal), register="high")
        found = [n.pitch for n in result.score.notes]
        print(json.dumps({"fixture": "louder_bass", "lead": found,
                          "candidates": len(result.candidates)}))
        self.assertEqual(found, list(pitches))
        self.assertGreater(len(result.candidates), len(result.score.notes))

    def test_real_onnx_chord_mode_retains_triad_through_nine_key_adaptation(self):
        from music_adapt import adapt_score
        from music_library import CALIBRATED_MAPPING
        from music_score import compile_score
        signal = np.zeros(round(2 * RATE), dtype=np.float32)
        for pitch in (60, 64, 67):
            add_tone(signal, .2, pitch, 1.1, .13)
        path = self.write(signal)
        lead = neural.analyze_instrument(path)
        result = neural.analyze_instrument(path, texture="chords")
        self.assertEqual(tuple(n for n in result.score.notes if n.track == 0), lead.score.notes)
        self.assertTrue({60, 64, 67} <= {n.pitch for n in result.score.notes})
        starts = [n.start for n in result.score.notes if n.pitch in (60, 64, 67)]
        self.assertLess(max(starts) - min(starts), .1)
        arranged = adapt_score(result.score, CALIBRATED_MAPPING, texture="chords")
        self.assertEqual(arranged.removed, 0)
        self.assertGreaterEqual(len({n.pitch for n in arranged.score.notes}), 3)
        self.assertEqual(compile_score(arranged.score, CALIBRATED_MAPPING).skipped, 0)
        exported = neural.export_diagnostics(result, self.directory / "diagnostics")
        metadata = json.loads(exported["json"].read_text(encoding="utf-8"))
        self.assertEqual(metadata["transcription_revision"], "chords-v1")

    def test_real_onnx_silence_white_and_pink_noise_rejected(self):
        white = np.random.default_rng(7).normal(0, 0.06, 3 * RATE).astype(np.float32)
        spectrum = np.fft.rfft(white)
        spectrum /= np.sqrt(np.maximum(np.arange(len(spectrum)), 1))
        pink = np.fft.irfft(spectrum, n=len(white)).astype(np.float32)
        pink *= 0.06 / np.std(pink)
        for data in (np.zeros(RATE, dtype=np.float32), white, pink):
            with self.subTest(peak=float(np.max(np.abs(data)))):
                with self.assertRaisesRegex(ScoreError, "No reliable"):
                    neural.transcribe_instrument(self.write(data))

    def test_length_limits_bad_samples_and_decode_failure(self):
        for duration in (0.09, 300.01):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ScoreError, "0.1 and 300"):
                    neural.transcribe_instrument(self.write(np.zeros(round(duration * 8000)), 8000))
        with self.assertRaisesRegex(ScoreError, "non-finite"):
            neural.transcribe_instrument(self.write(np.full(RATE, np.nan)))
        with self.assertRaisesRegex(ScoreError, "Cannot decode"):
            neural.transcribe_instrument(self.directory / "absent.mp3")

    def test_cancel_during_decode_inference_and_final_callback(self):
        self.write(tone(69, 8))
        for threshold in (0.02, 0.1, 0.75, 1.0):
            stop = threading.Event()

            def progress(value):
                if value >= threshold:
                    stop.set()

            with self.subTest(threshold=threshold), self.assertRaises(AudioCancelled):
                neural.transcribe_instrument(self.path, stop, progress)

    def test_cancel_acoustic_integral_and_dp_progress(self):
        self.write(legato(0.12)[0])
        for threshold in (0.78, 0.925, 0.945, 0.98):
            stop = threading.Event()
            requested = []
            values = []

            def progress(value):
                values.append(value)
                if value >= threshold and not requested:
                    requested.append(time.perf_counter())
                    stop.set()

            with self.subTest(threshold=threshold), self.assertRaises(AudioCancelled):
                neural.transcribe_instrument(self.path, stop, progress)
            latency = time.perf_counter() - requested[0]
            self.assertLess(latency, 1.0)
            self.assertTrue(all(a <= b for a, b in zip(values, values[1:])))
            print(json.dumps({"cancel_progress": threshold, "cancel_latency_seconds": latency}))

    def test_24_single_attack_vibrato_cases(self):
        for pitch in (81, 82):
            for speed in (4, 5, 6):
                for depth in (0, 10, 30, 50):
                    with self.subTest(pitch=pitch, hz=speed, cents=depth):
                        score = neural.transcribe_instrument(self.write(vibrato(pitch, depth, speed)))
                        self.assertEqual([n.pitch for n in score.notes], [pitch])
                        self.assertAlmostEqual(score.notes[0].start, 0.25, delta=0.09)
                        self.assertGreater(score.notes[0].duration, 3.8)

    def test_legato_120ms_release_eight_notes(self):
        signal, pitches, starts = legato(0.12)
        score = neural.transcribe_instrument(self.write(signal))
        self.assertEqual([n.pitch for n in score.notes], list(pitches))
        np.testing.assert_allclose([n.start for n in score.notes], starts, atol=0.09)

    def test_three_sixfold_reattacks_not_merged(self):
        t = np.arange(round(3.6 * RATE)) / RATE
        age = np.mod(t, 0.6)
        for gap in (0, 0.03, 0.1):
            if gap == 0:
                envelope = 0.14 + 0.86 * np.minimum(age / 0.01, 1) * np.exp(-age / 0.10)
            else:
                envelope = np.clip(age / 0.012, 0, 1) * np.clip((0.6 - gap - age) / 0.02, 0, 1)
            signal = 0.16 * envelope * (np.sin(2 * np.pi * 880 * t) + 0.4 * np.sin(2 * np.pi * 1760 * t))
            with self.subTest(gap=gap):
                score = neural.transcribe_instrument(self.write(signal))
                self.assertEqual([n.pitch for n in score.notes], [81] * 6)
                np.testing.assert_allclose([n.start for n in score.notes], np.arange(6) * 0.6, atol=0.09)

    def test_fast_repeats_and_actual_semitone_changes(self):
        for pitches, step in (((81,) * 8, 0.22), ((81, 82, 81, 82, 83, 82, 81, 80), 0.32)):
            signal = np.zeros(round((len(pitches) * step + 0.4) * RATE), dtype=np.float32)
            starts = 0.2 + np.arange(len(pitches)) * step
            for start, pitch in zip(starts, pitches):
                add_tone(signal, start, pitch, step - 0.03)
            with self.subTest(step=step):
                score = neural.transcribe_instrument(self.write(signal))
                self.assertEqual([n.pitch for n in score.notes], list(pitches))
                np.testing.assert_allclose([n.start for n in score.notes], starts, atol=0.08)

    def test_held_lead_with_nearby_and_octave_ornaments(self):
        for pitch in (83, 93):
            signal = vibrato(81, 0, 5)
            t = np.arange(len(signal)) / RATE
            age = t - 1.4
            envelope = np.clip(age / 0.006, 0, 1) * np.clip((0.15 - age) / 0.015, 0, 1)
            signal += (0.30 * envelope * np.sin(2 * np.pi * (440 * 2 ** ((pitch - 69) / 12)) * t)).astype(np.float32)
            with self.subTest(ornament=pitch):
                score = neural.transcribe_instrument(self.write(signal))
                self.assertEqual([n.pitch for n in score.notes], [81])
                self.assertGreater(score.notes[0].duration, 3.8)

    def test_short_audio_never_uses_incomplete_attack_windows(self):
        for duration in (0.1, 0.15, 0.2, 0.299):
            with self.subTest(duration=duration), patch.object(neural, "_acoustic_attack") as attack:
                try:
                    score = neural.transcribe_instrument(self.write(tone(69, duration)))
                except ScoreError as exc:
                    self.assertIn("No reliable", str(exc))
                    self.assertLess(duration, 0.2)
                else:
                    self.assertEqual([n.pitch for n in score.notes], [69])
                    self.assertAlmostEqual(score.duration, round(duration * RATE) / RATE)
                attack.assert_not_called()

    def test_guarded_envelope_blocks_match_unblocked_result(self):
        signal = vibrato(81, 30, 6, duration=8)
        path = self.write(signal)
        reference = neural.analyze_instrument(path)
        with patch.object(neural, "ENVELOPE_BLOCK_SAMPLES", 2 * RATE):
            blocked = neural.analyze_instrument(path)
        self.assertEqual(blocked.score, reference.score)
        self.assertEqual(blocked.candidates, reference.candidates)

    def test_full_300_seconds_no_crop_or_cumulative_time_drift(self):
        signal = np.zeros(300 * RATE, dtype=np.float32)
        for start in (0.3, 60.3, 150.3, 298.8):
            add_tone(signal, start, 69, 0.7)
        score = neural.transcribe_instrument(self.write(signal))
        self.assertEqual([n.pitch for n in score.notes], [69] * 4)
        np.testing.assert_allclose([n.start for n in score.notes], [0.3, 60.3, 150.3, 298.8], atol=0.09)
        self.assertEqual(score.duration, 300)
        self.assertLessEqual(score.notes[-1].start + score.notes[-1].duration, 300)

    def test_export_is_separate_raw_pitches_and_never_overwrites(self):
        result = neural.analyze_instrument(self.write(tone(78, 1)))
        first = neural.export_diagnostics(result, self.directory)
        old = {key: path.read_bytes() for key, path in first.items()}
        second = neural.export_diagnostics(result, self.directory)
        self.assertTrue(set(first.values()).isdisjoint(second.values()))
        for key, path in first.items():
            self.assertEqual(old[key], path.read_bytes())
        self.assertEqual(load_midi(first["lead"]).notes[0].pitch, 78)
        document = json.loads(first["json"].read_text(encoding="utf-8"))
        self.assertIn("candidates", document)
        self.assertEqual(document["score"]["notes"][0]["pitch"], 78)


@unittest.skipUnless(os.environ.get("MUSIC_NEURAL_AUDIO"), "Set MUSIC_NEURAL_AUDIO for real-song statistics")
class NeuralRealSongTests(unittest.TestCase):
    def test_real_files_all_registers_export_statistics_only(self):
        summary = []
        for variable in ("MUSIC_NEURAL_AUDIO", "MUSIC_NEURAL_SECONDARY"):
            value = os.environ.get(variable)
            if not value:
                continue
            path = Path(value)
            self.assertTrue(path.is_file(), f"Requested fixture missing: {path}")
            started = time.perf_counter()
            original = neural.analyze_instrument(path)
            inference_seconds = time.perf_counter() - started
            for register in neural.REGISTERS:
                with self.subTest(audio=path.name, register=register):
                    result = original if register == "auto" else neural.analyze_instrument(path, register=register)
                    notes, centers = result.score.notes, result.register_centers
                    self.assertTrue(notes)
                    score = result.score
                    paths = neural.export_diagnostics(result, ROOT / "artifacts" / "neural-check")
                    for left, right in zip(notes, notes[1:]):
                        self.assertLessEqual(left.start + left.duration, right.start + 1e-9)
                    self.assertTrue(all(0 <= n.start < n.start + n.duration <= score.duration for n in notes))
                    entry = {"audio": str(path), "register": register,
                             "inference_seconds": inference_seconds, "candidates": len(original.candidates),
                             "statistics_not_accuracy": stats(score), "centers": centers,
                             "outputs": {key: str(output) for key, output in paths.items()}}
                    summary.append(entry)
                    print(json.dumps(entry, ensure_ascii=False))
        directory = ROOT / "artifacts" / "neural-check"
        with tempfile.NamedTemporaryFile(mode="w", prefix="statistics-", suffix=".json",
                                         dir=directory, encoding="utf-8", delete=False) as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)


@unittest.skipUnless(os.environ.get("MUSIC_NEURAL_E_COMPARE"), "Set MUSIC_NEURAL_E_COMPARE for accepted E fixtures")
class NeuralAcceptedETests(unittest.TestCase):
    def test_two_real_files_preserve_accepted_E_and_only_restore_split_prefixes(self):
        directory = ROOT / "artifacts/neural-diagnosis-20260918-r1"
        expected = json.loads((directory / "CDE-real-song-AB.json").read_text(encoding="utf-8"))
        inputs = json.loads((directory / "initial-findings.json").read_text(encoding="utf-8"))
        for name in ("peter", "castle"):
            with self.subTest(name=name):
                started = time.perf_counter()
                result = neural.analyze_instrument(inputs[name]["audio"])
                wanted = expected[name]["versions"]["E-combined-candidate"]
                actual = [dict(start=n.start, duration=n.duration, pitch=n.pitch, track=n.track) for n in result.score.notes]
                self.assertTrue(all(note in actual for note in wanted))
                extra = [note for note, row in zip(result.score.notes, actual) if row not in wanted]
                for note in extra:
                    candidate = next(n for n in result.candidates
                                     if n.start == note.start and n.pitch == note.pitch)
                    self.assertLessEqual(note.duration, candidate.duration + 1e-8)
                    self.assertTrue(any(n["pitch"] == note.pitch and
                                        abs(n["start"] + n["duration"] - note.start) < 1e-8 for n in wanted))
                    self.assertTrue(any(abs(n["start"] - note.start - note.duration) < 1e-8 for n in wanted))
                if name == "castle":
                    self.assertTrue(any(abs(n.start - 10.467009070294786) < 1e-8 and n.pitch == 74 for n in extra))
                else:
                    self.assertEqual([n for n in actual if n["start"] < 15], [n for n in wanted if n["start"] < 15])
                self.assertEqual(result.register_centers, tuple(inputs[name]["centers"]))
                print(json.dumps({"E_notes_preserved": name, "restored_reattacks": len(extra), "notes": len(actual),
                                  "seconds": time.perf_counter() - started}))


def benchmark_300s():
    """Fresh-process workload so reported peak RSS is not polluted by other tests."""
    signal = np.zeros(300 * RATE, dtype=np.float32)
    for index, start in enumerate(np.arange(0.2, 299, 0.6)):
        add_tone(signal, start, (69, 71, 72, 74, 76, 74, 72, 71)[index % 8], 0.5)
        if index % 2 == 0:
            add_tone(signal, start, (40, 43, 45, 41)[index % 4], 0.9, 0.12)
    with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as temporary:
        path = Path(temporary) / "300s.wav"
        sf.write(path, signal, RATE, subtype="FLOAT")
        del signal
        block_max = []
        original = neural._pitch_envelope

        def measure(data, pitch, stop):
            block_max.append(len(data))
            return original(data, pitch, stop)

        started = time.perf_counter()
        with patch.object(neural, "_pitch_envelope", side_effect=measure):
            result = neural.analyze_instrument(path)
        elapsed = time.perf_counter() - started
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (field, ctypes.c_size_t) for field in ("PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        peak = counters.PeakWorkingSetSize
    else:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    print(json.dumps({"duration": result.score.duration, "notes": len(result.score.notes),
                      "candidates": len(result.candidates), "seconds": elapsed,
                      "peak_rss_mib": peak / 2 ** 20, "max_envelope_samples": max(block_max)}))


@unittest.skipUnless(os.environ.get("MUSIC_NEURAL_BENCHMARK"), "Set MUSIC_NEURAL_BENCHMARK for fresh-process 300s RSS")
class NeuralMemoryTests(unittest.TestCase):
    def test_300_seconds_bounded_memory(self):
        process = subprocess.run([sys.executable, "-B", "-c",
            "from tests.test_music_neural import benchmark_300s; benchmark_300s()"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True, timeout=120)
        report = json.loads(process.stdout)
        print(json.dumps(report))
        self.assertEqual(report["duration"], 300)
        self.assertLess(report["peak_rss_mib"], 512)
        self.assertLessEqual(report["max_envelope_samples"],
                             neural.ENVELOPE_BLOCK_SAMPLES + 2 * neural.ENVELOPE_GUARD_SAMPLES)


if __name__ == "__main__":
    unittest.main()
