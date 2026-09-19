"""Bounded local A/B audio previews. This module never uses an input backend."""

import base64
import io
import math
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly
import soundfile as sf

from music_score import ScoreError


RATE = 22050
AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg")


def clip_range(start, duration, total):
    start, duration = float(start), float(duration)
    if not math.isfinite(start) or not 0 <= start < total:
        raise ScoreError("试听起点必须在曲目范围内")
    if not math.isfinite(duration) or not 0.1 <= duration <= 30:
        raise ScoreError("试听片段长度必须在0.1～30秒之间")
    return start, min(duration, total - start)


def synthesize(score, start, duration):
    start, duration = clip_range(start, duration, score.duration)
    size = max(1, round(duration * RATE))
    data = np.zeros(size, dtype=np.float32)
    budget = 0
    for note in score.notes:
        left = max(0, round((note.start - start) * RATE))
        right = min(size, round((note.start + note.duration - start) * RATE))
        if right <= left:
            continue
        budget += right - left
        if budget > 20_000_000:
            raise ScoreError("此片段的原谱声部过密，请缩短试听长度")
        age = (np.arange(left, right) / RATE + start - note.start).clip(0)
        frequency = 440 * 2 ** ((note.pitch - 69) / 12)
        wave = np.zeros(len(age))
        for harmonic, weight in ((1, 1), (2, 0.3), (3, 0.12)):
            if frequency * harmonic < RATE / 2:
                wave += weight * np.sin(2 * np.pi * frequency * harmonic * age)
        attack = np.minimum(age / 0.008, 1)
        release = np.clip((note.duration - age) / 0.025, 0, 1)
        data[left:right] += (0.28 * wave * attack * release * (0.35 + 0.65 * np.exp(-3 * age))).astype(np.float32)
    peak = float(np.max(np.abs(data)))
    if peak > 0.85:
        data *= 0.85 / peak
    return data


def source_clip(path, start, duration):
    path = Path(path)
    if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
        raise ScoreError("原音频不存在，或该谱来自TXT/MIDI；仍可试听识别旋律和9键版本")
    with sf.SoundFile(path) as source:
        if not 1 <= source.channels <= 8 or not 8000 <= source.samplerate <= 384000:
            raise ScoreError("原音频的采样率或声道数不支持试听")
        start, duration = clip_range(start, duration, len(source) / source.samplerate)
        source.seek(round(start * source.samplerate))
        remaining = max(1, round(duration * source.samplerate))
        divisor = math.gcd(RATE, source.samplerate)
        chunks = []
        while remaining:
            chunk = source.read(min(remaining, source.samplerate), dtype="float32", always_2d=True)
            if not len(chunk):
                break
            remaining -= len(chunk)
            # Retain stereo; no source separation or per-channel normalization.
            chunks.append(resample_poly(chunk[:, :2], RATE // divisor, source.samplerate // divisor, axis=0))
    if not chunks:
        raise ScoreError("试听片段没有可读取的音频")
    return np.concatenate(chunks), start


def audition(item, kind, start=0, duration=15):
    from music_library import decode_score

    labels = {"source": "原曲片段", "original": "识别旋律 · 合成音色", "score": "9键版本 · 相同合成音色，非游戏实录"}
    if (item.get("transcription") or {}).get("texture") == "chords":
        labels["original"] = "识别和弦 · 合成音色"
    if kind not in labels:
        raise ScoreError("未知试听类型")
    start, _ = clip_range(start, duration, item["duration"])
    if kind == "source":
        data, start = source_clip(item["source"], start, duration)
    else:
        data = synthesize(decode_score(item[kind]), start, duration)
    output = io.BytesIO()
    sf.write(output, data, RATE, format="WAV", subtype="PCM_16")
    return {"kind": kind, "start": start, "duration": len(data) / RATE, "label": labels[kind],
            "data_url": "data:audio/wav;base64," + base64.b64encode(output.getvalue()).decode("ascii")}
