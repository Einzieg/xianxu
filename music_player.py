"""Instrument player UI; input is enabled only after explicit start and target selection."""

from datetime import datetime
import json
from pathlib import Path
import queue
import threading
from tkinter import filedialog

import customtkinter as ctk
from pynput import keyboard

from music_input import KeyboardSink, PreviewSink, TargetWindow
from music_playback import ScorePlayer
from music_adapt import POLICIES, adapt_score
from music_score import (DEFAULT_MAPPING, DEMO_SCORE, ScoreError, compile_score,
                         load_midi, parse_pitch, parse_text, pitch_name, score_to_text, validate_mapping)

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "music_settings.json"
FONT = ("Microsoft YaHei UI", 13)
MUTED = ("#566477", "#a1afbf")
ACCENT = "#247e89"


class MusicPlayerPanel(ctk.CTkFrame):
    def __init__(self, master, backend_factory, user32, *, listen=True):
        super().__init__(master, fg_color="transparent")
        self.backend_factory = backend_factory
        self.backends = {}
        self.user32 = user32
        self.player = ScorePlayer()
        self.events = queue.Queue()
        self.job = None
        self.job_pending = False
        self.calibration_sink = None
        self.cancel_job = threading.Event()
        self.target = None
        self.score = None
        self.source_name = "小星星 · 示例"
        self.text_mode = True
        self.track_ids = {"全部旋律音轨": None}
        self.output_ids = {"系统默认输出": None}
        self.calibration = []
        self.calibration_source = ""
        self.audio_path = None
        self.pending_adaptation = None
        self.adaptation_original = None
        self.adaptation_mapping = None
        self.capture_timer = None
        self.closed = False
        self.closing = False
        self.hotkeys_down = set()
        self.last_status = None
        self.last_busy = None
        self.editable_widgets = []
        self.mapping_rows = []
        self.key_tiles = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build_ui()
        self._load_settings()
        self.listener = None
        if listen:
            self.listener = keyboard.Listener(on_press=self._key_press, on_release=self._key_release)
            self.listener.start()
        self.poll_timer = self.after(40, self._poll)

    @property
    def busy(self):
        return self.player.running or self.job_pending or (self.job is not None and self.job.is_alive())

    def _label(self, parent, text, **options):
        return ctk.CTkLabel(parent, text=text, font=FONT, **options)

    def _button(self, parent, text, command, *, editable=True, **options):
        widget = ctk.CTkButton(parent, text=text, command=command, font=FONT, **options)
        if editable:
            self.editable_widgets.append(widget)
        return widget

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 6))
        ctk.CTkLabel(header, text="洛克 · 乐谱演奏", font=("Microsoft YaHei UI", 26, "bold")).pack(side="left")
        self._label(header, "F8 开始  /  F9 暂停或继续  /  F10 停止", text_color=MUTED).pack(side="right")
        self._label(self, "先确认乐器音高，再导入或转换旋律。演奏仅在选中的游戏窗口位于前台时进行。",
                    anchor="w", text_color=MUTED).grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 10))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=16)
        body.grid_columnconfigure(0, weight=3, minsize=450)
        body.grid_columnconfigure(1, weight=2, minsize=320)
        body.grid_rowconfigure(0, weight=1)
        self.tabs = ctk.CTkTabview(body)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self._build_score_tab(self.tabs.add("乐谱"))
        self._build_audio_tab(self.tabs.add("音频转谱"))
        self._build_adaptation_tab(self.tabs.add("乐器适配"))
        self._build_calibration_tab(self.tabs.add("音高校准"))
        settings = ctk.CTkScrollableFrame(body, label_text="演奏设置 / 键位映射", label_font=FONT)
        settings.grid(row=0, column=1, sticky="nsew", pady=(6, 0))
        settings.grid_columnconfigure(1, weight=1)
        self._build_settings(settings)

        self.summary = self._label(self, "已载入小星星示例；音高校准状态见右侧。", anchor="w", justify="left",
                                   wraplength=780, text_color=MUTED)
        self.summary.grid(row=3, column=0, sticky="ew", padx=24, pady=(10, 4))
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=4, column=0, sticky="ew", padx=22, pady=6)
        controls.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self._button(controls, "检查 / 无按键预览", lambda: self.start(preview=True), fg_color=ACCENT,
                     height=40).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._button(controls, "开始演奏 · F8", self.start, height=40).grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.pause_button = self._button(controls, "暂停 / 继续 · F9", self.player.toggle_pause, editable=False, height=40)
        self.pause_button.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        self.stop_button = self._button(controls, "停止 · F10", self.stop, editable=False, fg_color="#a44141",
                                        hover_color="#853232", height=40)
        self.stop_button.grid(row=0, column=3, sticky="ew")
        self.progress = ctk.CTkProgressBar(self, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.grid(row=5, column=0, sticky="ew", padx=24, pady=(4, 4))
        self.status = self._label(self, "就绪 · 预览只显示节奏，不发送按键", anchor="w", justify="left", wraplength=780)
        self.status.grid(row=6, column=0, sticky="ew", padx=24, pady=(4, 16))

    def _build_score_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)
        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        for index, (title, callback) in enumerate((("导入 TXT / MIDI", self.import_score),
                                                    ("示例", self.load_demo), ("导出 MIDI", self.save_midi),
                                                    ("适配", self.analyze_adaptation))):
            self._button(toolbar, title, callback, width=64 if title == "适配" else 100).grid(row=0, column=index, padx=(0, 8))
        self.track_menu = ctk.CTkOptionMenu(tab, values=list(self.track_ids), font=FONT)
        self.track_menu.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.editable_widgets.append(self.track_menu)
        self.editor = ctk.CTkTextbox(tab, font=("Consolas", 15), wrap="word", undo=True, height=150)
        self.editor.grid(row=2, column=0, sticky="nsew", padx=8)
        self.editor.insert("1.0", DEMO_SCORE)
        self._label(tab, "文本简谱：1=C（八度见右侧）；0 休止；1' 高八度；6, 低八度；\n"
                         "5:2 两拍；5:1/2 半拍；[3 5 7] 和弦；- 延音；| 小节；// 注释。",
                    justify="left", anchor="w", wraplength=395, text_color=MUTED).grid(row=3, column=0, sticky="ew", padx=8, pady=8)
        keyboard_view = ctk.CTkFrame(tab)
        keyboard_view.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 8))
        for index, (pitch, key) in enumerate(DEFAULT_MAPPING.items()):
            keyboard_view.grid_columnconfigure(index, weight=1)
            tile = ctk.CTkLabel(keyboard_view, text=f"{key}\n{pitch_name(pitch)}", font=FONT, corner_radius=6,
                                fg_color=("#d9e1ea", "#293749"), height=52, width=32)
            tile.grid(row=0, column=index, sticky="ew", padx=3, pady=8)
            self.key_tiles.append(tile)

    def _build_audio_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        self._label(tab, "没有谱子？先把一段旋律转换为可编辑简谱。", anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=16)
        self._label(tab, "支持 WAV / FLAC / OGG / MP3，最长 5 分钟。\n"
                         "适合单乐器独奏、哼唱或已经分离的主旋律。\n"
                         "完整歌曲的伴奏、鼓声和人声混合会影响识别；本版本不做音源分离。\n"
                         "识别后自动分析当前9键：移调、八度折叠及缺失音替代。\n"
                         "在“乐器适配”页比较结果，确认后应用；原谱保留。",
                    justify="left", anchor="nw", wraplength=430, text_color=MUTED).grid(row=1, column=0, sticky="ew", padx=14, pady=8)
        self.audio_label = self._label(tab, "尚未选择音频", anchor="w", wraplength=430)
        self.audio_label.grid(row=2, column=0, sticky="ew", padx=14, pady=16)
        self._button(tab, "选择音频并适配转谱", self.convert_audio, fg_color=ACCENT).grid(row=3, column=0, sticky="ew", padx=14, pady=8)
        self._label(tab, "可以先用“乐谱”里的小星星示例测试按键与节奏。\n"
                         "无按键预览只显示节奏；它不会发声，也不会操作游戏。",
                    justify="left", anchor="w", text_color=MUTED).grid(row=4, column=0, sticky="ew", padx=14, pady=16)

    def _build_adaptation_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(4, weight=1)
        self._label(tab, "把旋律编配到当前键位，保留原有节奏与休止。", anchor="w").grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        self.adaptation_policy = ctk.CTkOptionMenu(tab, values=list(POLICIES), font=FONT)
        self.adaptation_policy.grid(row=1, column=0, sticky="ew", padx=12, pady=5)
        self.editable_widgets.append(self.adaptation_policy)
        self._button(tab, "分析当前乐谱 / 重新适配", self.analyze_adaptation, fg_color=ACCENT).grid(row=2, column=0, sticky="ew", padx=12, pady=5)
        self.adaptation_summary = self._label(tab, "尚未分析。音频转谱会自动生成适配候选。", justify="left",
                                               anchor="w", wraplength=380, text_color=MUTED)
        self.adaptation_summary.grid(row=3, column=0, sticky="ew", padx=12, pady=8)
        self.adaptation_report = ctk.CTkTextbox(tab, font=("Consolas", 13), height=120, state="disabled", wrap="word")
        self.adaptation_report.grid(row=4, column=0, sticky="nsew", padx=12, pady=4)
        actions = ctk.CTkFrame(tab, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="ew", padx=12, pady=10)
        actions.grid_columnconfigure((0, 1), weight=1)
        self._button(actions, "应用适配谱", self.apply_adaptation).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._button(actions, "恢复原谱", self.restore_adaptation_source).grid(row=0, column=1, sticky="ew")
        self._button(actions, "将适配谱转为可编辑简谱", self.edit_adaptation).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _clear_adaptation(self):
        self.pending_adaptation = None
        self.adaptation_original = None
        self.adaptation_mapping = None
        self.adaptation_summary.configure(text="尚未分析。音频转谱会自动生成适配候选。")
        self.adaptation_report.configure(state="normal")
        self.adaptation_report.delete("1.0", "end")
        self.adaptation_report.configure(state="disabled")

    def _show_adaptation(self, result, mapping):
        self.pending_adaptation = result
        self.adaptation_original = result.source
        self.adaptation_mapping = dict(mapping)
        self.adaptation_summary.configure(text=result.summary + f"\n选用：{result.track_name}；全部输出音符可由当前键位演奏。")
        lines = ["时间       原音 → 输出音 / 按键", "比较的是实际音高；全局移调也会体现在输出中。", ""]
        for before, after in result.pairs[:200]:
            lines.append(f"{before.start:7.2f}s  {pitch_name(before.pitch):4s} → {pitch_name(after.pitch):4s} / {mapping[after.pitch]}")
        if len(result.pairs) > 200:
            lines.append(f"… 仅显示前200音，共 {len(result.pairs)} 音。")
        self.adaptation_report.configure(state="normal")
        self.adaptation_report.delete("1.0", "end")
        self.adaptation_report.insert("1.0", "\n".join(lines))
        self.adaptation_report.configure(state="disabled")
        self.tabs.set("乐器适配")
        self.message("适配分析完成。点击“应用适配谱”后可预览或演奏；替代音可能改变旋律，请比较结果。")

    def analyze_adaptation(self):
        if self.busy or self.capture_timer:
            return
        try:
            source = self.current_score()
            # Re-running a strategy on an unedited applied score should use the original, not quantize it again.
            if self.pending_adaptation is not None and source is self.pending_adaptation.score:
                source = self.adaptation_original
                track = self.pending_adaptation.selected_track
            else:
                track = self.track_ids.get(self.track_menu.get())
            mapping = self.read_mapping()
            policy = self.adaptation_policy.get()
            self.message("正在按当前键位分析旋律，F10 可取消…")
            self._job(lambda: adapt_score(source, mapping, policy, track, self.cancel_job),
                      lambda result: self._show_adaptation(result, mapping))
        except (ValueError, RuntimeError) as exc:
            self.message(str(exc), True)

    def apply_adaptation(self):
        if self.busy:
            return
        try:
            if self.pending_adaptation is None:
                raise ScoreError("请先分析乐谱")
            if self.read_mapping() != self.adaptation_mapping:
                raise ScoreError("键位已改变，请重新分析适配")
            self._set_midi(self.pending_adaptation.score, keep_adaptation=True, kind="适配乐谱")
            self.fields["transpose"].configure(state="normal")
            self.fields["transpose"].delete(0, "end")
            self.fields["transpose"].insert(0, "0")
            self.missing_menu.set("缺失音符：阻止演奏")
            self.summary.configure(text=self.pending_adaptation.summary)
            self.message("已应用适配谱（已包含移调），原谱可在“乐器适配”页恢复。")
        except ValueError as exc:
            self.message(str(exc), True)

    def restore_adaptation_source(self):
        if not self.busy and self.adaptation_original is not None:
            self._set_midi(self.adaptation_original, keep_adaptation=True, kind="原始乐谱")
            self.message("已恢复原始音高与时间轴；适配候选仍保留。")

    def edit_adaptation(self):
        if self.busy:
            return
        try:
            if self.pending_adaptation is None:
                raise ScoreError("请先分析乐谱")
            if self.read_mapping() != self.adaptation_mapping:
                raise ScoreError("键位已改变，请重新分析适配")
            score = self.pending_adaptation.score
            text = score_to_text(score, bpm=float(self.fields["bpm"].get()), octave=int(self.fields["octave"].get()))
            self._set_text(text, score.title, keep_adaptation=True)
            self.fields["transpose"].delete(0, "end")
            self.fields["transpose"].insert(0, "0")
            self.message("已转为可编辑简谱；微小时值可能受文本格式精度影响，原始时间轴仍在适配候选中。")
        except ValueError as exc:
            self.message(str(exc), True)

    def _build_calibration_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(4, weight=1)
        self._label(tab, "录下每个演奏键的声音，测量实际音高。", anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=12)
        self._label(tab, "先捕获游戏窗口，打开乐器界面。把背景音乐设为 0，保留乐器音效，\n"
                         "暂停其他声音。校准会依次短按右侧的 9 个按键。\n"
                         "切出游戏或按 F10 会中止；原始录音保存在 artifacts/calibration。",
                    justify="left", anchor="w", wraplength=450, text_color=MUTED).grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        self.output_menu = ctk.CTkOptionMenu(tab, values=list(self.output_ids), font=FONT)
        self.output_menu.grid(row=2, column=0, sticky="ew", padx=14, pady=4)
        self.editable_widgets.append(self.output_menu)
        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.grid(row=3, column=0, sticky="ew", padx=14, pady=8)
        self._button(toolbar, "刷新音频设备", self.refresh_devices, width=125).pack(side="left", padx=(0, 8))
        self._button(toolbar, "开始实测校准", self.start_calibration, width=140, fg_color=ACCENT).pack(side="left")
        self.calibration_log = ctk.CTkTextbox(tab, font=FONT, state="disabled", height=130)
        self.calibration_log.grid(row=4, column=0, sticky="nsew", padx=14, pady=8)
        self._button(tab, "应用测量结果并保存", self.apply_calibration).grid(row=5, column=0, sticky="ew", padx=14, pady=(4, 12))

    def _build_settings(self, parent):
        self.mode = ctk.CTkOptionMenu(parent, values=["DD 驱动", "Win32 兼容"], font=FONT)
        self.mode.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=8)
        self.editable_widgets.append(self.mode)
        self.target_button = self._button(parent, "捕获游戏窗口（3 秒）", self.capture_target)
        self.target_button.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        self.target_label = self._label(parent, "尚未选择目标窗口", wraplength=280, anchor="w", text_color=MUTED)
        self.target_label.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        self.fields = {}
        for row, (name, label, default) in enumerate((("bpm", "文本 BPM", "120"), ("speed", "速度倍率", "1.0"),
                                                    ("transpose", "移调（半音）", "0"), ("hold", "按键保持（毫秒）", "60"),
                                                    ("delay", "开始倒计时（秒）", "3"), ("octave", "简谱 1=C 的八度", "4")), 3):
            self._label(parent, label).grid(row=row, column=0, sticky="w", padx=10, pady=5)
            entry = ctk.CTkEntry(parent, width=105, font=FONT)
            entry.insert(0, default)
            entry.grid(row=row, column=1, sticky="ew", padx=10, pady=5)
            self.fields[name] = entry
            self.editable_widgets.append(entry)
        self.missing_menu = ctk.CTkOptionMenu(parent, values=["缺失音符：阻止演奏", "缺失音符：跳过"], font=FONT)
        self.missing_menu.grid(row=9, column=0, columnspan=2, sticky="ew", padx=10, pady=8)
        self.editable_widgets.append(self.missing_menu)
        self.mapping_label = self._label(parent, "音高未校准 · 默认按 1=C", wraplength=280, text_color="#d69b3b")
        self.mapping_label.grid(row=10, column=0, columnspan=2, sticky="ew", padx=10, pady=8)
        table = ctk.CTkFrame(parent)
        table.grid(row=11, column=0, columnspan=2, sticky="ew", padx=10)
        table.grid_columnconfigure((1, 2), weight=1)
        for column, label in enumerate(("截图音符", "实际音高", "按键")):
            self._label(table, label).grid(row=0, column=column, padx=8, pady=5)
        labels = ("低 6", "3", "4", "5", "6", "7", "高 1", "高 2", "高 3")
        for row, ((pitch, key), label) in enumerate(zip(DEFAULT_MAPPING.items(), labels), 1):
            self._label(table, label).grid(row=row, column=0, padx=8)
            pitch_entry = ctk.CTkEntry(table, width=75, font=FONT)
            key_entry = ctk.CTkEntry(table, width=55, font=FONT)
            pitch_entry.insert(0, pitch_name(pitch))
            key_entry.insert(0, key)
            pitch_entry.grid(row=row, column=1, sticky="ew", padx=5, pady=3)
            key_entry.grid(row=row, column=2, sticky="ew", padx=5, pady=3)
            self.mapping_rows.append((pitch_entry, key_entry))
            self.editable_widgets.extend((pitch_entry, key_entry))
        self._button(parent, "保存设置", self.save_settings).grid(row=12, column=0, sticky="ew", padx=10, pady=10)
        self._button(parent, "恢复截图默认", self.reset_mapping).grid(row=12, column=1, sticky="ew", padx=10, pady=10)

    def message(self, text, error=False):
        self.status.configure(text=text, text_color="#d97070" if error else MUTED)

    def read_mapping(self):
        mapping = {}
        for pitch_entry, key_entry in self.mapping_rows:
            pitch = parse_pitch(pitch_entry.get())
            if pitch in mapping:
                raise ScoreError(f"音高 {pitch_name(pitch)} 重复，请检查校准或配置")
            mapping[pitch] = key_entry.get().strip().upper()
        return validate_mapping(mapping)

    def _fill_mapping(self, mapping):
        for (pitch_entry, key_entry), (pitch, key) in zip(self.mapping_rows, mapping.items()):
            for entry, value in ((pitch_entry, pitch_name(pitch)), (key_entry, key)):
                entry.delete(0, "end")
                entry.insert(0, value)

    def reset_mapping(self):
        self._fill_mapping(DEFAULT_MAPPING)
        self.fields["octave"].delete(0, "end")
        self.fields["octave"].insert(0, "4")
        self.calibration = []
        self.calibration_source = ""
        self.mapping_label.configure(text="音高未校准 · 默认按 1=C", text_color="#d69b3b")

    def _load_settings(self):
        if not SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            mapping = validate_mapping({parse_pitch(item[0]): item[1] for item in data["mapping"]})
            if len(mapping) != len(self.mapping_rows):
                raise ScoreError("设置中的键位数量必须为 9")
            self._fill_mapping(mapping)
            for name, value in data.get("fields", {}).items():
                if name in self.fields:
                    self.fields[name].delete(0, "end")
                    self.fields[name].insert(0, str(value))
            self.calibration = data.get("calibration", [])
            self.calibration_source = data.get("calibration_source", "")
            self._update_calibration_label()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.message(f"无法加载设置，使用默认值: {exc}", True)

    def save_settings(self):
        try:
            mapping = self.read_mapping()
            data = {"mapping": [[pitch_name(pitch), key] for pitch, key in mapping.items()],
                    "fields": {name: entry.get() for name, entry in self.fields.items()}, "calibration": self.calibration,
                    "calibration_source": self.calibration_source}
            temporary = SETTINGS_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(SETTINGS_PATH)
            self.message("设置已保存")
        except (OSError, ValueError) as exc:
            self.message(str(exc), True)

    def _update_calibration_label(self):
        try:
            measured = {item["pitch"]: item["key"] for item in self.calibration}
            valid = bool(measured) and measured == self.read_mapping()
            self.mapping_label.configure(text="已应用实测音高" if valid else "音高未校准 · 请先实测或手动确认",
                                         text_color=ACCENT if valid else "#d69b3b")
        except (ValueError, KeyError, TypeError):
            self.mapping_label.configure(text="键位配置待检查", text_color="#d69b3b")

    def capture_target(self):
        if self.busy or self.capture_timer or self.closing:
            return
        self.message("3 秒内切换到游戏的乐器演奏界面")
        self.target_button.configure(state="disabled")
        self.capture_timer = self.after(3000, self._capture_target)

    def _capture_target(self):
        self.capture_timer = None
        self.target_button.configure(state="normal")
        try:
            self.target = TargetWindow(self.user32, self.user32.GetForegroundWindow())
            self.target_label.configure(text=self.target.title)
            self.message("目标窗口已捕获")
        except ValueError as exc:
            self.message(str(exc), True)

    def current_score(self):
        if self.text_mode:
            return parse_text(self.editor.get("1.0", "end"), float(self.fields["bpm"].get()), self.source_name,
                              octave=int(self.fields["octave"].get()))
        if self.score is None:
            raise ScoreError("请先导入乐谱")
        return self.score

    def make_plan(self):
        return compile_score(self.current_score(), self.read_mapping(), speed=float(self.fields["speed"].get()),
                             transpose=int(self.fields["transpose"].get()), hold_ms=float(self.fields["hold"].get()),
                             track=self.track_ids.get(self.track_menu.get()), skip_missing=self.missing_menu.get().endswith("跳过"))

    def get_backend(self):
        mode = self.mode.get()
        # DD initialization belongs to the app lifetime, not to an individual performance.
        if mode not in self.backends:
            self.backends[mode] = self.backend_factory(mode)
        return self.backends[mode]

    def start(self, preview=False):
        if self.busy or self.capture_timer or self.closing:
            return
        try:
            plan = self.make_plan()
            self._update_calibration_label()
            if preview:
                sink, guard, delay = PreviewSink(), lambda: "ready", 0
            else:
                if self.target is None or self.target.state() == "closed":
                    raise ScoreError("请先捕获游戏窗口")
                sink = KeyboardSink(self.get_backend(), self.read_mapping().values())
                guard, delay = self.target.state, float(self.fields["delay"].get())
            self.summary.configure(text=f"{self.source_name} · {plan.note_count} 次按键 · {plan.duration:.1f} 秒 · 跳过 {plan.skipped} 个音符"
                                         + (" · 无按键预览" if preview else ""))
            self.player.start(plan, sink, guard, delay)
        except (ValueError, RuntimeError, OSError) as exc:
            self.message(str(exc), True)

    def stop(self):
        self.player.request_stop()
        self.cancel_job.set()
        if self.capture_timer:
            self.after_cancel(self.capture_timer)
            self.capture_timer = None
            self.target_button.configure(state="normal")
        if not self.busy:
            if self.calibration_sink:
                try:
                    self.calibration_sink.release_all()
                except RuntimeError as exc:
                    self.message(str(exc), True)
                    return
            self.player.stop()
            self.message(self.player.snapshot().message if self.player.snapshot().state == "error" else "已停止")
        else:
            self.message("正在停止并释放按键…")

    def _key_press(self, key):
        if key not in (keyboard.Key.f8, keyboard.Key.f9, keyboard.Key.f10) or key in self.hotkeys_down:
            return
        self.hotkeys_down.add(key)
        if key == keyboard.Key.f10:
            self.player.request_stop()
            self.cancel_job.set()
        self.events.put(("hotkey", key))

    def _key_release(self, key):
        self.hotkeys_down.discard(key)

    def _job(self, operation, callback):
        if self.busy or self.capture_timer or self.closing:
            return
        self.cancel_job.clear()
        self.job_pending = True
        self.last_status = self.player.snapshot()

        def work():
            try:
                result, error = operation(), None
            except Exception as exc:
                result, error = None, str(exc)
            self.events.put(("job_done", (callback, result, error)))

        self.job = threading.Thread(target=work, name="music-import-or-calibration", daemon=True)
        self.job.start()

    def import_score(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self, title="导入乐谱", filetypes=[("乐谱", "*.txt *.mid *.midi"), ("所有文件", "*.*")])
        if not path:
            return
        path = Path(path)
        if path.suffix.lower() in (".mid", ".midi"):
            self.message("正在读取 MIDI…")
            self._job(lambda: load_midi(path), self._set_midi)
        else:
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise ScoreError("文本谱不能超过 1 MB")
                self._set_text(path.read_text(encoding="utf-8-sig"), path.stem)
            except (OSError, ValueError) as exc:
                self.message(str(exc), True)

    def _set_text(self, text, name, *, keep_adaptation=False):
        if not keep_adaptation:
            self._clear_adaptation()
        self.text_mode, self.score, self.source_name = True, None, name
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.track_ids = {"全部旋律音轨": None}
        self.track_menu.configure(values=list(self.track_ids))
        self.track_menu.set("全部旋律音轨")
        self.summary.configure(text=f"{name} · 文本简谱 · 可直接编辑")
        self.tabs.set("乐谱")
        self.message("已载入，请检查音域和键位")

    def _set_midi(self, score, *, keep_adaptation=False, kind="MIDI"):
        if not keep_adaptation:
            self._clear_adaptation()
        self.text_mode, self.score, self.source_name = False, score, score.title
        self.track_ids = {"全部旋律音轨": None}
        self.track_ids.update({f"{index + 1}: {name}": index for index, name in score.tracks})
        self.track_menu.configure(values=list(self.track_ids))
        self.track_menu.set("全部旋律音轨")
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        rows = [f"{kind} · 时间轴 / 速度变化已保留\n{len(score.notes)} 个音符 · {score.duration:.1f} 秒\n"]
        rows.extend(f"{note.start:8.3f}s  {pitch_name(note.pitch):4s}  {note.duration:.3f}s  轨{note.track + 1}" for note in score.notes[:500])
        self.editor.insert("1.0", "\n".join(rows))
        self.editor.configure(state="disabled")
        self.summary.configure(text=f"{score.title} · {kind} · 可选择旋律音轨；文本 BPM 不影响此乐谱")
        self.tabs.set("乐谱")
        self.message(f"{kind}已载入，可预览或点击“适配”分析音域")

    def load_demo(self):
        self._set_text(DEMO_SCORE, "小星星 · 示例")

    def save_midi(self):
        try:
            score = self.current_score()
            path = filedialog.asksaveasfilename(parent=self, title="导出当前原始乐谱", defaultextension=".mid", filetypes=[("MIDI", "*.mid")])
            if path:
                from music_audio import export_midi
                export_midi(score, path)
                self.message("MIDI 已导出（原始乐谱，不应用演奏速度、移调与跳过规则）")
        except (ValueError, OSError) as exc:
            self.message(str(exc), True)

    def convert_audio(self):
        if self.busy:
            return
        try:
            mapping = self.read_mapping()
            policy = self.adaptation_policy.get()
        except ValueError as exc:
            self.message(str(exc), True)
            return
        path = filedialog.askopenfilename(parent=self, title="选择单旋律音频", filetypes=[("音频", "*.wav *.flac *.ogg *.mp3"), ("所有文件", "*.*")])
        if not path:
            return
        self.audio_path = Path(path)
        self.audio_label.configure(text=self.audio_path.name)
        self.message("正在分析音频，F10 可取消…")

        def convert():
            from music_audio import transcribe_audio
            source = transcribe_audio(path, self.cancel_job, lambda value: self.events.put(("message", f"音频转谱 {value:.0%}")))
            self.events.put(("message", "音高识别完成，正在编配到当前9键…"))
            return adapt_score(source, mapping, policy, stop=self.cancel_job)

        def converted(result):
            self.fields["bpm"].configure(state="normal")
            self.fields["bpm"].delete(0, "end")
            self.fields["bpm"].insert(0, "120")
            self.fields["transpose"].configure(state="normal")
            self.fields["transpose"].delete(0, "end")
            self.fields["transpose"].insert(0, "0")
            self._set_midi(result.source, kind="音频识别原谱")
            self._show_adaptation(result, mapping)

        self._job(convert, converted)

    def refresh_devices(self):
        def find():
            from music_audio import list_outputs
            return list_outputs()

        def found(outputs):
            self.output_ids = {"系统默认输出": None}
            self.output_ids.update({f"{index + 1}. {name}": device_id for index, (device_id, name) in enumerate(outputs)})
            self.output_menu.configure(values=list(self.output_ids))
            self.output_menu.set("系统默认输出")
            self.message("音频输出设备已刷新")

        self._job(find, found)

    def _log_calibration(self, message):
        self.calibration_log.configure(state="normal")
        self.calibration_log.insert("end", message + "\n")
        self.calibration_log.see("end")
        self.calibration_log.configure(state="disabled")

    def start_calibration(self):
        if self.busy or self.capture_timer:
            return
        try:
            if not self.target or self.target.state() == "closed":
                raise ScoreError("请先捕获游戏窗口")
            keys = list(self.read_mapping().values())
            sink = KeyboardSink(self.get_backend(), keys)
            self.calibration_sink = sink
            target = self.target
            output_id = self.output_ids.get(self.output_menu.get())
            output_dir = APP_DIR / "artifacts" / "calibration" / datetime.now().strftime("%Y%m%d-%H%M%S")

            def measure():
                from dataclasses import asdict
                from music_audio import calibrate
                try:
                    results = calibrate(keys, sink, target.state, output_dir, self.cancel_job, output_id,
                                        lambda message: self.events.put(("calibration", message)))
                finally:
                    sink.release_all()
                return [asdict(result) for result in results]

            def measured(results):
                self.calibration = results
                self.calibration_source = str((output_dir / "measurements.json").relative_to(APP_DIR))
                self._log_calibration(f"测量完成，录音目录: {output_dir}")
                self.message("请检查测量结果，再点击“应用测量结果并保存”")

            self.calibration = []
            self._job(measure, measured)
        except (ValueError, RuntimeError, OSError) as exc:
            self.message(str(exc), True)

    def apply_calibration(self):
        try:
            if len(self.calibration) != len(self.mapping_rows):
                raise ScoreError("还没有完整测量结果，请先完成校准")
            mapping = {item["pitch"]: item["key"] for item in self.calibration}
            if len(mapping) != len(self.mapping_rows):
                raise ScoreError("测得重复音高，请关闭背景音乐并重新校准")
            self._fill_mapping(validate_mapping(mapping))
            # Screenshot's upper T key is high 1, so its C determines the unmarked 1's octave.
            top_c = next((item["pitch"] for item in self.calibration if item["key"] == "T"), None)
            if top_c is not None and top_c % 12 == 0:
                self.fields["octave"].delete(0, "end")
                self.fields["octave"].insert(0, str(top_c // 12 - 2))
            self._update_calibration_label()
            self.save_settings()
        except (ValueError, KeyError) as exc:
            self.message(str(exc), True)

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "hotkey":
                    if payload == keyboard.Key.f8:
                        self.start()
                    elif payload == keyboard.Key.f9:
                        self.player.toggle_pause()
                    else:
                        self.stop()
                elif kind == "job_done":
                    self.job_pending = False
                    callback, result, error = payload
                    # Keep the worker reference until it exits; never overlap a new input session.
                    if error:
                        self.message(error, True)
                    elif not self.cancel_job.is_set():
                        callback(result)
                    else:
                        self.message("已取消")
                elif kind == "calibration":
                    self._log_calibration(payload)
                    self.message(payload)
                else:
                    self.message(payload)
        except queue.Empty:
            pass
        except (ValueError, RuntimeError, OSError) as exc:
            self.message(str(exc), True)
        status = self.player.snapshot()
        if status != self.last_status:
            self.last_status = status
            self.progress.set(status.position / status.duration if status.duration else 0)
            self.message(f"{status.message}    {status.position:.1f} / {status.duration:.1f} 秒", status.state == "error")
        busy = bool(self.busy)
        if busy != self.last_busy:
            self.last_busy = busy
            for widget in self.editable_widgets:
                widget.configure(state="disabled" if busy else "normal")
            self.editor.configure(state="disabled" if busy or not self.text_mode else "normal")
        if self.capture_timer:
            self.target_button.configure(state="disabled")
        self.pause_button.configure(state="normal" if self.player.running else "disabled")
        for tile, (pitch_entry, key_entry) in zip(self.key_tiles, self.mapping_rows):
            key = key_entry.get().strip().upper()
            tile.configure(text=f"{key}\n{pitch_entry.get()}", fg_color=ACCENT if key in status.pressed else ("#d9e1ea", "#293749"))
        self.poll_timer = self.after(40, self._poll)

    def shutdown(self, callback):
        if self.closing:
            return
        self.closing = True
        self.stop()

        def finish():
            if self.busy:
                self.after(50, finish)
                return
            if not self.player.stop():
                self.closing = False
                self.message("按键释放失败，请检查驱动后再次停止", True)
                return
            if self.calibration_sink:
                try:
                    self.calibration_sink.release_all()
                except RuntimeError as exc:
                    self.closing = False
                    self.message(str(exc), True)
                    return
            self.closed = True
            if self.listener:
                self.listener.stop()
            self.after_cancel(self.poll_timer)
            callback()

        finish()


class MusicPlayerWindow(ctk.CTkToplevel):
    def __init__(self, master, backend_factory, user32, on_closed=lambda: None):
        super().__init__(master)
        self.title("洛克 · 乐谱演奏")
        self.geometry("1060x780")
        self.minsize(880, 620)
        self.panel = MusicPlayerPanel(self, backend_factory, user32)
        self.panel.pack(fill="both", expand=True)
        self.on_closed = on_closed
        self.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        def closed():
            self.on_closed()
            self.destroy()
        self.panel.shutdown(closed)


def run():
    from main import DDInputBackend, USER32, Win32InputBackend

    ctk.set_appearance_mode("Dark")
    root = ctk.CTk()
    root.title("洛克 · 乐谱演奏")
    root.geometry("1060x780")
    root.minsize(880, 620)
    panel = MusicPlayerPanel(root, lambda mode: DDInputBackend() if mode == "DD 驱动" else Win32InputBackend(USER32), USER32)
    panel.pack(fill="both", expand=True)
    root.protocol("WM_DELETE_WINDOW", lambda: panel.shutdown(root.destroy))
    root.mainloop()


if __name__ == "__main__":
    run()
