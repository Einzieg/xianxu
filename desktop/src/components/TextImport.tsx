import { useEffect, useRef, useState } from 'react';
import Icon from './Icon';

export default function TextImport({ open, locked, octave, bpm, onClose, onImport }: {
  open: boolean;
  locked: boolean;
  octave: number;
  bpm: number;
  onClose: () => void;
  onImport: (title: string, text: string) => Promise<boolean>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
  return <dialog ref={dialog} className="text-dialog" onCancel={onClose} onClose={onClose}>
    <form onSubmit={(event) => {
      event.preventDefault();
      if (locked || !title.trim() || !text.trim()) return;
      void onImport(title.trim(), text.trim()).then((success) => {
        if (success) { setTitle(''); setText(''); onClose(); }
      });
    }}><div className="section-heading"><div><span className="eyebrow">PASTE A MELODY</span><h2>粘贴 TXT 曲谱</h2></div><button className="icon-button" type="button" aria-label="关闭粘贴曲谱" onClick={onClose}><Icon name="close" /></button></div>
      <label className="field"><span>曲名</span><input autoFocus required maxLength={120} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="给这段旋律起个名字" disabled={locked} /></label>
      <label className="field"><span>曲谱内容</span><textarea required rows={9} value={text} onChange={(event) => setText(event.target.value)} placeholder="粘贴后端支持的 TXT 曲谱内容…" disabled={locked} /></label>
      <p className="fine-print">当前文本简谱设置：1 = C{octave} · {bpm} BPM。可在演奏设置中修改。格式校验与九键适配由后端处理。</p><div className="dialog-actions"><button className="button secondary" type="button" onClick={onClose}>取消</button><button className="button primary" disabled={locked || !title.trim() || !text.trim()} type="submit">导入谱库</button></div>
    </form>
  </dialog>;
}
