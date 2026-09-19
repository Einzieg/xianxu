"""Export separate original/adapted MIDI files and a reproducible adaptation report."""

import argparse
import json
from pathlib import Path

from music_adapt import POLICIES, adapt_score
from music_audio import export_midi, transcribe_audio
from music_score import compile_score, parse_pitch, pitch_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--engine", choices=("instrument", "melody", "yin"), default="instrument")
    parser.add_argument("--register", choices=("auto", "high", "mid", "low"), default="auto")
    parser.add_argument("--settings", type=Path, default=Path(__file__).parent / "music_settings.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "artifacts" / "arrangements")
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text(encoding="utf-8"))
    mapping = {parse_pitch(pitch): key for pitch, key in settings["mapping"]}
    last_bucket = -1

    def progress(value):
        nonlocal last_bucket
        bucket = int(value * 10)
        if bucket != last_bucket:
            print(f"Transcription: {bucket * 10}%", flush=True)
            last_bucket = bucket

    source = transcribe_audio(args.audio, progress=progress, engine=args.engine, register=args.register)
    result = adapt_score(source, mapping)
    plan = compile_score(result.score, mapping)
    missing_before = sum(note.pitch not in mapping for note in source.notes)
    args.output.mkdir(parents=True, exist_ok=True)
    original_path = args.output / (args.audio.stem + ".original.mid")
    adapted_path = args.output / (args.audio.stem + ".9keys.mid")
    report_path = args.output / (args.audio.stem + ".report.json")
    export_midi(source, original_path)
    export_midi(result.score, adapted_path)
    report = {
        "audio": str(args.audio.resolve()), "engine": args.engine, "register": args.register, "policy": POLICIES[0],
        "mapping": {pitch_name(pitch): key for pitch, key in mapping.items()},
        "original_notes": len(source.notes), "unplayable_original_notes": missing_before,
        "adapted_notes": len(result.score.notes), "unplayable_adapted_notes": plan.skipped,
        "duration_seconds": source.duration, "global_transpose": result.shift,
        "octave_folds": result.folded, "substituted_notes": result.approximated,
        "removed_notes": result.removed, "summary": result.summary,
        "note_changes": [{"time": a.start, "original_pitch": pitch_name(a.pitch),
                          "adapted_pitch": pitch_name(b.pitch), "key": mapping[b.pitch]}
                         for a, b in result.pairs],
        "limitations": "可演奏率不等于识别准确率；混音音频识别可能有误，九键编配不能完整保留任意旋律。",
    }
    if args.engine == "instrument":
        from music_neural import TRANSCRIPTION_REVISION
        report["revision"] = TRANSCRIPTION_REVISION
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result.summary, flush=True)
    print(f"Unplayable notes: {missing_before}/{len(source.notes)} -> {plan.skipped}/{plan.note_count}", flush=True)
    print(f"Adapted MIDI: {adapted_path}", flush=True)
    print(f"Report: {report_path}", flush=True)


if __name__ == "__main__":
    main()
