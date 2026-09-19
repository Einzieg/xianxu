"""Conservative bass under a locked lead; tempo data stays with the library item."""

from bisect import bisect_right
from itertools import groupby
import math

import mido

from music_score import Note, ScoreError


CONSONANT = {0, 3, 4, 5, 7, 8, 9}


def midi_tempo_map(path):
    midi = mido.MidiFile(path)
    if midi.type == 2 or midi.ticks_per_beat <= 0:
        raise ScoreError("稀疏伴奏需要同步、PPQ计时的MIDI")
    events = []
    for track_id, track in enumerate(midi.tracks):
        tick = 0
        for order, message in enumerate(track):
            tick += message.time
            if message.type == "set_tempo":
                events.append((tick, track_id, order, message.tempo))
    changes = {0: 500000}
    for tick, _, _, tempo in sorted(events):
        changes[tick] = tempo
    segments, seconds, previous, tempo = [], 0.0, 0, 500000
    for tick, value in sorted(changes.items()):
        seconds += mido.tick2second(tick - previous, midi.ticks_per_beat, tempo)
        segments.append([tick / midi.ticks_per_beat, seconds, value])
        previous, tempo = tick, value
    return segments


class BeatClock:
    def __init__(self, segments):
        try:
            rows = [tuple(row) for row in segments]
            if not rows or any(len(row) != 3 for row in rows):
                raise ValueError
            if any(not all(math.isfinite(v) for v in row) or row[0] < 0 or row[1] < 0
                   or row[2] <= 0 for row in rows) or rows[0][:2] != (0, 0):
                raise ValueError
            for a, b in zip(rows, rows[1:]):
                if b[0] <= a[0] or abs(b[1] - a[1] - (b[0] - a[0]) * a[2] / 1_000_000) > 1e-6:
                    raise ValueError
        except (ValueError, TypeError, OverflowError) as exc:
            raise ScoreError("稀疏伴奏的速度表无效") from exc
        self.rows = rows
        self.beats = [row[0] for row in rows]
        self.times = [row[1] for row in rows]

    def to_beat(self, time):
        beat, start, tempo = self.rows[max(0, bisect_right(self.times, time + 1e-9) - 1)]
        return beat + (time - start) * 1_000_000 / tempo

    def to_time(self, beat):
        start, time, tempo = self.rows[max(0, bisect_right(self.beats, beat + 1e-9) - 1)]
        return time + (beat - start) * tempo / 1_000_000


def sparse_backing(source, lead, mapping, config, stop):
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ScoreError("缺少稀疏伴奏配置或版本不支持")
    clock = BeatClock(config.get("tempo_map"))
    track = config.get("backing_track")
    if type(track) is not int or track not in {n.track for n in source.notes}:
        raise ScoreError("稀疏伴奏音轨不存在")
    locked = lead.score.notes
    starts = [n.start for n in locked]
    identities = {(n.start, n.pitch, n.track) for n, _ in lead.pairs}
    low_limit = sorted(mapping)[len(mapping) // 2]
    pitch_classes = {}
    for pitch in sorted(mapping):
        if pitch <= low_limit:
            pitch_classes.setdefault(pitch % 12, pitch)
    backing = sorted((n for n in source.notes if n.track == track),
                     key=lambda n: (n.start, n.pitch, -n.duration))
    pairs = []
    last_beat, last_end = -float("inf"), -float("inf")
    for start, group in groupby(backing, key=lambda n: n.start):
        if stop.is_set():
            raise ScoreError("已取消乐器适配")
        # Do not substitute an inner voice when the actual bottom note is unavailable.
        note = next(group)
        if (note.start, note.pitch, note.track) in identities:
            continue
        beat = clock.to_beat(start)
        if beat - last_beat < 1 - 1e-7 or start < last_end - 1e-8:
            continue
        pitch = pitch_classes.get((note.pitch + lead.shift) % 12)
        if pitch is None:
            continue
        cursor = max(0, bisect_right(starts, start + 1e-9) - 1)
        current = locked[cursor]
        if current.start > start + 1e-8 or current.start + current.duration <= start + 1e-8:
            continue
        if track == lead.selected_track and lead.pairs[cursor][0].pitch - note.pitch < 5:
            continue
        finish = min(start + note.duration, clock.to_time(beat + .5))
        while cursor < len(locked) and locked[cursor].start < finish - 1e-8:
            current = locked[cursor]
            if current.start + current.duration > start + 1e-8:
                interval = current.pitch - pitch
                if interval < 5 or interval % 12 not in CONSONANT:
                    finish = min(finish, current.start)
                    break
            cursor += 1
        if finish - start < .08:
            continue
        fitted = Note(start, finish - start, pitch, 1)
        pairs.append((note, fitted))
        last_beat, last_end = beat, finish
    return pairs
