"""Score import and compilation. This module never sends keyboard input."""

from collections import defaultdict, deque
from dataclasses import dataclass
from fractions import Fraction
import math
from pathlib import Path
import re


DEFAULT_MAPPING = {
    57: "B", 64: "F", 65: "G", 67: "H", 69: "J",
    71: "K", 72: "T", 74: "Y", 76: "U",
}
DEGREES = (0, 2, 4, 5, 7, 9, 11)
PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MAX_NOTES = 100_000
DEMO_SCORE = """// 小星星 · 适配截图音域，1=C，每个数字默认一拍
// ' 表示高八度，, 表示低八度，:2 表示两拍
5 5 2' 2' 3' 3' 2':2 |
1' 1' 7 7 6 6 5:2 |
2' 2' 1' 1' 7 7 6:2 |
2' 2' 1' 1' 7 7 6:2 |
5 5 2' 2' 3' 3' 2':2 |
1' 1' 7 7 6 6 5:2
"""


class ScoreError(ValueError):
    pass


@dataclass(frozen=True)
class Note:
    start: float
    duration: float
    pitch: int
    track: int = 0


@dataclass(frozen=True)
class Score:
    title: str
    notes: tuple[Note, ...]
    duration: float
    tracks: tuple[tuple[int, str], ...] = ((0, "文本简谱"),)


@dataclass(frozen=True)
class KeyEvent:
    time: float
    key: str
    down: bool


@dataclass(frozen=True)
class PlaybackPlan:
    events: tuple[KeyEvent, ...]
    duration: float
    note_count: int
    skipped: int
    preview: tuple[str, ...]


def pitch_name(pitch):
    return f"{PITCH_NAMES[pitch % 12]}{pitch // 12 - 1}"


def parse_pitch(value):
    match = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d)", value.strip())
    if not match:
        raise ScoreError(f"无效音高 {value!r}，请使用 C4、F#4、Bb3 等格式")
    letter, accidental, octave = match.groups()
    pitch = (int(octave) + 1) * 12 + {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter.upper()]
    pitch += {"": 0, "#": 1, "b": -1}[accidental]
    if not 0 <= pitch <= 127:
        raise ScoreError(f"音高超出 MIDI 范围: {value}")
    return pitch


def _degree_pitch(token, octave_number):
    match = re.fullmatch(r"([#b]?)([1-7])([',]*)", token)
    if not match:
        raise ScoreError(f"无法识别音符 {token!r}；音符之间请用空格分隔")
    accidental, degree, octave = match.groups()
    pitch = (octave_number + 1) * 12 + DEGREES[int(degree) - 1] + 12 * (octave.count("'") - octave.count(","))
    pitch += {"": 0, "#": 1, "b": -1}[accidental]
    if not 0 <= pitch <= 127:
        raise ScoreError(f"音符超出 MIDI 范围: {token}")
    return pitch


