import type { TrackingFrameResult } from '../types/annotation'

export interface TrackingResultEnvelope {
  format?: string
  state?: 'available' | 'not_generated'
  frames: TrackingFrameResult[]
  count: number
}

const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const index = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

/** A successful HTTP status does not establish that tracking data is readable. */
export function validateTrackingResult(value: unknown): asserts value is TrackingResultEnvelope | TrackingFrameResult {
  const invalid = () => { throw new Error('追踪结果结构损坏，请检查服务器结果文件并重新读取') }
  if (!record(value)) return invalid()
  const envelope = 'frames' in value
  if (envelope && (!Array.isArray(value.frames) || !index(value.count) || value.count !== value.frames.length)) return invalid()
  if ('state' in value && value.state !== 'available' && value.state !== 'not_generated') return invalid()
  const frames = envelope ? value.frames as unknown[] : [value]
  if (value.state === 'not_generated' && (!envelope || frames.length !== 0)) return invalid()
  const frameIds = new Set<number>()
  for (const frame of frames) {
    if (!record(frame) || !index(frame.frameIndex) || frameIds.has(frame.frameIndex) || !Array.isArray(frame.annotations)) return invalid()
    if (frame.timestampMs !== undefined && (!finite(frame.timestampMs) || frame.timestampMs < 0)) return invalid()
    frameIds.add(frame.frameIndex)
    const objects = new Set<number>()
    for (const obj of frame.annotations) {
      if (!record(obj) || !index(obj.objectId) || obj.objectId === 0 || objects.has(obj.objectId)) return invalid()
      if (obj.frameIndex !== undefined && obj.frameIndex !== frame.frameIndex) return invalid()
      if (!Array.isArray(obj.bbox) || obj.bbox.length !== 4 || !obj.bbox.every(finite)) return invalid()
      if (obj.bbox[2] <= obj.bbox[0] || obj.bbox[3] <= obj.bbox[1]) return invalid()
      objects.add(obj.objectId)
    }
  }
}
