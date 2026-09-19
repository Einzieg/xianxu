"""Offline Basic Pitch ONNX notes followed by conservative voice tracking.

The returned MIDI pitches are NOT adapted to game keys or a musical scale.
Basic Pitch is instrument-agnostic, not an instrument/source separator.

Portions adapted from Spotify basic-pitch (Copyright 2022 Spotify AB),
Apache-2.0, commit fa5997af0a8210982619003269994a1be25eddf3:
constants.py, inference.py (windowing/output names), and note_creation.py
(onset decoding/time alignment). See models/basic-pitch/LICENSE and SOURCES.json.
Modifications: scipy resampling, bounded/cancellable CPU inference, stricter
onset-only decoding without the Melodia residual-energy pass, and an original
note-event dynamic program with acoustic reattack validation, sparse tail
integrals, adaptive register and explicit rest edges (legato-v2.1).
"""

from dataclasses import dataclass, asdict
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import uuid

import numpy as np
from scipy.ndimage import gaussian_filter1d, uniform_filter1d
from scipy.signal import butter, find_peaks, hilbert, resample_poly, sosfiltfilt
import soundfile as sf

from music_score import MAX_NOTES, Note, Score, ScoreError


SAMPLE_RATE = 22050
FFT_HOP = 256
WINDOW_SAMPLES = 43844
WINDOW_FRAMES = 172
OVERLAP_FRAMES = 30
WINDOW_HOP = WINDOW_SAMPLES - OVERLAP_FRAMES * FFT_HOP
KEPT_FRAMES = WINDOW_FRAMES - OVERLAP_FRAMES
MODEL_PATH = Path(__file__).resolve().parent / "models" / "basic-pitch" / "nmp.onnx"
MODEL_SHA256 = "2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec"
INPUT_NAME = "serving_default_input_2:0"
OUTPUT_NAMES = {
    "note": "StatefulPartitionedCall:1",
    "onset": "StatefulPartitionedCall:2",
    "contour": "StatefulPartitionedCall:0",
}
REGISTERS = ("auto", "high", "mid", "low")
TRANSCRIPTION_REVISION = "legato-v2.1"
CHORD_TRANSCRIPTION_REVISION = "chords-v1"
ENVELOPE_BLOCK_SAMPLES = 40 * SAMPLE_RATE
ENVELOPE_GUARD_SAMPLES = 2 * SAMPLE_RATE


@dataclass(frozen=True)
class NeuralNote:
    """Polyphonic candidate; confidence is model evidence, not correctness odds."""

    start: float
    duration: float
    pitch: int
    confidence: float
    onset: float
    support: float


@dataclass(frozen=True)
class NeuralTranscription:
    score: Score
    candidates: tuple[NeuralNote, ...]
    register: str
    register_centers: tuple[float, ...]
    texture: str = "melody"


def _cancel(stop):
    if stop is not None and stop.is_set():
        # A lazy import also permits music_audio to bridge to this module.
        from music_audio import AudioCancelled
        raise AudioCancelled("Instrument transcription cancelled")


def _validate_register(register):
    if register not in REGISTERS:
        raise ScoreError("register must be auto, high, mid, or low")


def _load_model():
    path = Path(MODEL_PATH)
    if not path.is_file():
        raise ScoreError(f"Basic Pitch ONNX model missing: {path}")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ScoreError(f"Cannot read Basic Pitch model: {path}: {exc}") from exc
    if digest != MODEL_SHA256:
        raise ScoreError(f"Basic Pitch model SHA256 mismatch: {path}")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise ScoreError("Missing onnxruntime; install the CPU onnxruntime package") from exc
    try:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(path), sess_options=options,
                                       providers=["CPUExecutionProvider"])
        inputs = session.get_inputs()
        outputs = {item.name: item for item in session.get_outputs()}
        if (len(inputs) != 1 or inputs[0].name != INPUT_NAME
                or inputs[0].shape[1:] != [WINDOW_SAMPLES, 1]
                or inputs[0].type != "tensor(float)"):
            raise ValueError("Unexpected model input signature")
        for key, name in OUTPUT_NAMES.items():
            width = 264 if key == "contour" else 88
            if name not in outputs or outputs[name].shape[1:] != [WINDOW_FRAMES, width]:
                raise ValueError(f"Unexpected model output signature: {name}")
        return session
    except Exception as exc:
        raise ScoreError(f"Cannot initialize Basic Pitch ONNX: {exc}") from exc


