import { withRequestTimeout } from '../utils/browserCompat'
import { notifyAuthExpired, tokenStore } from './http'
import type { TrackingFrameResult } from '../types/annotation'

const jsonRequest = async <T>(path: string, init: RequestInit = {}): Promise<T> => withRequestTimeout(30000, async (signal) => {
  const res = await fetch(`/api${path}`, {
    ...init,
    signal,
    headers: {
      'Content-Type': 'application/json',
      ...(tokenStore.get() ? { Authorization: `Bearer ${tokenStore.get()}` } : {}),
      ...(init.headers ?? {}),
    },
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    if (res.status === 401) notifyAuthExpired()
    const detail = (data as any)?.message || (data as any)?.detail
    const error = new Error(typeof detail === 'string' ? detail : detail?.message || `请求失败 (${res.status})`) as Error & { status: number }
    error.status = res.status
    throw error
  }
  return data as T
}, init.signal)

export interface TrackUploadResponse {
  mediaId: string
  videoName: string
  videoUrl: string
  width?: number
  height?: number
  duration?: number
  fps?: number
  frameCount?: number
  videoMimeType?: string
}

export interface TrackRunResponse {
  taskId: string
  status: 'queued' | 'running' | 'success' | 'failed'
  maxFrames?: number
  trackFrames?: number
  seedFile?: string
}


export interface TrackStatusResponse {
  taskId: string
  status: 'queued' | 'running' | 'success' | 'failed' | 'paused'
  message?: string
  paused?: boolean
  pausedFrame?: number
  pausedObjects?: Array<{
    object_id: number
    name?: string
    display_name?: string
    type: string
    reasons?: string[]
    details?: Record<string, number | string | boolean | number[] | null | undefined>
    currentBox?: number[] | null
    manualBaselineBox?: number[] | null
    manualBaselineFrame?: number | null
    metrics?: Record<string, number | null | undefined>
    reviewStartFrame?: number | null
    reviewEndFrame?: number | null
    reviewLookbackFrames?: number | null
    other_object_id?: number | null
    other_display_name?: string | null
    ratio?: number | null
    prevArea?: number | null
    currArea?: number | null
  }>
  anomalyLevels?: Record<string, string>
  processedFrames?: number
  lastProcessedFrame?: number
  reachedVideoEnd?: boolean
  warningSummary?: TrackingWarningSummary[]
}

export interface TrackingWarningSummary {
  objectId: number
  reason: string
  count: number
  firstFrame: number
  lastFrame: number
  calibrated: boolean
}

export interface NormalMotionSample {
  objectId: number
  frameIndex: number
  reason: 'motion'
  decision: 'normal'
  calibrate: boolean
  features: { motionNormalized: number }
}

export interface ObjectDeletionSummary {
  objectId: number
  name: string
  frameCount: number
  manualCount: number
  aiCount: number
  firstFrame: number | null
  lastFrame: number | null
  totalCount: number
}

export interface TrackingFeedbackInput {
  expectedRevision: number
  objectId: number
  frameIndex: number
  decision: 'normal' | 'corrected' | 'reset'
  calibrate: boolean
}

export interface TrackingFeedbackResponse {
  ok: boolean
  mediaId: string
  revision: number
  normalMotionSamples: NormalMotionSample[]
  pausedAnomalies: unknown[]
  lastPausedContext: { mediaId: string; frameIndex: number } | null
}

export interface TrackResultFile {
  format?: string
  media?: Record<string, unknown>
  tracking?: Record<string, unknown>
  frames: TrackingFrameResult[]
}

export interface TrackMediaItem {
  mediaId: string
  directoryName: string
  sourceVideoName: string
  videoName: string
  videoUrl: string
  videoMimeType?: string
  hasTrackingResult: boolean
  hasWorkspaceState?: boolean
  fps?: number
  width?: number
  height?: number
  frameCount?: number
  duration?: number
}

export interface TrackWorkspaceState {
  exists: boolean
  format?: 'annotation-workspace-v1'
  mediaId: string
  frontendMediaId?: string
  updatedAt?: string
  revision?: number
  expectedRevision?: number
  currentFrame?: number
  manualAnnotations?: unknown[]
  manualBaselines?: Array<{
    objectId: number
    name?: string
    source: 'manual'
    frameIndex: number
    bbox: [number, number, number, number]
  }>
  deletedTrackingIds?: string[]
  deletedObjectIds?: number[]
  deletedFrameObjects?: Array<{ objectId: number; frameIndex: number }>
  normalMotionSamples?: NormalMotionSample[]
  anomalyFrames?: Array<{ frame_index: number; level: string; reasons: string[] }>
  pausedAnomalies?: unknown[]
  lastPausedContext?: { mediaId: string; frameIndex: number } | null
  display?: { brightness?: number; contrast?: number; zoom?: number }
  editor?: {
    activeTool?: 'select' | 'point' | 'bbox'
    objectNameInput?: string
    selectedObjectId?: string | null
  }
}

export const trackApi = {
  async uploadVideo(file: File): Promise<TrackUploadResponse> {
    const form = new FormData()
    form.append('file', file, file.name)
    const res = await fetch('/api/track/upload', {
      method: 'POST',
      headers: {
        ...(tokenStore.get() ? { Authorization: `Bearer ${tokenStore.get()}` } : {}),
      },
      body: form,
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      if (res.status === 401) notifyAuthExpired()
      throw new Error((data as any)?.message || (data as any)?.detail || `视频上传失败 (${res.status})`)
    }
    return data as TrackUploadResponse
  },

  async saveFrameAnnotations(input: {
    mediaId: string
    mediaName: string
    mediaWidth: number
    mediaHeight: number
    frameIndex: number
    timestampMs: number
    annotations: unknown[]
  }) {
    return jsonRequest<{ filename: string; path?: string; frameIndex: number }>('/track/annotations', {
      method: 'POST', body: JSON.stringify(input),
    })
  },


  async rewind(input: { mediaId: string; startFrame: number }) {
    return jsonRequest<{
      ok: boolean
      mediaId: string
      cutoffFrame: number
      keptRows: number
      removedRows: number
      deletedFutureSeedFiles: number
      overlayRegenerated: boolean
    }>('/track/rewind', {
      method: 'POST',
      body: JSON.stringify(input),
    })
  },

  async run(input: {
    mediaId: string
    mediaName: string
    mediaWidth: number
    mediaHeight: number
    startFrame: number
    maxFrames?: number
    annotations: unknown[]
  }) {
    return jsonRequest<TrackRunResponse>('/track', { method: 'POST', body: JSON.stringify(input) })
  },

  async getStatus(taskId: string) {
    return jsonRequest<TrackStatusResponse>(`/track/status/${encodeURIComponent(taskId)}`)
  },

  async getFrameBlob(mediaId: string, frameIndex: number): Promise<Blob> {
    return withRequestTimeout(20000, async (signal) => {
      const res = await fetch(`/api/track/frame/${encodeURIComponent(mediaId)}/${encodeURIComponent(frameIndex)}`, {
        signal,
        headers: {
          ...(tokenStore.get() ? { Authorization: `Bearer ${tokenStore.get()}` } : {}),
        },
    })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      if (res.status === 401) notifyAuthExpired()
      throw new Error((data as any)?.message || (data as any)?.detail || `读取第 ${frameIndex} 帧失败 (${res.status})`)
    }
    return res.blob()
    })
  },

  async getResult(mediaId: string, frameIndex?: number) {
    const query = frameIndex == null ? '' : `?frameIndex=${encodeURIComponent(frameIndex)}`
    return jsonRequest<TrackResultFile | TrackingFrameResult>(`/track/result/${encodeURIComponent(mediaId)}${query}`)
  },

  async getWorkspaceState(mediaId: string) {
    return jsonRequest<TrackWorkspaceState>(`/track/workspace/${encodeURIComponent(mediaId)}`)
  },

  async saveWorkspaceState(mediaId: string, state: Omit<TrackWorkspaceState, 'exists' | 'mediaId'>, requestKey?: string) {
    return jsonRequest<{
      ok: boolean
      mediaId: string
      filename: string
      manualAnnotationCount: number
      manualBaselineCount: number
      revision: number
    }>(`/track/workspace/${encodeURIComponent(mediaId)}`, {
      method: 'PUT',
      headers: requestKey ? { 'Idempotency-Key': requestKey } : {},
      body: JSON.stringify(state),
    })
  },

  async getObjectDeletionSummary(mediaId: string, objectId: number) {
    return jsonRequest<ObjectDeletionSummary>(`/track/deletion-preview/${encodeURIComponent(mediaId)}/${objectId}`)
  },

  async submitFeedback(mediaId: string, input: TrackingFeedbackInput, requestKey: string) {
    return jsonRequest<TrackingFeedbackResponse>(`/track/feedback/${encodeURIComponent(mediaId)}`, {
      method: 'POST', headers: { 'Idempotency-Key': requestKey }, body: JSON.stringify(input),
    })
  },

  async deleteMedia(mediaId: string) {
    return jsonRequest<{ deleted: boolean; mediaId: string }>(`/track/media/${encodeURIComponent(mediaId)}`, { method: 'DELETE' })
  },

  async listMedia() {
    return jsonRequest<{ items: TrackMediaItem[] }>('/track/media')
  },

}
