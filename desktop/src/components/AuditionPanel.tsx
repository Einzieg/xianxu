import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import type { AuditionKind, AuditionResult, LibraryItem } from '../types';
import { formatTime, friendlyError, hasAudioSource, validateAuditionRange } from '../lib/model';
import { AUDITION_STOP_EVENT, runAuditionRequest } from '../lib/audition';
import { rpc } from '../lib/rpc';

const KINDS: { kind: AuditionKind; label: string }[] = [
  { kind: 'source', label: '原曲' },
  { kind: 'original', label: '识别旋律' },
  { kind: 'score', label: '9键版本' },
];

function releaseAudio(audio: HTMLAudioElement | null) {
  if (!audio) return;
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
}

export default function AuditionPanel({ item, locked, stopSequence }: {
  item: LibraryItem;
  locked: boolean;
  stopSequence: number;
}) {
  const [start, setStart] = useState('0');
  const [duration, setDuration] = useState('15');
  const [clip, setClip] = useState<AuditionResult | null>(null);
  const [busy, setBusy] = useState<AuditionKind | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState('');
  const audioRef = useRef<HTMLAudioElement>(null);
  const generation = useRef(0);
  const range = validateAuditionRange(start, duration);
  const sourceAvailable = hasAudioSource(item.source);
  const kinds = KINDS.map(entry => entry.kind === 'original' && item.transcription?.texture === 'chords'
    ? { ...entry, label: '识别和弦' } : entry);

  const clear = useCallback(() => {
    generation.current += 1;
    releaseAudio(audioRef.current);
    setClip(null);
    setBusy(null);
    setWaiting(false);
    setError('');
  }, []);

  useLayoutEffect(() => {
    clear();
    const audio = audioRef.current;
    return () => {
      // Invalidate late RPC results and stop the captured element even on unmount.
      generation.current += 1;
      releaseAudio(audio);
    };
  }, [item.id, item.source, locked, stopSequence, clear]);

  useLayoutEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'F10') clear();
    };
    window.addEventListener(AUDITION_STOP_EVENT, clear);
    window.addEventListener('keydown', onKeyDown, true);
    return () => {
      window.removeEventListener(AUDITION_STOP_EVENT, clear);
      window.removeEventListener('keydown', onKeyDown, true);
    };
  }, [clear]);

  async function audition(kind: AuditionKind) {
    if (locked || !range.ok || (kind === 'source' && !sourceAvailable)) return;
    clear();
    const request = generation.current;
    setBusy(kind);
    try {
      // Deliberately bypass App's pending lock: audition has its own busy state.
      const response = await runAuditionRequest(() => rpc('audition', { id: item.id, kind, ...range.value }));
      if (request !== generation.current) return;
      if (response.status === 'busy') {
        setWaiting(true);
        return;
      }
      const result = response.value;
      if (!result.data_url.startsWith('data:audio/wav;base64,'))
        throw new Error('未收到有效的 WAV 试听片段，请重试。');
      setClip(result);
    } catch (cause) {
      if (request === generation.current) setError(`试听未能生成：${friendlyError(cause)}`);
    } finally {
      if (request === generation.current) setBusy(null);
    }
  }

  return <section className="audition-panel" aria-labelledby="audition-heading">
    <h3 id="audition-heading">片段试听</h3>
    <div className="audition-range">
      <label className="field"><span>起点（秒）</span><input type="number" min="0" step="any" value={start} disabled={locked} onChange={(event) => { clear(); setStart(event.target.value); }} /></label>
      <label className="field"><span>时长（秒）</span><input type="number" min="0.1" max="30" step="any" value={duration} disabled={locked} onChange={(event) => { clear(); setDuration(event.target.value); }} /></label>
    </div>
    {!range.ok && <p className="error-text" role="alert">{range.error}</p>}
    <div className="button-row" aria-label="选择试听版本">{kinds.filter(({ kind }) => kind !== 'source' || sourceAvailable).map(({ kind, label }) =>
      <button key={kind} className="button secondary compact" disabled={locked || !range.ok || busy === kind} aria-pressed={clip?.kind === kind} onClick={() => void audition(kind)}>{label}</button>)}</div>
    {!sourceAvailable && <p className="fine-print">TXT / MIDI 等非音频来源没有原曲试听。</p>}
    <p className="fine-print">识别旋律与 9 键版本使用同一种合成音色，不是游戏实录；试听不发送按键。</p>
    {locked && <p className="fine-print">演奏、后台任务或应用锁定期间，试听已停止且不可用。</p>}
    {busy && <p className="fine-print" role="status">正在准备{kinds.find(({ kind }) => kind === busy)?.label}片段，请稍候…</p>}
    {waiting && <p className="fine-print" role="status">已有试听请求在处理中，本次未排队；请等待完成后再次点击。</p>}
    {error && <p className="error-text" role="alert">{error}</p>}
    {clip && !locked && <p className="fine-print">{clip.label} · 起点 {formatTime(clip.start)} · {clip.duration} 秒。请手动点击下方播放。</p>}
    <audio ref={audioRef} controls preload="none" hidden={!clip || locked} src={clip?.data_url} aria-label="片段试听播放器" onError={() => { if (clip) setError('试听音频无法载入，请重新选择版本生成片段。'); }} />
  </section>;
}
