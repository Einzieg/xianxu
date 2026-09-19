export type PlayMode = 'order' | 'random' | 'repeat_all' | 'repeat_one';
export type PlaybackState = 'idle' | 'countdown' | 'playing' | 'paused' | 'finished' | 'stopped' | 'error';
export type TranscriptionEngine = 'instrument' | 'melody' | 'yin';
export type MelodyRegister = 'auto' | 'high' | 'mid' | 'low';
export type ScoreTexture = 'melody' | 'chords';
export type AuditionKind = 'source' | 'original' | 'score';
export interface AuditionResult {
  data_url: string;
  start: number;
  duration: number;
  kind: AuditionKind;
  label: string;
}
export interface KeyMapping { pitch: number; key: string }
export interface Settings {
  speed: number;
  hold: number;
  delay: number;
  backend: 'DD' | 'Win32';
  mapping: KeyMapping[];
  octave: number;
  bpm: number;
}
export interface LibraryItem {
  id: string;
  title: string;
  duration: number;
  note_count: number;
  source: string;
  created_at: string;
  summary: string | Record<string, unknown> | null;
  original_count: number;
  transcription?: { engine: TranscriptionEngine; register?: MelodyRegister; revision?: string; texture?: ScoreTexture } | null;
}
export interface ScoreNote { start: number; duration: number; pitch: number; key?: string }
export interface ScoreDetail {
  item: LibraryItem;
  notes: ScoreNote[];
  original_notes: ScoreNote[];
  mapping: KeyMapping[];
}
export interface AppState {
  library: LibraryItem[];
  stop_sequence: number;
  current_id: string | null;
  mode: PlayMode;
  preview: boolean;
  settings: Settings;
  target: { title: string; state: string } | null;
  playback: {
    state: PlaybackState;
    position: number;
    duration: number;
    message: string;
    pressed: string[];
  };
  job: null | {
    id: string;
    kind: string;
    state: 'running' | 'done' | 'cancelled' | 'error';
    progress: number;
    message: string;
    error?: string;
  };
  queue_active: boolean;
}
export interface RpcParams {
  get_state: Record<string, never>;
  import_score: { path: string };
  add_text: { title: string; text: string };
  transcribe: { path: string; engine: TranscriptionEngine; register: MelodyRegister; texture: ScoreTexture };
  audition: { id: string; kind: AuditionKind; start: number; duration: number };
  cancel_job: Record<string, never>;
  select: { id: string };
  remove: { id: string };
  play: { id?: string; preview: boolean; activate_target?: boolean; stop_sequence?: number };
  pause: { paused?: true };
  resume: { activate_target: boolean; stop_sequence?: number };
  stop: Record<string, never>;
  next: { activate_target?: boolean; stop_sequence?: number };
  previous: { activate_target?: boolean; stop_sequence?: number };
  set_mode: { mode: PlayMode };
  set_preview: { preview: boolean };
  capture: { delay: 3 };
  set_settings: Partial<Settings>;
  get_score: { id: string };
  export: { id: string; path: string };
  calibrate: { delay: 3 };
}
export type RpcResult<M extends keyof RpcParams> = M extends 'get_state'
  ? AppState : M extends 'get_score' ? ScoreDetail : M extends 'audition' ? AuditionResult : unknown;
