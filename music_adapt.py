"""Arrange a lead or opt-in polyphony for a sparse keyboard, preserving onsets."""

from dataclasses import dataclass
from bisect import bisect_right
from itertools import groupby
import math
import threading

import numpy as np
from scipy.optimize import linear_sum_assignment

from music_score import Note, Score, ScoreError, validate_mapping

POLICIES = ("保留旋律（推荐）", "尽量保留原调", "简化演奏")


@dataclass(frozen=True)
class Adaptation:
    source: Score
    score: Score
    shift: int
    folded: int
    approximated: int
    removed: int
    track_name: str
    selected_track: int
    pairs: tuple[tuple[Note, Note], ...]
    texture: str = "melody"

    @property
    def summary(self):
        if self.texture in ("melody_chords", "melody_bass"):
            lead_count = sum(n.track == 0 for n in self.score.notes)
            backing = "稀疏单低音" if self.texture == "melody_bass" else "轻伴奏"
            return (f"旋律优先 · 锁定主旋律 {lead_count} 音 · {backing} {len(self.score.notes) - lead_count} 音 · "
                    f"全局移调 {self.shift:+d} 半音 · 八度折叠 {self.folded} 音 · "
                    f"旋律替代 {self.approximated} 音 · 删减伴奏/过密音 {self.removed} 音")
        if self.texture == "chords":
            return (f"保留和弦 · 全局移调 {self.shift:+d} 半音 · 八度折叠 {self.folded} 音 · "
                    f"替代 {self.approximated} 音 · 同键合并/超限删减 {self.removed} 音 · 输出 {len(self.score.notes)} 音")
        return (f"全局移调 {self.shift:+d} 半音 · 八度折叠 {self.folded} 音 · 替代 {self.approximated} 音 · "
                f"去除伴奏/过密音符 {self.removed} 音 · 输出 {len(self.score.notes)} 音")


def _check_cancel(stop):
    if stop.is_set():
        raise ScoreError("已取消乐器适配")


def _melody(score, track, min_gap):
    available = sorted({note.track for note in score.notes})
    names = dict(score.tracks)
    if track is not None and track not in available:
        raise ScoreError("选中的音轨没有音符")
    if track is None:
        def rank(track_id):
            notes = sorted((note for note in score.notes if note.track == track_id), key=lambda note: note.start)
            name = names.get(track_id, "").lower()
            lead = any(word in name for word in ("melody", "lead", "vocal", "旋律", "主唱", "人声"))
            bass = any(word in name for word in ("bass", "drum", "低音", "鼓"))
            overlaps = sum(a.start + a.duration > b.start + 0.03 for a, b in zip(notes, notes[1:]))
            coverage = sum(min(note.duration, 0.5) for note in notes) / max(score.duration, 0.1)
            return 3 * lead - 3 * bass - overlaps / len(notes) + min(coverage, 1) + np.median([n.pitch for n in notes]) / 128
        track = max(available, key=rank)
    selected = sorted((note for note in score.notes if note.track == track), key=lambda note: (note.start, -note.pitch))
    melody = []
    for note in selected:
        if melody and note.start - melody[-1].start < min_gap - 1e-8:
            continue
        # Adjacent triplets can overlap by floating-point roundoff, not a real voice.
        if melody and note.start < melody[-1].start + melody[-1].duration - 1e-8:
            previous = melody[-1]
            if note.pitch < previous.pitch:
                continue
            melody[-1] = Note(previous.start, note.start - previous.start, previous.pitch, previous.track)
        melody.append(note)
    return melody, names.get(track, f"音轨 {track + 1}"), track


def _distance(pitches, targets, shift, faithful):
    distance = np.abs(pitches[:, None] + shift - targets[None, :])
    chromatic = distance % 12
    chromatic = np.minimum(chromatic, 12 - chromatic)
    return chromatic * (6 if faithful else 4) + distance * (0.22 if faithful else 0.12)


