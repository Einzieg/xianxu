import { useState } from 'react';
import type { LibraryItem, ScoreDetail, ScoreNote } from '../types';
import { fileName, formatTime, percent, pitchName, transcriptionLabel } from '../lib/model';
import Icon from './Icon';
import AuditionPanel from './AuditionPanel';

function NoteRoll({ notes, duration, position }: { notes: ScoreNote[]; duration: number; position: number }) {
  const valid = notes.filter((note) => Number.isFinite(note.start) && Number.isFinite(note.duration) && Number.isFinite(note.pitch));
  const end = Math.max(1, duration, valid.reduce((max, note) => Math.max(max, note.start + note.duration), 0));
  const pitches = [...new Set(valid.map((note) => note.pitch))].sort((a, b) => b - a);
  const row = 138 / Math.max(pitches.length, 9);
  return <div className="note-roll">
    <svg viewBox="0 0 520 164" role="img" aria-label={`音符时间轴，共 ${notes.length} 个音符${notes.length > 1500 ? '，仅绘制前 1500 个' : ''}`}>
      {Array.from({ length: 10 }, (_, index) => <line key={`h${index}`} x1="0" x2="520" y1={index * 15 + 8} y2={index * 15 + 8} className="roll-grid" />)}
      {Array.from({ length: 9 }, (_, index) => <line key={`v${index}`} x1={index * 65} x2={index * 65} y1="0" y2="151" className="roll-grid vertical" />)}
      {valid.slice(0, 1500).map((note, index) => <rect key={index}
        x={Math.max(0, note.start) / end * 516} y={pitches.indexOf(note.pitch) * row + 7}
        width={Math.max(2, note.duration / end * 516)} height={Math.max(1, Math.min(8, row - 2))}
        rx="2" className="roll-note"><title>{pitchName(note.pitch)} · {formatTime(note.start)} · {note.key || '原始音高'}</title></rect>)}
      {position > 0 && <line x1={percent(position / end) / 100 * 516} x2={percent(position / end) / 100 * 516} y1="0" y2="150" className="roll-playhead" />}
    </svg>
    {notes.length === 0 && <span className="roll-empty">导入曲谱后，在这里查看旋律的形状</span>}
    <div className="roll-times"><span>00:00</span><span>{formatTime(end / 2)}</span><span>{formatTime(end)}</span></div>
  </div>;
}

export default function ScorePanel({ item, detail, loading, error, onRetry, position, onExport, onDelete, locked, stopSequence }: {
  item: LibraryItem | undefined;
  detail: ScoreDetail | null;
  loading: boolean;
  error: string;
  onRetry: () => void;
  position: number;
  onExport: () => void;
  onDelete: () => void;
  locked: boolean;
  stopSequence: number;
}) {
  const [original, setOriginal] = useState(false);
  const notes = original ? detail?.original_notes ?? [] : detail?.notes ?? [];
  const originalCount = item?.original_count ?? detail?.original_notes.length ?? 0;
  return <section className="panel score-panel" aria-labelledby="score-heading">
    <div className="panel-heading"><h2 id="score-heading"><Icon name="audio" size={17} />旋律视窗</h2><span className="eyebrow">SCORE INSPECTOR</span></div>
    <div className="score-identity"><div className={`cover-art ${item ? 'has-score' : ''}`}><Icon name="music" size={30} /></div>
      <div className="min-width-zero"><span className="overline">{item ? '当前选择' : '等待一段旋律'}</span><h3 title={item?.title}>{item?.title || '还没有选择曲目'}</h3><p>{item ? `${formatTime(item.duration)} · ${item.note_count} 个音符` : '你的旋律，即将有迹可循'}</p></div>
    </div>
    <div className="score-tabs" role="group" aria-label="音符视图">
      <button className={!original ? 'selected' : ''} aria-pressed={!original} onClick={() => setOriginal(false)}>9 键谱</button>
      <button className={original ? 'selected' : ''} aria-pressed={original} onClick={() => setOriginal(true)}>原始音符</button>
      <span>{loading ? '读取中…' : item ? '时间轴 · 秒' : '待导入'}</span>
    </div>
    {error ? <div className="inline-error" role="alert">{error}<button className="text-button" onClick={onRetry}>重新读取</button></div> :
      <NoteRoll notes={notes} duration={item?.duration || 0} position={position} />}
    {notes.length > 1500 && <p className="fine-print">视图仅绘制前 1,500 个音符，播放使用完整曲谱。</p>}
    <div className="score-stats">
      <div><span>原始音符</span><strong>{item ? originalCount.toLocaleString() : '—'}</strong></div>
      <Icon name="arrow" size={17} />
      <div><span>适配后音符</span><strong className="accent-text">{item ? item.note_count.toLocaleString() : '—'}</strong></div>
      <div><span>目标键位</span><strong>9<span className="stat-unit"> 键</span></strong></div>
    </div>
    {item && <dl className="score-meta"><div><dt>来源</dt><dd title={item.source}>{fileName(item.source) || '未知来源'}</dd></div>
      <div><dt>识别模型</dt><dd>{transcriptionLabel(item.transcription)}</dd></div>
      <div><dt>入库时间</dt><dd>{item.created_at || '未提供'}</dd></div></dl>}
    {typeof item?.summary === 'string' && item.summary && <p className="score-summary">{item.summary}</p>}
    <p className="fine-print"><Icon name="info" size={14} />九键适配会折叠音域、调整或舍弃部分音符，并不等同于原曲复现。</p>
    {item && <AuditionPanel key={item.id} item={item} locked={locked} stopSequence={stopSequence} />}
    <div className="score-actions"><button className="button secondary" disabled={!item || locked} onClick={onExport} title="导出 MIDI，精确保留时间轴"><Icon name="export" size={16} />导出 MIDI</button>
      <button className="button secondary danger" title={locked ? '请先停止演奏并等待当前任务完成' : '仅从谱库删除，不删除原始文件'} disabled={!item || locked} onClick={onDelete}><Icon name="trash" size={16} />删除曲谱</button></div>
  </section>;
}
