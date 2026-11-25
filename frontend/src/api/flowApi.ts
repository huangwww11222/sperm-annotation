import { FrameCache } from '../annotation/frameCache'
import type { FlowAnswer, FlowDifference, FlowItem, FlowRecheck, FlowMisses, FlowProgress, FlowTile } from '../flow/flowLogic'
import { createRequestId, withRequestTimeout } from '../utils/browserCompat'
import { notifyAuthExpired, tokenStore } from './http'

export interface FlowDetail {
  mediaId: string; sample: string; cell: number; progress: FlowProgress
  pages: FlowTile[][]; picks: Record<string, { picked: string[]; revision: number }>
  second: FlowTile[] | null; answers: Record<string, { answer: FlowAnswer; dropped: number[]; revision: number }>
  scan: { frame: number; width: number; height: number; points: number[][]; done: boolean }
  recheck: FlowRecheck | null; imported: boolean; differences?: FlowDifference[] | null
}
export interface FlowExport {
  exportId: string; state: 'queued' | 'running' | 'ready' | 'failed'; completedFrames: number; totalFrames: number; videoCount: number; step: number
  counts: { train: number; val: number; sperm: number; ignore: number } | null; errorMessage: string | null; createdAt: string; downloadUrl: string | null
}
export interface FlowFailure { message: string; status: number; code?: string; requestId?: string }

async function request<T>(path: string, method = 'GET', body?: unknown, key?: string): Promise<T> {
  try {
    return await withRequestTimeout(30000, async signal => {
      const response = await fetch('/api/flow' + path, {
        method, signal,
        headers: { Authorization: `Bearer ${tokenStore.get()}`, 'Content-Type': 'application/json', ...(key ? { 'X-Review-Contract': '2', 'Idempotency-Key': key } : {}) },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        if (response.status === 401) notifyAuthExpired()
        throw { status: response.status, message: data?.message || (typeof data?.detail === 'string' ? data.detail : response.status >= 500 ? '服务器没有响应，请稍后重试' : `请求失败（${response.status}）`), code: data?.code, requestId: data?.requestId || response.headers.get('X-Request-ID') || undefined } as FlowFailure
      }
      if (data === null) throw { status: 0, message: '服务器响应无法读取，请重试' } as FlowFailure
      return data as T
    })
  } catch (error) {
    const failure: FlowFailure = typeof (error as FlowFailure)?.status === 'number' ? error as FlowFailure : { status: 0, message: '连接后端失败或请求超时，请重试' }
    console.error('[flow.request_failed]', { method, path, ...failure })
    throw failure
  }
}

// Strips are small and never change within a run; judged pages are often looked at again.
const images = new FrameCache(800, 64 * 1024 * 1024)
let owner = ''
function image(path: string): Promise<Blob> {
  const token = tokenStore.get()
  if (owner !== token) { owner = token; images.clear() }
  return images.get(path, () => withRequestTimeout(30000, async signal => {
    const response = await fetch('/api/flow' + path, { signal, headers: { Authorization: `Bearer ${token}` } })
    if (!response.ok) throw { status: response.status, message: '图片加载失败' } as FlowFailure
    const blob = await response.blob()
    if (!blob.size) throw { status: 0, message: '图片是空的' } as FlowFailure
    return blob
  }))
}

const media = (id: string) => `/media/${encodeURIComponent(id)}`
export type FlowView = 'e' | 'r'
export const flowApi = {
  list: () => request<{ items: FlowItem[] }>('/media'),
  add: (mediaIds: string[], sample: string) => request<{ added: string[]; queued: string[] }>('/media', 'PUT', { mediaIds, sample }),
  remove: (mediaIds: string[]) => request<{ removed: string[] }>('/media/remove', 'POST', { mediaIds }),
  find: (id: string, force = false) => request<{ taskId: string; status: string }>(media(id) + '/find', 'POST', { force }, createRequestId()),
  detail: (id: string, recheck = false) => request<FlowDetail>(media(id) + (recheck ? '?recheck=true' : '')),
  startRecheck: (id: string) => request<FlowRecheck>(media(id) + '/recheck', 'POST'),
  cancelRecheck: (id: string, recheckId: string) => request<{ cancelled: boolean }>(`${media(id)}/recheck/${encodeURIComponent(recheckId)}`, 'DELETE'),
  compareRecheck: (id: string, recheckId: string) => request<FlowDetail>(`${media(id)}/recheck/${encodeURIComponent(recheckId)}/compare`, 'POST'),
  settle: (id: string, recheckId: string, key: string, body: { answer: FlowAnswer; dropped: number[]; revision: number }) =>
    request<{ key: string; answer: FlowAnswer; dropped: number[]; revision: number; recheck: FlowRecheck }>(`${media(id)}/recheck/${encodeURIComponent(recheckId)}/settles/${encodeURIComponent(key)}`, 'PUT', body),
  savePage: (id: string, page: number, body: { runId: string; picked: string[]; ms: number; revision: number }) =>
    request<{ page: number; picked: string[]; revision: number; progress: FlowProgress }>(`${media(id)}/pages/${page}`, 'PUT', body),
  saveAnswer: (id: string, key: string, body: { runId: string; answer: FlowAnswer; dropped: number[]; ms: number; revision: number }) =>
    request<{ key: string; answer: FlowAnswer; dropped: number[]; revision: number; progress: FlowProgress }>(`${media(id)}/answers/${encodeURIComponent(key)}`, 'PUT', body),
  saveScan: (id: string, body: { runId: string; points: number[][]; ms: number }) =>
    request<{ done: boolean; points: number[][]; progress: FlowProgress }>(media(id) + '/scan', 'PUT', body),
  misses: () => request<FlowMisses>('/misses'),
  exports: () => request<{ items: FlowExport[] }>('/exports'),
  createExport: (body: { mediaIds: string[]; step: number; trainRatio?: number; includeSourceVideos?: boolean }, key: string) => request<FlowExport>('/exports', 'POST', body, key),
  removeExport: (id: string) => request<{ deleted: boolean }>(`/exports/${id}`, 'DELETE'),
  strip: (id: string, run: string, key: string, view: FlowView) => image(`${media(id)}/strips/${encodeURIComponent(key)}?run=${encodeURIComponent(run)}&view=${view}`),
  scanImage: (id: string, run: string, view: FlowView) => image(`${media(id)}/scan-image?run=${encodeURIComponent(run)}&view=${view}`),
}
