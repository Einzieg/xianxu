import { useEffect, useState } from 'react';
import type { Settings } from '../types';
import { pitchName, toDraft, validateSettings } from '../lib/model';
import type { SettingsDraft } from '../lib/model';
import Icon from './Icon';

export default function SettingsPage({ settings, locked, connected, targetReady, onSave, onCapture, onCalibrate }: {
  settings: Settings;
  locked: boolean;
  connected: boolean;
  targetReady: boolean;
  onSave: (settings: Partial<Settings>) => Promise<boolean>;
  onCapture: () => void;
  onCalibrate: () => void;
}) {
  const [draft, setDraft] = useState(() => toDraft(settings));
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const signature = JSON.stringify(settings);
  useEffect(() => {
    if (!dirty) setDraft(toDraft(JSON.parse(signature) as Settings));
  }, [signature, dirty]);

  function change<K extends keyof SettingsDraft>(key: K, value: SettingsDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
    setDirty(true);
    setSaved(false);
    setError('');
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (locked) return;
    const result = validateSettings(draft);
    if (!result.ok) { setError(result.error); return; }
    if (await onSave(result.value)) { setDirty(false); setSaved(true); }
  }

  return <div className="settings-page">
    <header className="page-heading"><div><div className="eyebrow">MAKE IT YOURS</div><h1>演奏设置<span className="title-dot">.</span></h1><p>调整每一次落键，让节奏恰到好处。</p></div><span className="badge neutral">{connected ? '由后端持久保存' : '示例默认值 · 未连接'}</span></header>
    <form onSubmit={(event) => void submit(event)}>
      <section className="panel settings-section"><div className="section-heading"><div><h2>节奏与输入</h2><p>播放、暂停或任务运行时不可修改设置。</p></div><Icon name="settings" /></div>
        <fieldset disabled={locked} className="settings-fields">
          <label className="field"><span>播放速度 <small>0.25–4 倍</small></span><div className="input-unit"><input type="number" min="0.25" max="4" step="0.05" value={draft.speed} onChange={(event) => change('speed', event.target.value)} required /><span>×</span></div><small>影响整首曲目的播放节奏</small></label>
          <label className="field"><span>按键时长 <small>10–150 ms</small></span><div className="input-unit"><input type="number" min="10" max="150" step="1" value={draft.hold} onChange={(event) => change('hold', event.target.value)} required /><span>ms</span></div><small>每个音符的真实按键保持时长</small></label>
          <label className="field"><span>开始延迟 <small>0–30 秒</small></span><div className="input-unit"><input type="number" min="0" max="30" step="0.5" value={draft.delay} onChange={(event) => change('delay', event.target.value)} required /><span>秒</span></div><small>给切换到目标窗口留出时间</small></label>
          <label className="field"><span>输入后端</span><select value={draft.backend} onChange={(event) => change('backend', event.target.value as Settings['backend'])}><option value="DD">DD · 真实驱动输入</option><option value="Win32">Win32 · 系统输入</option></select><small>DD 需驱动可用；两种后端均会发送真实按键</small></label>
          <label className="field"><span>简谱八度 <small>1 = C{draft.octave || '…'}</small></span><input type="number" min="-1" max="9" step="1" value={draft.octave} onChange={(event) => change('octave', event.target.value)} required /><small>文本简谱导入时使用，默认 1 = C3</small></label>
          <label className="field"><span>简谱速度 <small>BPM</small></span><input type="number" min="0.01" step="any" value={draft.bpm} onChange={(event) => change('bpm', event.target.value)} required /><small>文本简谱的基础节拍，默认 120 BPM</small></label>
        </fieldset>
      </section>
      <section className="panel settings-section"><div className="section-heading"><div><h2>九键映射</h2><p>将 MIDI 音高映射到一个字母或数字键，音高和按键都不可重复。</p></div><span className="badge">9 KEYS</span></div>
        <fieldset className="mapping-editor" disabled={locked}>
          {draft.mapping.map((entry, index) => <div className="mapping-column" key={index}><span className="mapping-index">{String(index + 1).padStart(2, '0')}</span>
            <input className="key-input" aria-label={`第 ${index + 1} 个映射按键`} value={entry.key} maxLength={1} onChange={(event) => change('mapping', draft.mapping.map((value, i) => i === index ? { ...value, key: event.target.value.toUpperCase() } : value))} required />
            <span className="mapping-arrow">↓</span><input type="number" min="0" max="127" step="1" aria-label={`第 ${index + 1} 个 MIDI 音高`} value={entry.pitch} onChange={(event) => change('mapping', draft.mapping.map((value, i) => i === index ? { ...value, pitch: event.target.value } : value))} required />
            <span className="pitch-label">{entry.pitch && Number.isInteger(Number(entry.pitch)) ? pitchName(Number(entry.pitch)) : '—'}</span>
          </div>)}
        </fieldset>
        <div className="mapping-legend"><span>上方：键盘按键</span><span>下方：MIDI 音高 · C4 = 60</span></div>
      </section>
      <div className="settings-save"><span className={error ? 'error-text' : 'muted'} role="status">{error || (saved ? '设置已保存。' : dirty ? '有尚未保存的修改' : '设置以桌面后端返回值为准')}</span><div className="button-row"><button className="button secondary" type="button" disabled={locked || !dirty} onClick={() => { setDraft(toDraft(settings)); setDirty(false); setError(''); setSaved(false); }}>放弃修改</button><button className="button primary" type="submit" disabled={locked || !dirty}><Icon name="check" size={17} />保存设置</button></div></div>
    </form>
    <section className="safety-panel"><div className="section-heading"><div><h2><Icon name="shield" size={19} />失焦保护与快捷键</h2><p>真实演奏必须先捕获目标窗口。目标失焦时，后端保护会阻止继续向错误窗口发送按键。</p></div></div>
      <div className="safety-columns"><div><p>点击播放后，请在倒计时内切回目标窗口。不要在真实演奏期间切换窗口或输入文字。若需要观察本界面，请使用无按键预览。</p><div className="hotkeys"><span><kbd>F8</kbd>播放</span><span><kbd>F9</kbd>暂停 / 继续</span><span><kbd>F10</kbd>停止</span></div><p className="fine-print">快捷键由桌面后端处理；浏览器界面预览不会注册全局热键。</p></div>
        <div className="calibration"><h3>真实 DD 按键校准</h3><p>这不是无按键预览。确认后将延迟 3 秒，向已捕获目标发送真实测试键。</p><div className="test-keys">测试键：{settings.mapping.map(({ key }) => key).join(' · ')}</div><div className="button-row"><button type="button" className="button secondary" disabled={locked} onClick={onCapture}><Icon name="target" size={16} />捕获目标</button><button type="button" className="button gold" disabled={locked || !targetReady || settings.backend !== 'DD' || dirty} onClick={onCalibrate}>确认并校准</button></div><small>{!targetReady ? '请先捕获目标。' : settings.backend !== 'DD' ? '请先保存 DD 后端设置。' : dirty ? '请先保存或放弃修改。' : '将再次弹出真实按键确认。'}</small></div></div>
    </section>
  </div>;
}