def parse_text(text, bpm=120, title="文本简谱", octave=4):
    if not math.isfinite(bpm) or not 20 <= bpm <= 300:
        raise ScoreError("BPM 必须在 20～300 之间")
    if not isinstance(octave, int) or not 0 <= octave <= 8:
        raise ScoreError("简谱基准八度必须是 0～8 的整数")
    beat_seconds = 60.0 / bpm
    cursor = 0.0
    notes = []
    previous_indices = None
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.split("//", 1)[0].replace("’", "'").replace("，", ",")
        tokens = re.findall(r"\[[^\[\]\r\n]+\](?::[^\s|]+)?|[^\s|]+", line)
        for token in tokens:
            try:
                parts = token.rsplit(":", 1)
                beats = float(Fraction(parts[1])) if len(parts) == 2 else 1.0
                if not math.isfinite(beats) or not 1 / 64 <= beats <= 256:
                    raise ScoreError("时值必须在 1/64～256 拍之间")
                duration = beats * beat_seconds
                symbol = parts[0]
                if symbol == "-":
                    if previous_indices is None:
                        raise ScoreError("延音 - 前面必须有音符或休止符")
                    for index in previous_indices:
                        note = notes[index]
                        notes[index] = Note(note.start, note.duration + duration, note.pitch)
                else:
                    previous_indices = []
                    if symbol != "0":
                        pitches = symbol[1:-1].split() if symbol.startswith("[") and symbol.endswith("]") else [symbol]
                        if not pitches:
                            raise ScoreError("和弦不能为空")
                        for pitch in dict.fromkeys(_degree_pitch(item, octave) for item in pitches):
                            previous_indices.append(len(notes))
                            notes.append(Note(cursor, duration, pitch))
                cursor += duration
                if len(notes) > MAX_NOTES:
                    raise ScoreError(f"乐谱最多支持 {MAX_NOTES} 个音符")
            except (ValueError, ZeroDivisionError) as exc:
                raise ScoreError(f"第 {line_number} 行，{token!r}: {exc}") from exc
    if not notes:
        raise ScoreError("乐谱中没有音符")
    return Score(title, tuple(notes), cursor)


def load_midi(path):
    try:
        import mido
    except ImportError as exc:
        raise ScoreError("缺少 MIDI 依赖，请先运行 uv sync") from exc
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ScoreError("MIDI 文件不能超过 16 MB")
    try:
        midi = mido.MidiFile(path)
    except (OSError, ValueError, EOFError, KeyError) as exc:
        raise ScoreError(f"无法读取 MIDI: {exc}") from exc
    if midi.type == 2 or midi.ticks_per_beat <= 0:
        raise ScoreError("仅支持使用 PPQ 时间格式的 MIDI type 0/1")

    timeline = []
    names = {}
    for track_id, track in enumerate(midi.tracks):
        tick = 0
        names[track_id] = track.name or f"音轨 {track_id + 1}"
        for order, message in enumerate(track):
            tick += message.time
            timeline.append((tick, track_id, order, message))
    timeline.sort(key=lambda event: event[:3])

    active = defaultdict(deque)
    notes = []
    seconds = 0.0
    last_tick = 0
    tempo = 500_000
    for tick, track_id, _, message in timeline:
        seconds += mido.tick2second(tick - last_tick, midi.ticks_per_beat, tempo)
        last_tick = tick
        if message.type == "set_tempo":
            if message.tempo <= 0:
                raise ScoreError("MIDI 包含无效速度")
            tempo = message.tempo
        if message.type not in ("note_on", "note_off") or message.channel == 9:
            continue
        identity = (track_id, message.channel, message.note)
        if message.type == "note_on" and message.velocity > 0:
            active[identity].append(seconds)
        elif active[identity]:
            start = active[identity].popleft()
            notes.append(Note(start, max(0.001, seconds - start), message.note, track_id))
        if len(notes) > MAX_NOTES:
            raise ScoreError(f"乐谱最多支持 {MAX_NOTES} 个音符")
    for (track_id, _, pitch), starts in active.items():
        for start in starts:
            notes.append(Note(start, max(0.08, seconds - start), pitch, track_id))
    if not notes:
        raise ScoreError("MIDI 中没有可演奏音符（打击乐通道已忽略）")
    if len(notes) > MAX_NOTES:
        raise ScoreError(f"乐谱最多支持 {MAX_NOTES} 个音符")
    notes.sort(key=lambda note: (note.start, note.pitch, note.track))
    track_ids = sorted({note.track for note in notes})
    return Score(path.stem, tuple(notes), max(seconds, max(note.start + note.duration for note in notes)),
                 tuple((track_id, names[track_id]) for track_id in track_ids))