def _fit_path(notes, targets, shift, faithful, simple, stop):
    pitches = np.array([note.pitch for note in notes])
    weights = np.array([0.5 + min(note.duration, 1) for note in notes])
    local = _distance(pitches, targets, shift, faithful) * weights[:, None]
    costs = local[0]
    history = np.zeros((len(notes), len(targets)), dtype=np.int16)
    intervals = targets[None, :] - targets[:, None]
    for index in range(1, len(notes)):
        if index % 128 == 0:
            _check_cancel(stop)
        desired = pitches[index] - pitches[index - 1]
        transition = np.abs(intervals - desired) * (0.10 if faithful else 0.35)
        if desired:
            transition += ((intervals * desired) < 0) * (1 if faithful else 3)
        transition += np.maximum(0, np.abs(intervals) - max(7, abs(desired))) * (0.8 if simple else 0.4)
        if notes[index].start - (notes[index - 1].start + notes[index - 1].duration) > 0.7:
            transition *= 0.1
        if desired == 0:
            # Rearticulation must not invent a pitch change just to ease the next leap.
            transition[intervals != 0] = np.inf
        candidates = costs[:, None] + transition
        history[index] = np.argmin(candidates, axis=0)
        costs = candidates[history[index], np.arange(len(targets))] + local[index]
    cursor = int(np.argmin(costs))
    score = float(costs[cursor])
    fitted = [int(targets[cursor])]
    for index in range(len(notes) - 1, 0, -1):
        cursor = int(history[index, cursor])
        fitted.append(int(targets[cursor]))
    return score, list(reversed(fitted))


def _adapt_chords(score, mapping, faithful, track, stop):
    selected = [n for n in score.notes if track is None or n.track == track]
    if not selected:
        raise ScoreError("选中的音轨没有音符")
    # Exact unisons across tracks are one physical key, not extra attacks.
    unique = {}
    for index, note in enumerate(selected):
        if index % 128 == 0:
            _check_cancel(stop)
        key = (note.start, note.pitch)
        if key not in unique or note.duration > unique[key].duration:
            unique[key] = note
    notes = sorted(unique.values(), key=lambda n: (n.start, n.pitch))
    targets = np.array(sorted(mapping))
    groups = []
    for start, attacks in groupby(notes, key=lambda n: n.start):
        attacks = list(attacks)
        # Keep model onset jitter, but assign near-simultaneous distinct pitches
        # jointly. A repeated pitch starts a new group instead of being merged.
        if (not groups or start - groups[-1][0].start > 0.04
                or {n.pitch for n in attacks}.intersection(n.pitch for n in groups[-1])):
            groups.append([])
        groups[-1].extend(attacks)
    if all(n.pitch in mapping for n in notes):
        shift, pairs = 0, [(n, n.pitch) for n in notes]
    else:
        histogram = np.zeros(128)
        for n in notes:
            histogram[n.pitch] += 0.5 + min(n.duration, 1)
        pitches = np.flatnonzero(histogram)
        weights = histogram[pitches]
        ranked = []
        for shift in range(max(-127, int(targets[0]) - int(pitches.max()) - 12),
                           min(127, int(targets[-1]) - int(pitches.min()) + 12) + 1):
            _check_cancel(stop)
            if faithful and shift % 12:
                continue
            cost = float(_distance(pitches, targets, shift, faithful).min(axis=1) @ weights)
            ranked.append((cost + abs(shift) * 0.005, shift))
        best = None
        for _, shift in sorted(ranked)[:6]:
            cost, pairs = abs(shift) * 0.005, []
            for group in groups:
                _check_cancel(stop)
                weights = np.array([0.5 + min(n.duration, 1) for n in group])
                distance = _distance(np.array([n.pitch for n in group]), targets, shift, faithful)
                # Rectangular assignment retains distinct keys; excess voices
                # pay a duration-weighted omission cost instead of all collapsing.
                costs = (distance - 100) * weights[:, None]
                rows, columns = linear_sum_assignment(costs)
                cost += float(costs[rows, columns].sum() + 100 * weights.sum())
                pairs.extend((group[row], int(targets[column])) for row, column in zip(rows, columns))
            candidate = (cost, abs(shift), shift, pairs)
            if best is None or candidate[:3] < best[:3]:
                best = candidate
        _, _, shift, pairs = best
    pairs.sort(key=lambda pair: (pair[0].start, pair[1]))
    adapted = [Note(n.start, n.duration, pitch) for n, pitch in pairs]
    previous = {}
    for index, note in enumerate(adapted):
        if index % 128 == 0:
            _check_cancel(stop)
        prior = previous.get(note.pitch)
        if prior is not None:
            left = adapted[prior]
            if left.start + left.duration > note.start:
                # A retrigger replaces the same key's tail, never another voice.
                adapted[prior] = Note(left.start, note.start - left.start, left.pitch)
        previous[note.pitch] = index
    folded = sum(p != n.pitch + shift and (p - n.pitch - shift) % 12 == 0 for n, p in pairs)
    approximated = sum((p - n.pitch - shift) % 12 != 0 for n, p in pairs)
    result = Score(score.title + " · 和弦适配", tuple(adapted), score.duration, ((0, "适配和弦"),))
    _check_cancel(stop)
    return Adaptation(score, result, shift, folded, approximated, len(score.notes) - len(adapted),
                      dict(score.tracks).get(track, "全部音轨"), track if track is not None else -1,
                      tuple((n, a) for (n, _), a in zip(pairs, adapted)), "chords")


