from copy import deepcopy
from dataclasses import replace
import unittest

from music_contour import contour_errors, fit_contour, rebuild_library
from music_library import CALIBRATED_MAPPING, encode_score
from music_score import Note, Score


def pairs(source, old):
    return tuple((Note(i * .2, .15, p), Note(i * .2, .15, q))
                 for i, (p, q) in enumerate(zip(source, old)))


class ContourTests(unittest.TestCase):
    def test_peak_and_repeated_descent(self):
        original = pairs([73, 82, 77, 77, 75, 73, 70], [60, 57, 64, 64, 62, 60, 57])
        fitted = fit_contour(original, CALIBRATED_MAPPING, -13)
        self.assertEqual([n.pitch for _, n in fitted], [60, 64, 62, 62, 60, 59, 57])
        self.assertEqual([a for a, _ in fitted], [a for a, _ in original])
        self.assertEqual([(n.start, n.duration) for _, n in fitted],
                         [(n.start, n.duration) for _, n in original])

    def test_exact_line_unchanged(self):
        original = pairs([52, 55, 57, 57, 55, 52], [52, 55, 57, 57, 55, 52])
        self.assertEqual(fit_contour(original, CALIBRATED_MAPPING, 0), original)

    def test_long_runs_compress_without_reversal(self):
        for pitches in (list(range(60, 80)), list(range(80, 60, -1))):
            fitted = fit_contour(pairs(pitches, [60] * 20), CALIBRATED_MAPPING, -12)
            self.assertEqual(contour_errors(fitted)["reversals"], 0)
            self.assertGreater(contour_errors(fitted)["plateaus"], 0)
            self.assertTrue(all(n.pitch in CALIBRATED_MAPPING for _, n in fitted))

    def test_empty_and_single(self):
        self.assertEqual(fit_contour([], CALIBRATED_MAPPING, 0), ())
        one = pairs([72], [60])
        self.assertEqual(fit_contour(one, CALIBRATED_MAPPING, -12), one)

    def test_rest_allows_register_reset(self):
        original = ((Note(0, .1, 70), Note(0, .1, 64)),
                    (Note(3, .1, 72), Note(3, .1, 52)))
        fitted = fit_contour(original, CALIBRATED_MAPPING, -12)
        self.assertGreater(fitted[0][1].pitch, fitted[1][1].pitch)

    def test_rebuild_keeps_dense_attacks_and_metadata(self):
        source = Score("Synthetic", tuple(Note(i * .06, .04, p) for i, p in
                       enumerate([73, 82, 77, 77, 75, 73, 70])), 1, ((0, "Lead"),))
        old = replace(source, notes=tuple(replace(n, pitch=57) for n in source.notes))
        item = {"id": "synthetic", "title": "Synthetic", "source": "not-required.mid",
                "original": encode_score(source), "score": encode_score(old), "texture": "chords",
                "note_count": 7, "summary": "Original", "custom": {"preserve": True}}
        library = {"version": 1, "items": [item], "mode": "random", "current_id": "synthetic",
                   "settings": {"mapping": [{"pitch": p, "key": k} for p, k in CALIBRATED_MAPPING.items()]}}
        untouched = deepcopy(library)
        candidate, report = rebuild_library(library)
        self.assertEqual(library, untouched)
        self.assertEqual(candidate["items"][0]["note_count"], 7)
        self.assertEqual(candidate["items"][0]["original"], item["original"])
        self.assertEqual(candidate["items"][0]["custom"], item["custom"])
        self.assertEqual(candidate["settings"], library["settings"])
        self.assertEqual(report[0]["after"]["reversals"], 0)
        item["original"] = encode_score(old)
        candidate, report = rebuild_library(library)
        self.assertEqual(candidate, library)
        self.assertEqual(report[0]["status"], "preserved_already_nine_key")
        item["original"] = encode_score(replace(source, notes=source.notes + (Note(0, .1, 60),)))
        candidate, report = rebuild_library(library)
        self.assertEqual(candidate, library)
        self.assertEqual(report[0]["status"], "preserved_unverified_polyphony")


if __name__ == "__main__":
    unittest.main()
