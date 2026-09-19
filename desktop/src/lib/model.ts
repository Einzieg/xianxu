import type { AppState, LibraryItem, PlayMode, Settings } from '../types.ts';

export const MODES: { value: PlayMode; label: string; hint: string }[] = [
  { value: 'order', label: '顺序播放', hint: '依次播放，列表结束后停止' },
  { value: 'random', label: '随机播放', hint: '随机选择下一首' },
  { value: 'repeat_all', label: '列表循环', hint: '从列表末尾回到第一首' },
  { value: 'repeat_one', label: '单曲循环', hint: '重复播放当前曲目' },
];

// These are display-only defaults until get_state returns the persisted settings.
export const DEFAULT_SETTINGS: Settings = {
  speed: 1, hold: 60, delay: 3, backend: 'DD', octave: 3, bpm: 120,
  mapping: [
    { pitch: 45, key: 'B' }, { pitch: 52, key: 'F' }, { pitch: 53, key: 'G' },
    { pitch: 55, key: 'H' }, { pitch: 57, key: 'J' }, { pitch: 59, key: 'K' },
    { pitch: 60, key: 'T' }, { pitch: 62, key: 'Y' }, { pitch: 64, key: 'U' },
  ],
};

export const EMPTY_STATE: AppState = {
  library: [], current_id: null, mode: 'order', preview: false, stop_sequence: 0,
  settings: DEFAULT_SETTINGS, target: null, queue_active: false, job: null,
  playback: { state: 'idle', position: 0, duration: 0, message: '', pressed: [] },
};

export function formatTime(seconds: number): string {
  const value = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  return `${Math.floor(value / 60).toString().padStart(2, '0')}:${(value % 60).toString().padStart(2, '0')}`;
}

export function percent(value: number): number {
  return Number.isFinite(value) ? Math.round(Math.min(1, Math.max(0, value)) * 100) : 0;
}

export function pitchName(pitch: number): string {
  const names = ['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B'];
  return `${names[((pitch % 12) + 12) % 12]}${Math.floor(pitch / 12) - 1}`;
}

export function fileName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

export function transcriptionLabel(metadata: LibraryItem['transcription']): string {
  if (metadata?.engine === 'instrument') {
    if (metadata.texture === 'chords') return 'BASIC PITCH · 保留和弦 v1（实验）';
    if (metadata.revision === 'legato-v2.1') return 'BASIC PITCH · 重奏补全 v2.1';
    if (metadata.revision === 'legato-v2') return 'BASIC PITCH · 连奏修正 v2';
    return metadata.revision ? `BASIC PITCH · ${metadata.revision}` : 'BASIC PITCH · 主声部跟踪（旧版）';
  }
  if (metadata?.engine === 'melody') return '旧版频谱（对照）';
  if (metadata?.engine === 'yin') return 'YIN · 独奏单音';
  return '导入谱 / 未记录引擎';
}

export function hasAudioSource(source: string): boolean {
  return /\.(mp3|wav|flac|ogg)$/i.test(fileName(source));
}

export function validateAuditionRange(startText: string, durationText: string):
  { ok: true; value: { start: number; duration: number } } |
  { ok: false; error: string } {
  const start = Number(startText);
  const duration = Number(durationText);
  if (!startText.trim() || !Number.isFinite(start) || start < 0)
    return { ok: false, error: '试听起点须为大于或等于 0 的秒数。' };
  if (!durationText.trim() || !Number.isFinite(duration) || duration < 0.1 || duration > 30)
    return { ok: false, error: '试听时长须为 0.1–30 秒。' };
  return { ok: true, value: { start, duration } };
}

export function filterLibrary(items: LibraryItem[], query: string): LibraryItem[] {
  const term = query.trim().toLocaleLowerCase();
  return items.filter((item) => `${item.title} ${item.source}`.toLocaleLowerCase().includes(term));
}

export function hasTarget(state: AppState): boolean {
  return Boolean(state.target?.title?.trim()) &&
    !['lost', 'closed', 'invalid', 'missing', 'error', 'unavailable'].includes(state.target!.state.toLowerCase());
}

