import { useCallback, useEffect, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import type { ScoreDetail } from './types';
import { EMPTY_STATE, friendlyError } from './lib/model';
import { hasDesktopBridge, rpc } from './lib/rpc';

export function useBackend() {
  const [state, setState] = useState(EMPTY_STATE);
  const [connection, setConnection] = useState<'offline' | 'connecting' | 'connected' | 'error'>(
    hasDesktopBridge() ? 'connecting' : 'offline',
  );
  const [error, setError] = useState('');
  const barriers = useRef<{ after: number; resolve: () => void }[]>([]);
  const waitForPoll = useCallback(() => new Promise<void>((resolve) => {
    if (!hasDesktopBridge()) return resolve();
    barriers.current.push({ after: performance.now(), resolve });
  }), []);

  useEffect(() => {
    if (!hasDesktopBridge()) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Schedule only after settlement: slow bridge requests cannot stack up.
    const poll = async () => {
      const started = performance.now();
      try {
        const next = await rpc('get_state');
        if (disposed) return;
        setState(next);
        setConnection('connected');
        setError('');
      } catch (cause) {
        if (disposed) return;
        setConnection('error');
        setError(friendlyError(cause));
      } finally {
        barriers.current = barriers.current.filter((barrier) => {
          if (started < barrier.after) return true;
          barrier.resolve();
          return false;
        });
        if (!disposed) timer = setTimeout(poll, 400);
      }
    };
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      barriers.current.forEach((barrier) => barrier.resolve());
      barriers.current = [];
    };
  }, []);

  return { state, connection, connectionError: error, connected: connection === 'connected', waitForPoll };
}

export function useScore(id: string | null, connected: boolean, mappingSignature: string) {
  const [detail, setDetail] = useState<ScoreDetail | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let disposed = false;
    setDetail(null);
    setError('');
    setLoading(Boolean(id && connected));
    if (!id || !connected) return;
    void rpc('get_score', { id }).then((result) => {
      if (!disposed) setDetail(result);
    }).catch((cause: unknown) => {
      if (!disposed) setError(friendlyError(cause));
    }).finally(() => {
      if (!disposed) setLoading(false);
    });
    return () => { disposed = true; };
  }, [id, connected, mappingSignature, retry]);
  return { detail, error, loading, retry: () => setRetry((value) => value + 1) };
}

export function useShutdown() {
  const [closing, setClosing] = useState(false);
  const [shutdownError, setShutdownError] = useState('');
  const [listenerError, setListenerError] = useState('');
  useEffect(() => {
    if (!hasDesktopBridge()) return;
    let disposed = false;
    const cleanups: (() => void)[] = [];
    const subscribe = async () => {
      try {
        for (const [name, callback] of [
          ['engine-shutdown-started', () => { setClosing(true); setShutdownError(''); }],
          ['engine-shutdown-error', (message: string) => { setClosing(true); setShutdownError(message || '按键或工作任务未能完全释放。'); }],
        ] as const) {
          const unlisten = await listen<{ message?: string }>(name, (event) => {
            if (!disposed) callback(event.payload?.message || '');
          });
          if (disposed) unlisten();
          else cleanups.push(unlisten);
        }
      } catch (cause) {
        if (!disposed) setListenerError(`无法监听宿主关闭状态：${friendlyError(cause)}`);
      }
    };
    void subscribe();
    return () => { disposed = true; cleanups.forEach((cleanup) => cleanup()); };
  }, []);
  return { closing, shutdownError, listenerError };
}
