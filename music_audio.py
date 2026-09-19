"""Offline predominant-melody transcription and monophonic calibration."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import ctypes
import json
import math
from pathlib import Path
import threading
import time

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import fftconvolve, find_peaks, medfilt, resample_poly
import soundfile as sf

from music_score import Note, Score, ScoreError, pitch_name

SAMPLE_RATE = 22050
FRAME_SIZE = 2048
HOP = 220


class AudioCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class MeasuredNote:
    key: str
    frequency: float
    pitch: int
    cents: float
    confidence: float


def estimate_pitch(frame, sample_rate=SAMPLE_RATE, fmin=65, fmax=2100):
    """YIN cumulative mean normalized difference, with parabolic refinement."""
    frame = np.asarray(frame, dtype=np.float64)
    frame = frame - np.mean(frame)
    if len(frame) < 128 or np.sqrt(np.mean(frame * frame)) < 0.0002:
        return None, 0.0
    maximum = min(int(sample_rate / fmin), len(frame) // 2)
    minimum = max(2, int(sample_rate / fmax))
    correlation = fftconvolve(frame, frame[::-1], mode="full")[len(frame) - 1:len(frame) + maximum]
    squares = np.concatenate(([0.0], np.cumsum(frame * frame)))
    lags = np.arange(maximum + 1)
    difference = np.maximum(0, squares[len(frame) - lags] + squares[-1] - squares[lags] - 2 * correlation)
    normalized = np.ones(maximum + 1)
    normalized[1:] = difference[1:] * lags[1:] / np.maximum(np.cumsum(difference[1:]), 1e-15)
    candidates = np.flatnonzero(normalized[minimum:maximum] < 0.15)
    if not len(candidates):
        return None, 0.0
    lag = int(candidates[0]) + minimum
    while lag < maximum - 1 and normalized[lag + 1] < normalized[lag]:
        lag += 1
    left, center, right = normalized[lag - 1:lag + 2]
    denominator = left - 2 * center + right
    refined = lag + (0.5 * (left - right) / denominator if abs(denominator) > 1e-12 else 0)
    return sample_rate / refined, float(1 - center)


def _mono(data):
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 2:
        # Selecting the loudest channel avoids cancellation on phase-inverted stereo recordings.
        data = data[:, int(np.argmax(np.mean(data * data, axis=0)))]
    return data


def _resample(data, sample_rate):
    if sample_rate == SAMPLE_RATE:
        return data
    divisor = math.gcd(int(sample_rate), SAMPLE_RATE)
    return resample_poly(data, SAMPLE_RATE // divisor, int(sample_rate) // divisor)


def measure_note(data, sample_rate, key):
    data = _resample(_mono(data), sample_rate)
    pitches = []
    confidences = []
    for start in range(0, len(data) - FRAME_SIZE + 1, HOP):
        frequency, confidence = estimate_pitch(data[start:start + FRAME_SIZE])
        if frequency and confidence >= 0.9:
            pitches.append(69 + 12 * math.log2(frequency / 440))
            confidences.append(confidence)
    if len(pitches) < 8:
        raise ScoreError(f"按键 {key} 未检测到稳定音高，请检查游戏音效、输出设备并关闭背景音乐")
    median = float(np.median(pitches))
    stable = np.abs(np.asarray(pitches) - median) < 0.35
    if float(np.mean(stable)) < 0.7:
        raise ScoreError(f"按键 {key} 音高不稳定，可能混入背景音乐或其他声音")
    pitch = round(median)
    return MeasuredNote(key, 440 * 2 ** ((median - 69) / 12), pitch,
                        (median - pitch) * 100, float(np.median(np.asarray(confidences)[stable])))


def _yin_notes(data, stop, progress):
    """Keep the original single-instrument transcription path."""
    starts = list(range(0, len(data) - FRAME_SIZE + 1, HOP))
    if not starts:
        raise ScoreError("音频太短，无法识别")
    energies = np.array([np.sqrt(np.mean(data[start:start + FRAME_SIZE] ** 2)) for start in starts])
    threshold = max(0.001, float(np.max(energies)) * 0.08)
    detected = []
    for index, start in enumerate(starts):
        if stop.is_set():
            raise AudioCancelled("已取消转谱")
        frequency, confidence = estimate_pitch(data[start:start + FRAME_SIZE]) if energies[index] >= threshold else (None, 0)
        detected.append(round(69 + 12 * math.log2(frequency / 440)) if frequency and confidence >= 0.85 else -1)
        if index % 100 == 0:
            progress(0.1 + 0.85 * index / len(starts))
    pitches = medfilt(np.asarray(detected, dtype=float), kernel_size=5).astype(int)
    notes = []
    begin = 0
    for end in range(1, len(pitches) + 1):
        if end < len(pitches) and pitches[end] == pitches[begin]:
            continue
        duration = (end - begin) * HOP / SAMPLE_RATE
        if pitches[begin] >= 0 and duration >= 0.08:
            notes.append(Note(begin * HOP / SAMPLE_RATE, duration, int(pitches[begin])))
        begin = end
    return notes


def _melody_features(data, stop, progress):
    # Include bass candidates as harmonic explanations, not as melody states.
    pitches = np.arange(36, 97)
    harmonics = np.arange(1, 7)
    frame_size, fft_size = 4096, 8192
    frequencies = 440 * 2 ** ((pitches[:, None] + np.array([-0.3, 0, 0.3]) - 69) / 12)
    bins = frequencies[:, :, None] * harmonics * fft_size / SAMPLE_RATE
    valid = bins < fft_size // 2 - 1
    lower = np.minimum(bins.astype(int), fft_size // 2 - 1)
    fraction = bins - np.floor(bins)
    weights = harmonics ** -0.8
    weights /= weights.sum()
    window = np.hanning(frame_size)
    padded = np.pad(data, (frame_size // 2, frame_size // 2))
    count = (len(data) + HOP - 1) // HOP
    salience = np.zeros((count, len(pitches)), dtype=np.float32)
    amplitudes = np.zeros_like(salience)
    energies = np.zeros(count)
    tonal = np.zeros(count, dtype=bool)
    flux = np.zeros(count)
    previous = np.zeros(fft_size // 2 + 1)
    band = slice(round(130 * fft_size / SAMPLE_RATE), round(5000 * fft_size / SAMPLE_RATE))
    for index in range(count):
        if stop.is_set():
            raise AudioCancelled("已取消转谱")
        frame = padded[index * HOP:index * HOP + frame_size]
        frame = frame - np.mean(frame)
        center = frame[frame_size // 2 - 512:frame_size // 2 + 512]
        energies[index] = np.sqrt(np.mean(center ** 2))
        magnitude = np.abs(np.fft.rfft(frame * window, n=fft_size))
        power = magnitude[band] ** 2 + 1e-15
        tonal[index] = np.exp(np.mean(np.log(power))) / np.mean(power) < 0.3
        # Partial whitening stops a loud bass from monopolizing every candidate.
        envelope = uniform_filter1d(magnitude, size=75)
        whitened = np.maximum(magnitude - envelope, 0) / np.maximum(
            envelope, max(float(magnitude.max()) * 0.015, 1e-10)) ** 0.7
        samples = ((1 - fraction) * whitened[lower] + fraction * whitened[lower + 1]) * valid
        raw = ((1 - fraction) * magnitude[lower] + fraction * magnitude[lower + 1]) * valid
        scores = samples @ weights
        # Require some fundamental evidence to limit subharmonic hallucinations,
        # but let several partials outweigh a stronger isolated octave peak.
        scores *= np.minimum(1, samples[:, :, 0] / (0.2 * samples.max(axis=2) + 1e-10)) ** 0.5
        tuning = np.argmax(scores, axis=1)
        salience[index] = scores[np.arange(len(pitches)), tuning]
        amplitudes[index] = (raw[:, :, 0] + 0.5 * raw[:, :, 1])[np.arange(len(pitches)), tuning]
        flux[index] = np.maximum(magnitude[band] - previous[band], 0).sum() / max(
            float(magnitude[band].sum()), 1e-10)
        previous = magnitude
        if index % 100 == 0:
            progress(0.1 + 0.65 * index / count)

    # Discount octave/fifth copies of an already explained lower fundamental.
    evidence = salience.copy()
    explained = np.zeros_like(salience)
    for semitones, penalty in ((12, 0.65), (19, 0.45), (24, 0.3), (28, 0.2)):
        explained[:, semitones:] = np.maximum(explained[:, semitones:], penalty * evidence[:, :-semitones])
    salience = np.maximum(salience[:, 12:] - explained[:, 12:], 0)
    pitches = pitches[12:]
    # Explicit register prior: this mode targets C3-C7 leads, not bass solos.
    salience *= np.clip((pitches - 45) / 15, 0.15, 1)[None, :]
    active = tonal & (energies >= max(0.0003, float(energies.max()) * 0.015))
    active &= salience.max(axis=1) > evidence.max(axis=1) * 0.12
    return pitches, salience, amplitudes[:, 12:], active, flux


def _track_melody(pitches, salience, active, flux, stop, progress):
    """Viterbi path with an explicit rest state and onset-aware jump costs."""
    count, width = salience.shape
    relative = salience / np.maximum(salience.max(axis=1, keepdims=True), 1e-10)
    emissions = np.full((count, width + 1), -2.5, dtype=np.float32)
    emissions[:, :width] = 2 * np.log(np.maximum(relative, 0.001))
    emissions[~active, :width] = -20
    emissions[~active, width] = 0
    distance = np.abs(pitches[:, None] - pitches[None, :])
    transitions = np.full((width + 1, width + 1), 1.5)
    transitions[:width, :width] = 1.4 + np.minimum(distance * 0.18, 3)
    transitions[:width, :width] += (distance == 12) * 0.6
    np.fill_diagonal(transitions, 0)
    back = np.zeros((count, width + 1), dtype=np.int16)
    values = emissions[0].copy()
    for index in range(1, count):
        if stop.is_set():
            raise AudioCancelled("已取消转谱")
        cost = transitions * (0.55 if flux[index] > 0.15 else 1)
        choices = values[:, None] - cost
        back[index] = np.argmax(choices, axis=0)
        values = emissions[index] + choices[back[index], np.arange(width + 1)]
        values -= values.max()
        if index % 100 == 0:
            progress(0.75 + 0.2 * index / count)
    states = np.empty(count, dtype=int)
    states[-1] = np.argmax(values)
    for index in range(count - 1, 0, -1):
        if index % 100 == 0 and stop.is_set():
            raise AudioCancelled("已取消转谱")
        states[index - 1] = back[index, states[index]]
    return np.append(pitches, -1)[states]


def _melody_notes(data, stop, progress):
    pitches, salience, amplitudes, active, flux = _melody_features(data, stop, progress)
    detected = _track_melody(pitches, salience, active, flux, stop, progress)
    # Work in centered frame timestamps; pad analysis, not the returned score.
    step = HOP / SAMPLE_RATE
    duration = len(data) / SAMPLE_RATE
    boundaries = np.r_[0, np.flatnonzero(np.diff(detected)) + 1, len(detected)]
    notes = []
    for begin, end in zip(boundaries[:-1], boundaries[1:]):
        if stop.is_set():
            raise AudioCancelled("已取消转谱")
        pitch = int(detected[begin])
        if pitch < 0:
            continue
        amplitude = amplitudes[begin:end, pitch - int(pitches[0])]
        # Pitch-local spectral flux avoids splitting a held lead on every bass
        # or drum attack. A new attack must follow a substantial amplitude dip.
        delayed = amplitude[np.maximum(np.arange(len(amplitude)) - 3, 0)]
        rise = np.maximum(amplitude - delayed, 0)
        attacks, _ = find_peaks(rise, height=float(amplitude.max()) * 0.12,
                                prominence=float(amplitude.max()) * 0.08,
                                distance=max(1, round(0.1 / step)))
        splits = [int(begin)]
        for attack in attacks:
            before = amplitude[max(0, attack - 10):attack]
            after = amplitude[attack:min(len(amplitude), attack + 6)]
            valley = max(0, attack - 10) + int(np.argmin(before))
            previous_peak = amplitude[:valley].max() if valley else 0
            boundary = int(begin + attack)
            if (before.min() < after.max() * 0.75
                    and before.min() < previous_peak * 0.75
                    and (boundary - splits[-1]) * step >= 0.08
                    and (end - boundary) * step >= 0.08):
                splits.append(boundary)
        splits.append(int(end))
        for left, right in zip(splits[:-1], splits[1:]):
            start = left * step
            length = min(right * step, duration) - start
            if length >= 0.08:
                notes.append(Note(start, length, pitch))
    return notes


def transcribe_audio(path, stop=None, progress=lambda value: None, engine="melody", register="auto", *, texture="melody"):
    """Extract editable note events; default to one lead, without beat quantizing.

    ``instrument`` uses the local Basic Pitch ONNX model and lead selection.
    Its optional ``texture="chords"`` also retains evidence-filtered support notes.
    ``melody`` searches C3-C7 using offline spectral/harmonic evidence and
    temporal tracking, with a mid/high-register prior. Dense chords, doubled
    voices, missing fundamentals and bass-only passages remain ambiguous.
    Notes shorter than 80 ms are discarded; timing is not beat-quantized.
    ``yin`` retains the original solo-instrument detector.
    Calibration deliberately continues to use estimate_pitch/measure_note.
    """
    if texture not in ("melody", "chords") or (texture == "chords" and engine != "instrument"):
        raise ScoreError("保留和弦模式仅支持乐器音符模型")
    if engine == "instrument":
        from music_neural import transcribe_instrument
        return transcribe_instrument(path, stop=stop, progress=progress, register=register, texture=texture)
    if engine not in ("melody", "yin"):
        raise ValueError(f"Unknown transcription engine: {engine!r}")
    stop = stop or threading.Event()
    if stop.is_set():
        raise AudioCancelled("已取消转谱")
    progress(0.0)
    path = Path(path)
    info = sf.info(path)
    if info.duration > 300 or info.duration < 0.1 or info.channels > 8:
        raise ScoreError("音频需为 0.1～300 秒，最多 8 个声道；长曲请先截取片段")
    # Resample in bounded chunks so a high-rate multichannel file cannot exhaust memory.
    chunks = []
    frames = 0
    with sf.SoundFile(path) as source:
        for chunk in source.blocks(blocksize=source.samplerate * 5, dtype="float32", always_2d=True):
            if stop.is_set():
                raise AudioCancelled("已取消转谱")
            chunks.append(_resample(_mono(chunk), source.samplerate))
            frames += len(chunk)
            progress(0.1 * min(1, frames / max(info.frames, 1)))
    data = np.concatenate(chunks)
    notes = (_melody_notes if engine == "melody" else _yin_notes)(data, stop, progress)
    progress(0.99)
    if stop.is_set():
        raise AudioCancelled("已取消转谱")
    if not notes:
        raise ScoreError("未识别出稳定旋律；请尝试旋律更突出的片段，或用 yin 模式识别独奏")
    progress(1.0)
    return Score(path.stem, tuple(notes), len(data) / SAMPLE_RATE, ((0, "音频识别 · 待校对"),))


def export_midi(score, path):
    import mido

    midi = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    events = []
    for note in score.notes:
        start = round(note.start * 960)
        end = max(start + 1, round((note.start + note.duration) * 960))
        events.extend(((start, True, note.pitch), (end, False, note.pitch)))
    previous = 0
    for tick, down, pitch in sorted(events):
        track.append(mido.Message("note_on" if down else "note_off", note=pitch,
                                  velocity=80 if down else 0, time=tick - previous))
        previous = tick
    track.append(mido.MetaMessage("end_of_track", time=max(0, round(score.duration * 960) - previous)))
    midi.save(path)


@contextmanager
def audio_devices():
    # SoundCard initializes COM on its importing thread; initialize additional worker threads afterwards.
    import soundcard

    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    ole32.CoInitializeEx.restype = ctypes.c_long
    result = ole32.CoInitializeEx(None, 0)
    if result not in (0, 1, -2147417850):
        raise RuntimeError(f"音频设备 COM 初始化失败: {result}")
    try:
        yield soundcard
    finally:
        if result in (0, 1):
            ole32.CoUninitialize()


def list_outputs():
    with audio_devices() as soundcard:
        return [(speaker.id, speaker.name) for speaker in soundcard.all_speakers()]


def calibrate(keys, sink, target_state, output_dir, stop=None, device_id=None, progress=lambda message: None, delay=3):
    stop = stop or threading.Event()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for remaining in range(delay, 0, -1):
        progress(f"{remaining} 秒后校准，请切到游戏并关闭背景音乐")
        if stop.wait(1):
            raise AudioCancelled("已取消校准")
    if target_state() != "ready":
        raise RuntimeError("游戏不在前台，校准已取消")
    results = []
    with audio_devices() as soundcard:
        speaker = soundcard.get_speaker(device_id) if device_id else soundcard.default_speaker()
        if speaker is None:
            raise RuntimeError("找不到音频输出设备")
        microphone = soundcard.get_microphone(speaker.id, include_loopback=True)
        sample_rate = 48000

        def record(recorder, seconds):
            chunks = []
            frames = round(seconds * sample_rate)
            while frames > 0:
                if stop.is_set():
                    raise AudioCancelled("已取消校准")
                if target_state() != "ready":
                    raise RuntimeError("游戏失去焦点，校准已中止")
                count = min(2400, frames)
                chunks.append(recorder.record(numframes=count))
                frames -= count
            return np.concatenate(chunks)

        with microphone.recorder(samplerate=sample_rate, blocksize=2400) as recorder:
            for index, key in enumerate(keys, 1):
                progress(f"校准 {index}/{len(keys)} · {key}")
                record(recorder, 0.8)
                if stop.is_set() or target_state() != "ready":
                    raise AudioCancelled("校准已中止")
                # Release before waiting for audio capture; a stalled recorder must never hold a key.
                try:
                    sink.key_down(key)
                    stop.wait(0.06)
                finally:
                    last_error = None
                    for _ in range(3):
                        try:
                            sink.key_up(key)
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = exc
                    if last_error:
                        raise RuntimeError(f"校准时释放 {key} 失败: {last_error}")
                data = record(recorder, 0.8)
                sf.write(output_dir / f"{key}.wav", data, sample_rate, subtype="PCM_16")
                result = measure_note(data, sample_rate, key)
                results.append(result)
                progress(f"{key} = {result.frequency:.1f} Hz / {pitch_name(result.pitch)} ({result.cents:+.1f} 音分)")
    (output_dir / "measurements.json").write_text(json.dumps([asdict(result) for result in results],
                                                             ensure_ascii=False, indent=2), encoding="utf-8")
    return results
