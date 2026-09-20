"""Opt-in, offline contour repair. No driver initialization or live-library writes."""

import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import threading

import numpy as np

from music_adapt import adapt_score
from music_library import decode_score, encode_score, mapping_from_settings
from music_score import ScoreError, compile_score, validate_mapping
from music_sparse import sparse_backing


def contour_errors(pairs):
    reversals = plateaus = 0
    for (a, b), (c, d) in zip(pairs, pairs[1:]):
        if c.start - a.start - a.duration > .7:
            continue
        reversals += (c.pitch - a.pitch) * (d.pitch - b.pitch) < 0
        plateaus += c.pitch != a.pitch and d.pitch == b.pitch
    return {"reversals": int(reversals), "plateaus": int(plateaus)}


def fit_contour(pairs, mapping, shift, stop=None):
    """Repair an existing lead adaptation without changing its attacks or lengths.

    Prefer few edits, forbid reversed motion within phrases, and penalize lost
    steps. Runs longer than the keyboard can still require plateaus. Rests permit
    register changes; repeated source pitches always retain one output pitch.
    """
    mapping = validate_mapping(mapping)
    pairs = tuple(pairs)
    if len(pairs) < 2:
        return pairs
    targets = np.array(sorted(mapping))
    motion = targets[None, :] - targets[:, None]
    source = np.array([a.pitch for a, _ in pairs])
    old = np.array([b.pitch for _, b in pairs])
    desired = np.clip(source + shift, targets[0], targets[-1])
    local = (np.abs(targets[None, :] - old[:, None]) * .45
             + (targets[None, :] != old[:, None]) * 2
             + np.abs(targets[None, :] - desired[:, None]) * .25)
    history = np.zeros((len(pairs), len(targets)), dtype=int)
    cost = local[0]
    for i in range(1, len(pairs)):
        if stop is not None and i % 128 == 1 and stop.is_set():
            raise ScoreError("Contour adaptation cancelled")
        delta = int(source[i] - source[i - 1])
        gap = pairs[i][0].start - (pairs[i - 1][0].start + pairs[i - 1][0].duration)
        transition = np.abs(motion - delta) * .08
        if gap <= .7:
            transition[motion * delta < 0] = np.inf
            if delta:
                transition[motion == 0] += 20
        else:
            transition *= .1
        if not delta:
            transition[motion != 0] = np.inf
        values = cost[:, None] + transition
        history[i] = np.argmin(values, axis=0)
        cost = values[history[i], np.arange(len(targets))] + local[i]
    cursor = int(np.argmin(cost))
    result = []
    for i in range(len(pairs) - 1, -1, -1):
        original, fitted = pairs[i]
        result.append((original, replace(fitted, pitch=int(targets[cursor]))))
        cursor = history[i, cursor]
    return tuple(reversed(result))


def refine_adaptation(lead, mapping, stop=None):
    """Opt-in refinement of a selected monophonic lead, not voice selection."""
    if all(a.pitch in mapping for a, _ in lead.pairs) or not any(contour_errors(lead.pairs).values()):
        return lead
    pairs = fit_contour(lead.pairs, mapping, lead.shift, stop)
    return replace(lead, pairs=pairs, score=replace(lead.score, notes=tuple(b for _, b in pairs)),
                   folded=sum(b.pitch != a.pitch + lead.shift and (b.pitch-a.pitch-lead.shift) % 12 == 0
                              for a, b in pairs),
                   approximated=sum((b.pitch-a.pitch-lead.shift) % 12 != 0 for a, b in pairs))


def validate_events(score, mapping):
    for speed in (.5, 1, 2):
        for hold in (10, 60, 150):
            plan = compile_score(score, mapping, speed=speed, hold_ms=hold)
            if plan.skipped:
                raise ValueError("Unmapped notes in arranged score")
            held = set()
            for event in plan.events:
                if event.down == (event.key in held):
                    raise ValueError("Unbalanced key event")
                if event.down:
                    held.add(event.key)
                else:
                    held.remove(event.key)
            if held:
                raise ValueError("Keys still held at end")


def rebuild_library(library):
    """Return a separate candidate and report; preserve originals and metadata."""
    if library.get("version") != 1:
        raise ValueError("Unsupported library version")
    result = deepcopy(library)
    mapping = mapping_from_settings(result["settings"])
    report = []
    ids = [item["id"] for item in result["items"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate library IDs")
    for item in result["items"]:
        source, old = decode_score(item["original"]), decode_score(item["score"])
        texture = item.get("texture", "melody")
        row = {"id": item["id"], "title": item["title"]}
        status = None
        if all(n.pitch in mapping for n in source.notes):
            status = "preserved_already_nine_key"
        elif texture not in ("melody", "melody_bass", "chords"):
            status = "preserved_unsupported_texture"
        elif texture == "chords":
            ordered = sorted(source.notes, key=lambda n: n.start)
            if any(a.start + a.duration > b.start + .03 for a, b in zip(ordered, ordered[1:])):
                status = "preserved_unverified_polyphony"
        if status:
            validate_events(old, mapping)
            report.append({**row, "status": status})
            continue
        # Chord-mode monophonic imports must retain even very dense attacks.
        lead = adapt_score(source, mapping, track=item.get("melody_track"),
                           texture="chords" if texture == "chords" else "melody")
        before = contour_errors(lead.pairs)
        pairs = fit_contour(lead.pairs, mapping, lead.shift) if any(before.values()) else lead.pairs
        solo = replace(lead.score, notes=tuple(n for _, n in pairs))
        adjusted = replace(lead, score=solo, pairs=pairs)
        backing = (sparse_backing(source, adjusted, mapping, item["sparse_config"], threading.Event())
                   if texture == "melody_bass" else [])
        notes = tuple(sorted(solo.notes + tuple(n for _, n in backing),
                             key=lambda n: (n.start, n.pitch, n.track)))
        score = replace(old, notes=notes)
        validate_events(score, mapping)
        after = contour_errors(pairs)
        changed = score.notes != old.notes
        if changed:
            item["score"] = encode_score(score)
            item["note_count"] = len(notes)
            item["summary"] += " | Contour-first revision (lossy; not individually auditioned)"
            item["contour_revision"] = {"version": 1, "before": before, "after": after}
        report.append({**row, "status": "updated" if changed else "unchanged",
                       "before": before, "after": after,
                       "lead_pitch_changes": sum(b.pitch != d.pitch for (_, b), (_, d) in zip(lead.pairs, pairs))})
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Saved library snapshot, not the running player's library")
    parser.add_argument("destination", type=Path, help="New directory for candidate and report; must not exist")
    args = parser.parse_args()
    library = json.loads(args.source.read_text(encoding="utf-8-sig"))
    candidate, report = rebuild_library(library)
    # Exclusive directory creation prevents overwriting a live library or old run.
    args.destination.mkdir(parents=True, exist_ok=False)
    for name, data in (("library.json", candidate), ("report.json", report)):
        (args.destination / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
                                            encoding="utf-8")
    print(f"Validated {len(report)} entries; candidate only: {args.destination}")


if __name__ == "__main__":
    main()