def _read_audio(path, stop, progress):
    try:
        with sf.SoundFile(path) as audio:
            duration = audio.frames / audio.samplerate
            if not 0.1 <= duration <= 300:
                raise ScoreError("Audio length must be between 0.1 and 300 seconds")
            if not 8000 <= audio.samplerate <= 192000 or not 1 <= audio.channels <= 8:
                raise ScoreError("Unsupported audio sample rate or channel count")
            rate = audio.samplerate
            chunks = []
            for chunk in audio.blocks(blocksize=rate * 5, dtype="float32", always_2d=True):
                _cancel(stop)
                if not np.isfinite(chunk).all():
                    raise ScoreError("Audio contains non-finite samples")
                mono = chunk.mean(axis=1)
                channel_power = np.mean(chunk * chunk, axis=0)
                # Standard mono averaging, except for severely phase-cancelled stereo.
                if np.mean(mono * mono) < 0.01 * float(channel_power.max()):
                    mono = chunk[:, int(channel_power.argmax())]
                chunks.append(mono)
                progress(0.02 + 0.05 * audio.tell() / audio.frames)
            data = np.concatenate(chunks)
    except (OSError, RuntimeError) as exc:
        _cancel(stop)
        raise ScoreError(f"Cannot decode audio {path}: {exc}") from exc
    _cancel(stop)
    if rate != SAMPLE_RATE:
        divisor = math.gcd(rate, SAMPLE_RATE)
        data = resample_poly(data, SAMPLE_RATE // divisor, rate // divisor)
    _cancel(stop)
    return np.asarray(data, dtype=np.float32), duration


def _frame_times(count):
    """Exact Spotify model_frames_to_time, including its 0.0018 s correction."""
    frames = np.arange(count, dtype=np.float64)
    window_offset = (FFT_HOP / SAMPLE_RATE) * (
        WINDOW_FRAMES - WINDOW_SAMPLES / FFT_HOP) + 0.0018
    return frames * FFT_HOP / SAMPLE_RATE - window_offset * np.floor(frames / WINDOW_FRAMES)


def _infer(data, session, stop, progress):
    # Spotify prepends half the overlap, pads the LAST window, then removes
    # 15 prediction frames from EACH side of EVERY 172-frame window.
    padded = np.pad(data, (OVERLAP_FRAMES * FFT_HOP // 2, 0))
    starts = range(0, len(padded), WINDOW_HOP)
    expected = int(len(data) / WINDOW_HOP * KEPT_FRAMES)
    output = {key: np.empty((expected, 264 if key == "contour" else 88), dtype=np.float32)
              for key in OUTPUT_NAMES}
    for index, start in enumerate(starts):
        _cancel(stop)
        window = padded[start:start + WINDOW_SAMPLES]
        window = np.pad(window, (0, WINDOW_SAMPLES - len(window)))
        try:
            values = session.run(list(OUTPUT_NAMES.values()),
                                 {INPUT_NAME: window[None, :, None]})
        except Exception as exc:
            raise ScoreError(f"Basic Pitch inference failed: {exc}") from exc
        _cancel(stop)
        for (key, _), value in zip(OUTPUT_NAMES.items(), values):
            width = 264 if key == "contour" else 88
            if value.shape != (1, WINDOW_FRAMES, width) or not np.isfinite(value).all():
                raise ScoreError(f"Invalid Basic Pitch output: {key}")
            begin = index * KEPT_FRAMES
            count = max(0, min(KEPT_FRAMES, expected - begin))
            output[key][begin:begin + count] = value[0, 15:15 + count]
        progress(0.08 + 0.65 * (index + 1) / len(starts))
    return output


def _onset_peaks(onsets):
    peaks, _ = find_peaks(np.pad(onsets, (1, 1)), height=0.5, distance=8, prominence=0.04)
    return peaks - 1


def _decode_candidates(output, duration, stop, progress, *, peak_indices=None):
    """Spotify onset-first decoder, without frame-only/Melodia note invention.

    Changes from upstream: handle boundary/plateau peaks, stop at a subsequent
    same-pitch attack exactly, use five-frame release tolerance, and retain
    separate mean-frame/onset/support evidence for the downstream voice tracker.
    """
    frames, onsets = output["note"], output["onset"]
    times = _frame_times(len(frames) + 1)
    candidates = []
    for pitch_index in range(88):
        _cancel(stop)
        # A low frame value can extend a detected note, but cannot create one.
        peaks = (_onset_peaks(onsets[:, pitch_index]) if peak_indices is None
                 else peak_indices[pitch_index])
        for index, begin in enumerate(peaks):
            _cancel(stop)
            limit = int(peaks[index + 1]) if index + 1 < len(peaks) else len(frames)
            end, below = int(begin) + 1, 0
            while end < limit:
                if end % 256 == 0:
                    _cancel(stop)
                below = below + 1 if frames[end, pitch_index] < 0.3 else 0
                end += 1
                if below >= 5:
                    break
            end -= below
            if end - begin < 11:
                continue
            evidence = frames[begin:end, pitch_index]
            start = max(0.0, float(times[begin]))
            finish = min(duration, float(times[end]))
            if finish - start < 0.1:
                continue
            candidates.append(NeuralNote(
                start, finish - start, pitch_index + 21, float(evidence.mean()),
                float(onsets[begin, pitch_index]), float(np.mean(evidence >= 0.3))))
            if len(candidates) > MAX_NOTES:
                raise ScoreError("Too many Basic Pitch candidate notes")
        progress((pitch_index + 1) / 88)
    return tuple(sorted(candidates, key=lambda note: (note.start, note.pitch)))


def _pitch_envelope(data, pitch, stop):
    """E's pitch-local amplitude, for ONE bounded block, without a cents track."""
    _cancel(stop)
    hz = 440 * 2 ** ((pitch - 69) / 12)
    sos = butter(3, [hz * 2 ** (-0.75 / 12), hz * 2 ** (0.75 / 12)],
                 fs=SAMPLE_RATE, btype="bandpass", output="sos")
    filtered = sosfiltfilt(sos, data)
    _cancel(stop)
    envelope = uniform_filter1d(abs(hilbert(filtered)), size=round(0.008 * SAMPLE_RATE))
    _cancel(stop)
    return envelope


def _acoustic_attack(envelope, time, sample_offset):
    def section(a, b):
        left = math.ceil((time + a) * SAMPLE_RATE) - sample_offset
        right = math.ceil((time + b) * SAMPLE_RATE) - sample_offset
        return envelope[max(0, left):max(0, right)]

    before = float(np.median(section(-0.13, -0.06)))
    after = float(np.median(section(0.06, 0.13)))
    valley = float(section(-0.05, 0.05).min()) / max(before, after, 1e-12)
    rise = float(section(0, 0.08).max()) / max(float(section(-0.09, -0.01).min()), 1e-12)
    return valley < 0.65 and rise > 1.5


def _acoustic_evidence(data, output, stop, progress):
    """Keep only frame-rate envelopes and validated ORIGINAL onset indices.

    At most one 40 s core (+2 s context per side) Hilbert buffer is live.
    Boundary decisions use sample-rate envelopes before those buffers are
    discarded. Guarded blocks prevent a 300 s file from allocating 88 full
    Hilbert signals. Clips shorter than 0.3 s bypass the two-sided attack test.
    """
    frames, onsets = output["note"], output["onset"]
    times = _frame_times(len(frames))
    duration = len(data) / SAMPLE_RATE
    raw = []
    for column in onsets.T:
        _cancel(stop)
        raw.append(_onset_peaks(column))
    active = [pi for pi, peaks in enumerate(raw) if len(peaks)]
    envelopes, validated = {}, [[] for _ in range(88)]
    for order, pi in enumerate(active):
        _cancel(stop)
        peaks = raw[pi]
        acoustic = np.ones(len(peaks), dtype=bool)
        sampled = np.empty(len(times), dtype=np.float32)
        for start in range(0, len(data), ENVELOPE_BLOCK_SAMPLES):
            _cancel(stop)
            finish = min(len(data), start + ENVELOPE_BLOCK_SAMPLES)
            left = max(0, start - ENVELOPE_GUARD_SAMPLES)
            right = min(len(data), finish + ENVELOPE_GUARD_SAMPLES)
            envelope = _pitch_envelope(data[left:right], pi + 21, stop)
            lo, hi = np.searchsorted(times, [start / SAMPLE_RATE, finish / SAMPLE_RATE])
            positions = times[lo:hi] * SAMPLE_RATE - left
            lower = np.minimum(positions.astype(int), len(envelope) - 1)
            fraction = positions - lower
            sampled[lo:hi] = ((1 - fraction) * envelope[lower]
                              + fraction * envelope[np.minimum(lower + 1, len(envelope) - 1)])
            for index in np.flatnonzero((times[peaks] >= start / SAMPLE_RATE)
                                       & (times[peaks] < finish / SAMPLE_RATE)):
                _cancel(stop)
                time = float(times[peaks[index]])
                if time > 0.15 and time + 0.15 < duration:
                    acoustic[index] = _acoustic_attack(envelope, time, left)
            del envelope
            progress((order + finish / len(data)) / len(active))
        keep = []
        for peak, attack in zip(peaks, acoustic):
            _cancel(stop)
            time = times[peak]
            if (keep and 0.15 < time < duration - 0.15
                    and frames[max(keep[-1], peak - 10):peak + 1, pi].min() >= 0.3
                    and not attack):
                continue
            keep.append(int(peak))
        validated[pi] = keep
        envelopes[pi + 21] = sampled
    progress(1.0)
    _cancel(stop)
    return envelopes, validated


def _register_curve(candidates, duration, register, stop=None):
    """Slowly varying evidence-supported voice region, never a pitch/scale mask."""
    blocks = max(1, math.ceil(duration / 6))
    histogram = np.zeros((blocks, 88), dtype=np.float64)
    for note in candidates:
        _cancel(stop)
        # Cap drones; isolated brief ornaments must not set the high-register
        # quantile and thereby eject the sustained voice before path tracking.
        stability = min(note.duration / 0.3, 1.0) ** 2
        weight = min(note.duration, 0.7) * stability * note.confidence ** 2 * note.onset
        histogram[min(blocks - 1, int(note.start // 6)), note.pitch - 21] += weight
    histogram = gaussian_filter1d(histogram, sigma=4, axis=1, mode="constant")
    total = histogram.sum(axis=0)
    quantile = {"auto": 0.60, "high": 0.85, "mid": 0.50, "low": 0.15}[register]
    target = int(np.searchsorted(np.cumsum(total), total.sum() * quantile)) + 21
    centers = np.arange(21, 109)
    local = histogram / np.maximum(histogram.max(axis=1, keepdims=True), 1e-9)
    strength = 0.35 if register == "auto" else 1.6
    local -= strength * np.minimum(np.abs(centers - target) / 18, 2)[None, :]
    cost = 0.055 * np.abs(centers[:, None] - centers[None, :])
    back = np.zeros((blocks, 88), dtype=np.int16)
    scores = local[0].copy()
    for block in range(1, blocks):
        _cancel(stop)
        choices = scores[:, None] - cost
        back[block] = choices.argmax(axis=0)
        scores = local[block] + choices[back[block], np.arange(88)]
    path = np.empty(blocks, dtype=int)
    path[-1] = scores.argmax()
    for block in range(blocks - 1, 0, -1):
        _cancel(stop)
        path[block - 1] = back[block, path[block]]
    return tuple(float(centers[index]) for index in path)


def _select_lead(candidates, duration, register, stop, progress, *,
                 output=None, envelopes=None, fixed_centers=None):
    """E's onset-entry DP with sparse local integrals and vectorized edges.

    A transition subtracts the actual remaining tail integral, instead of
    forbidding release overlaps. It cannot enter a held note mid-way to fill
    around an ornament. Negative full-tail scores remain eligible: their
    truncated prefixes may be useful. Candidate-only calls use constant
    evidence; production always supplies neural frames and acoustic envelopes.
    Contiguous validated same-pitch prefixes can repair decoder-created gaps
    between selected notes, without altering the selected pitch path.
    """
    _validate_register(register)
    _cancel(stop)
    notes = [note for note in candidates if note.confidence >= 0.45 and note.support >= 0.65]
    if not notes:
        return (), ()
    centers = fixed_centers or _register_curve(notes, duration, register, stop)
    starts = np.array([note.start for note in notes])
    lengths = np.array([note.duration for note in notes])
    ends = starts + lengths
    pitches = np.array([note.pitch for note in notes])
    onsets = np.array([note.onset for note in notes])
    count = len(output["note"]) if output is not None else math.ceil(duration * SAMPLE_RATE / WINDOW_HOP * KEPT_FRAMES) + 1
    edges = _frame_times(count + 1)
    times, steps = edges[:-1], np.diff(edges)
    center = np.interp(times, np.arange(len(centers)) * 6 + 3, centers)
    lefts = np.searchsorted(times, starts)
    rights = np.searchsorted(times, ends)
    sizes = rights - lefts
    offsets = np.r_[0, np.cumsum(sizes + 1)]
    # Decoding gives disjoint intervals per pitch. Bound packed storage even
    # for unexpectedly dense input; never allocate a notes-by-song matrix.
    if offsets[-1] > 88 * count + 2 * len(notes):
        raise ScoreError("Candidate integral storage exceeds the audio bound")
    packed = np.empty(int(offsets[-1]), dtype=np.float64)
    totals = np.empty(len(notes))
    for index, note in enumerate(notes):
        _cancel(stop)
        lo, hi = lefts[index], rights[index]
        affinity = 0.25 + 0.75 * np.exp(-0.5 * ((note.pitch - center[lo:hi]) / 12) ** 2)
        prob = (output["note"][lo:hi, note.pitch - 21].astype(float) if output is not None
                else np.full(hi - lo, note.confidence))
        if envelopes is not None and hi > lo:
            env = envelopes[note.pitch][lo:hi]
            scale = max(float(np.quantile(env, 0.85)), 1e-9)
            prob *= np.clip(env / (0.50 * scale), 0, 1)
        density = 3 * (prob * affinity - 0.4) + 0.18 * (note.onset - 0.5)
        begin = offsets[index]
        packed[begin] = 0
        np.cumsum(density * steps[lo:hi], out=packed[begin + 1:offsets[index + 1]])
        totals[index] = packed[offsets[index + 1] - 1]
        if index % 64 == 0:
            progress(0.35 * (index + 1) / len(notes))
    best = np.full(len(notes), -np.inf)
    back = np.full(len(notes), -1, dtype=int)
    for index in range(len(notes)):
        _cancel(stop)
        best[index] = totals[index] - 0.10
        cut = np.minimum(ends[:index], starts[index])
        valid = (cut - starts[:index] >= 0.075) & (sizes[:index] > 0)
        frame = np.searchsorted(edges, cut, side="right") - 1
        frame = np.maximum(lefts[:index], np.minimum(frame, rights[:index] - 1))
        cell = offsets[:index] + frame - lefts[:index]
        fraction = np.clip((cut - times[frame]) / steps[frame], 0, 1)
        prefix = packed[cell] + fraction * (packed[cell + 1] - packed[cell])
        lost = totals[:index] - prefix
        gap = np.maximum(0, starts[index] - ends[:index])
        jump = np.abs(pitches[:index] - pitches[index])
        transition = (0.025 * np.minimum(jump, 24) * (1 - 0.55 * onsets[index])
                      / (1 + gap) + 0.06 * (gap > 0.2))
        choices = best[:index] - lost - transition
        choices[~valid] = -np.inf
        if index and choices.max() > 0:
            previous = int(choices.argmax())
            best[index] += choices[previous]
            back[index] = previous
        if index % 64 == 0:
            progress(0.35 + 0.65 * (index + 1) / len(notes))
    if not np.isfinite(best).any() or best.max() <= 0:
        return (), centers
    selected = []
    index = int(best.argmax())
    while index >= 0:
        _cancel(stop)
        selected.append(notes[index])
        index = back[index]
    selected.reverse()
    continuations = {}
    for candidate_index, note in enumerate(notes):
        _cancel(stop)
        continuations[note.pitch, round(note.start, 8)] = candidate_index
    result = []
    for index, note in enumerate(selected):
        _cancel(stop)
        end = note.start + note.duration
        if index + 1 < len(selected):
            end = min(end, selected[index + 1].start)
        result.append(Note(note.start, end - note.start, note.pitch))
        if index + 1 == len(selected):
            continue
        next_start = selected[index + 1].start
        continuation = continuations.get((note.pitch, round(end, 8)))
        if continuation is None or next_start - end < 0.075:
            continue
        fragment = notes[continuation]
        if (not math.isclose(fragment.start, end, rel_tol=0, abs_tol=1e-8)
                or fragment.start + fragment.duration < next_start - 1e-8):
            continue
        lo, hi = lefts[continuation], rights[continuation]
        prefix = np.interp(next_start, edges[lo:hi + 1],
                           packed[offsets[continuation]:offsets[continuation + 1]])
        # Onset decoding cut the held voice at this validated reattack. A short
        # prefix can lose to the DP's entry cost; recover only its evidenced gap,
        # without changing the selected pitch path or filling a genuine rest.
        if prefix > 0:
            result.append(Note(fragment.start, next_start - fragment.start, fragment.pitch))
    progress(1.0)
    _cancel(stop)
    return tuple(result), centers


def _select_chords(candidates, stop=None, *, lead=()):
    """Keep the lead intact; add only independently evidenced supporting notes."""
    notes = list(lead)
    by_pitch = {}
    for note in lead:
        _cancel(stop)
        by_pitch.setdefault(note.pitch, []).append(note)
    ends = {pitch: [n.start + n.duration for n in voice] for pitch, voice in by_pitch.items()}
    for candidate in candidates:
        _cancel(stop)
        if candidate.confidence >= 0.65 and candidate.onset >= 0.65 and candidate.support >= 0.8:
            voice = by_pitch.get(candidate.pitch, ())
            index = bisect_right(ends.get(candidate.pitch, ()), candidate.start + 1e-8)
            if index < len(voice) and voice[index].start < candidate.start + candidate.duration - 1e-8:
                continue  # Do not duplicate or lengthen a selected same-pitch lead.
            notes.append(Note(candidate.start, candidate.duration, candidate.pitch, 1))
            if len(notes) > MAX_NOTES:
                raise ScoreError("Too many polyphonic notes")
    return tuple(sorted(notes, key=lambda n: (n.start, n.pitch)))


def analyze_instrument(path, stop=None, progress=lambda value: None, register="auto", *, texture="melody"):
    """Return a lead (default) or opt-in polyphony plus validated candidates.

    stop is a threading.Event-compatible object. progress receives monotonic
    fractions in [0, 1]. Missing dependencies/model and no reliable lead raise
    ScoreError; cancellation raises the existing music_audio.AudioCancelled.
    Audio duration remains the full source duration, including silent tails.
    Chord mode preserves the lead and adds supporting candidates through
    stricter evidence gates. It is not source separation.
    """
    _validate_register(register)
    if texture not in ("melody", "chords"):
        raise ScoreError("Unknown transcription texture")
    _cancel(stop)
    progress(0.0)
    session = _load_model()
    _cancel(stop)
    data, duration = _read_audio(Path(path), stop, progress)
    output = _infer(data, session, stop, progress)
    del output["contour"]  # Validated by inference; this revision uses note/onset only.
    raw = _decode_candidates(output, duration, stop, lambda v: progress(0.73 + 0.04 * v))
    trusted_raw = [note for note in raw if note.confidence >= 0.45 and note.support >= 0.65]
    # Reproduce accepted E: register is anchored BEFORE acoustic onset cleanup.
    centers = _register_curve(trusted_raw, duration, register, stop) if trusted_raw else ()
    envelopes, peaks = _acoustic_evidence(data, output, stop, lambda v: progress(0.77 + 0.15 * v))
    candidates = _decode_candidates(output, duration, stop, lambda v: progress(0.92 + 0.02 * v),
                                    peak_indices=peaks)
    notes, centers = _select_lead(candidates, duration, register, stop,
                                  lambda v: progress(0.94 + 0.059 * v), output=output,
                                  envelopes=envelopes, fixed_centers=centers)
    if texture == "chords":
        notes = _select_chords(candidates, stop, lead=notes)
    _cancel(stop)
    if not notes:
        raise ScoreError("No reliable instrument notes detected by Basic Pitch")
    tracks = ((0, "Basic Pitch instrument lead"),)
    if texture == "chords":
        tracks += ((1, "Basic Pitch supporting chord notes"),)
    score = Score(Path(path).stem, notes, duration, tracks)
    progress(1.0)
    _cancel(stop)
    return NeuralTranscription(score, candidates, register, centers, texture)


def transcribe_instrument(path, stop=None, progress=lambda value: None, register="auto", *, texture="melody") -> Score:
    """Transcribe original instrumental pitches without nine-key adaptation."""
    return analyze_instrument(path, stop, progress, register, texture=texture).score


def export_diagnostics(result, directory):
    """Write new, uniquely named lead/candidate MIDI and UTF-8 JSON; no playback.

    mido is needed only for this optional export, not for ONNX transcription.
    Separate MIDI files deliberately avoid presenting all candidates as a lead.
    """
    try:
        import mido
    except ImportError as exc:
        raise ScoreError("Diagnostic MIDI export requires mido") from exc
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    stem = f"neural-{stamp}-{uuid.uuid4().hex[:12]}-{result.register}"
    paths = {}
    for label, notes in (("polyphonic", result.candidates), ("lead", result.score.notes)):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = mido.MidiTrack()
        midi.tracks.append(track)
        track.append(mido.MetaMessage("set_tempo", tempo=1000000))
        track.append(mido.MetaMessage("track_name", name=f"Basic Pitch {label}"))
        events = []
        for note in notes:
            velocity = max(1, min(127, round(getattr(note, "confidence", 0.8) * 127)))
            events.append((round(note.start * 1000), True, note.pitch, velocity))
            events.append((round((note.start + note.duration) * 1000), False, note.pitch, 0))
        last = 0
        for tick, down, pitch, velocity in sorted(events):
            track.append(mido.Message("note_on" if down else "note_off", note=pitch,
                                      velocity=velocity, time=tick - last))
            last = tick
        track.append(mido.MetaMessage("end_of_track", time=max(0, round(result.score.duration * 1000) - last)))
        path = directory / f"{stem}-{label}.mid"
        with path.open("xb") as handle:
            midi.save(file=handle)
        paths[label] = path
    document = asdict(result)
    document["model_sha256"] = MODEL_SHA256
    document["transcription_revision"] = (CHORD_TRANSCRIPTION_REVISION if result.texture == "chords"
                                           else TRANSCRIPTION_REVISION)
    document["warning"] = "No real-song ground truth; note counts are not accuracy. No instrument separation."
    path = directory / f"{stem}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
    paths["json"] = path
    return paths
