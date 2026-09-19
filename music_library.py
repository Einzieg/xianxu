"""Persistent source/arranged scores and deterministic playlist navigation."""

from datetime import datetime, timezone
from copy import deepcopy
import json
import math
from pathlib import Path
import random
import uuid

from music_adapt import adapt_score
from music_score import MAX_NOTES, Note, Score, ScoreError, parse_pitch, validate_mapping


MODES = ("order", "random", "repeat_all", "repeat_one")
CALIBRATED_MAPPING = {45: "B", 52: "F", 53: "G", 55: "H", 57: "J", 59: "K", 60: "T", 62: "Y", 64: "U"}


def encode_score(score):
    return {"title": score.title, "duration": score.duration, "tracks": score.tracks,
            "notes": [[n.start, n.duration, n.pitch, n.track] for n in score.notes]}


def decode_score(value):
    notes = tuple(Note(*row) for row in value["notes"])
    duration = float(value["duration"])
    if not notes or len(notes) > MAX_NOTES or not math.isfinite(duration) or duration <= 0:
        raise ScoreError("谱库包含无效乐谱")
    if any(not math.isfinite(n.start) or not math.isfinite(n.duration) or n.start < 0 or n.duration <= 0
           or n.start + n.duration > duration + 0.01 or type(n.pitch) is not int or not 0 <= n.pitch <= 127
           for n in notes):
        raise ScoreError("谱库包含无效音符")
    return Score(str(value["title"]), notes, duration, tuple(tuple(t) for t in value["tracks"]))


def mapping_from_settings(settings):
    rows = settings["mapping"]
    if len(rows) != 9 or len({row["pitch"] for row in rows}) != 9:
        raise ScoreError("请配置9个互不重复的音高与按键")
    return validate_mapping({row["pitch"]: row["key"] for row in rows})


def default_settings(legacy_path):
    settings = {"speed": 1.0, "hold": 60.0, "delay": 3.0, "backend": "DD", "octave": 3, "bpm": 120.0,
                "mapping": [{"pitch": p, "key": k} for p, k in CALIBRATED_MAPPING.items()]}
    if legacy_path and Path(legacy_path).exists():
        value = json.loads(Path(legacy_path).read_text(encoding="utf-8"))
        settings["mapping"] = [{"pitch": parse_pitch(p), "key": k} for p, k in value["mapping"]]
        mapping_from_settings(settings)
    return settings


class ScoreLibrary:
    def __init__(self, directory, legacy_path=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "library.json"
        self.items = []
        self.settings = default_settings(legacy_path)
        self.mode = "order"
        self.current_id = None
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data["version"] != 1:
                    raise ValueError("不支持的谱库版本")
                self.items = data["items"]
                self.settings.update(data["settings"])
                mapping_from_settings(self.settings)
                ids = set()
                for item in self.items:
                    if item["id"] in ids:
                        raise ValueError("重复谱子 ID")
                    ids.add(item["id"])
                    decode_score(item["original"])
                    decode_score(item["score"])
                self.mode = data["mode"] if data["mode"] in MODES else "order"
                self.current_id = data["current_id"] if data["current_id"] in ids else None
            except (ValueError, KeyError, TypeError) as exc:
                raise ScoreError(f"谱库无法读取，原文件未覆盖：{self.path}：{exc}") from exc

    def save(self):
        data = {"version": 1, "items": self.items, "settings": self.settings,
                "mode": self.mode, "current_id": self.current_id}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def get(self, item_id):
        for item in self.items:
            if item["id"] == item_id:
                return item
        raise ScoreError("谱子已不存在，请重新选择")

    @staticmethod
    def describe(item):
        return {**{key: item[key] for key in ("id", "title", "duration", "note_count", "source", "created_at",
                                             "summary", "original_count")},
                "transcription": item.get("transcription")}

    def arrange(self, source, source_path, stop=None, *, texture="melody", melody_track=None, sparse_config=None):
        mapping = mapping_from_settings(self.settings)
        adapted = adapt_score(source, mapping, stop=stop, texture=texture, melody_track=melody_track,
                              sparse_config=sparse_config)
        return {"id": uuid.uuid4().hex, "title": source.title, "duration": adapted.score.duration,
                "note_count": len(adapted.score.notes), "original_count": len(source.notes),
                "source": str(source_path), "created_at": datetime.now(timezone.utc).isoformat(),
                "summary": adapted.summary, "mapping": self.settings["mapping"], "texture": texture,
                "original": encode_score(source), "score": encode_score(adapted.score),
                **({"melody_track": adapted.selected_track} if texture in ("melody_chords", "melody_bass") else {}),
                **({"sparse_config": deepcopy(sparse_config)} if texture == "melody_bass" else {})}

    def add(self, item):
        previous_id = self.current_id
        self.items.append(item)
        self.current_id = item["id"]
        try:
            self.save()
        except OSError:
            self.items.pop()
            self.current_id = previous_id
            raise
        return self.describe(item)

    def remove(self, item_id):
        item = self.get(item_id)
        index = self.items.index(item)
        previous_id = self.current_id
        self.items.pop(index)
        if self.current_id == item_id:
            self.current_id = self.items[0]["id"] if self.items else None
        try:
            self.save()
        except (OSError, ValueError, TypeError):
            self.items.insert(index, item)
            self.current_id = previous_id
            raise


class PlaylistNavigator:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.bag = []
        self.history = []

    def reset(self):
        self.bag.clear()
        self.history.clear()

    def next(self, ids, current, mode, *, automatic=True):
        if not ids:
            return None
        if current not in ids:
            return ids[0]
        if automatic and mode == "repeat_one":
            return current
        if mode == "random":
            self.bag = [item for item in self.bag if item in ids and item != current]
            if not self.bag:
                self.bag = [item for item in ids if item != current]
                self.rng.shuffle(self.bag)
            result = self.bag.pop() if self.bag else current
        else:
            index = ids.index(current) + 1
            result = ids[index] if index < len(ids) else (None if mode == "order" else ids[0])
        if result is not None:
            self.history.append(current)
            self.history = self.history[-1000:]
        return result

    def previous(self, ids, current, mode):
        if not ids:
            return None
        if mode == "random":
            while self.history:
                item = self.history.pop()
                if item in ids:
                    return item
        index = ids.index(current) if current in ids else 0
        return ids[index - 1] if index else (ids[0] if mode == "order" else ids[-1])
