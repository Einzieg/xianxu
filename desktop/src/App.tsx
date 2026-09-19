import { useEffect, useRef, useState } from 'react';
import { confirm, open, save } from '@tauri-apps/plugin-dialog';
import Icon from './components/Icon';
import type { IconName } from './components/Icon';
import ScorePanel from './components/ScorePanel';
import SettingsPage from './components/SettingsPage';
import TranscribePage from './components/TranscribePage';
import TextImport from './components/TextImport';
import { useBackend, useScore, useShutdown } from './hooks';
import { filterLibrary, formatTime, friendlyError, getLocks, hasTarget, MODES, percent, pitchName } from './lib/model';
import { rpc, setCompact } from './lib/rpc';
import { runTransport, transportAction } from './lib/transport';
import { AUDITION_STOP_EVENT } from './lib/audition';
import type { LibraryItem, PlayMode, RpcParams } from './types';

type Page = 'library' | 'transcribe' | 'settings';
const NAV: { id: Page; label: string; icon: IconName }[] = [
  { id: 'library', label: '我的谱库', icon: 'library' },
  { id: 'transcribe', label: '音频转谱', icon: 'audio' },
  { id: 'settings', label: '演奏设置', icon: 'settings' },
];
const PLAYBACK_LABELS = {
  idle: '等待播放', countdown: '准备演奏', playing: '正在播放', paused: '已暂停',
  stopped: '已停止', finished: '播放完成', error: '播放异常',
};

