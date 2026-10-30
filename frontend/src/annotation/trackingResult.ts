import type { AnnotationObject, TrackingFrameResult } from '../types/annotation'

export interface TrackingResultEnvelope {
  jointTracking?: {state:'checked'|'failed';checkedFrames:number}
  terminalIdentity?: {frameIndex:number;anchorHash:string;matches:Record<string,number>}
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
  if(value.jointTracking!==undefined){
    const j=value.jointTracking
    if(!record(j)||!['checked','failed'].includes(String(j.state))||!index(j.checkedFrames))return invalid()
  }
  if(value.terminalIdentity!==undefined){
    const t=value.terminalIdentity
    if(!record(t)||!index(t.frameIndex)||typeof t.anchorHash!=='string'||!/^[a-f0-9]{64}$/.test(t.anchorHash)||!record(t.matches)||!Object.entries(t.matches).every(([k,v])=>/^[1-9]\d*$/.test(k)&&index(v)&&v>0)||new Set(Object.values(t.matches)).size!==Object.keys(t.matches).length)return invalid()
  }
  const envelope = 'frames' in value
  if (envelope && (!Array.isArray(value.frames) || !index(value.count) || value.count !== value.frames.length)) return invalid()
  if ('state' in value && value.state !== 'available' && value.state !== 'not_generated') return invalid()
  const frames = envelope ? value.frames as unknown[] : [value]
  if(record(value.jointTracking) && (Number(value.jointTracking.checkedFrames)>frames.length || (value.jointTracking.state==='checked' && value.jointTracking.checkedFrames!==frames.length)))return invalid()
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

/** Reconcile only the explicitly supplied terminal anchors, once per run.
 * Original ID and coordinates remain in the server's anchor snapshot. This
 * makes global deletion and later continuation use the same biological ID.
 */
export function reconcileTerminalIdentities(objects:AnnotationObject[], terminal:TrackingResultEnvelope['terminalIdentity']):AnnotationObject[]{
  if(!terminal)return objects
  return objects.map(o=>{
    if(o.source!=='manual'||o.frameIndex!==terminal.frameIndex||o.objectId==null)return o
    const original=o.endpointAnchorHash===terminal.anchorHash?(o.endpointOriginalObjectId??o.objectId):o.objectId
    const canonical=terminal.matches[String(original)]
    return canonical==null||canonical===o.objectId&&o.endpointAnchorHash===terminal.anchorHash?o:{...o,objectId:canonical,endpointOriginalObjectId:original,endpointAnchorHash:terminal.anchorHash}
  })
}