export function playbackActive(state: AppState): boolean {
  return state.queue_active || ['countdown', 'playing', 'paused'].includes(state.playback.state);
}

export function getLocks(state: AppState, connected: boolean, pending: boolean) {
  const active = playbackActive(state);
  const job = state.job?.state === 'running';
  return {
    active, job,
    mutate: !connected || pending || job || active,
    start: !connected || pending || job || active,
    transport: !connected || pending || job,
  };
}

export function friendlyError(error: unknown): string {
  const message = typeof error === 'string' ? error : error instanceof Error ? error.message :
    (error && typeof error === 'object' && 'message' in error ? String(error.message) : '请求未完成，请重试。');
  if (/__TAURI|not a function|invoke.*undefined/i.test(message)) return '桌面桥接不可用。请在 Tauri 桌面应用中重试。';
  if (/permission|not allowed|denied/i.test(message)) return `权限不足，请检查桌面权限或驱动状态。${message}`;
  return message;
}

export interface SettingsDraft {
  speed: string;
  hold: string;
  delay: string;
  octave: string;
  bpm: string;
  backend: Settings['backend'];
  mapping: { pitch: string; key: string }[];
}

export function toDraft(settings: Settings): SettingsDraft {
  return {
    speed: String(settings.speed), hold: String(settings.hold), delay: String(settings.delay),
    octave: String(settings.octave), bpm: String(settings.bpm),
    backend: settings.backend,
    mapping: settings.mapping.map(({ pitch, key }) => ({ pitch: String(pitch), key })),
  };
}

export function validateSettings(draft: SettingsDraft):
  { ok: true; value: Settings } |
  { ok: false; error: string } {
  const speed = Number(draft.speed);
  const hold = Number(draft.hold);
  const delay = Number(draft.delay);
  const octave = Number(draft.octave);
  const bpm = Number(draft.bpm);
  if (!draft.speed.trim() || !Number.isFinite(speed) || speed < 0.25 || speed > 4)
    return { ok: false, error: '播放速度须在 0.25–4 倍之间。' };
  if (!draft.hold.trim() || !Number.isInteger(hold) || hold < 10 || hold > 150)
    return { ok: false, error: '按键时长须为 10–150 ms 的整数。' };
  if (!draft.delay.trim() || !Number.isFinite(delay) || delay < 0 || delay > 30)
    return { ok: false, error: '开始延迟须在 0–30 秒之间。' };
  if (!draft.octave.trim() || !Number.isInteger(octave) || octave < -1 || octave > 9)
    return { ok: false, error: '文本简谱八度须为 -1–9 的整数（C-1–C9）。' };
  if (!draft.bpm.trim() || !Number.isFinite(bpm) || bpm <= 0)
    return { ok: false, error: '文本简谱 BPM 须为大于 0 的数值。' };
  if (!['DD', 'Win32'].includes(draft.backend)) return { ok: false, error: '请选择 DD 或 Win32 后端。' };
  if (draft.mapping.length !== 9) return { ok: false, error: '映射必须包含 9 个键。' };
  const mapping = draft.mapping.map(({ pitch, key }) => ({ pitch: Number(pitch), key: key.trim().toUpperCase() }));
  if (mapping.some(({ pitch }, index) => !draft.mapping[index].pitch.trim() || !Number.isInteger(pitch) || pitch < 0 || pitch > 127))
    return { ok: false, error: 'MIDI 音高须为 0–127 的整数。' };
  if (mapping.some(({ key }) => !/^[A-Z0-9]$/.test(key)))
    return { ok: false, error: '每个映射键请填写一个英文字母或数字。F8 / F9 / F10 为保留快捷键。' };
  if (new Set(mapping.map(({ key }) => key)).size !== 9 || new Set(mapping.map(({ pitch }) => pitch)).size !== 9)
    return { ok: false, error: '9 个按键和音高均不能重复。' };
  return { ok: true, value: { speed, hold, delay, backend: draft.backend, mapping, octave, bpm } };
}