export default function App() {
  const { state, connected, connection, connectionError, waitForPoll } = useBackend();
  const { closing, shutdownError, listenerError } = useShutdown();
  const [page, setPage] = useState<Page>('library');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [audioPath, setAudioPath] = useState('');
  const [textOpen, setTextOpen] = useState(false);
  const [pending, setPending] = useState('');
  const commandBusy = useRef(false);
  const [notice, setNotice] = useState<{ kind: 'error' | 'success'; message: string } | null>(null);
  const [dismissedJob, setDismissedJob] = useState('');
  const [compact, setCompactView] = useState(false);
  const [windowPending, setWindowPending] = useState(false);
  const [autoCompact, setAutoCompact] = useState(() => {
    try { return localStorage.getItem('auto-compact') !== 'false'; } catch { return true; }
  });
  const stopGeneration = useRef(0);

  useEffect(() => {
    if (state.current_id) setSelectedId(state.current_id);
  }, [state.current_id]);
  useEffect(() => {
    if (!selectedId || !state.library.some((item) => item.id === selectedId)) {
      setSelectedId(state.library.find((item) => item.id === state.current_id)?.id ?? state.library[0]?.id ?? null);
    }
  }, [selectedId, state.current_id, state.library]);

  const selected = state.library.find((item) => item.id === selectedId);
  const current = state.library.find((item) => item.id === state.current_id);
  const score = useScore(selectedId, connected, JSON.stringify(state.settings.mapping));
  const locks = getLocks(state, connected, Boolean(pending));
  const mutateLocked = locks.mutate || closing;
  const targetReady = hasTarget(state);
  const preview = state.preview;
  const displayPreview = state.preview;
  const list = filterLibrary(state.library, search);
  const mode = MODES.find((value) => value.value === state.mode) ?? MODES[0];
  const canToggle = connected && !pending && !windowPending && !locks.job && !closing &&
    (['playing', 'countdown'].includes(state.playback.state) || (state.playback.state === 'paused' && (state.preview || targetReady)));
  const canStart = !locks.start && !windowPending && !closing && Boolean(selected) && (preview || targetReady);
  const canSkip = !locks.transport && !windowPending && !closing && Boolean(current);
  const statusLabel = !connected ? (connection === 'connecting' ? '正在连接' : '后端未连接') : PLAYBACK_LABELS[state.playback.state];
  const action = transportAction(state);
  const playTitle = action === 'pause' ? '暂停播放' : action === 'resume' ? '继续播放' : preview ? '开始无按键预览' : '开始真实演奏';
  const playerMessage = shutdownError || (closing ? '正在关闭并释放按键…' : '') ||
    (notice?.kind === 'error' ? notice.message : '') || connectionError ||
    (pending ? '正在处理…' : state.playback.message) ||
    (displayPreview ? '仅检查时间轴 · 不发声 · 不发送按键' : targetReady ? '播放 / 继续后自动切回游戏' : '请先捕获目标，或启用无按键预览');
  const job = state.job;
  const showJob = job && (job.state === 'running' || dismissedJob !== `${job.id}:${job.state}`);

  async function task(label: string, work: () => Promise<boolean | void>, success?: string) {
    if (!connected || commandBusy.current) return false;
    commandBusy.current = true;
    setPending(label);
    setNotice(null);
    try {
      const result = await work();
      if (result === false) return false;
      // Keep controls locked until a poll begun after the command observes its effects.
      await waitForPoll();
      if (success) setNotice({ kind: 'success', message: success });
      return true;
    } catch (cause) {
      setNotice({ kind: 'error', message: friendlyError(cause) });
      return false;
    } finally {
      commandBusy.current = false;
      setPending('');
    }
  }

  function command<M extends keyof RpcParams>(method: M, params: RpcParams[M], success?: string) {
    if (closing && method !== 'stop' && method !== 'cancel_job') return Promise.resolve(false);
    return task(method, async () => { await rpc(method, params); }, success);
  }

  const capture = () => {
    if (!mutateLocked) void command('capture', { delay: 3 });
  };

  function importScore() {
    if (mutateLocked) return;
    void task('import_score', async () => {
      const path = await open({ multiple: false, directory: false, title: '导入曲谱', filters: [{ name: '曲谱 · TXT / MIDI', extensions: ['txt', 'mid', 'midi'] }] });
      if (!path || Array.isArray(path)) return false;
      await rpc('import_score', { path });
    }, '曲谱已导入，谱库由后端持久保存。');
  }

  function pickAudio() {
    if (mutateLocked) return;
    void task('pick_audio', async () => {
      const path = await open({ multiple: false, directory: false, title: '选择待转谱音频', filters: [{ name: '音频', extensions: ['mp3', 'wav', 'flac', 'ogg'] }] });
      if (!path || Array.isArray(path)) return false;
      setAudioPath(path);
    });
  }

  function exportScore() {
    if (!selected || mutateLocked) return;
    void task('export', async () => {
      const path = await save({ title: '导出 MIDI 曲谱', defaultPath: `${selected.title.replace(/[<>:"/\\|?*]/g, '_')}.mid`, filters: [{ name: 'MIDI 曲谱', extensions: ['mid', 'midi'] }] });
      if (!path) return false;
      await rpc('export', { id: selected.id, path });
    }, '曲谱已导出。');
  }

  function deleteScore(item: LibraryItem | undefined = selected) {
    if (!item || mutateLocked) return;
    void task('remove', async () => {
      const accepted = await confirm(`从谱库删除「${item.title}」？\n\n此操作不可撤销，但不会删除原始音频、TXT 或 MIDI 文件。`, {
        title: '删除曲谱', kind: 'warning', okLabel: '删除', cancelLabel: '取消',
      });
      if (!accepted) return false;
      window.dispatchEvent(new Event(AUDITION_STOP_EVENT));
      await rpc('remove', { id: item.id });
    }, `「${item.title}」已从谱库删除，原始文件未删除。`);
  }

  function calibrate() {
    if (mutateLocked || !targetReady || state.settings.backend !== 'DD') return;
    if (window.confirm(`真实 DD 按键测试（不是预览）\n\n目标：${state.target!.title}\n真实测试键：${state.settings.mapping.map(({ key }) => key).join('、')}\n\n确认后有 3 秒切回目标窗口。测试会实际发送按键。确认开始？`)) {
      void command('calibrate', { delay: 3 });
    }
  }

  function togglePlayback() {
    if (!canToggle && !canStart) return;
    const generation = stopGeneration.current;
    window.dispatchEvent(new Event(AUDITION_STOP_EVENT));
    void task(action, () => runTransport(action, state, selected?.id, {
      prepareWindow: async () => {
        if (autoCompact && !compact) {
          const small = await setCompact(true);
          setCompactView(small);
        }
      },
      cancelled: () => generation !== stopGeneration.current,
      send: rpc,
    }));
  }

  function changeWindow(small: boolean) {
    if (pending || windowPending) return;
    setWindowPending(true);
    void setCompact(small).then(setCompactView)
      .catch((cause: unknown) => setNotice({ kind: 'error', message: friendlyError(cause) }))
      .finally(() => setWindowPending(false));
  }

  function changeAutoCompact(enabled: boolean) {
    setAutoCompact(enabled);
    try { localStorage.setItem('auto-compact', String(enabled)); } catch { /* Session preference still applies. */ }
  }

  function emergencyStop() {
    stopGeneration.current += 1;
    window.dispatchEvent(new Event(AUDITION_STOP_EVENT));
    // Never gate emergency release on the editor/job command lock.
    if (!connected) return;
    void rpc('stop', {}).catch((cause: unknown) => setNotice({ kind: 'error', message: friendlyError(cause) }));
  }

  return <div className={`app-shell ${compact ? 'compact-shell' : ''}`}>
    {compact && <header className="compact-heading"><span><Icon name="music" size={15} />弦序 · 置顶小窗</span><button className="button secondary compact" disabled={Boolean(pending) || windowPending} onClick={() => changeWindow(false)} aria-label="展开谱库">展开谱库 <Icon name="expand" size={14} /></button></header>}
    <aside className="sidebar">
      <a className="brand" href="#library" onClick={(event) => { event.preventDefault(); setPage('library'); }} aria-label="弦序，返回谱库"><span className="brand-mark"><i /><i /><i /><i /></span><span><strong>弦序<span className="brand-period">.</span></strong><small>NINE KEY PLAYER</small></span></a>
      <div className="sidebar-label">你的音乐空间</div>
      <nav aria-label="主导航">{NAV.map((entry) => <button key={entry.id} className={`nav-item ${page === entry.id ? 'active' : ''}`} aria-current={page === entry.id ? 'page' : undefined} onClick={() => setPage(entry.id)}><Icon name={entry.icon} size={20} /><span>{entry.label}</span>{entry.id === 'library' && <span className="nav-count">{state.library.length}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="target-mini"><span className={`status-dot ${connected && targetReady ? 'online' : ''}`} /><span>{connected && targetReady ? '目标已捕获' : '尚未捕获目标'}</span></div><p title={state.target?.title}>{connected && state.target?.title ? state.target.title : '真实演奏前，请先连接目标窗口'}</p><div className="sidebar-footer"><span>DESKTOP / 01</span><span>{connected ? state.settings.backend : 'OFFLINE'}</span></div></div>
    </aside>

    <main className="main-content" id="main-content">
      <div className="topbar"><span><span className={`status-dot ${connected ? 'online' : ''}`} />{connected ? '桌面引擎已连接' : connection === 'connecting' ? '正在连接桌面引擎' : '仅界面预览 · 后端未连接'}</span><div className="window-options"><label><input type="checkbox" checked={autoCompact} onChange={(event) => changeAutoCompact(event.target.checked)} />播放时自动小窗</label><button className="text-button" disabled={!connected || Boolean(pending) || closing} onClick={() => changeWindow(true)}><Icon name="shrink" size={14} />置顶小窗</button></div></div>

      {connection === 'offline' && <div className="notice offline-notice"><Icon name="info" size={18} /><div><strong>现在是浏览器界面预览</strong><p>未连接 Tauri 后端。导入、保存和播放不可用；这里不会模拟连接或生成虚构曲目。</p></div></div>}
      {connection === 'error' && <div className="notice error-notice" role="alert"><Icon name="info" /><div><strong>后端连接中断 · 正在重试</strong><p>{connectionError}</p><p>如果真实演奏尚未停止，请切回目标并尝试 F10；当前显示可能是最后一次状态。</p></div></div>}
      {listenerError && <div className="notice error-notice" role="alert"><Icon name="info" /><p>{listenerError}</p></div>}
      {closing && <div className={`notice ${shutdownError ? 'error-notice' : 'gold-notice'}`} role="alert"><Icon name="shield" /><div><strong>{shutdownError ? '关闭失败：尚未确认安全释放' : '正在关闭并释放按键与任务…'}</strong><p>{shutdownError || '新操作已锁定。等待宿主完成释放，请勿强制退出。'}</p>{shutdownError && <p>可再次停止或取消任务，然后重试关闭窗口。界面不会把请求成功误报为已经安全退出。</p>}<div className="button-row"><button className="button secondary compact" disabled={!connected || Boolean(pending)} onClick={() => void command('stop', {})}>再次停止 / 释放按键</button><button className="button secondary compact" disabled={!connected || Boolean(pending)} onClick={() => void command('cancel_job', {})}>再次取消任务</button></div></div></div>}
      {notice && <div className={`notice ${notice.kind === 'error' ? 'error-notice' : 'success-notice'}`} role={notice.kind === 'error' ? 'alert' : 'status'}><Icon name={notice.kind === 'error' ? 'info' : 'check'} size={18} /><p>{notice.message}</p><button className="icon-button" aria-label="关闭提示" onClick={() => setNotice(null)}><Icon name="close" size={17} /></button></div>}
      {showJob && <section className={`job-banner ${job.state === 'error' ? 'job-error' : ''}`} aria-label="后台任务"><div className="job-icon"><Icon name={job.state === 'done' ? 'check' : job.kind === 'capture' ? 'target' : 'audio'} /></div><div className="job-info"><div><strong>{job.kind === 'capture' ? '捕获目标窗口' : job.kind === 'calibrate' ? '真实 DD 按键校准' : '音频转谱'}</strong><span>{job.state === 'running' ? `${percent(job.progress)}%` : job.state === 'done' ? '已完成' : job.state === 'cancelled' ? '已取消' : '任务失败'}</span></div><p role={job.state === 'error' ? 'alert' : undefined}>{job.error || job.message || '任务正在处理…'}{job.kind === 'capture' && job.state === 'running' ? ' · 请在 3 秒内切换到目标窗口，不会发送测试键。' : ''}</p>{job.state === 'running' && <progress max="1" value={Math.max(0, Math.min(1, job.progress))} aria-label="任务进度" />}</div>{job.state === 'running' ? <button className="button secondary compact" disabled={!connected || Boolean(pending)} onClick={() => void command('cancel_job', {})}>取消任务</button> : <button className="icon-button" aria-label="关闭任务结果" onClick={() => setDismissedJob(`${job.id}:${job.state}`)}><Icon name="close" size={18} /></button>}</section>}

      {page === 'library' && <div className="library-page">
        <header className="page-heading"><div><div className="eyebrow">YOUR PERSONAL COLLECTION</div><h1>我的谱库<span className="title-dot">.</span></h1><p>收藏旋律，在九个键上重新相遇。</p></div><div className="button-row"><button className="button secondary" disabled={mutateLocked} onClick={() => setTextOpen(true)}><Icon name="text" size={17} />粘贴曲谱</button><button className="button primary" disabled={mutateLocked} onClick={importScore}><Icon name="plus" size={19} />导入曲谱</button></div></header>
        <section className="performance-banner"><div className="banner-icon"><Icon name="shield" size={24} /></div><div className="banner-copy"><h2>{displayPreview ? '只看时间轴，不发送按键' : targetReady && connected ? '目标就绪，等待你的下一段旋律' : '先连接目标，再安心演奏'}</h2><p>{displayPreview ? '无按键预览不会发声，也不会操作任何窗口。' : state.target && connected ? `${state.target.title} · ${state.target.state} · 失焦保护由后端执行` : '真实演奏将发送键盘输入。捕获后，请在播放倒计时内切回目标。'}</p></div><button className="button capture-button" disabled={mutateLocked} onClick={capture}><Icon name="target" size={16} />{targetReady ? '重新捕获' : '捕获目标'}<span>3s</span></button></section>
        <section className="key-rack" aria-label="当前九键映射"><div className="key-rack-label"><span className="eyebrow">KEY MAPPING</span><strong>九键音域</strong><span>{connected ? '当前演奏映射' : '默认映射示意'}</span></div><div className="key-list">{state.settings.mapping.map((entry, index) => <div className={`key-tile ${state.playback.pressed.includes(entry.key) && connected ? 'pressed' : ''}`} key={`${entry.key}-${index}`}><span className="key-number">{String(index + 1).padStart(2, '0')}</span><kbd>{entry.key}</kbd><span>{pitchName(entry.pitch)} <small>{entry.pitch}</small></span></div>)}</div><button className="icon-button" title="编辑九键映射" aria-label="编辑九键映射" onClick={() => setPage('settings')}><Icon name="settings" size={18} /></button></section>
        <div className="library-grid"><section className="panel library-panel" aria-labelledby="collection-heading"><div className="panel-heading"><h2 id="collection-heading">全部曲目 <span className="count-pill">{state.library.length}</span></h2><span className="eyebrow">{connected ? 'PERSISTENT LIBRARY' : 'YOUR LIBRARY'}</span></div><label className="search-box"><Icon name="search" size={18} /><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索曲名或来源…" aria-label="搜索谱库" /><span>TXT / MIDI</span></label>
          {state.library.length === 0 ? <div className="library-empty"><div className="empty-art"><span /><span /><span /><Icon name="music" size={39} /></div><h3>每一份谱库，都始于一段旋律</h3><p>导入 TXT、MIDI 曲谱，<br />或将音频转为可演奏的九键谱。</p><button className="button secondary" disabled={mutateLocked} onClick={importScore}><Icon name="import" size={17} />导入第一首曲谱</button><button className="text-button" onClick={() => setPage('transcribe')}>试试音频转谱<Icon name="arrow" size={15} /></button></div> : list.length === 0 ? <div className="search-empty"><Icon name="search" size={30} /><h3>没有找到匹配曲目</h3><p>换个关键词，或清空搜索再看看。</p><button className="text-button" onClick={() => setSearch('')}>清空搜索</button></div> :
            <div className="track-list" role="list" aria-label="曲谱列表">{list.map((item, index) =>
              <div key={item.id} role="listitem" className={`track-row ${item.id === selectedId ? 'selected' : ''}`}>
                <button className="track-select" disabled={mutateLocked} aria-current={item.id === selectedId ? 'true' : undefined} onClick={() => { void command('select', { id: item.id }); }}>
                  <span className="track-index">{item.id === current?.id && state.playback.state === 'playing' ? <Icon name="audio" size={17} /> : String(index + 1).padStart(2, '0')}</span>
                  <span className="track-art"><Icon name="music" size={18} /></span>
                  <span className="track-name"><strong>{item.title}</strong><small>{item.note_count.toLocaleString()} 个音符 · {item.source.split(/[\\/]/).pop()}</small></span>
                  <span className="track-duration">{formatTime(item.duration)}</span>{item.id === selectedId && <span className="selected-dot" />}
                </button>
                <button className="button secondary compact danger track-delete" disabled={mutateLocked} aria-label={`删除曲谱：${item.title}`} title={mutateLocked ? '请先停止演奏并等待当前任务完成' : `从谱库删除「${item.title}」，保留原始文件`} onClick={() => deleteScore(item)}><Icon name="trash" size={14} />删除</button>
              </div>
            )}</div>}
          <div className="library-footnote"><Icon name="folder" size={14} /><span>{connected ? '曲目由后端持久保存，重启后仍在这里' : '连接桌面后端后读取已保存的谱库'}</span></div></section>
          <ScorePanel item={selected} detail={score.detail?.item.id === selectedId ? score.detail : null} loading={score.loading} error={score.error} onRetry={score.retry} position={selectedId === state.current_id ? state.playback.position : 0} locked={mutateLocked} stopSequence={state.stop_sequence ?? 0} onExport={exportScore} onDelete={() => deleteScore()} />
        </div>
        <div className="library-bottom-note"><span><Icon name="shield" size={14} />真实演奏受目标窗口与失焦保护约束</span><span><kbd>F8</kbd>播放 <kbd>F9</kbd>暂停 / 继续 <kbd>F10</kbd>停止</span></div>
      </div>}

      {page === 'transcribe' && <TranscribePage path={audioPath} onPick={pickAudio} onStart={(engine, register, texture) => { if (!mutateLocked && audioPath) void command('transcribe', { path: audioPath, engine, register, texture }); }} locked={mutateLocked} job={state.job} result={score.detail?.item.id === state.current_id ? score.detail : null} onLibrary={() => setPage('library')} />}
      {page === 'settings' && <SettingsPage settings={state.settings} locked={mutateLocked} connected={connected} targetReady={targetReady} onSave={(settings) => command('set_settings', settings)} onCapture={capture} onCalibrate={calibrate} />}
      {locks.active && <div className="activity-hint"><Icon name="info" size={15} />{state.playback.state === 'paused' ? '当前已暂停，仍保留播放会话。' : '演奏会话进行中。'}导入、转谱与设置已锁定；停止后即可修改。</div>}
    </main>

    <footer className="player" aria-label="播放控制台">
      <div className="player-track"><div className={`mini-cover ${state.playback.state === 'playing' ? 'is-playing' : ''}`}><Icon name="music" size={23} /></div><div className="min-width-zero"><strong title={current?.title || selected?.title}>{current?.title || selected?.title || '等待你的第一首旋律'}</strong><span className={state.playback.state === 'error' ? 'error-text' : ''}><span className={`status-dot ${state.playback.state === 'playing' && connected ? 'online' : ''}`} />{statusLabel}{connected && locks.active ? state.preview ? ' · 无按键 · 无声' : ` · ${state.settings.backend} 真实输入` : ''}</span></div></div>
      <div className="player-center"><div className="transport"><button className="icon-button" title="上一首" aria-label="上一首" disabled={!canSkip} onClick={() => void command('previous', { activate_target: !preview, stop_sequence: state.stop_sequence })}><Icon name="previous" size={20} /></button><button className="play-button" title={playTitle} aria-label={playTitle} disabled={!canToggle && !canStart} onClick={togglePlayback}><Icon name={action === 'pause' ? 'pause' : 'play'} size={23} /></button><button className="icon-button" title="下一首" aria-label="下一首" disabled={!canSkip} onClick={() => void command('next', { activate_target: !preview, stop_sequence: state.stop_sequence })}><Icon name="next" size={20} /></button><button className="icon-button stop-button" title="停止播放、试听并释放按键" aria-label="停止播放、试听并释放按键" onClick={emergencyStop}><Icon name="stop" size={18} /></button></div><div className="player-timeline"><span>{formatTime(state.playback.position)}</span><progress max="1" value={state.playback.duration > 0 ? Math.max(0, Math.min(1, state.playback.position / state.playback.duration)) : 0} aria-label="播放进度（只读，不支持跳转）" /><span>{formatTime(state.playback.duration || current?.duration || selected?.duration || 0)}</span></div><span className="player-message" role={notice?.kind === 'error' || shutdownError ? 'alert' : 'status'} title={playerMessage}>{playerMessage}</span></div>
      <div className="player-options"><label className="mode-select" title={mode.hint}><Icon name={state.mode === 'random' ? 'shuffle' : state.mode === 'order' ? 'order' : 'repeat'} size={18} /><select aria-label="播放模式" value={state.mode} disabled={!connected || Boolean(pending) || locks.job || closing} onChange={(event) => void command('set_mode', { mode: event.target.value as PlayMode })}>{MODES.map((entry) => <option key={entry.value} value={entry.value}>{entry.label}</option>)}</select></label><label className={`preview-switch ${displayPreview ? 'enabled' : ''}`} title="同步 F8 播放模式：仅推进时间轴，不发送任何按键，也不发声"><input type="checkbox" checked={displayPreview} disabled={mutateLocked} onChange={(event) => void command('set_preview', { preview: event.target.checked })} /><span className="switch-track" /><span>无按键预览<small>不发声</small></span></label></div>
    </footer>
    <TextImport open={textOpen} locked={mutateLocked} octave={state.settings.octave} bpm={state.settings.bpm} onClose={() => setTextOpen(false)} onImport={(title, text) => command('add_text', { title, text }, '文本曲谱已入库。')} />
  </div>;
}