def _adapt_melody_chords(score, mapping, policy, melody_track, stop):
    # Freeze the solo result first: accompaniment never votes on its key or pitch.
    lead = adapt_score(score, mapping, policy, track=melody_track, stop=stop)
    locked = lead.score.notes
    starts = [n.start for n in locked]
    reserved = {pitch: [n for n in locked if n.pitch == pitch] for pitch in mapping}
    ends = {pitch: [n.start + n.duration for n in notes] for pitch, notes in reserved.items()}
    identities = {(n.start, n.pitch, n.track) for n, _ in lead.pairs}
    pairs = list(lead.pairs)
    active = {}
    for index, note in enumerate(sorted(score.notes, key=lambda n: (n.start, n.pitch, -n.duration, n.track))):
        if index % 128 == 0:
            _check_cancel(stop)
        if (note.start, note.pitch, note.track) in identities:
            continue
        active = {pitch: end for pitch, end in active.items() if end > note.start + 1e-8}
        if len(active) >= 2:
            continue
        finish = note.start + note.duration
        ceiling = max(mapping) + 1
        cursor = max(0, bisect_right(starts, note.start) - 1)
        while cursor < len(locked) and locked[cursor].start < finish - 1e-8:
            current = locked[cursor]
            if current.start + current.duration > note.start + 1e-8:
                ceiling = min(ceiling, current.pitch)
            cursor += 1
        candidates = []
        for pitch in mapping:
            if pitch >= ceiling or pitch in active or (pitch - note.pitch - lead.shift) % 12:
                continue
            duration = note.duration
            next_lead = bisect_right(ends[pitch], note.start + 1e-8)
            if next_lead < len(reserved[pitch]):
                # A future melody attack may shorten backing, never the reverse.
                duration = min(duration, reserved[pitch][next_lead].start - note.start)
            if duration >= 0.02:
                candidates.append((abs(pitch - note.pitch - lead.shift), -duration, pitch))
        if not candidates:
            continue
        _, negative_duration, pitch = min(candidates)
        fitted = Note(note.start, -negative_duration, pitch, 1)
        pairs.append((note, fitted))
        active[pitch] = fitted.start + fitted.duration
    pairs.sort(key=lambda pair: (pair[1].start, pair[1].pitch, pair[1].track))
    folded = sum(a.pitch != n.pitch + lead.shift and (a.pitch - n.pitch - lead.shift) % 12 == 0 for n, a in pairs)
    approximated = sum((a.pitch - n.pitch - lead.shift) % 12 != 0 for n, a in pairs)
    result = Score(score.title + " · 旋律优先", tuple(a for _, a in pairs), score.duration,
                   ((0, "锁定主旋律"), (1, "简化伴奏")))
    _check_cancel(stop)
    return Adaptation(score, result, lead.shift, folded, approximated, len(score.notes) - len(pairs),
                      lead.track_name, lead.selected_track, tuple(pairs), "melody_chords")


