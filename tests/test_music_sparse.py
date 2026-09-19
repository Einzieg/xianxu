import tempfile
import threading
import unittest
from pathlib import Path

import mido

from music_adapt import adapt_score
from music_library import CALIBRATED_MAPPING as MAPPING
from music_score import Note, Score, ScoreError, compile_score
from music_sparse import BeatClock, midi_tempo_map


class SparseBassTests(unittest.TestCase):
    def arrange(self, lead, backing, *, track=1, tempo_map=None):
        source = Score("Source", tuple(lead) + tuple(backing), 5)
        config = {"version": 1, "backing_track": track,
                  "tempo_map": tempo_map or [[0, 0, 1000000]]}
        return adapt_score(source, MAPPING, texture="melody_bass", melody_track=0, sparse_config=config)

    def test_locks_lead_and_keeps_only_one_bottom_note_per_beat(self):
        lead = tuple(Note(t, .9, 64) for t in range(4))
        backing = tuple(Note(t / 4, .4, p, 1) for t in range(16) for p in (57, 60))
        result = self.arrange(lead, backing)
        self.assertEqual(tuple(n for n in result.score.notes if n.track == 0), lead)
        bass = [n for n in result.score.notes if n.track == 1]
        self.assertEqual([n.start for n in bass], [0, 1, 2, 3])
        self.assertEqual({n.pitch for n in bass}, {45})
        self.assertTrue(all(n.pitch == 57 for n, a in result.pairs if a.track == 1))
        self.assertIn("稀疏单低音", result.summary)
        for speed in (.5, 1, 2):
            for hold in (10, 60, 150):
                solo = compile_score(Score("Lead", lead, 5), MAPPING, speed=speed, hold_ms=hold)
                full = compile_score(result.score, MAPPING, speed=speed, hold_ms=hold)
                self.assertTrue(set(solo.events) <= set(full.events))

    def test_unplayable_root_is_not_replaced_with_inner_voice(self):
        result = self.arrange((Note(0, 1, 64),), (Note(0, .4, 48, 1), Note(0, .4, 57, 1)))
        self.assertFalse(any(n.track == 1 for n in result.score.notes))

    def test_tritone_and_bass_during_lead_rest_are_omitted(self):
        result = self.arrange((Note(0, .9, 59),), (Note(0, .5, 53, 1), Note(2, .5, 57, 1)))
        self.assertFalse(any(n.track == 1 for n in result.score.notes))

    def test_conflict_only_shortens_bass_not_melody(self):
        lead = (Note(0, .2, 60), Note(.2, .8, 59))
        result = self.arrange(lead, (Note(0, .8, 53, 1),))
        self.assertEqual(tuple(n for n in result.score.notes if n.track == 0), lead)
        self.assertEqual([n for n in result.score.notes if n.track == 1], [Note(0, .2, 53, 1)])

    def test_mixed_track_never_duplicates_lead_as_bass(self):
        lead = (Note(0, .8, 64), Note(1, .8, 60), Note(2, .8, 64))
        backing = (Note(0, .5, 57), Note(1, .5, 57), Note(2, .5, 57))
        result = self.arrange(lead, backing, track=0)
        self.assertEqual([n.start for n in result.score.notes if n.track == 1], [0, 2])
        solo = self.arrange(lead, (), track=0)
        self.assertFalse(any(n.track == 1 for n in solo.score.notes))

    def test_tempo_changes_control_density_and_gate_length(self):
        lead = (Note(0, 4, 64),)
        backing = tuple(Note(t / 4, 1, 57, 1) for t in range(12))
        result = self.arrange(lead, backing, tempo_map=[[0, 0, 500000], [2, 1, 1000000]])
        bass = [n for n in result.score.notes if n.track == 1]
        self.assertEqual([n.start for n in bass], [0, .5, 1, 2])
        self.assertEqual([n.duration for n in bass], [.25, .25, .5, .5])

    def test_tempo_map_import_preserves_default_and_changes(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        midi.tracks.append(mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=1000000, time=960)]))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tempo.mid"
            midi.save(path)
            rows = midi_tempo_map(path)
        self.assertEqual(rows, [[0, 0, 500000], [2, 1, 1000000]])
        clock = BeatClock(rows)
        for time in (0, .5, 1, 1.5, 2):
            self.assertAlmostEqual(clock.to_time(clock.to_beat(time)), time)

    def test_invalid_config_and_cancel_are_rejected(self):
        for rows in (None, [], [[1, 0, 1]], [[0, 0, 0]], [[0, 0, float("nan")]],
                     [[0, 0, 500000], [2, 2, 500000]]):
            with self.assertRaises(ScoreError):
                BeatClock(rows)
        source = Score("Lead", (Note(0, 1, 64),), 1)
        with self.assertRaises(ScoreError):
            adapt_score(source, MAPPING, texture="melody_bass")
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(ScoreError, "取消"):
            adapt_score(source, MAPPING, texture="melody_bass", stop=stop)


if __name__ == "__main__":
    unittest.main()
