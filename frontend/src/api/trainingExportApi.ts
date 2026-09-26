import { tokenStore } from './http'

export interface ExportSettings {
  finalVersionIds: string[]
  format: 'yolo' | 'coco' | 'both'
  splitRatio: number
}
export interface ExportSummary {
  frameCount: number
  objectCount: number
  emptyFrames: number
  classNames: string[]
}
export interface ExportJob {
  exportId: string
  state: 'queued' | 'running' | 'ready' | 'failed' | 'invalidated'
  completedFrames: number
  totalFrames: number
  errorCode: string | null
  errorMessage: string | null
  downloadUrl: string | null
  manifest: { counts: ExportSummary & { train: number; val: number } } | null
}

async function request<T>(path: string, method = 'GET', body?: unknown, key?: string, blob = false): Promise<T> {
  try {
    const response = await fetch('/api/datasets' + path, {
      method,
      signal: AbortSignal.timeout(blob ? 300000 : 30000),
      headers: {
        Authorization: `Bearer ${tokenStore.get()}`,
        'Content-Type': 'application/json',
        ...(key ? { 'X-Review-Contract': '2', 'Idempotency-Key': key } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (!response.ok) {
      const data = await response.json().catch(() => ({}))
      throw {
        message: data.message || `导出请求失败（${response.status}）`,
        code: data.code,
        status: response.status,
        requestId: data.requestId || response.headers.get('X-Request-ID'),
      }
    }
    return (blob ? await response.blob() : await response.json()) as T
  } catch (error) {
    console.error('[dataset.request_failed]', { path, method, error })
    throw error
  }
}

export const trainingExportApi = {
  preview: (ids: string[]) => request<ExportSummary>('/exports/preview', 'POST', { finalVersionIds: ids }),
  create: (settings: ExportSettings, key: string) => request<ExportJob>('/exports', 'POST', settings, key),
  status: (id: string) => request<ExportJob>(`/exports/${id}`),
  download: (id: string) => request<Blob>(`/exports/${id}/download`, 'GET', undefined, undefined, true),
}
