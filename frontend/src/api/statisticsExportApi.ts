import { notifyAuthExpired, tokenStore } from './http'
import { withRequestTimeout } from '../utils/browserCompat'

export interface StatisticsExportJob {
  exportId: string
  state: 'queued' | 'running' | 'ready' | 'failed'
  stage: 'queued' | 'reading' | 'compressing' | 'ready' | 'failed'
  scope: 'instance'
  schemaVersion: 'annotation-confirmation-statistics-v1'
  progress: { completedTables: number; totalTables: number; rowsWritten: number; currentTable: string | null }
  createdAt: string
  updatedAt: string
  expiresAt: string
  fileName: string | null
  sizeBytes: number | null
  downloadAllowed: boolean
  error: { code: string; message: string } | null
}

export interface StatisticsExportError {
  message: string
  code?: string
  status?: number
  requestId?: string | null
}

function validateJob(value: unknown): StatisticsExportJob {
  const job = value as StatisticsExportJob | null
  const count = (n: unknown) => typeof n === 'number' && Number.isSafeInteger(n) && n >= 0
  if (!job || typeof job.exportId !== 'string' || !job.exportId
    || !['queued', 'running', 'ready', 'failed'].includes(job.state)
    || !['queued', 'reading', 'compressing', 'ready', 'failed'].includes(job.stage)
    || job.scope !== 'instance' || job.schemaVersion !== 'annotation-confirmation-statistics-v1'
    || !job.progress || !count(job.progress.completedTables) || !count(job.progress.totalTables)
    || !count(job.progress.rowsWritten) || job.progress.completedTables > job.progress.totalTables
    || !(job.progress.currentTable === null || typeof job.progress.currentTable === 'string')
    || typeof job.downloadAllowed !== 'boolean'
    || typeof job.createdAt !== 'string' || typeof job.updatedAt !== 'string' || typeof job.expiresAt !== 'string'
    || !(job.fileName === null || typeof job.fileName === 'string')
    || !(job.sizeBytes === null || count(job.sizeBytes))
    || !(job.error === null || (typeof job.error?.code === 'string' && typeof job.error?.message === 'string'))
    || (job.downloadAllowed && job.state !== 'ready')
    || (job.state === 'ready' && (!job.downloadAllowed || job.error !== null || typeof job.fileName !== 'string' || !job.fileName || !job.sizeBytes))
    || (job.state === 'failed' && (!job.error || !job.error.code || !job.error.message))) {
    throw { message: '统计导出任务响应损坏，请重试原请求', code: 'STATISTICS_EXPORT_INVALID_RESPONSE', status: 0 }
  }
  return job
}

async function request<T>(path: string, method: 'POST' | 'GET', signal?: AbortSignal, key?: string, download = false): Promise<T> {
  try {
    return await withRequestTimeout(download ? 300000 : 30000, async timeoutSignal => {
      const token = tokenStore.get()
      const response = await fetch(`/api/statistics/exports${path}`, {
        method,
        signal: timeoutSignal,
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(method === 'POST' ? { 'Content-Type': 'application/json' } : {}),
          ...(key ? { 'Idempotency-Key': key } : {}),
        },
        body: method === 'POST' ? '{}' : undefined,
      })
      const requestId = response.headers.get('X-Request-ID')
      if (!response.ok) {
        const data = await response.json().catch(() => null)
        if (response.status === 401) notifyAuthExpired()
        throw {
          message: data?.message || (typeof data?.detail === 'string' ? data.detail : `统计导出请求失败（${response.status}）`),
          code: data?.code,
          status: response.status,
          requestId: data?.requestId || requestId,
        } satisfies StatisticsExportError
      }
      if (download) {
        if (!response.headers.get('Content-Type')?.toLowerCase().includes('zip')) {
          throw { message: '服务器未返回有效 ZIP 文件，请重试下载', code: 'STATISTICS_EXPORT_INVALID_DOWNLOAD', requestId }
        }
        const blob = await response.blob()
        if (!blob.size) throw { message: '统计数据 ZIP 为空，请重试下载', code: 'STATISTICS_EXPORT_INVALID_DOWNLOAD', requestId }
        return blob as T
      }
      try { return validateJob(await response.json()) as T }
      catch (error) {
        const failure = error as StatisticsExportError
        throw { message: failure.message || '统计导出响应无法读取，请重试原请求', code: failure.code || 'STATISTICS_EXPORT_INVALID_RESPONSE', status: 0, requestId }
      }
    }, signal)
  } catch (error) {
    if (!(error instanceof DOMException && error.name === 'AbortError' && signal?.aborted)) {
      console.error('[statistics_export.request_failed]', { path, method, error })
    }
    throw error
  }
}

export const statisticsExportApi = {
  create: (key: string, signal?: AbortSignal) => request<StatisticsExportJob>('', 'POST', signal, key),
  status: (id: string, signal?: AbortSignal) => request<StatisticsExportJob>(`/${encodeURIComponent(id)}`, 'GET', signal),
  download: (id: string, signal?: AbortSignal) => request<Blob>(`/${encodeURIComponent(id)}/download`, 'GET', signal, undefined, true),
}
