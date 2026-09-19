"""Offline transcription regressions; no device capture, GUI or keyboard input.

Run with: python -m unittest tests.test_music_transcription -v
Set MUSIC_TRANSCRIPTION_AUDIO to a file to also print reproducible, non-listening
statistics for both engines. These statistics are not real-song pitch accuracy.
"""

from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from music_audio import AudioCancelled, estimate_pitch, measure_note, transcribe_audio
from music_score import ScoreError


RATE = 22050


def tone(pitch, duration, amplitude=0.12, partials=(1, 0.5, 0.25), rate=RATE):
    t = np.arange(round(duration * rate)) / rate
    frequency = 440 * 2 ** ((pitch - 69) / 12)
    wave = sum(weight * np.sin(2 * np.pi * frequency * harmonic * t)
               for harmonic, weight in enumerate(partials, 1))
    envelope = np.minimum(t / 0.012, 1) * np.minimum((duration - t) / 0.025, 1)
    return amplitude * wave * envelope


def mixture(pitches=(64, 67, 69, 72, 71, 67, 64, 62), bass_amplitude=0.38):
    length = 0.2 + len(pitches) * 0.45 + 0.2
    signal = np.zeros(round(length * RATE))
    truth = []
    for index, pitch in enumerate(pitches):
        start = 0.2 + index * 0.45
        note = tone(pitch, 0.42)
        offset = round(start * RATE)
        signal[offset:offset + len(note)] += note
        truth.append((start, 0.42, pitch))
        bass = tone((40, 43, 45, 41)[(index // 2) % 4], 0.45,
                    bass_amplitude, (1, 0.55, 0.3, 0.18, 0.12))
        signal[offset:offset + len(bass)] += bass
    return signal, truth


def accuracy(score, truth):
    correct = total = 0
    for start, duration, pitch in truth:
        for instant in np.arange(start + 0.1, start + duration - 0.06, 0.01):
            found = next((n.pitch for n in score.notes if n.start <= instant < n.start + n.duration), -1)
            correct += found == pitch
            total += 1
    return correct / total


def statistics(score):
    pitches = np.array([n.pitch for n in score.notes])
    durations = np.array([n.duration for n in score.notes])
    return dict(duration=score.duration, notes=len(pitches),
                coverage=float(durations.sum() / score.duration),
                pitch_percentiles=np.percentile(pitches, [0, 25, 50, 75, 100]).tolist(),
                low_below_55=int((pitches < 55).sum()),
                median_note_seconds=float(np.median(durations)),
                short_below_120ms=int((durations < 0.12).sum()),
                octave_jumps=int((np.abs(np.diff(pitches)) >= 12).sum()),
                repeated_adjacent=int((np.diff(pitches) == 0).sum()),
                top_pitches=Counter(pitches.tolist()).most_common(8))


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fixture.wav"

    def write(self, data, rate=RATE):
        sf.write(self.path, data, rate, subtype="FLOAT")
        return self.path

    def test_melody_over_louder_harmonic_bass(self):
        for bass_amplitude in (0.2, 0.38, 0.55):
            with self.subTest(bass_amplitude=bass_amplitude):
                signal, truth = mixture(bass_amplitude=bass_amplitude)
                self.write(signal)
                melody = transcribe_audio(self.path)
                try:
                    solo = transcribe_audio(self.path, engine="yin")
                    solo_accuracy = accuracy(solo, truth)
                except ScoreError:
                    solo_accuracy = 0
                melodic_accuracy = accuracy(melody, truth)
                print(json.dumps(dict(fixture="harmonic_bass", bass_amplitude=bass_amplitude,
                                      melody_accuracy=melodic_accuracy, yin_accuracy=solo_accuracy)))
                self.assertGreaterEqual(melodic_accuracy, 0.9)
                self.assertGreaterEqual(melodic_accuracy - solo_accuracy, 0.5)
                self.assertEqual([n.pitch for n in melody.notes], [p for _, _, p in truth])

    def test_weak_fundamental_with_real_octave_leaps(self):
        pitches = (60, 67, 64, 72, 69, 81, 69, 60)
        signal = np.zeros(round(3.2 * RATE))
        truth = []
        for index, pitch in enumerate(pitches):
            start = 0.2 + index * 0.35
            offset = round(start * RATE)
            lead = tone(pitch, 0.32, 0.14, (0.3, 1, 0.6, 0.3))
            bass = tone((36, 41, 43, 38)[index // 2], 0.35, 0.4, (1, 0.6, 0.3, 0.2))
            signal[offset:offset + len(lead)] += lead
            signal[offset:offset + len(bass)] += bass
            truth.append((start, 0.32, pitch))
        score = transcribe_audio(self.write(signal))
        self.assertGreaterEqual(accuracy(score, truth), 0.9)
        self.assertIn(81, [n.pitch for n in score.notes])

    def test_vibrato_and_brief_competitor_do_not_change_pitch(self):
        t = np.arange(round(2.4 * RATE)) / RATE
        frequency = 440 * 2 ** (0.18 * np.sin(2 * np.pi * 5 * t) / 12)
        phase = 2 * np.pi * np.cumsum(frequency) / RATE
        signal = 0.12 * (np.sin(phase) + 0.4 * np.sin(2 * phase))
        signal += 0.35 * np.sin(2 * np.pi * 110 * t)
        competitor = tone(81, 0.035, 0.5)
        signal[RATE:RATE + len(competitor)] += competitor
        score = transcribe_audio(self.write(signal))
        self.assertEqual([n.pitch for n in score.notes], [69])
        self.assertGreater(score.notes[0].duration, 2.3)

    def test_repeated_attacks_without_silent_gaps(self):
        # A nonzero sustain floor means simple silence/pitch-change splitting
        # cannot recover these repetitions; bass continues through each attack.
        t = np.arange(round(2.4 * RATE)) / RATE
        phase = np.mod(t, 0.4)
        envelope = 0.18 + 0.82 * np.minimum(phase / 0.01, 1) * np.exp(-phase / 0.09)
        lead = 0.18 * envelope * (np.sin(2 * np.pi * 440 * t) + 0.35 * np.sin(2 * np.pi * 880 * t))
        bass = 0.35 * np.sin(2 * np.pi * 110 * t)
        score = transcribe_audio(self.write(lead + bass))
        self.assertEqual([n.pitch for n in score.notes], [69] * 6)
        np.testing.assert_allclose([n.start for n in score.notes], np.arange(6) * 0.4, atol=0.065)

    def test_held_note_not_split_by_bass_and_noise_attacks(self):
        length = 2.4
        signal = tone(72, length, 0.14)
        rng = np.random.default_rng(23)
        for start in np.arange(0.15, 2.1, 0.3):
            bass = tone(40, 0.22, 0.3)
            offset = round(start * RATE)
            signal[offset:offset + len(bass)] += bass
            hit = rng.normal(0, 0.1, round(0.025 * RATE)) * np.exp(-np.arange(round(0.025 * RATE)) / 100)
            signal[offset:offset + len(hit)] += hit
        score = transcribe_audio(self.write(signal))
        self.assertEqual([n.pitch for n in score.notes], [72])

    def test_silence_and_noise_rejected(self):
        for data in (np.zeros(RATE), np.random.default_rng(7).normal(0, 0.025, RATE)):
            with self.subTest(noise=bool(data.any())), self.assertRaises(ScoreError):
                transcribe_audio(self.write(data))

    def test_bass_alone_is_not_a_high_melody(self):
        for pitch in (36, 40, 43, 45):
            with self.subTest(pitch=pitch), self.assertRaises(ScoreError):
                transcribe_audio(self.write(tone(pitch, 0.8, 0.3, (1, 0.55, 0.3, 0.18, 0.12))))

    def test_melody_rest_over_continuous_bass(self):
        signal = tone(40, 2.4, 0.3, (1, 0.55, 0.3, 0.18, 0.12))
        for start in (0.3, 1.5):
            offset = round(start * RATE)
            lead = tone(67, 0.5)
            signal[offset:offset + len(lead)] += lead
        score = transcribe_audio(self.write(signal))
        self.assertEqual([n.pitch for n in score.notes], [67, 67])
        np.testing.assert_allclose([n.start for n in score.notes], [0.3, 1.5], atol=0.07)
        self.assertFalse(any(n.start <= 1.1 < n.start + n.duration for n in score.notes))

    def test_rests_resampling_and_inverted_stereo(self):
        rate = 48000
        data = np.r_[np.zeros(rate // 5), tone(67, 0.4, rate=rate),
                     np.zeros(rate // 2), tone(67, 0.4, rate=rate), np.zeros(rate // 5)]
        score = transcribe_audio(self.write(np.column_stack((data, -data)), rate))
        self.assertEqual([n.pitch for n in score.notes], [67, 67])
        np.testing.assert_allclose([n.start for n in score.notes], [0.2, 1.1], atol=0.06)
        np.testing.assert_allclose([n.duration for n in score.notes], [0.4, 0.4], atol=0.07)
        self.assertLessEqual(score.notes[-1].start + score.notes[-1].duration, score.duration)

    def test_yin_solo_and_calibration_compatibility(self):
        data = tone(45, 0.6, partials=(1,))
        score = transcribe_audio(self.write(data), engine="yin")
        self.assertEqual([n.pitch for n in score.notes], [45])
        self.assertEqual(measure_note(data, RATE, "J").pitch, 45)
        frequency, confidence = estimate_pitch(data[2048:4096])
        self.assertAlmostEqual(frequency, 110, delta=0.5)
        self.assertGreater(confidence, 0.95)

    def test_progress_and_cancellation_during_each_phase(self):
        self.write(mixture()[0])
        for engine in ("melody", "yin"):
            values = []
            transcribe_audio(self.path, progress=values.append, engine=engine)
            self.assertEqual(values[0], 0)
            self.assertEqual(values[-1], 1)
            self.assertTrue(all(a <= b for a, b in zip(values, values[1:])))
            for limit in (0, 0.05, 0.15, 0.8):
                stop = threading.Event()

                def progress(value):
                    if value >= limit:
                        stop.set()

                with self.subTest(engine=engine, phase=limit), self.assertRaises(AudioCancelled):
                    transcribe_audio(self.path, stop, progress, engine=engine)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(AudioCancelled):
            transcribe_audio(self.path, stop)

    def test_limits_and_invalid_engine(self):
        with self.assertRaises(ValueError):
            transcribe_audio(self.path, engine="neural")
        for duration, channels in ((300.01, 2), (0.099, 1), (1, 9)):
            with self.subTest(duration=duration, channels=channels):
                with patch("music_audio.sf.info") as info, patch("music_audio.sf.SoundFile") as source:
                    info.return_value.duration = duration
                    info.return_value.channels = channels
                    with self.assertRaises(ScoreError):
                        transcribe_audio(self.path)
                    source.assert_not_called()

    @unittest.skipUnless(os.environ.get("MUSIC_TRANSCRIPTION_AUDIO"), "optional real-audio statistics")
    def test_real_audio_statistics(self):
        path = Path(os.environ["MUSIC_TRANSCRIPTION_AUDIO"])
        for engine in ("yin", "melody"):
            started = time.perf_counter()
            values = []
            score = transcribe_audio(path, progress=values.append, engine=engine)
            result = dict(file=str(path), engine=engine, seconds=time.perf_counter() - started,
                          **statistics(score))
            print(json.dumps(result, ensure_ascii=False))
            self.assertEqual(values[-1], 1)
            self.assertTrue(all(a <= b for a, b in zip(values, values[1:])))
            self.assertTrue(all(0 <= n.start < n.start + n.duration <= score.duration for n in score.notes))


if __name__ == "__main__":
    unittest.main()
