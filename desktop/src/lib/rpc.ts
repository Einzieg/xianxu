import { invoke, isTauri } from '@tauri-apps/api/core';
import type { RpcParams, RpcResult } from '../types';

export const hasDesktopBridge = () => isTauri();

export async function setCompact(compact: boolean): Promise<boolean> {
  if (!hasDesktopBridge()) throw new Error('小窗功能仅在桌面应用中可用。');
  return invoke<boolean>('set_compact', { compact });
}

export async function rpc<M extends keyof RpcParams>(
  method: M,
  params: RpcParams[M] = {} as RpcParams[M],
): Promise<RpcResult<M>> {
  if (!hasDesktopBridge()) throw new Error('后端未连接，请在 Tauri 桌面应用中操作。');
  return invoke<RpcResult<M>>('rpc', { request: { method, params } });
}