def adapt_score(score, mapping, policy=POLICIES[0], track=None, stop=None, *, texture="melody", melody_track=None,
                sparse_config=None):
    mapping = validate_mapping(mapping)
    if policy not in POLICIES:
        raise ScoreError("未知适配策略")
    if texture not in ("melody", "chords", "melody_chords", "melody_bass"):
        raise ScoreError("未知声部模式")
    if not score.notes:
        raise ScoreError("乐谱中没有音符")
    if any(not math.isfinite(n.start) or not math.isfinite(n.duration) or n.start < 0 or n.duration <= 0
           or not isinstance(n.pitch, int) or not 0 <= n.pitch <= 127 for n in score.notes):
        raise ScoreError("乐谱包含无效音符")
    stop = stop or threading.Event()
    _check_cancel(stop)
    faithful, simple = policy == POLICIES[1], policy == POLICIES[2]
    if texture == "melody_bass":
        from music_sparse import sparse_backing
        lead = adapt_score(score, mapping, policy, track=melody_track if melody_track is not None else track,
                           stop=stop)
        pairs = list(lead.pairs) + sparse_backing(score, lead, mapping, sparse_config, stop)
        pairs.sort(key=lambda pair: (pair[1].start, pair[1].pitch, pair[1].track))
        folded = sum(a.pitch != n.pitch + lead.shift and (a.pitch - n.pitch - lead.shift) % 12 == 0
                     for n, a in pairs)
        result = Score(score.title + " · 旋律锁定疏低音", tuple(a for _, a in pairs), score.duration,
                       ((0, "锁定主旋律"), (1, "稀疏单低音")))
        return Adaptation(score, result, lead.shift, folded, lead.approximated, len(score.notes) - len(pairs),
                          lead.track_name, lead.selected_track, tuple(pairs), texture)
    if texture == "melody_chords":
        return _adapt_melody_chords(score, mapping, policy, melody_track if melody_track is not None else track, stop)
    if texture == "chords":
        return _adapt_chords(score, mapping, faithful, track, stop)
    notes, track_name, selected_track = _melody(score, track, 0.14 if simple else 0.08)
    targets = np.array(sorted(mapping))
    if all(note.pitch in mapping for note in notes):
        shift, fitted = 0, [note.pitch for note in notes]
    else:
        histogram = np.zeros(128)
        for note in notes:
            histogram[note.pitch] += 0.5 + min(note.duration, 1)
        pitches = np.flatnonzero(histogram)
        shifts = range(max(-127, int(targets[0]) - int(pitches[-1]) - 12),
                       min(127, int(targets[-1]) - int(pitches[0]) + 12) + 1)
        ranked = []
        for shift in shifts:
            if faithful and shift % 12:
                continue
            costs = _distance(pitches, targets, shift, faithful).min(axis=1)
            ranked.append((float(costs @ histogram[pitches]) + abs(shift) * 0.005, shift))
        # Evaluate melodic continuity for the best register/key candidates instead of rounding each note independently.
        best = None
        for _, shift in sorted(ranked)[:6]:
            _check_cancel(stop)
            cost, fitted = _fit_path(notes, targets, shift, faithful, simple, stop)
            candidate = (cost + abs(shift) * 0.005, abs(shift), shift, fitted)
            if best is None or candidate[:3] < best[:3]:
                best = candidate
        _, _, shift, fitted = best
    adapted = tuple(Note(note.start, note.duration, pitch) for note, pitch in zip(notes, fitted))
    folded = sum(pitch != note.pitch + shift and (pitch - note.pitch - shift) % 12 == 0 for note, pitch in zip(notes, fitted))
    approximated = sum((pitch - note.pitch - shift) % 12 != 0 for note, pitch in zip(notes, fitted))
    result = Score(score.title + " · 乐器适配", adapted, score.duration, ((0, "适配旋律"),))
    return Adaptation(score, result, shift, folded, approximated, len(score.notes) - len(adapted),
                      track_name, selected_track, tuple(zip(notes, adapted)))
