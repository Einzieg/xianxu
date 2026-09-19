import { useState } from 'react';
import type { AppState, MelodyRegister, ScoreDetail, ScoreTexture, TranscriptionEngine } from '../types';
import { fileName, formatTime } from '../lib/model';
import Icon from './Icon';

export default function TranscribePage({ path, onPick, onStart, locked, job, result, onLibrary }: {
  path: string;
  onPick: () => void;
  onStart: (engine: TranscriptionEngine, register: MelodyRegister, texture: ScoreTexture) => void;
  locked: boolean;
  job: AppState['job'];
  result: ScoreDetail | null;
  onLibrary: () => void;
}) {
  const [engine, setEngine] = useState<TranscriptionEngine>('instrument');
  const [register, setRegister] = useState<MelodyRegister>('auto');
  const [texture, setTexture] = useState<ScoreTexture>('melody');
  const chordMode = engine === 'instrument' && texture === 'chords';
  const completed = job?.kind === 'transcribe' && job.state === 'done';
  return <div className="transcribe-page">
    <header className="page-heading"><div><div className="eyebrow">FROM SOUND TO SCORE</div><h1>把旋律，变成曲谱<span className="title-dot">.</span></h1><p>不是所有检测到的音符都要加入主旋律：先选择主声部，再适配九键。</p></div><span className="badge gold-badge">AUDIO → 9 KEYS</span></header>
    <div className="transcribe-grid"><section className="panel transcribe-main">
      <div className="step-title"><span>01</span><h2>选择音频</h2><span className="muted">本地文件</span></div>
      <button className={`file-picker ${path ? 'file-selected' : ''}`} disabled={locked} onClick={onPick}><span className="file-icon"><Icon name={path ? 'audio' : 'folder'} size={30} /></span><strong>{path ? fileName(path) : '选择一段你喜欢的旋律'}</strong><span>{path ? '点击更换音频文件' : '点击选择音频文件，无需拖拽'}</span><span className="format-pills"><b>MP3</b><b>WAV</b><b>FLAC</b><b>OGG</b></span></button>
      {path && <p className="selected-path" title={path}>{path}</p>}
      <div className="step-title"><span>02</span><h2>选择提取引擎</h2></div>
      <div className="engine-options" role="radiogroup" aria-label="音频转谱引擎">
        <button role="radio" aria-checked={engine === 'instrument'} className={`engine-option ${engine === 'instrument' ? 'selected' : ''}`} onClick={() => setEngine('instrument')} disabled={locked}><div className="engine-top"><Icon name="audio" /><span className="radio-dot" /></div><strong>乐器音符模型 <small>BASIC PITCH · V2.1</small></strong><p>保留连奏旋律，补回有起音证据却被筛选截掉的短重奏；复杂合奏仍需试听。</p></button>
        <button role="radio" aria-checked={engine === 'melody'} className={`engine-option ${engine === 'melody' ? 'selected' : ''}`} onClick={() => setEngine('melody')} disabled={locked}><div className="engine-top"><Icon name="audio" /><span className="radio-dot" /></div><strong>旧版频谱（对照） <small>MELODY</small></strong><p>保留原有频谱提取算法，供试听比较；伴奏与泛音可能混入旋律。</p></button>
        <button role="radio" aria-checked={engine === 'yin'} className={`engine-option ${engine === 'yin' ? 'selected' : ''}`} onClick={() => setEngine('yin')} disabled={locked}><div className="engine-top"><Icon name="music" /><span className="radio-dot" /></div><strong>独奏单音 <small>YIN</small></strong><p>适合清晰的单声部独奏，不适合复杂混音与和弦。</p></button>
      </div>
      {engine === 'instrument' && <label className="field register-field"><span>声部模式</span><select value={texture} disabled={locked} onChange={(event) => setTexture(event.target.value as ScoreTexture)}><option value="melody">主旋律（默认，推荐）</option><option value="chords">保留和弦（多音同时演奏，实验）</option></select><small>{chordMode ? '保留 v2.1 主旋律，补充通过起音与持续证据筛选的和弦音；可能带入伴奏或泛音，请先试听。' : '保持现有 v2.1 主声部识别，不自动混入和弦或伴奏。'}</small></label>}
      {engine === 'instrument' && <label className="field register-field"><span>主声部音区偏好</span><select value={register} disabled={locked} onChange={(event) => setRegister(event.target.value as MelodyRegister)}><option value="auto">自动</option><option value="high">偏高音主奏</option><option value="mid">偏中音主奏</option><option value="low">偏低音主奏</option></select><small>仅影响乐器音符模型的主声部选择，不代表识别出某一种乐器。</small></label>}
      <div className="transcribe-submit"><span><Icon name="shield" size={15} />转换过程不会发送按键</span><button className="button primary" disabled={locked || !path} onClick={() => onStart(engine, engine === 'instrument' ? register : 'auto', chordMode ? 'chords' : 'melody')}><Icon name="audio" size={18} />开始转谱<Icon name="arrow" size={16} /></button></div>
    </section>
    <aside className="transcribe-aside"><section className="panel workflow-panel"><span className="eyebrow">A LITTLE LESS COMPLEX</span><h2>从声音，到九个键</h2><ol><li><span>1</span><div><strong>分析音频</strong><p>识别音符，再按时序跟踪主声部</p></div></li><li><span>2</span><div><strong>自动九键适配</strong><p>将选出的旋律映射到可演奏音域</p></div></li><li><span>3</span><div><strong>自动收入谱库</strong><p>完成后选中新曲，随时查看与导出</p></div></li></ol></section>
      <section className="limitations"><Icon name="info" size={20} /><h3>先了解它的边界</h3><p>BASIC PITCH 使用本地 ONNX 神经音符模型，不是音源分离。和弦模式不推测缺失和声，也不能保证正确区分伴奏与泛音。</p><p>九键是音域限制，不是单声部限制。和弦适配会保留同时发音，但仍可能移调、替代缺失音、合并同键或删减超限音符。请先试听再演奏；无按键预览只检查时间轴，不发声。</p></section>
    </aside></div>
    {completed && result && <section className="panel conversion-result"><div className="result-check"><Icon name="check" size={23} /></div><div><span className="overline">转换完成 · 已自动入库</span><h3>{result.item.title}</h3><p>{formatTime(result.item.duration)} · 原始 {result.item.original_count ?? result.original_notes.length} 音符 <span className="accent-text">→ 适配后 {result.item.note_count} 音符</span></p></div><button className="button secondary" onClick={onLibrary}>查看曲谱<Icon name="arrow" size={16} /></button></section>}
  </div>;
}
