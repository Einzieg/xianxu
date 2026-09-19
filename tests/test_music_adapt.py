from fractions import Fraction
import threading
import unittest

import numpy as np

from music_adapt import POLICIES, _fit_path, adapt_score
from music_score import DEFAULT_MAPPING, Note, Score, ScoreError, compile_score, parse_text

MAPPING = {pitch - 12: key for pitch, key in DEFAULT_MAPPING.items()}


class AdaptationTests(unittest.TestCase):
    def test_melody_first_locks_standalone_lead_even_with_dense_accompaniment(self):
        lead = (Note(0, .3, 72, 0), Note(.4, .3, 71, 0), Note(.8, .8, 72, 0))
        backing = tuple(Note(start, .3, pitch, 1) for start in (0, .4, .8, 1.2)
                        for pitch in (36, 48, 53, 57, 60, 65, 69))
        source = Score("Lead priority", lead + backing, 2, ((0, "Lead"), (1, "Bass")))
        solo = adapt_score(source, MAPPING, track=0)
        result = adapt_score(source, MAPPING, texture="melody_chords", melody_track=0)
        self.assertEqual(result.shift, solo.shift)
        self.assertEqual(tuple(n for n in result.score.notes if n.track == 0), solo.score.notes)
        self.assertTrue(any(n.track == 1 for n in result.score.notes))
        self.assertIn("旋律优先", result.summary)
        combined_plan = compile_score(result.score, MAPPING)
        self.assertEqual(combined_plan.skipped, 0)
        self.assertTrue(set(compile_score(solo.score, MAPPING).events) <= set(combined_plan.events))
        backing = [n for n in result.score.notes if n.track == 1]
        for time in {n.start for n in backing}:
            self.assertLessEqual(sum(n.start <= time < n.start + n.duration for n in backing), 2)
        for note in result.score.notes:
            if note.track == 1:
                for locked in solo.score.notes:
                    if min(note.start + note.duration, locked.start + locked.duration) > max(note.start, locked.start) + 1e-8:
                        self.assertLess(note.pitch, locked.pitch)

    def test_melody_first_repeated_lead_is_not_changed_by_chord_voicing(self):
        notes = (Note(0, .3, 72, 0), Note(.4, .3, 71, 0), Note(.8, .3, 72, 0),
                 Note(0, .3, 48, 1), Note(.4, .3, 57, 1), Note(.8, .3, 67, 1))
        source = Score("Repeat", notes, 2, ((0, "Lead"), (1, "Accompaniment")))
        result = adapt_score(source, MAPPING, texture="melody_chords", melody_track=0)
        lead = [n for n in result.score.notes if n.track == 0]
        self.assertEqual(lead[0].pitch, lead[2].pitch)
        for before, after in result.pairs:
            if after.track == 1:
                self.assertEqual((after.pitch - before.pitch - result.shift) % 12, 0)

    def test_melody_first_never_retriggers_or_shortens_a_sustained_lead(self):
        source = Score("Hold", (Note(0, 1, 60, 0), Note(1, .5, 62, 0),
                                Note(.2, .3, 60, 1), Note(.4, .3, 72, 1), Note(.8, .8, 62, 1)),
                       1.6, ((0, "Melody"), (1, "Backing")))
        result = adapt_score(source, MAPPING, texture="melody_chords", melody_track=0)
        self.assertEqual([n for n in result.score.notes if n.track == 0],
                         [Note(0, 1, 60), Note(1, .5, 62)])
        self.assertFalse(any(n.track == 1 and n.pitch in (60, 62) for n in result.score.notes))

    def test_melody_first_rejects_missing_lead_track_and_keeps_input_immutable(self):
        source = parse_text("1 2 3")
        before = source.notes
        with self.assertRaisesRegex(ScoreError, "音轨"):
            adapt_score(source, MAPPING, texture="melody_chords", melody_track=42)
        result = adapt_score(source, MAPPING, texture="melody_chords")
        self.assertEqual(source.notes, before)
        self.assertEqual(result.score.duration, source.duration)
        self.assertTrue(all(n.duration > 0 for n in result.score.notes))

    def test_chord_mode_keeps_playable_chords_and_all_tracks(self):
        notes = (Note(0, .5, 52, 0), Note(0, .5, 55, 1), Note(0, .5, 59, 1),
                 Note(1, .3, 60, 0), Note(1, .3, 64, 1))
        source = Score("Chords", notes, 2)
        result = adapt_score(source, MAPPING, texture="chords")
        self.assertEqual([(n.start, n.duration, n.pitch) for n in result.score.notes],
                         [(n.start, n.duration, n.pitch) for n in notes])
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.shift, 0)
        downs = [e for e in compile_score(result.score, MAPPING).events if e.down and e.time == 0]
        self.assertEqual(len({e.key for e in downs}), 3)
        self.assertLess(len(adapt_score(source, MAPPING).score.notes), len(notes))

    def test_chord_transposition_assigns_distinct_keys_without_moving_attacks(self):
        notes = (Note(.2, .3, 72), Note(.21, .3, 76), Note(.22, .3, 79))
        result = adapt_score(Score("Jitter", notes, 1), MAPPING, texture="chords")
        self.assertEqual([n.start for n in result.score.notes], [.2, .21, .22])
        self.assertEqual([n.duration for n in result.score.notes], [.3] * 3)
        self.assertEqual(len({n.pitch for n in result.score.notes}), 3)
        self.assertEqual(compile_score(result.score, MAPPING).skipped, 0)

    def test_chord_unisons_merge_but_repeated_attacks_and_other_tails_remain(self):
        notes = (Note(0, 1, 52), Note(0, .5, 52, 1), Note(0, 1, 55), Note(.3, .5, 52))
        result = adapt_score(Score("Unison", notes, 1), MAPPING, texture="chords")
        self.assertEqual(result.score.notes, (Note(0, .3, 52), Note(0, 1, 55), Note(.3, .5, 52)))
        self.assertEqual(result.removed, 1)
        self.assertEqual([e.time for e in compile_score(result.score, MAPPING).events
                          if e.down and e.key == MAPPING[52]], [0, .3])

    def test_chord_over_capacity_is_bounded_and_reports_removed_voices(self):
        notes = tuple(Note(0, 1, pitch) for pitch in range(60, 72))
        result = adapt_score(Score("Dense", notes, 1), MAPPING, texture="chords")
        self.assertEqual(len(result.score.notes), 9)
        self.assertEqual(len({n.pitch for n in result.score.notes}), 9)
        self.assertEqual(result.removed, 3)

    def test_chord_single_key_retriggers_are_positive_and_cancellable(self):
        source = Score("One key", (Note(0, 1, 80), Note(.2, .5, 84), Note(.2, .5, 88)), 1)
        result = adapt_score(source, {45: "B"}, texture="chords")
        self.assertEqual(len(result.score.notes), 2)
        self.assertTrue(all(n.duration > 0 for n in result.score.notes))
        self.assertEqual([n.start for n in result.score.notes], [0, .2])
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(ScoreError, "取消"):
            adapt_score(source, MAPPING, stop=stop, texture="chords")
        with self.assertRaises(ScoreError):
            adapt_score(source, MAPPING, texture="unknown")

    def test_octave_transpose_preserves_phrase_and_rhythm(self):
        original = parse_text("5 5 2' 2' 3' 3' 2':2 0:2")
        result = adapt_score(original, MAPPING)
        self.assertEqual(result.shift, -12)
        self.assertEqual(result.approximated, 0)
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.folded, 0)
        self.assertEqual(result.score.duration, original.duration)
        for before, after in zip(original.notes, result.score.notes):
            self.assertEqual((before.start, before.duration), (after.start, after.duration))
            self.assertEqual(before.pitch - 12, after.pitch)
        self.assertEqual(compile_score(result.score, MAPPING).skipped, 0)

    def test_playable_melody_is_unchanged(self):
        original = parse_text("6, 3 4 5 6 7 1' 2' 3'", octave=3)
        result = adapt_score(original, MAPPING)
        self.assertEqual(result.score.notes, original.notes)
        self.assertEqual(result.shift, 0)

    def test_adjacent_triplets_do_not_lose_descending_notes_to_roundoff(self):
        pitches = (64, 62, 60, 59, 57, 55) * 6
        step = Fraction(5, 12)
        notes = tuple(Note(float(index * step), float(step), pitch)
                      for index, pitch in enumerate(pitches))
        self.assertTrue(any(a.start + a.duration > b.start for a, b in zip(notes, notes[1:])))
        source = Score("Exact triplets", notes, float(len(notes) * step))
        result = adapt_score(source, MAPPING)
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.score.notes, notes)

    def test_real_overlaps_still_select_the_melody(self):
        source = Score("Overlap", (Note(0, 1, 64), Note(.4, .2, 52), Note(.8, .3, 65)), 1.1)
        result = adapt_score(source, MAPPING)
        self.assertEqual(result.removed, 1)
        self.assertEqual([(n.start, n.duration) for n in result.score.notes], [(0, .8), (.8, .3)])

    def test_chromatic_missing_notes_are_arranged_not_skipped(self):
        notes = tuple(Note(index * 0.3, 0.2, pitch) for index, pitch in enumerate(range(60, 72)))
        original = Score("Chromatic", notes, 4)
        result = adapt_score(original, MAPPING)
        self.assertEqual(len(result.score.notes), len(notes))
        self.assertGreater(result.approximated, 0)
        self.assertEqual(compile_score(result.score, MAPPING).skipped, 0)
        self.assertEqual(original.notes, notes)

    def test_repeated_missing_pitch_cannot_turn_into_different_keys(self):
        # Castle's B5 repetitions used to become G3, G3, E4 at shift -17.
        pitches = (85, 85, 83, 83, 83, 81, 79, 81, 69, 74, 69)
        durations = (.383, .175, .302, .174, .116, .534, .557, 1.731, .477, .430, 1.091)
        starts = np.r_[0, np.cumsum(durations[:-1])]
        notes = tuple(Note(float(start), duration, pitch)
                      for start, duration, pitch in zip(starts, durations, pitches))
        for faithful in (False, True):
            for simple in (False, True):
                with self.subTest(faithful=faithful, simple=simple):
                    _, fitted = _fit_path(notes, np.array(sorted(MAPPING)), -17,
                                           faithful, simple, threading.Event())
                    self.assertEqual(len(set(fitted[2:5])), 1)
        source = Score("Repeated missing note", notes, sum(durations))
        for policy in POLICIES:
            result = adapt_score(source, MAPPING, policy)
            for (left, a), (right, b) in zip(result.pairs, result.pairs[1:]):
                if left.pitch == right.pitch:
                    self.assertEqual(a.pitch, b.pitch)
            self.assertEqual([(n.start, n.duration) for n in result.score.notes],
                             [(n.start, n.duration) for n, _ in result.pairs])

    def test_same_pitch_rearticulations_stay_separate_with_identical_keys(self):
        notes = tuple(Note(i * .3, .2, 83) for i in range(6))
        source = Score("Six repeated notes", notes, 2)
        result = adapt_score(source, MAPPING)
        self.assertEqual(len(result.score.notes), 6)
        self.assertEqual(len({n.pitch for n in result.score.notes}), 1)
        self.assertEqual([n.start for n in result.score.notes], [n.start for n in notes])

    def test_rising_phrase_keeps_direction(self):
        result = adapt_score(parse_text("1 2 3 4 5 6 7 1'"), MAPPING)
        pitches = [note.pitch for note in result.score.notes]
        self.assertEqual(pitches, sorted(pitches))

    def test_faithful_mode_only_transposes_octaves(self):
        result = adapt_score(parse_text("#1 #2 #4 #5 #6"), MAPPING, POLICIES[1])
        self.assertEqual(result.shift % 12, 0)
        self.assertEqual(compile_score(result.score, MAPPING).skipped, 0)

    def test_simplification_preserves_time_axis(self):
        notes = tuple(Note(index * 0.05, 0.04, 67 + index % 5) for index in range(40))
        source = Score("Fast", notes, 3)
        normal = adapt_score(source, MAPPING)
        simple = adapt_score(source, MAPPING, POLICIES[2])
        self.assertLess(len(simple.score.notes), len(normal.score.notes))
        self.assertTrue({note.start for note in simple.score.notes} <= {note.start for note in source.notes})
        self.assertEqual(simple.score.duration, 3)
        self.assertTrue(all(b.start - a.start >= 0.14 - 1e-8 for a, b in zip(simple.score.notes, simple.score.notes[1:])))

    def test_track_selection_and_explicit_override(self):
        score = Score("Song", (Note(0, 1, 36, 0), Note(1, 1, 40, 0),
                               Note(0, 0.4, 72, 1), Note(1, 0.4, 74, 1)), 2,
                      ((0, "Bass"), (1, "Lead melody")))
        self.assertEqual(adapt_score(score, MAPPING).track_name, "Lead melody")
        self.assertEqual(adapt_score(score, MAPPING, track=0).track_name, "Bass")

    def test_cancellation(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(ScoreError, "取消"):
            adapt_score(parse_text("1 2"), MAPPING, stop=stop)

    def test_single_key_and_extreme_register(self):
        result = adapt_score(Score("High", (Note(0, 1, 127),), 2), {45: "B"})
        self.assertEqual(result.score.notes[0].pitch, 45)
        self.assertEqual(result.score.duration, 2)


if __name__ == "__main__":
    unittest.main()
