"""Audio A/B previews must be bounded, aligned, and independent of DD."""

import base64
import io
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from music_audition import RATE, audition, clip_range, source_clip, synthesize
from music_library import encode_score
from music_score import Note, Score, ScoreError


class AuditionTests(unittest.TestCase):
    def test_range_rejects_invalid_or_oversized_requests(self):
        for start, duration in ((-1, 10), (10, 1), (float("nan"), 1), (0, 31), (0, float("inf")), (0, 0)):
            with self.assertRaises(ScoreError):
                clip_range(start, duration, 10)
        self.assertEqual(clip_range(9, 15, 10), (9, 1))

    def test_synth_preserves_pitch_and_rest(self):
        score = Score("fixture", (Note(0.2, 0.4, 69),), 1)
        data = synthesize(score, 0, 1)
        self.assertTrue(np.all(data[:round(0.2 * RATE)] == 0))
        self.assertTrue(np.all(data[round(0.6 * RATE):] == 0))
        window = data[round(0.25 * RATE):round(0.5 * RATE)]
        spectrum = abs(np.fft.rfft(window * np.hanning(len(window))))
        frequency = np.argmax(spectrum) * RATE / len(window)
        self.assertAlmostEqual(frequency, 440, delta=5)

    def test_segment_clips_sustained_note_without_moving_time(self):
        score = Score("fixture", (Note(0, 1, 60),), 1.2)
        whole = synthesize(score, 0, 1.2)
        segment = synthesize(score, 0.5, 0.5)
        np.testing.assert_allclose(segment, whole[round(0.5 * RATE):round(RATE)], atol=1e-5)

    def test_polyphonic_preview_cannot_clip(self):
        score = Score("fixture", tuple(Note(0, 0.2, 60 + index) for index in range(24)), 0.5)
        data = synthesize(score, 0, 0.5)
        self.assertLessEqual(float(np.max(abs(data))), 0.851)
        self.assertTrue(np.isfinite(data).all())

    def test_source_retains_stereo_and_requested_segment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.wav"
            t = np.arange(48000) / 48000
            tone = np.sin(2 * np.pi * 440 * t).astype(np.float32) * 0.3
            sf.write(path, np.column_stack([tone, -tone]), 48000)
            data, start = source_clip(path, 0.25, 0.5)
            self.assertEqual(start, 0.25)
            self.assertEqual(data.shape, (round(0.5 * RATE), 2))
            self.assertGreater(float(np.max(abs(data))), 0.25)
            np.testing.assert_allclose(data[:, 0], -data[:, 1], atol=0.001)

    def test_rpc_payload_decodes_as_wav_with_consistent_timing(self):
        score = Score("fixture", (Note(0, 0.3, 60),), 0.4)
        item = {"duration": score.duration, "source": "missing.mid",
                "original": encode_score(score), "score": encode_score(score)}
        for kind in ("original", "score"):
            result = audition(item, kind)
            payload = base64.b64decode(result["data_url"].split(",", 1)[1])
            data, rate = sf.read(io.BytesIO(payload))
            self.assertEqual(rate, RATE)
            self.assertAlmostEqual(len(data) / rate, result["duration"])
            self.assertIn("合成音色", result["label"])
        with self.assertRaises(ScoreError):
            audition(item, "source")
        with self.assertRaises(ScoreError):
            audition(item, "invalid")

    def test_tiny_remaining_tail_is_valid_after_clipping(self):
        score = Score("tail", (Note(0, 0.2, 60),), 0.2)
        item = {"duration": score.duration, "original": encode_score(score)}
        self.assertAlmostEqual(audition(item, "original", 0.18, 15)["duration"], 0.02, places=4)


if __name__ == "__main__":
    unittest.main()