def validate_mapping(mapping):
    if not mapping:
        raise ScoreError("请至少配置一个音符")
    normalized = {}
    for pitch, key in mapping.items():
        if not isinstance(pitch, int) or not 0 <= pitch <= 127:
            raise ScoreError(f"无效 MIDI 音高: {pitch}")
        key = key.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]", key):
            raise ScoreError("演奏键位必须是单个英文字母或数字")
        if key in normalized.values():
            raise ScoreError(f"按键 {key} 重复绑定，请为每个音符使用独立按键")
        normalized[pitch] = key
    return normalized


def score_to_text(score, bpm=120, octave=4):
    """Convert a monophonic transcription to editable, explicitly timed numbered notation."""
    symbols = ("1", "#1", "2", "#2", "3", "4", "#4", "5", "#5", "6", "#6", "7")
    tokens = ["// 音频识别结果，请校对；1=C；时值以当前 BPM 为基准\n"]
    cursor = 0.0
    for note in sorted(score.notes, key=lambda item: item.start):
        if note.start < cursor - 0.001:
            raise ScoreError("多声部乐谱不能直接转为单旋律简谱，请先选择单独音轨")
        rest = (note.start - cursor) * bpm / 60
        while rest >= 1 / 64:
            part = min(rest, 256)
            tokens.append(f"0:{part:.6f}")
            rest -= part
        shift = note.pitch // 12 - (octave + 1)
        symbol = symbols[note.pitch % 12] + "'" * max(0, shift) + "," * max(0, -shift)
        beats = note.duration * bpm / 60
        if not 1 / 64 <= beats <= 256:
            raise ScoreError("音符时值超出文本简谱范围，请使用 MIDI")
        tokens.append(f"{symbol}:{beats:.6f}")
        cursor = note.start + note.duration
    return " ".join(tokens) + "\n"


def compile_score(score, mapping=None, speed=1.0, transpose=0, hold_ms=60, track=None, skip_missing=False):
    mapping = validate_mapping(DEFAULT_MAPPING if mapping is None else mapping)
    if not math.isfinite(speed) or not 0.25 <= speed <= 4:
        raise ScoreError("速度倍率必须在 0.25～4 之间")
    if not isinstance(transpose, int) or not -48 <= transpose <= 48:
        raise ScoreError("移调必须为 -48～48 之间的整数（半音）")
    if not math.isfinite(hold_ms) or not 10 <= hold_ms <= 150:
        raise ScoreError("按键保持时间必须在 10～150 毫秒之间")
    selected = [note for note in score.notes if track is None or note.track == track]
    if not selected:
        raise ScoreError("选中的音轨没有音符")
    missing = [note for note in selected if note.pitch + transpose not in mapping]
    if missing and not skip_missing:
        names = ", ".join(pitch_name(pitch) for pitch in sorted({note.pitch + transpose for note in missing}))
        raise ScoreError(f"有 {len(missing)} 个音符不在键位映射中: {names}。请移调、调整键位或选择跳过。")

    by_key = defaultdict(dict)
    preview = []
    for note in selected:
        pitch = note.pitch + transpose
        if pitch not in mapping:
            continue
        key = mapping[pitch]
        start = note.start / speed
        duration = min(hold_ms / 1000.0, note.duration / speed * 0.8)
        by_key[key][start] = max(duration, by_key[key].get(start, 0))
        if len(preview) < 120:
            preview.append(f"{start:7.2f}s  {pitch_name(pitch):4s} → {key}")
    if not by_key:
        raise ScoreError("没有可演奏的音符，请调整音轨、移调或键位")
    events = []
    for key, hits in by_key.items():
        starts = sorted(hits)
        for index, start in enumerate(starts):
            duration = hits[start]
            if index + 1 < len(starts):
                duration = min(duration, (starts[index + 1] - start) * 0.8)
            events.extend((KeyEvent(start, key, True), KeyEvent(start + duration, key, False)))
    events.sort(key=lambda event: (event.time, event.down, event.key))
    return PlaybackPlan(tuple(events), max(score.duration / speed, events[-1].time),
                        len(events) // 2, len(missing), tuple(preview))
