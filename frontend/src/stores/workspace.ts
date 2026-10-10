import { createRequestId } from '../utils/browserCompat'
import { computed, nextTick, reactive, ref, shallowRef, watch } from 'vue'
import { FrameCache } from '../annotation/frameCache'
import { hitHandle, moveBox, resizeBox, type Box } from '../annotation/geometry'
import type { AnnotationObject, AnnotationTool, EffectResult, MediaAsset, SavedAnnotationFile } from '../types/annotation'
// 登录、人工标注、视频目录、SAM3 Tracking 均走真实后端
import { httpAnnotationApi } from '../api/httpAnnotationApi'
import { trackApi, type DuplicateVideo, type TrackUploadResponse, type TrackStatusResponse, type ObjectDeletionSummary, type NormalMotionSample, type TrackingWarningSummary, type TrackingFeedbackEvent, type TrackingFeedbackResponse, type TrackingFeedbackInput, type TrackingRestartInput } from '../api/trackApi'
import { WorkspaceWrites, type WorkspaceDraft } from '../annotation/workspaceWrites'
import { useAuth } from './auth'
import { reviewWorkflowApi } from '../api/reviewWorkflowApi'
import type { TrackingFrameObject, TrackingFrameResult } from '../types/annotation'

const createWorkspace = () => {
  const VIDEO_EXTENSIONS = new Set([
    '.mp4', '.avi', '.mov', '.mkv', '.webm', '.m4v', '.mpg', '.mpeg', '.wmv', '.flv',
    '.ts', '.m2ts', '.mts', '.3gp', '.ogv', '.ogg', '.asf', '.vob', '.divx', '.xvid',
  ])
  const isVideoFile = (file: File) => file.type.startsWith('video/') || VIDEO_EXTENSIONS.has(`.${file.name.split('.').pop()?.toLowerCase() || ''}`)
  const api = httpAnnotationApi

  // ── mediaAssets 持久化：刷新后 id 不变，annotationsByMedia 才能匹配 ──
  const loadMediaAssets = (): MediaAsset[] => {
    try {
      const raw = localStorage.getItem('mediaAssets')
      if (raw) return JSON.parse(raw)
    } catch {}
    return [
      {
        id: 'img-demo-001',
        name: 'microfluidic-sample.svg',
        type: 'image',
        url: '/demo/microfluidic-sample.svg',
        width: 1600,
        height: 900,
      },
    ]
  }
  const mediaAssets = ref<MediaAsset[]>(loadMediaAssets())
  // Read legacy closed-media IDs so upgrades retain their original annotation identity.
  const loadClosedMediaFrontendIds = (): Record<string, string> => {
    try {
      return JSON.parse(localStorage.getItem('closedMediaFrontendIds') || '{}')
    } catch {
      return {}
    }
  }
  const closedMediaFrontendIds = loadClosedMediaFrontendIds()
  const persistClosedMediaFrontendIds = () => {
    try { localStorage.setItem('closedMediaFrontendIds', JSON.stringify(closedMediaFrontendIds)) } catch {}
  }
  let mediaSaveTimer: ReturnType<typeof setTimeout> | null = null
  watch(mediaAssets, (val) => {
    if (mediaSaveTimer) clearTimeout(mediaSaveTimer)
    mediaSaveTimer = setTimeout(() => {
      try {
        // URL.createObjectURL 生成的 blob: URL 在刷新后失效，持久化前剔除
        const persistable = val.map((m) => ({ ...m }))
        localStorage.setItem('mediaAssets', JSON.stringify(persistable))
      } catch {}
    }, 300)
  }, { deep: true })

  let rememberedMedia = ''
  try { rememberedMedia = localStorage.getItem('annotation-selected-media') || '' } catch {}
  const selectedMediaId = ref(mediaAssets.value.find(m => m.id === rememberedMedia)?.id ?? mediaAssets.value[0]?.id ?? '')
  let workspaceRestoreInProgress = false
  let persistWorkspaceState: (mediaId: string, useCurrentUiState?: boolean) => Promise<void> = async () => {}
  let scheduleWorkspaceStateSave: (mediaId: string) => void = () => {}
  const activeTool = ref<AnnotationTool>('bbox')
  const objectNameInput = ref('rare sperm')
  const selectedObjectId = ref<string | null>(null)
  const currentFrame = ref(0)
  const currentTime = ref(0)
  const videoDuration = ref(0)
  const videoFps = ref(30)
  const playbackRate = ref(1)
  const frameInput = ref(0)
  const isPlaying = ref(false)
  const isAiBusy = ref(false)
  const deletingMediaId = ref<string | null>(null)
  const mediaDeleteError = ref('')
  const trackingFrameCount = ref(5)
  const statusMessage = ref('就绪')
  const saveState = ref<'idle'|'saving'|'saved'|'error'>('idle')
  const saveError = ref('')
  const messageOf = (error: unknown, fallback: string) => typeof (error as {message?:unknown})?.message === 'string' ? (error as {message:string}).message : fallback
  type TrackingJob = {mediaId:string;startFrame:number;input:Parameters<typeof trackApi.run>[0];key:string;taskId?:string;maxFrames?:number}
  const trackingJobs = reactive(new Map<string, TrackingJob>())
  const trackingErrors = ref<Record<string,{message:string;phase:string;frameIndex:number}>>({})
  const trackingError = computed(()=>trackingErrors.value[currentMediaId.value])
  const trackingRetryBusy = ref(false)
  const trackingProgress = ref('')
  const saveQueues = new Map<string, Promise<unknown>>()
  const workspaceViews = new Map<string, Record<string, unknown>>()
  const saveTickets = new Map<string, number>()
  const workspaceWrites = new WorkspaceWrites()
  const workspaceReadError = ref('')
  type TrackingRestartIntent = { actorId: string; mediaId: string; serverMediaId: string; key: string; body: TrackingRestartInput; phase: 'restart' | 'start' | 'conflict' }
  const pendingRestart = ref<TrackingRestartIntent | null>(null)
  const trackingRestartBusy = ref(false), trackingRestartError = ref(''), trackingRestartStorageWarning = ref('')
  const trackingRestartPendingAction = computed(() => pendingRestart.value?.mediaId === currentMediaId.value ? pendingRestart.value : null)
  let restartController: AbortController | null = null, restartEpoch = 0
  const cancelTrackingRestartRequest = () => { restartController?.abort(); restartController = null; restartEpoch++; trackingRestartBusy.value = false }
  const zoom = ref(1)
  const isInteracting = () => !!tempBbox.value || !!draggingObjectId || !!bboxStart
  const zoomIn = (step = 0.1) => { if (isInteracting()) return; zoom.value = Math.min(5, +(zoom.value + step).toFixed(2)) }
  const zoomOut = (step = 0.1) => { if (isInteracting()) return; zoom.value = Math.max(0.25, +(zoom.value - step).toFixed(2)) }
  const zoomReset = () => { if (!isInteracting()) zoom.value = 1 }
  const toastMessage = ref('')
  let toastTimer: ReturnType<typeof setTimeout> | null = null
  const showToast = (message: string) => {
    toastMessage.value = message
    if (toastTimer) clearTimeout(toastTimer)
    toastTimer = setTimeout(() => { toastMessage.value = '' }, 2800)
  }
  const savedResults = ref<SavedAnnotationFile[]>([])
  const loadedRemoteResultKeys = new Set<string>()
  let remoteResultsLoaded = false
  const effectResults = ref<EffectResult[]>([])
  const selectedEffectId = ref<string | null>(null)
  const effectTime = ref(0)
  const effectPlaying = ref(false)
  const effectVideoRef = ref<HTMLVideoElement | null>(null)

  const imageRef = ref<HTMLImageElement | null>(null)
  const videoRef = ref<HTMLVideoElement | null>(null)
  const exactFrameImageRef = ref<HTMLImageElement | null>(null)
  const exactFrameUrl = ref<string | null>(null)
  const exactFrameLoading = ref(false)
  let exactFrameRequestSerial = 0
  const annotationHitRef = ref<HTMLDivElement | null>(null)
  const fileInputRef = ref<HTMLInputElement | null>(null)
  const videoInputRef = ref<HTMLInputElement | null>(null)
  const effectFolderInputRef = ref<HTMLInputElement | null>(null)

  // 从 localStorage 恢复标注数据
  const loadAnnotations = (): Record<string, AnnotationObject[]> => {
    try {
      const raw = localStorage.getItem('annotationsByMedia')
      if (raw) return JSON.parse(raw)
    } catch {}
    return {
      'img-demo-001': [],
      'video-demo-001': [],
    }
  }
  // Every edit replaces its media array. Avoid deep proxies/watch traversal of
  // tens of thousands of immutable AI boxes on each frame navigation or commit.
  const annotationsByMedia = shallowRef<Record<string, AnnotationObject[]>>(loadAnnotations())
  const workspaceGenerations = new Map<string,string|undefined>()

  const persistLocalAnnotations = () => {
    const serverIds = new Set(mediaAssets.value.filter(m => m.serverMediaId).map(m => m.id))
    const compact = Object.fromEntries(Object.entries(annotationsByMedia.value).map(([mid, objects]) =>
      [mid, serverIds.has(mid) ? objects.filter(o => o.source === 'manual') : objects]))
    localStorage.setItem('annotationsByMedia', JSON.stringify(compact))
  }

  // 自动持久化到 localStorage（debounce 300ms）
  let saveTimer: ReturnType<typeof setTimeout> | null = null
  watch(annotationsByMedia, () => {
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      try { persistLocalAnnotations() } catch (error) { console.error('[annotation.local_save_failed]', error); saveError.value = '本机缓存写入失败，请保持页面打开并检查存储空间' }
      if (!workspaceRestoreInProgress) scheduleWorkspaceStateSave(selectedMediaId.value)
    }, 300)
  })

  // ── 撤销/重做栈 ──
  type FrameDeletion = { objectId: number; frameIndex: number }
  let historySequence = 0
  type HistoryState = { objects: AnnotationObject[]; deleted: string[]; frameDeleted: FrameDeletion[]; sequence: number; pause?: { items: typeof pausedAnomalies.value; context: typeof lastPausedContext.value } }
  const histories = new Map<string, { undo: HistoryState[]; redo: HistoryState[] }>()
  const historyRevision = ref(0)
  const history = () => {
    const key = `${currentMediaId.value}:${currentFrame.value}`
    if (!histories.has(key)) histories.set(key, { undo: [], redo: [] })
    return histories.get(key)!
  }
  const captureHistory = (): HistoryState => ({
    objects: JSON.parse(JSON.stringify(currentObjects.value)),
    deleted: [...(deletedTrackingIds.value[currentMediaId.value] ?? [])],
    frameDeleted: clone(deletedFrameObjects.value[currentMediaId.value] ?? []),
    sequence: ++historySequence,
    pause: lastPausedContext.value?.mediaId === currentMediaId.value && lastPausedContext.value.frameIndex === currentFrame.value ? { items: clone(pausedAnomalies.value), context: clone(lastPausedContext.value) } : undefined,
  })
  const snapshotUndo = () => {
    if (videoDeletionHistory.value[currentMediaId.value]?.undone) { delete videoDeletionHistory.value[currentMediaId.value]; rememberControls(currentMediaId.value) }
    const h = history(); h.undo.push(captureHistory()); h.redo.length = 0
    if (h.undo.length > 50) h.undo.shift()
    historyRevision.value++
  }
  const canUndo = computed(() => { void historyRevision.value; return history().undo.length > 0 || canUndoVideoDeletion.value })
  const canRedo = computed(() => { void historyRevision.value; return history().redo.length > 0 || !!videoDeletionHistory.value[currentMediaId.value]?.undone })
  const restoreHistory = (state: HistoryState) => {
    const mid = currentMediaId.value, fi = currentFrame.value
    const affected = new Set([...currentObjects.value, ...state.objects].map(o => o.id))
    const rest = (annotationsByMedia.value[mid] ?? []).filter(o => (o.frameIndex ?? 0) !== fi)
    annotationsByMedia.value = { ...annotationsByMedia.value, [mid]: [...rest, ...state.objects.filter(o => !deletedObjectIds.value[mid]?.includes(o.objectId!))] }
    // Undo is frame-local: never restore deletion flags for unrelated frames.
    const otherDeleted = [...(deletedTrackingIds.value[mid] ?? [])].filter(id => !affected.has(id))
    deletedTrackingIds.value[mid] = new Set([...otherDeleted, ...state.deleted.filter(id => affected.has(id))])
    deletedFrameObjects.value[mid] = [...(deletedFrameObjects.value[mid] ?? []).filter(d => d.frameIndex !== fi), ...state.frameDeleted.filter(d => d.frameIndex === fi)]
    if (state.pause) { pausedAnomalies.value = clone(state.pause.items).filter(item => !deletedObjectIds.value[mid]?.includes(item.objectId)); lastPausedContext.value = pausedAnomalies.value.length ? clone(state.pause.context) : null }
    if (!state.objects.some(o => o.id === selectedObjectId.value)) selectedObjectId.value = null
    historyRevision.value++; scheduleWorkspaceStateSave(mid)
  }
  const undo = () => {
    if (editingBlocked.value || isInteracting()) return
    const h = history()
    const deletion = videoDeletionHistory.value[currentMediaId.value]
    if (deletion && !deletion.undone && deletion.sequence >= (h.undo[h.undo.length - 1]?.sequence ?? 0)) { void undoVideoObjectDeletion(); return }
    if (!h.undo.length) return
    h.redo.push(captureHistory()); restoreHistory(h.undo.pop()!); statusMessage.value = '已撤销本帧操作'
  }
  const redo = () => {
    if (editingBlocked.value || isInteracting()) return
    const h = history()
    const deletion = videoDeletionHistory.value[currentMediaId.value]
    if (deletion?.undone && deletion.sequence >= (h.redo[h.redo.length - 1]?.sequence ?? 0)) { void applyVideoDeletion('redo', deletion); return }
    if (!h.redo.length) return
    h.undo.push(captureHistory()); restoreHistory(h.redo.pop()!); statusMessage.value = '已重做本帧操作'
  }

  // SAM3 逐帧结果缓存。key=前端 media.id，value=服务器 tracking_result.json 的 frames。
  const trackingFramesByMedia = ref<Record<string, TrackingFrameResult[]>>({})
  /** 记录前端删除的 AI tracking 对象 id，避免重新加载后又出现 */
  const deletedTrackingIds = ref<Record<string, Set<string>>>({})
  const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
  const deletedObjectIds = ref<Record<string, number[]>>({})
  const deletedFrameObjects = ref<Record<string, FrameDeletion[]>>({})
  const normalMotionSamples = ref<Record<string, NormalMotionSample[]>>({})
  const trackingFeedbackEvents = ref<Record<string, TrackingFeedbackEvent[]>>({})
  const trackingWarningSummary = ref<TrackingWarningSummary[]>([])
  const objectDeletionBusy = ref(false)
  const objectDeletionError = ref('')
  const trackingFeedbackBusy = ref(false)
  const trackingFeedbackError = ref('')
  type VideoDeletion = { objectId: number; name: string; frameCount: number; objects: AnnotationObject[]; sequence: number; undone: boolean; pause?: { item: typeof pausedAnomalies.value[number]; context: NonNullable<typeof lastPausedContext.value> } }
  const videoDeletionHistory = ref<Record<string, VideoDeletion>>({})
  type DeletionIntent = { mediaId: string; serverMediaId: string; key: string; action: 'delete' | 'undo' | 'redo'; record: VideoDeletion; state: WorkspaceDraft }
  const pendingDeletion = ref<DeletionIntent | null>(null)
  type FeedbackIntent = { mediaId: string; serverMediaId: string; key: string; body: TrackingFeedbackInput }
  const pendingFeedback = ref<FeedbackIntent | null>(null)
  const objectDeletionPendingAction = computed(() => pendingDeletion.value ? { objectId: pendingDeletion.value.record.objectId, action: pendingDeletion.value.action } : null)
  const trackingFeedbackPendingAction = computed(() => pendingFeedback.value ? { objectId: pendingFeedback.value.body.objectId, decision: pendingFeedback.value.body.decision, learn: pendingFeedback.value.body.calibrate } : null)
  const trackingFeedbackPending = computed(() => !!pendingFeedback.value)
  const canUndoVideoDeletion = computed(() => !!videoDeletionHistory.value[currentMediaId.value] && !videoDeletionHistory.value[currentMediaId.value].undone)
  const lastVideoObjectDeletion = computed(() => canUndoVideoDeletion.value ? videoDeletionHistory.value[currentMediaId.value] : null)
  const trackingCalibrationSummary = computed(() => {
    const counts = new Map<number, number>()
    for (const sample of normalMotionSamples.value[currentMediaId.value] ?? []) counts.set(sample.objectId, (counts.get(sample.objectId) || 0) + 1)
    const shapes = new Map<number, number>()
    for (const event of trackingFeedbackEvents.value[currentMediaId.value] ?? []) {
      if (event.decision === 'reset' || event.decision === 'corrected') shapes.delete(event.objectId)
      else if (event.geometryReference) shapes.set(event.objectId, event.geometryReference.frameIndex)
    }
    // These are stored human confirmations; a newer manual frame can supersede
    // their use during tracking. Keep the reset control available either way.
    return [...new Set([...counts.keys(), ...shapes.keys()])]
      .filter(objectId => !deletedObjectIds.value[currentMediaId.value]?.includes(objectId))
      .map(objectId => ({ objectId, sampleCount: counts.get(objectId) ?? 0, geometryFrame: shapes.get(objectId) }))
  })
  const isObjectDeleted = (mediaId: string, objectId: number | undefined, frameIndex: number) => objectId != null && (
    deletedObjectIds.value[mediaId]?.includes(objectId) || deletedFrameObjects.value[mediaId]?.some(d => d.objectId === objectId && d.frameIndex === frameIndex)
  )

  // 异常物体 ID 列表（面积突变等），用于高亮
  const anomalyObjectIds = ref<number[]>([])
  // 异常帧列表（给时间轴标记用）
  const anomalyFrames = ref<Array<{ frame_index: number; level: string; reasons: string[] }>>([])
  // 当前暂停的可读诊断，供人工在画面旁直接判断如何修框/续追。
  const pausedAnomalies = ref<Array<{
    objectId: number
    displayName: string
    title: string
    summary: string
    metrics: string[]
    baselineFrame?: number
    geometryReferenceFrame?: number
    geometryReferenceSource?: 'manual' | 'confirmed-normal'
    acceptsGeometry: boolean
    reviewRange?: string
    reviewNotice?: string
    suggestion: string
    canLearn: boolean
    rawReasons: string[]
    resolved?: boolean
  }>>([])
  const anomalyPanelVisible = ref(false)
  const closeAnomalyPanel = () => { anomalyPanelVisible.value = false }
  const lastPausedContext = ref<{ mediaId: string; frameIndex: number } | null>(null)
  let trackingLoadSerial = 0
  let videoFrameCallbackId: number | null = null
  let seekSerial = 0
  let isSeekingVideo = false
  const videoPlaybackFallback = ref(false)
  let fallbackPlaybackSerial = 0

  const selectedMedia = computed(() => mediaAssets.value.find((item) => item.id === selectedMediaId.value) ?? mediaAssets.value[0] ?? null)
  const isVideo = computed(() => selectedMedia.value?.type === 'video')
  const maxFrameIndex = computed(() => {
    const media = selectedMedia.value
    if (media?.frameCount && media.frameCount > 0) return Math.max(0, media.frameCount - 1)
    return Math.max(0, Math.ceil(videoDuration.value * videoFps.value) - 1)
  })

  /** 视频按真实 currentFrame 标注；Tracking 从当前人工标注帧开始。 */
  const currentMediaId = computed(() => selectedMediaId.value)
  // The pause banner describes the tracking event, not the frame the user is
  // currently browsing afterwards. Keep it pinned to the backend pausedFrame.
  const trackingPausedFrame = computed(() => {
    const context = lastPausedContext.value
    return context?.mediaId === currentMediaId.value ? context.frameIndex : null
  })

  const annotationIdentity = (obj: AnnotationObject) =>
    `${obj.frameIndex ?? 0}:${obj.objectId ?? obj.id}`

  const dedupeAnnotationObjects = (objects: AnnotationObject[]) => {
    const unique = new Map<string, AnnotationObject>()
    for (const obj of objects) {
      const key = annotationIdentity(obj)
      const previous = unique.get(key)
      // Manual state is authoritative over an AI object on the same frame/ID.
      if (!previous || obj.source === 'manual' || previous.source !== 'manual') unique.set(key, obj)
    }
    return [...unique.values()]
  }

  /** 全局 objectId 计数器：从现有最大 objectId 继续分配 */
  const getNextObjectId = (mediaId?: string): number => {
    let maxId = 0
    for (const [mid, objs] of Object.entries(annotationsByMedia.value)) {
      if (mediaId && mid !== mediaId) continue
      for (const o of objs) {
        if (typeof o.objectId === 'number' && o.objectId > maxId) maxId = o.objectId
      }
    }
    for (const [mid, ids] of Object.entries(deletedObjectIds.value)) if (!mediaId || mid === mediaId) for (const id of ids) maxId = Math.max(maxId, id)
    for (const [mid, items] of Object.entries(deletedFrameObjects.value)) if (!mediaId || mid === mediaId) for (const item of items) maxId = Math.max(maxId, item.objectId)
    return maxId + 1
  }
  // Index membership only when annotations change; ordinary frame navigation does not scan the whole video.
  const objectsByFrame = computed(() => {
    const index = new Map<number, AnnotationObject[]>()
    for (const obj of annotationsByMedia.value[currentMediaId.value] ?? []) {
      const fi = obj.frameIndex ?? 0
      if (isObjectDeleted(currentMediaId.value, obj.objectId, fi)) continue
      if (!index.has(fi)) index.set(fi, [])
      index.get(fi)!.push(obj)
    }
    return index
  })
  const currentObjects = computed(() => isVideo.value
    ? objectsByFrame.value.get(currentFrame.value) ?? []
    : annotationsByMedia.value[currentMediaId.value] ?? [])
  const dragPreview = ref<{ id: string; bbox: Box } | null>(null)
  const displayObjects = computed(() => dragPreview.value
    ? currentObjects.value.map(o => o.id === dragPreview.value!.id ? { ...o, bbox: dragPreview.value!.bbox } : o)
    : currentObjects.value)
  const submissionLocks=ref(new Set<string>())
  const submissionBusy=computed(()=>submissionLocks.value.size>0)
  const mediaImportBusy = ref(false)
  const duplicateImport = ref<(DuplicateVideo & {key:string;pending:boolean;error:string})|null>(null)
  const importError = ref('')
  let finishDuplicate: ((result:TrackUploadResponse|null)=>void)|null=null
  const editingBlocked = computed(() => submissionBusy.value || mediaImportBusy.value || !!duplicateImport.value || objectDeletionBusy.value || trackingFeedbackBusy.value || trackingRestartBusy.value || !!pendingRestart.value || !!pendingDeletion.value || !!pendingFeedback.value || !!workspaceReadError.value || isAiBusy.value || !!deletingMediaId.value || exactFrameLoading.value || isPlaying.value || workspaceRestoring.value || !!frameError.value)
  const workspaceRestoring = ref(false)
  const selectedObject = computed(() => currentObjects.value.find((item) => item.id === selectedObjectId.value) ?? null)
  const selectedEffect = computed(() => effectResults.value.find((item) => item.id === selectedEffectId.value) ?? effectResults.value[0] ?? null)

  const formatTime = (seconds: number) => {
    if (!Number.isFinite(seconds)) return '00:00.000'
    const m = Math.floor(seconds / 60).toString().padStart(2, '0')
    const s = Math.floor(seconds % 60).toString().padStart(2, '0')
    const ms = Math.floor((seconds % 1) * 1000).toString().padStart(3, '0')
    return `${m}:${s}.${ms}`
  }

  const timeToFrame = (time: number) => Math.max(0, Math.round(time * videoFps.value))
  const frameToTime = (frame: number) => frame / videoFps.value

  // 浏览器实际呈现帧优先使用 requestVideoFrameCallback 的 mediaTime。
  // 对严格 CFR 视频，mediaTime 与 frameIndex 的关系为 frame / fps；
  // 不再用 video.currentTime 在 seek 尚未真正呈现时提前切换标注帧。
  const commitPresentedFrame = (mediaTime: number) => {
    const time = Number.isFinite(mediaTime) ? Math.max(0, mediaTime) : 0
    const frame = timeToFrame(time)
    currentTime.value = time
    currentFrame.value = Math.max(0, Math.min(maxFrameIndex.value, frame))
    frameInput.value = currentFrame.value
  }

  const getStagePoint = (event: MouseEvent | PointerEvent) => {
    const stage = annotationHitRef.value
    if (!stage) return { x: 50, y: 50 }
    const rect = stage.getBoundingClientRect()
    if (!rect.width || !rect.height) return { x: 50, y: 50 }
    return {
      x: Math.max(0, Math.min(100, ((event.clientX - rect.left) / rect.width) * 100)),
      y: Math.max(0, Math.min(100, ((event.clientY - rect.top) / rect.height) * 100)),
    }
  }

  const baseObjectName = (name: string) => {
    const cleaned = name.trim() || 'rare sperm'
    return cleaned.replace(/\s+\d+$/, '').trim() || 'rare sperm'
  }

  const numberedObjectName = (name: string, objectId: number) => `${baseObjectName(name)} ${objectId}`

  const normalizeAnnotationObject = (obj: AnnotationObject, fallbackObjectId: number): AnnotationObject => {
    const objectId = typeof obj.objectId === 'number' ? obj.objectId : fallbackObjectId
    return { ...obj, objectId, name: numberedObjectName(obj.name, objectId) }
  }

  const addObject = (point: { x: number; y: number }, bbox?: { x: number; y: number; width: number; height: number }) => {
    if (editingBlocked.value) return
    const mediaId = currentMediaId.value
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media) return
    snapshotUndo()
    const objectId = getNextObjectId(mediaId)
    const name = numberedObjectName(objectNameInput.value.trim() || 'rare sperm', objectId)
    const object: AnnotationObject = {
      id: `manual-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      objectId,
      name,
      source: 'manual',
      point: bbox ? undefined : { ...point },
      bbox,
      frameIndex: media.type === 'video' ? currentFrame.value : undefined,
      timestampMs: media.type === 'video' ? Math.round(currentTime.value * 1000) : undefined,
    }

    const existing = annotationsByMedia.value[mediaId] ?? []
    annotationsByMedia.value = { ...annotationsByMedia.value, [mediaId]: [...existing, object] }
    selectedObjectId.value = object.id
    statusMessage.value = `已添加：${name}${media.type === 'video' ? `（第 ${currentFrame.value + 1} 帧）` : ''}`
  }

  const revokeExactFrameUrl = () => {
    if (exactFrameUrl.value) {
      try { URL.revokeObjectURL(exactFrameUrl.value) } catch {}
      exactFrameUrl.value = null
    }
  }

  const frameCache = new FrameCache()
  const frameError = ref('')
  const failedFrame = ref<number | null>(null)
  const retryExactFrame = () => loadExactFrame(failedFrame.value ?? currentFrame.value)
  const loadExactFrame = async (frameIndex: number, mediaId = currentMediaId.value) => {
    const media = mediaAssets.value.find(item => item.id === mediaId)
    if (!media || media.type !== 'video' || !media.serverMediaId) return false
    const serial = ++exactFrameRequestSerial, started = performance.now()
    exactFrameLoading.value = true; frameError.value = ''; failedFrame.value = null
    let url: string | null = null
    const read = (fi: number) => frameCache.get(`${media.serverMediaId}:${fi}`, () => trackApi.getFrameBlob(media.serverMediaId!, fi))
    try {
      const blob = await read(frameIndex)
      if (serial !== exactFrameRequestSerial || mediaId !== currentMediaId.value) return false
      url = URL.createObjectURL(blob)
      const decoded = new Image(); decoded.src = url; await decoded.decode()
      if (serial !== exactFrameRequestSerial || mediaId !== currentMediaId.value) return false
      const old = exactFrameUrl.value
      exactFrameUrl.value = url; url = null
      currentFrame.value = frameIndex; frameInput.value = frameIndex; currentTime.value = frameToTime(frameIndex)
      await nextTick()
      if (old) URL.revokeObjectURL(old)
      const elapsedMs = Math.round(performance.now() - started)
      if (elapsedMs > 250) console.info('[annotation.frame_slow]', { mediaId, frameIndex, elapsedMs })
      for (const next of [frameIndex + 1, frameIndex - 1]) {
        if (next >= 0 && next < (media.frameCount || 0)) void read(next).catch(error => console.debug('[annotation.prefetch_failed]', { mediaId, frameIndex: next, error }))
      }
      return true
    } catch (error) {
      if (url) frameCache.invalidate(`${media.serverMediaId}:${frameIndex}`)
      if (serial === exactFrameRequestSerial && mediaId === currentMediaId.value) {
        failedFrame.value = frameIndex
        frameError.value = `第 ${frameIndex + 1} 帧读取失败，请重试`; statusMessage.value = frameError.value
        console.error('[annotation.frame_failed]', { mediaId, frameIndex, error })
      }
      return false
    } finally {
      if (url) URL.revokeObjectURL(url)
      if (serial === exactFrameRequestSerial) exactFrameLoading.value = false
    }
  }

  const resetVideoViewToFirstFrame = (mediaId = currentMediaId.value) => {
    if (currentMediaId.value !== mediaId || !isVideo.value) return
    currentFrame.value = 0
    currentTime.value = 0
    frameInput.value = 0
    revokeExactFrameUrl()
    if (videoRef.value?.readyState) {
      try { videoRef.value.currentTime = 0 } catch { }
    }
    void loadExactFrame(0, mediaId)
  }

  const ensureVideoFirstFrame = async (_mediaId = currentMediaId.value) => true

  const toolLabel = (tool: AnnotationTool) =>
    tool === 'select' ? '选择框' : tool === 'point' ? '点标注' : '框标注'

  const selectTool = (tool: AnnotationTool) => {
    if (editingBlocked.value) return
    activeTool.value = tool
    statusMessage.value = isVideo.value
      ? `已切换到${toolLabel(tool)} · 当前第 ${currentFrame.value + 1} 帧`
      : `已切换到${toolLabel(tool)}`
  }

  const onStageClick = (event: MouseEvent) => {
    if (editingBlocked.value) return
    // 如果本次按下命中了已有框，跳过 click（防止添加新点标注覆盖选中）
    if (hitExisting) { hitExisting = false; return }
    if (activeTool.value === 'select') {
      const point = getStagePoint(event)
      const hit = [...currentObjects.value].reverse().find((obj) => obj.bbox && pointInBbox(point.x, point.y, obj.bbox))
      if (hit) {
        selectedObjectId.value = hit.id
        statusMessage.value = `已选中 ${hit.name}`
      } else {
        clearSelection()
      }
      event.preventDefault()
      event.stopPropagation()
      return
    }
    if (activeTool.value !== 'point') return
    event.preventDefault()
    event.stopPropagation()
    addObject(getStagePoint(event))
  }

  let bboxStart: { x: number; y: number } | null = null
  let bboxPointerId: number | null = null
  const tempBbox = ref<{ x: number; y: number; width: number; height: number } | null>(null)

  // 拖拽已有框移动 / 调整大小
  let draggingObjectId: string | null = null
  let dragStartPoint: { x: number; y: number } | null = null
  let dragStartBbox: { x: number; y: number; width: number; height: number } | null = null
  let dragMoved = false
  let resizeHandle: 'nw' | 'ne' | 'sw' | 'se' | null = null
  let hitExisting = false // 标记本次按下命中了已有框，用于阻止后续 click 事件

  const pointInBbox = (px: number, py: number, b: Box) => px >= b.x && px <= b.x+b.width && py >= b.y && py <= b.y+b.height
  const onBboxDown = (event: PointerEvent) => {
    if (editingBlocked.value || event.button !== 0) return
    event.preventDefault(); event.stopPropagation()
    cancelAnnotationGesture()
    bboxPointerId = event.pointerId
    const stage = annotationHitRef.value
    stage?.setPointerCapture(event.pointerId)
    const point = getStagePoint(event), rect = stage!.getBoundingClientRect()
    const selected = currentObjects.value.find(o => o.id === selectedObjectId.value)
    const handle = selected?.bbox ? hitHandle(point.x, point.y, selected.bbox, rect.width, rect.height) : null
    const hit = handle ? selected : [...currentObjects.value].reverse().find(o => o.bbox && pointInBbox(point.x, point.y, o.bbox))
    if (hit?.bbox) {
      selectedObjectId.value = hit.id; draggingObjectId = hit.id
      dragStartPoint = point; dragStartBbox = { ...hit.bbox }; dragMoved = false
      resizeHandle = handle; hitExisting = true
    } else if (activeTool.value === 'bbox') {
      bboxStart = point; tempBbox.value = { ...point, width: 0, height: 0 }; hitExisting = false
    } else if (activeTool.value === 'select') { clearSelection() }
  }
  const onBboxMove = (event: PointerEvent) => {
    if (bboxPointerId !== event.pointerId) return
    const point = getStagePoint(event)
    if (draggingObjectId && dragStartPoint && dragStartBbox) {
      const dx = point.x-dragStartPoint.x, dy = point.y-dragStartPoint.y
      const w = selectedMedia.value?.width || 1000, h = selectedMedia.value?.height || 1000
      const box = resizeHandle ? resizeBox(dragStartBbox, dx, dy, resizeHandle, 200/w, 200/h) : moveBox(dragStartBbox, dx, dy)
      dragMoved = JSON.stringify(box) !== JSON.stringify(dragStartBbox)
      dragPreview.value = { id: draggingObjectId, bbox: box }
    } else if (bboxStart) {
      tempBbox.value = { x: Math.min(bboxStart.x, point.x), y: Math.min(bboxStart.y, point.y), width: Math.abs(point.x-bboxStart.x), height: Math.abs(point.y-bboxStart.y) }
    }
  }
  const cancelAnnotationGesture = () => {
    const id = bboxPointerId
    bboxPointerId = null; bboxStart = null; tempBbox.value = null
    draggingObjectId = null; dragStartPoint = null; dragStartBbox = null; dragMoved = false; resizeHandle = null; dragPreview.value = null
    if (id !== null && annotationHitRef.value?.hasPointerCapture(id)) annotationHitRef.value.releasePointerCapture(id)
  }
  const onBboxUp = (event: PointerEvent) => {
    if (bboxPointerId !== event.pointerId) return
    onBboxMove(event)
    if (dragPreview.value && dragMoved) {
      snapshotUndo() // Snapshot the original, before the single committed mutation.
      const { id, bbox } = dragPreview.value, mid = currentMediaId.value
      annotationsByMedia.value = { ...annotationsByMedia.value, [mid]: annotationsByMedia.value[mid].map(o => o.id === id ? { ...o, bbox, source: 'manual' as const } : o) }
      statusMessage.value = resizeHandle ? '已调整标注框' : '已移动标注框'
    } else if (bboxStart && tempBbox.value) {
      const b = tempBbox.value, w = selectedMedia.value?.width || 1000, h = selectedMedia.value?.height || 1000
      if (b.width*w/100 >= 2 && b.height*h/100 >= 2) addObject({ x: b.x+b.width/2, y: b.y+b.height/2 }, { ...b })
    }
    cancelAnnotationGesture()
  }
  const nudgeSelected = (dx: number, dy: number) => {
    const obj = selectedObject.value, media = selectedMedia.value
    if (editingBlocked.value || isInteracting() || !obj?.bbox || !media?.width || !media.height) return
    const bbox = moveBox(obj.bbox, dx/media.width*100, dy/media.height*100)
    if (JSON.stringify(bbox) === JSON.stringify(obj.bbox)) return
    snapshotUndo()
    annotationsByMedia.value = { ...annotationsByMedia.value, [media.id]: annotationsByMedia.value[media.id].map(o => o.id === obj.id ? { ...o, bbox, source: 'manual' as const } : o) }
    statusMessage.value = `已微调 ${obj.name}`
  }

  const onObjectDropdownChange = () => {
    if (selectedObjectId.value) selectObject(selectedObjectId.value)
  }

  const selectObject = (objectId: string) => {
    selectedObjectId.value = objectId
    const object = currentObjects.value.find((item) => item.id === objectId)
    if (!object) return

    statusMessage.value = isVideo.value
      ? `已定位：${object.name} · 第 ${currentFrame.value + 1} 帧`
      : `已定位：${object.name}`
  }

  const removeObject = (objectId: string) => {
    if (editingBlocked.value || isInteracting()) return
    const target = currentObjects.value.find(o => o.id === objectId)
    if (!target) return
    snapshotUndo()
    const mediaId = currentMediaId.value, frameIndex = currentFrame.value
    annotationsByMedia.value = { ...annotationsByMedia.value, [mediaId]: (annotationsByMedia.value[mediaId] ?? []).filter(o => !((o.frameIndex ?? 0) === frameIndex && (target.objectId != null ? o.objectId === target.objectId : o.id === objectId))) }
    if (target.objectId != null) {
      deletedFrameObjects.value[mediaId] = [...(deletedFrameObjects.value[mediaId] ?? []).filter(d => !(d.objectId === target.objectId && d.frameIndex === frameIndex)), { objectId: target.objectId, frameIndex }]
    } else {
      if (!deletedTrackingIds.value[mediaId]) deletedTrackingIds.value[mediaId] = new Set()
      deletedTrackingIds.value[mediaId].add(objectId)
    }
    if (lastPausedContext.value?.mediaId === mediaId && lastPausedContext.value.frameIndex === frameIndex) {
      pausedAnomalies.value = pausedAnomalies.value.filter(item => item.objectId !== target.objectId)
      if (!pausedAnomalies.value.length) lastPausedContext.value = null
    }
    statusMessage.value = `已删除第 ${frameIndex + 1} 帧的 ${target.name}，可撤销`
    scheduleWorkspaceStateSave(mediaId)
    selectedObjectId.value = null
  }

  const renameObject = () => {
    if (editingBlocked.value || isInteracting()) return
    const name = objectNameInput.value.trim()
    if (!selectedObjectId.value || !name) return
    snapshotUndo()
    const mediaId = currentMediaId.value
    const allObjects = annotationsByMedia.value[mediaId] ?? []
    annotationsByMedia.value = {
      ...annotationsByMedia.value,
      [mediaId]: allObjects.map((obj) => obj.id === selectedObjectId.value ? { ...obj, name, source: 'manual' as const } : obj),
    }
    statusMessage.value = `对象已命名为：${name}`
  }

  // 复制上一帧标注到当前帧
  const copyPreviousFrame = () => {
    if (editingBlocked.value || isInteracting()) return
    if (!isVideo.value) { showToast('仅视频支持复制上一帧'); return }
    const mediaId = currentMediaId.value
    const prevFrame = currentFrame.value - 1
    if (prevFrame < 0) { showToast('已是第一帧'); return }
    const allObjs = annotationsByMedia.value[mediaId] ?? []
    const present = new Set(currentObjects.value.map(o => o.objectId ?? o.id))
    const prevObjs = allObjs.filter(o => (o.frameIndex ?? 0) === prevFrame && !present.has(o.objectId ?? o.id))
    if (!prevObjs.length) { showToast(`上一帧没有可补充的对象`); return }
    snapshotUndo()
    const newObjs = prevObjs.map((o) => ({
      ...JSON.parse(JSON.stringify(o)),
      id: `manual-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      frameIndex: currentFrame.value,
      timestampMs: Math.round(currentTime.value * 1000),
      source: 'manual' as const,
    }))
    const copiedIds = new Set(newObjs.map(o => o.objectId))
    deletedFrameObjects.value[mediaId] = (deletedFrameObjects.value[mediaId] ?? []).filter(d => d.frameIndex !== currentFrame.value || !copiedIds.has(d.objectId))
    annotationsByMedia.value = { ...annotationsByMedia.value, [mediaId]: [...allObjs, ...newObjs] }
    statusMessage.value = `已从上一帧补充 ${newObjs.length} 个标注`
  }

  // 亮度/对比度
  const brightness = ref(100)
  const contrast = ref(100)
  const mediaFilterStyle = computed(() => `brightness(${brightness.value}%) contrast(${contrast.value}%)`)
  const resetMediaFilter = () => { brightness.value = 100; contrast.value = 100 }

  const workspaceSaveTimers = new Map<string, ReturnType<typeof setTimeout>>()

  const latestManualBaselines = (media: MediaAsset, objects: AnnotationObject[]) => {
    if (!media.width || !media.height) return []
    const latest = new Map<number, AnnotationObject>()
    for (const obj of objects) {
      if (obj.source !== 'manual' || obj.objectId == null || !obj.bbox) continue
      const previous = latest.get(obj.objectId)
      if (!previous || (obj.frameIndex ?? 0) >= (previous.frameIndex ?? 0)) latest.set(obj.objectId, obj)
    }
    return [...latest.values()].map((obj) => ({
      objectId: obj.objectId as number,
      name: obj.name,
      source: 'manual' as const,
      frameIndex: obj.frameIndex ?? 0,
      bbox: [
        obj.bbox!.x / 100 * media.width!,
        obj.bbox!.y / 100 * media.height!,
        (obj.bbox!.x + obj.bbox!.width) / 100 * media.width!,
        (obj.bbox!.y + obj.bbox!.height) / 100 * media.height!,
      ] as [number, number, number, number],
    }))
  }

  const buildWorkspaceState = (mediaId: string, useCurrentUiState = false): WorkspaceDraft => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media) throw new Error('素材不存在')
    const includeUiState = useCurrentUiState || mediaId === currentMediaId.value
    const manualAnnotations = dedupeAnnotationObjects(
      (annotationsByMedia.value[mediaId] ?? []).filter((obj) => obj.source === 'manual'),
    )
    const state = {
      format: 'annotation-workspace-v1' as const,
      frontendMediaId: media.id,
      updatedAt: new Date().toISOString(),
      currentFrame: includeUiState ? currentFrame.value : 0,
      manualAnnotations: JSON.parse(JSON.stringify(manualAnnotations)),
      manualBaselines: latestManualBaselines(media, manualAnnotations),
      deletedTrackingIds: [...(deletedTrackingIds.value[mediaId] ?? new Set<string>())],
      deletedObjectIds: [...(deletedObjectIds.value[mediaId] ?? [])],
      deletedFrameObjects: clone(deletedFrameObjects.value[mediaId] ?? []),
      anomalyFrames: includeUiState ? JSON.parse(JSON.stringify(anomalyFrames.value)) : [],
      pausedAnomalies: includeUiState ? JSON.parse(JSON.stringify(pausedAnomalies.value)) : [],
      lastPausedContext: includeUiState && lastPausedContext.value?.mediaId === mediaId
        ? { mediaId, frameIndex: lastPausedContext.value.frameIndex }
        : null,
      display: {
        brightness: includeUiState ? brightness.value : 100,
        contrast: includeUiState ? contrast.value : 100,
        zoom: includeUiState ? zoom.value : 1,
      },
      editor: {
        activeTool: includeUiState ? activeTool.value : 'select',
        objectNameInput: includeUiState ? objectNameInput.value : 'rare sperm',
        selectedObjectId: includeUiState ? selectedObjectId.value : null,
      },
    }
    if (!includeUiState) Object.assign(state, workspaceViews.get(mediaId) || {})
    else workspaceViews.set(mediaId, { currentFrame: state.currentFrame, display: state.display, editor: state.editor, anomalyFrames: state.anomalyFrames, pausedAnomalies: state.pausedAnomalies, lastPausedContext: state.lastPausedContext })
    return state
  }

  persistWorkspaceState = async (mediaId: string, useCurrentUiState = false) => {
    if (deletingMediaId.value === mediaId || pendingDeletion.value || pendingFeedback.value || pendingRestart.value) return
    const media = mediaAssets.value.find(item => item.id === mediaId)
    if (!media?.serverMediaId || workspaceRestoreInProgress || (workspaceRestoring.value && mediaId === currentMediaId.value)) return
    if (workspaceReadError.value) throw new Error(workspaceReadError.value)
    const state = buildWorkspaceState(mediaId, useCurrentUiState)
    const ticket = (saveTickets.get(mediaId) || 0) + 1
    saveTickets.set(mediaId, ticket)
    if (mediaId === currentMediaId.value) saveState.value = 'saving'
    const prior = saveQueues.get(mediaId) || Promise.resolve()
    const task = prior.catch(() => {}).then(() => workspaceWrites.write(media.serverMediaId!, 'workspace', state))
    saveQueues.set(mediaId, task)
    try {
      await task
      if (mediaId === currentMediaId.value && ticket === saveTickets.get(mediaId)) { saveState.value = 'saved'; saveError.value = '' }
    } catch (error) {
      markVersionConflict(error)
      console.error('[annotation.workspace_save_failed]', { mediaId, ticket, error })
      if (mediaId === currentMediaId.value && ticket === saveTickets.get(mediaId)) {
        saveState.value = 'error'; saveError.value = messageOf(error, '保存失败，请重试')
      }
      throw error
    } finally { if (saveQueues.get(mediaId) === task) saveQueues.delete(mediaId) }
  }

  scheduleWorkspaceStateSave = (mediaId: string) => {
    if (mediaImportBusy.value || duplicateImport.value || deletingMediaId.value === mediaId || pendingDeletion.value || pendingFeedback.value || pendingRestart.value || trackingRestartBusy.value || objectDeletionBusy.value || trackingFeedbackBusy.value || workspaceReadError.value) return
    if (!mediaId || workspaceRestoreInProgress || workspaceRestoring.value || !mediaAssets.value.find(m => m.id === mediaId)?.serverMediaId) return
    if (mediaId === currentMediaId.value) saveState.value = 'saving'
    const previous = workspaceSaveTimers.get(mediaId)
    if (previous) clearTimeout(previous)
    workspaceSaveTimers.set(mediaId, setTimeout(() => {
      workspaceSaveTimers.delete(mediaId)
      void persistWorkspaceState(mediaId).catch((error) => console.warn('工作区状态保存失败：', error))
    }, 800))
  }

  const cancelScheduledSave = (mediaId: string) => {
    const timer = workspaceSaveTimers.get(mediaId)
    if (timer) clearTimeout(timer)
    workspaceSaveTimers.delete(mediaId)
  }
  const workspaceRecoveryRequired = computed(()=>!!workspaceReadError.value)
  const saveRecoveryLabel = computed(()=>workspaceRecoveryRequired.value?'重新读取工作区':pendingDeletion.value?'重试本次删除/撤销':pendingFeedback.value?'重试异常确认':'重试保存')
  const retryWorkspaceSave = async () => {
    cancelScheduledSave(currentMediaId.value)
    if(pendingDeletion.value){await retryObjectDeletion();return}
    if(pendingFeedback.value){await retryTrackingAnomalyFeedback();return}
    await persistWorkspaceState(currentMediaId.value,true)
  }
  const reloadWorkspaceFromServer = async () => {
    const mediaId=currentMediaId.value
    // Explicit reload keeps a local backup of geometry and pending intent.
    const payload={workspace:buildWorkspaceState(mediaId,true),objects:annotationsByMedia.value[mediaId]||[],tracking:trackingFramesByMedia.value[mediaId]||[],pendingDeletion:pendingDeletion.value,pendingFeedback:pendingFeedback.value,pendingRestart:pendingRestart.value}
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}))
    const anchor=document.createElement('a');anchor.href=url;anchor.download='annotation-recovery-'+createRequestId()+'.json';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
    cancelScheduledSave(mediaId)
    if (pendingRestart.value?.phase === 'conflict' && pendingRestart.value.mediaId === mediaId) { rememberRestart(pendingRestart.value, true); pendingRestart.value = null; trackingRestartError.value = '' }
    annotationsByMedia.value={...annotationsByMedia.value,[mediaId]:(annotationsByMedia.value[mediaId]||[]).filter(o=>o.source==='manual')}
    trackingFramesByMedia.value[mediaId]=[]
    await resetAnnotationViewForMedia(mediaId)
  }
  const controlsCacheKey = (serverMediaId: string) => {
    let actor = 'unknown'
    try { actor = String(JSON.parse(localStorage.getItem('rare-sperm-auth') || '{}').id || actor) } catch {}
    return `annotation-controls:${actor}:${serverMediaId}`
  }
  const rememberControls = (mediaId: string) => {
    const media = mediaAssets.value.find(m => m.id === mediaId)
    if (!media?.serverMediaId) return
    try {
      sessionStorage.setItem(controlsCacheKey(media.serverMediaId), JSON.stringify({
        deletion: pendingDeletion.value?.mediaId === mediaId ? pendingDeletion.value : null,
        feedback: pendingFeedback.value?.mediaId === mediaId ? pendingFeedback.value : null,
        history: videoDeletionHistory.value[mediaId] ?? null,
      }))
    } catch (error) { console.warn('[annotation.controls_cache_failed]', { mediaId, error }) }
  }
  const definiteRejection = (error: unknown) => {
    const status = Number((error as { status?: number })?.status)
    return status >= 400 && status < 500 && status !== 408 && status !== 429
  }
  const markVersionConflict = (error: unknown) => {
    if (error instanceof Error && /工作区版本|重试标识/.test(error.message)) {
      workspaceReadError.value = `${error.message}；请重新加载素材`
      saveState.value = 'error'; saveError.value = workspaceReadError.value
    }
  }
  const restartJournalKey = (actorId: string, serverMediaId: string) => `annotation-restart:${actorId}:${serverMediaId}`
  const rememberRestart = (intent: TrackingRestartIntent, remove = false) => {
    try {
      const key = restartJournalKey(intent.actorId, intent.serverMediaId)
      if (remove) sessionStorage.removeItem(key)
      else sessionStorage.setItem(key, JSON.stringify(intent))
    } catch (error) {
      trackingRestartStorageWarning.value = '浏览器无法保存重追恢复记录。请保留当前页面重试；刷新或关闭页面后无法保证恢复此次请求。'
      console.error('[annotation.restart_journal_failed]', { mediaId: intent.serverMediaId, error })
    }
  }
  const restoreRestart = (mediaId: string) => {
    const media = mediaAssets.value.find(item => item.id === mediaId)
    const actorId = useAuth().user.value?.id
    if (!media?.serverMediaId || !actorId) return
    try {
      const intent = JSON.parse(sessionStorage.getItem(restartJournalKey(actorId, media.serverMediaId)) || 'null') as TrackingRestartIntent | null
      if (!intent) return
      if (intent.actorId !== actorId || intent.serverMediaId !== media.serverMediaId || !intent.key
        || !['restart', 'start', 'conflict'].includes(intent.phase) || !Number.isSafeInteger(intent.body?.expectedRevision)
        || !Number.isSafeInteger(intent.body?.startFrame) || !Number.isSafeInteger(intent.body?.expectedPausedFrame)
        || intent.body.startFrame < 0 || intent.body.startFrame >= intent.body.expectedPausedFrame || intent.body.confirmDiscardFuture !== true) throw new Error('回退重追恢复记录损坏')
      intent.mediaId = mediaId
      if (trackingJobs.has(mediaId)) { rememberRestart(intent, true); return }
      pendingRestart.value = intent
      trackingRestartError.value = intent.phase === 'conflict' ? '回退时工作区发生冲突，请备份并重新读取工作区后再选择起点。'
        : intent.phase === 'start' ? '旧分支已替换，新追踪尚未开始。请继续从已确认的起点追踪。'
          : '上次回退请求结果尚未确认，请重试原请求；不能重新创建或替换起点。'
      if (intent.phase === 'conflict') workspaceReadError.value = trackingRestartError.value
    } catch (error) {
      trackingRestartError.value = '回退重追恢复记录无法读取，请保留页面并联系管理员检查。'
      workspaceReadError.value = trackingRestartError.value
      console.error('[annotation.restart_restore_failed]', { mediaId: media.serverMediaId, error })
    }
  }
  const retryTrackingRestart = async (): Promise<boolean> => {
    const intent = pendingRestart.value
    if (!intent || trackingRestartBusy.value || intent.phase === 'conflict' || currentMediaId.value !== intent.mediaId || useAuth().user.value?.id !== intent.actorId) return false
    const serial = ++restartEpoch, actorId = intent.actorId
    restartController?.abort(); restartController = new AbortController()
    const isCurrent = () => serial === restartEpoch && actorId === useAuth().user.value?.id && currentMediaId.value === intent.mediaId && pendingRestart.value === intent
    trackingRestartBusy.value = true; trackingRestartError.value = ''
    try {
      if (intent.phase === 'restart') {
        const result = await trackApi.restartBranch(intent.serverMediaId, intent.body, intent.key, restartController.signal)
        if (!isCurrent()) return false
        workspaceWrites.setRevision(intent.serverMediaId, result.revision)
        intent.phase = 'start'; rememberRestart(intent)
        console.info('[annotation.restart_committed]', { mediaId: intent.serverMediaId, startFrame: intent.body.startFrame, pausedFrame: intent.body.expectedPausedFrame, revision: result.revision, removedRows: result.removedRows })
      }
      if (!isCurrent()) return false
      // A replayed restart receipt confirms the past file operation. It does
      // not grant permission to seed a source that has since been submitted.
      if (submissionBusy.value) throw new Error('当前有送审操作尚未结束，请先完成或关闭送审操作，再继续原起点追踪')
      const submission = await reviewWorkflowApi.submissionStatus(intent.serverMediaId)
      if (!isCurrent()) return false
      if (!Array.isArray(submission.items)) throw new Error('送审状态响应无法读取，已停止续追；请重试查询原起点')
      if (submission.items.length) throw Object.assign(new Error('该视频已送审，不能继续重追或改写起始框；请先在允许撤回时撤回送审'), {status:409,code:'TRACKING_SOURCE_SUBMITTED'})
      const start = intent.body.startFrame
      // Human edits anywhere in the video survive replacing an AI branch.
      annotationsByMedia.value = { ...annotationsByMedia.value, [intent.mediaId]: (annotationsByMedia.value[intent.mediaId] || []).filter(object => object.source === 'manual' || (object.frameIndex ?? 0) <= start) }
      trackingFramesByMedia.value[intent.mediaId] = (trackingFramesByMedia.value[intent.mediaId] || []).filter(frame => frame.frameIndex <= start)
      pausedAnomalies.value = []; lastPausedContext.value = null; anomalyObjectIds.value = []; anomalyPanelVisible.value = false
      anomalyFrames.value = anomalyFrames.value.filter(frame => frame.frame_index <= start)
      delete videoDeletionHistory.value[intent.mediaId]
      for (const key of histories.keys()) if (key.startsWith(`${intent.mediaId}:`)) histories.delete(key)
      historyRevision.value++; rememberControls(intent.mediaId)
      if (currentFrame.value !== start || frameError.value) await seekVideo(frameToTime(start))
      if (!isCurrent() || currentFrame.value !== start || frameError.value || exactFrameLoading.value) throw new Error('分支回退已保存，但起始帧未读取成功；请重试从原起点继续追踪')
      await performTracking(false, intent)
      if (pendingRestart.value === intent && isCurrent()) {
        const failure = trackingErrors.value[intent.mediaId]
        trackingRestartError.value = failure ? `${failure.phase}失败：${failure.message}。已替换的分支保留，请继续原起点。` : '新追踪尚未开始，请重试从原起点继续追踪。'
        delete trackingErrors.value[intent.mediaId]
        return false
      }
      return true
    } catch (error) {
      if (!isCurrent()) return false
      const requestId = (error as {requestId?: string}).requestId
      trackingRestartError.value = `${messageOf(error, '回退重追失败，请重试原请求')}${requestId ? `（记录号 ${requestId}）` : ''}`
      if (definiteRejection(error)) {
        intent.phase = 'conflict'; rememberRestart(intent)
        workspaceReadError.value = `${trackingRestartError.value}；请备份并重新读取工作区，不能自动覆盖新状态`
        saveState.value = 'error'; saveError.value = workspaceReadError.value
      }
      console.error('[annotation.restart_failed]', { mediaId: intent.serverMediaId, startFrame: intent.body.startFrame, pausedFrame: intent.body.expectedPausedFrame, phase: intent.phase, requestId, error })
      return false
    } finally {
      if (serial === restartEpoch && actorId === useAuth().user.value?.id) trackingRestartBusy.value = false
    }
  }
  const restartTrackingFromEarlierFrame = async (startFrame: number, pausedFrame: number): Promise<boolean> => {
    const media = selectedMedia.value, actorId = useAuth().user.value?.id
    if (!media?.serverMediaId || !actorId || editingBlocked.value || isInteracting() || startFrame !== currentFrame.value
      || pausedFrame !== trackingPausedFrame.value || startFrame >= pausedFrame || !currentObjects.value.some(object => object.bbox)) return false
    const serial = ++restartEpoch
    const isCurrent = () => serial === restartEpoch && actorId === useAuth().user.value?.id && media.id === currentMediaId.value
    trackingRestartBusy.value = true; trackingRestartError.value = ''; trackingRestartStorageWarning.value = ''
    try {
      cancelScheduledSave(media.id)
      await persistWorkspaceState(media.id)
      if (!isCurrent()) return false
      const expectedRevision = workspaceWrites.getRevision(media.serverMediaId)
      if (expectedRevision == null) throw new Error('工作区版本尚未读取，请重新读取素材后再重追')
      pendingRestart.value = { actorId, mediaId: media.id, serverMediaId: media.serverMediaId, key: createRequestId(), phase: 'restart', body: { expectedRevision, expectedPausedFrame: pausedFrame, startFrame, confirmDiscardFuture: true, ...(workspaceGenerations.get(media.id) ? {generationId: workspaceGenerations.get(media.id)} : {}) } }
      rememberRestart(pendingRestart.value)
    } catch (error) {
      if (isCurrent()) { trackingRestartError.value = messageOf(error, '请先重试保存当前修正，再重新追踪'); markVersionConflict(error); console.error('[annotation.restart_prepare_failed]', {mediaId:media.serverMediaId,startFrame,error}) }
      return false
    } finally { if (isCurrent()) trackingRestartBusy.value = false }
    return retryTrackingRestart()
  }
  const getObjectDeletionSummary = async (objectId: number): Promise<ObjectDeletionSummary> => {
    const media = selectedMedia.value
    if (!media?.serverMediaId || objectDeletionBusy.value || trackingFeedbackPending.value) throw new Error('当前无法读取删除范围')
    if (pendingDeletion.value) throw new Error('请先重试上次删除操作')
    objectDeletionBusy.value = true
    try {
      cancelScheduledSave(media.id)
      await persistWorkspaceState(media.id)
      return await trackApi.getObjectDeletionSummary(media.serverMediaId, objectId)
    } finally { objectDeletionBusy.value = false }
  }
  const retryObjectDeletion = async (): Promise<boolean> => {
    const intent = pendingDeletion.value
    if (!intent || objectDeletionBusy.value) return false
    objectDeletionBusy.value = true; objectDeletionError.value = ''
    try {
      await workspaceWrites.write(intent.serverMediaId, 'workspace', intent.state, intent.key)
      const mid = intent.mediaId, record = intent.record
      deletedObjectIds.value[mid] = [...(intent.state.deletedObjectIds ?? [])]
      deletedFrameObjects.value[mid] = clone(intent.state.deletedFrameObjects ?? [])
      const others = (annotationsByMedia.value[mid] ?? []).filter(o => o.objectId !== record.objectId)
      annotationsByMedia.value = { ...annotationsByMedia.value, [mid]: intent.action === 'undo' ? dedupeAnnotationObjects([...others, ...record.objects]) : others }
      videoDeletionHistory.value[mid] = { ...record, sequence: ++historySequence, undone: intent.action === 'undo' }
      if (mid === currentMediaId.value) {
        pausedAnomalies.value = (intent.state.pausedAnomalies ?? []) as typeof pausedAnomalies.value
        lastPausedContext.value = intent.state.lastPausedContext ?? null
        selectedObjectId.value = null
        saveState.value = 'saved'; saveError.value = ''; workspaceReadError.value = ''
      }
      pendingDeletion.value = null; rememberControls(mid); historyRevision.value++
      console.info('[annotation.object_deletion_saved]', { mediaId: intent.serverMediaId, objectId: record.objectId, action: intent.action, key: intent.key })
      statusMessage.value = intent.action === 'undo' ? `已恢复 ${record.name} 在全视频中的标注` : `已删除 ${record.name} 在全视频中的标注，可撤销`
      showToast(statusMessage.value)
      if (intent.action === 'undo' && mid === currentMediaId.value) await loadTrackingResult(mid, true)
      return true
    } catch (error) {
      objectDeletionError.value = error instanceof Error ? error.message : '删除保存失败，请重试'
      if (definiteRejection(error)) { pendingDeletion.value = null; rememberControls(intent.mediaId) }
      markVersionConflict(error)
      console.error('[annotation.object_deletion_failed]', { mediaId: intent.serverMediaId, objectId: intent.record.objectId, key: intent.key, error })
      return false
    } finally { objectDeletionBusy.value = false }
  }
  const applyVideoDeletion = async (action: 'delete' | 'undo' | 'redo', record: VideoDeletion): Promise<boolean> => {
    const media = selectedMedia.value
    if (!media?.serverMediaId || editingBlocked.value || isInteracting()) return false
    objectDeletionBusy.value = true; objectDeletionError.value = ''
    try {
      pausePlayback(); cancelScheduledSave(media.id)
      await persistWorkspaceState(media.id)
      const state = buildWorkspaceState(media.id)
      const deleted = new Set(state.deletedObjectIds ?? [])
      if (action === 'undo') deleted.delete(record.objectId); else deleted.add(record.objectId)
      state.deletedObjectIds = [...deleted]
      const otherManual = (state.manualAnnotations as AnnotationObject[]).filter(o => o.objectId !== record.objectId)
      state.manualAnnotations = action === 'undo' ? dedupeAnnotationObjects([...otherManual, ...record.objects.filter(o => o.source === 'manual')]) : otherManual
      state.manualBaselines = latestManualBaselines(media, state.manualAnnotations as AnnotationObject[])
      if (action === 'undo' && record.pause) {
        state.pausedAnomalies = [...(state.pausedAnomalies as typeof pausedAnomalies.value).filter(item => item.objectId !== record.objectId), clone(record.pause.item)]
        state.lastPausedContext = clone(record.pause.context)
      }
      if (action !== 'undo') {
        state.pausedAnomalies = (state.pausedAnomalies as typeof pausedAnomalies.value).filter(item => item.objectId !== record.objectId)
        if (!state.pausedAnomalies.length) state.lastPausedContext = null
      }
      pendingDeletion.value = { mediaId: media.id, serverMediaId: media.serverMediaId, key: createRequestId(), action, record: clone(record), state }
      rememberControls(media.id)
    } catch (error) {
      objectDeletionError.value = error instanceof Error ? error.message : '删除准备失败，请重试'
      markVersionConflict(error)
      return false
    } finally { objectDeletionBusy.value = false }
    return retryObjectDeletion()
  }
  const removeObjectAcrossVideo = async (objectId: number): Promise<boolean> => {
    const objects = (annotationsByMedia.value[currentMediaId.value] ?? []).filter(o => o.objectId === objectId)
    if (!objects.length) { objectDeletionError.value = '当前对象不存在，请重新选择'; return false }
    const item = pausedAnomalies.value.find(item => item.objectId === objectId)
    const pause = item && lastPausedContext.value?.mediaId === currentMediaId.value ? { item: clone(item), context: clone(lastPausedContext.value) } : undefined
    return applyVideoDeletion('delete', { pause, objectId, name: objects[0].name, objects: clone(objects), frameCount: new Set(objects.map(o => o.frameIndex ?? 0)).size, sequence: ++historySequence, undone: false })
  }
  const undoVideoObjectDeletion = async (): Promise<boolean> => {
    const record = videoDeletionHistory.value[currentMediaId.value]
    if (!record || record.undone) return false
    return applyVideoDeletion('undo', record)
  }
  const retryTrackingAnomalyFeedback = async (): Promise<boolean> => {
    const intent = pendingFeedback.value
    if (!intent || trackingFeedbackBusy.value) return false
    trackingFeedbackBusy.value = true; trackingFeedbackError.value = ''
    let resume = false
    try {
      const response = await workspaceWrites.write(intent.serverMediaId, 'feedback', intent.body, intent.key) as unknown as TrackingFeedbackResponse
      normalMotionSamples.value[intent.mediaId] = response.normalMotionSamples
      trackingFeedbackEvents.value[intent.mediaId] = response.trackingFeedbackEvents ?? []
      if (intent.mediaId === currentMediaId.value) {
        pausedAnomalies.value = (response.pausedAnomalies as typeof pausedAnomalies.value).map(normalizePauseNotice)
        lastPausedContext.value = response.lastPausedContext ? { mediaId: intent.mediaId, frameIndex: response.lastPausedContext.frameIndex } : null
        anomalyObjectIds.value = pausedAnomalies.value.map(item => item.objectId)
        anomalyPanelVisible.value = pausedAnomalies.value.length > 0
        resume = intent.body.decision !== 'reset' && !pausedAnomalies.value.length
      }
      pendingFeedback.value = null; rememberControls(intent.mediaId)
      showToast(intent.body.decision === 'reset' ? `已恢复对象 #${intent.body.objectId} 的默认判断` : resume ? '人工确认已保存，继续追踪' : '已确认此对象，请继续检查其他暂停对象')
      console.info('[annotation.feedback_saved]', { mediaId: intent.serverMediaId, objectId: intent.body.objectId, decision: intent.body.decision, calibrate: intent.body.calibrate, key: intent.key })
    } catch (error) {
      trackingFeedbackError.value = error instanceof Error ? error.message : '确认保存失败，请重试'
      if (definiteRejection(error)) { pendingFeedback.value = null; rememberControls(intent.mediaId) }
      markVersionConflict(error)
      console.error('[annotation.feedback_failed]', { mediaId: intent.serverMediaId, key: intent.key, error })
      return false
    } finally { trackingFeedbackBusy.value = false }
    if (resume) {
      // Confirmation applies to the paused frame even if the view moved during
      // a slow request. Resume only from that exact saved frame.
      if (currentFrame.value !== intent.body.frameIndex) await seekVideo(frameToTime(intent.body.frameIndex))
      if (currentMediaId.value === intent.mediaId && currentFrame.value === intent.body.frameIndex && !frameError.value && !workspaceRestoring.value && !exactFrameLoading.value) await runAiTrack()
      else showToast('确认已保存；暂停帧读取未完成，请返回该帧后手动继续追踪')
    }
    return true
  }
  const prepareTrackingFeedback = async (objectId: number, decision: 'normal' | 'corrected' | 'reset', learn: boolean): Promise<boolean> => {
    const media = selectedMedia.value
    if (!media?.serverMediaId || editingBlocked.value || isInteracting()) return false
    const context = lastPausedContext.value
    if (decision !== 'reset' && (context?.mediaId !== media.id || context.frameIndex !== currentFrame.value || !pausedAnomalies.value.some(item => item.objectId === objectId))) {
      trackingFeedbackError.value = '请返回暂停帧并选择待确认对象'; return false
    }
    trackingFeedbackBusy.value = true; trackingFeedbackError.value = ''
    try {
      cancelScheduledSave(media.id)
      await persistWorkspaceState(media.id)
      pendingFeedback.value = { mediaId: media.id, serverMediaId: media.serverMediaId, key: createRequestId(), body: { expectedRevision: 0, objectId, frameIndex: context?.frameIndex ?? currentFrame.value, decision, calibrate: decision === 'normal' && learn } }
      rememberControls(media.id)
    } catch (error) {
      trackingFeedbackError.value = error instanceof Error ? error.message : '请先重试工作区保存'
      markVersionConflict(error)
      return false
    } finally { trackingFeedbackBusy.value = false }
    return retryTrackingAnomalyFeedback()
  }
  const confirmTrackingAnomaly = (objectId: number, decision: 'normal' | 'corrected', learn: boolean) => prepareTrackingFeedback(objectId, decision, learn)
  const resetTrackingCalibration = (objectId: number) => prepareTrackingFeedback(objectId, 'reset', false)

  const deleteMedia = async (mediaId: string) => {
    const media = mediaAssets.value.find(item => item.id === mediaId)
    if (!media || isAiBusy.value || deletingMediaId.value || workspaceRestoring.value || objectDeletionBusy.value || trackingFeedbackBusy.value || pendingDeletion.value || pendingFeedback.value) return
    mediaDeleteError.value = ''
    const message = media.serverMediaId
      ? `将从共享素材库删除「${media.name}」的原视频、工作区和追踪文件，其他使用者也将无法访问。此操作不能撤销；已送审的视频不能删除。是否继续？`
      : `将删除「${media.name}」在本机工作区中的素材和标注，不能撤销。电脑原文件和已保存到服务器的标注记录会保留。是否继续？`
    if (!window.confirm(message)) return
    deletingMediaId.value = mediaId
    pausePlayback(); cancelAnnotationGesture()
    const pendingTimer = workspaceSaveTimers.get(mediaId)
    if (pendingTimer) clearTimeout(pendingTimer)
    workspaceSaveTimers.delete(mediaId)
    try {
      // Drain writes already sent before DELETE. New saves are blocked until
      // it finishes, so a delayed autosave cannot recreate the removed folder.
      await saveQueues.get(mediaId)?.catch(() => {})
      if (media.serverMediaId) {
        const result = await trackApi.deleteMedia(media.serverMediaId)
        if (!result.deleted || result.mediaId !== media.serverMediaId) throw new Error('服务器未确认删除，请刷新素材列表核对')
      }
      mediaAssets.value = mediaAssets.value.filter(item => item.id !== mediaId)
      const remainingAnnotations = {...annotationsByMedia.value}
      delete remainingAnnotations[mediaId]
      annotationsByMedia.value = remainingAnnotations
      delete trackingFramesByMedia.value[mediaId]
      delete deletedTrackingIds.value[mediaId]
      if (media.serverMediaId) {
        delete closedMediaFrontendIds[media.serverMediaId]
        persistClosedMediaFrontendIds()
      }
      workspaceViews.delete(mediaId); saveTickets.delete(mediaId)
      if (media.url.startsWith('blob:')) URL.revokeObjectURL(media.url)
      frameCache.clear()
      if (selectedMediaId.value === mediaId) selectedMediaId.value = mediaAssets.value[0]?.id || ''
      // Persist immediately as well as the normal debounced save, allowing an
      // immediate refresh after successful deletion.
      try {
        localStorage.setItem('mediaAssets', JSON.stringify(mediaAssets.value))
        persistLocalAnnotations()
      } catch (error) { console.warn('[annotation.local_save_failed]', error) }
      console.info('[annotation.media_deleted]', { mediaId: media.serverMediaId || media.id, scope: media.serverMediaId ? 'server' : 'local' })
      statusMessage.value = `已删除素材：${media.name}`
      showToast(statusMessage.value)
    } catch (error) {
      console.error('[annotation.media_delete_failed]', { mediaId: media.serverMediaId || media.id, scope: media.serverMediaId ? 'server' : 'local', error })
      mediaDeleteError.value = error instanceof Error ? error.message : '删除失败，请重试'
      statusMessage.value = mediaDeleteError.value
    } finally {
      deletingMediaId.value = null
      // A refused/failed delete must not cancel unsaved annotation work.
      if (mediaDeleteError.value && pendingTimer) scheduleWorkspaceStateSave(mediaId)
    }
  }

  // Switching assets happens before the new asset resets the shared editor
  // refs. Persist the old asset with the still-current UI values so its frame,
  // anomaly panel and display/editor state are not overwritten by defaults.
  watch(selectedMediaId, (nextId, previousId) => {
    try { localStorage.setItem('annotation-selected-media', nextId) } catch (error) { console.warn('[annotation.selection_save_failed]', error) }
    if (!previousId || previousId === nextId) return
    void persistWorkspaceState(previousId, true).catch((error) => console.warn('切换素材前保存工作区状态失败：', error))
  })

  watch(
    [currentFrame, brightness, contrast, zoom, anomalyFrames, pausedAnomalies, lastPausedContext, activeTool, objectNameInput, selectedObjectId],
    () => {
      if (!workspaceRestoreInProgress) scheduleWorkspaceStateSave(currentMediaId.value)
    },
    { deep: true },
  )

  const restoreWorkspaceState = async (mediaId: string, isCurrent: () => boolean): Promise<number | null> => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media?.serverMediaId) return null
    try {
      try {
        const controls = JSON.parse(sessionStorage.getItem(controlsCacheKey(media.serverMediaId)) || 'null')
        if (controls?.history) { videoDeletionHistory.value[mediaId] = controls.history; historySequence = Math.max(historySequence, controls.history.sequence) }
        if (controls?.deletion) pendingDeletion.value = { ...controls.deletion, mediaId }
        if (controls?.feedback) pendingFeedback.value = { ...controls.feedback, mediaId }
      } catch (error) { console.warn('[annotation.controls_restore_failed]', { mediaId, error }) }
      await saveQueues.get(mediaId)?.catch(() => {})
      if (!isCurrent()) return null
      let state = await trackApi.getWorkspaceState(media.serverMediaId)
      if (!isCurrent()) return null
      const generationKey=`annotation-generation:${media.serverMediaId}`
      if(state.generationId&&localStorage.getItem(generationKey)!==state.generationId){
        await workspaceWrites.discardForReset(media.serverMediaId,state.revision||0)
        if (!isCurrent()) return null
        clearResetCache(media.serverMediaId,state.revision||0)
        if(pendingDeletion.value?.mediaId===mediaId)pendingDeletion.value=null
        if(pendingFeedback.value?.mediaId===mediaId)pendingFeedback.value=null
        localStorage.setItem(generationKey,state.generationId)
        showToast('视频已覆盖重新标注，已清除旧工作区缓存')
      }else{
        await workspaceWrites.replay(media.serverMediaId)
        if (!isCurrent()) return null
        state = await trackApi.getWorkspaceState(media.serverMediaId)
        if (!isCurrent()) return null
      }
      workspaceGenerations.set(mediaId,state.generationId)
      workspaceWrites.setRevision(media.serverMediaId, state.revision || 0)
      workspaceReadError.value = ''
      deletedObjectIds.value[mediaId] = state.deletedObjectIds ?? []
      deletedFrameObjects.value[mediaId] = state.deletedFrameObjects ?? []
      normalMotionSamples.value[mediaId] = state.normalMotionSamples ?? []
      trackingFeedbackEvents.value[mediaId] = state.trackingFeedbackEvents ?? []
      if (pendingDeletion.value?.mediaId === mediaId) {
        const intent = pendingDeletion.value
        const deleted = state.deletedObjectIds?.includes(intent.record.objectId)
        if ((intent.action === 'undo') === !deleted) videoDeletionHistory.value[mediaId] = { ...intent.record, undone: intent.action === 'undo', sequence: ++historySequence }
        pendingDeletion.value = null
      }
      if (pendingFeedback.value?.mediaId === mediaId) pendingFeedback.value = null
      rememberControls(mediaId)
      if (!state.exists || mediaId !== currentMediaId.value) return null
      workspaceRestoreInProgress = true
      const restoredManual = (state.manualAnnotations ?? [])
        .filter((value): value is AnnotationObject => !!value && typeof value === 'object')
        .map((value) => ({ ...value, source: 'manual' as const }))
      const retainedAi = (annotationsByMedia.value[mediaId] ?? []).filter((obj) => obj.source === 'ai')
      annotationsByMedia.value = {
        ...annotationsByMedia.value,
        [mediaId]: dedupeAnnotationObjects([...retainedAi, ...restoredManual]).filter(o => !isObjectDeleted(mediaId, o.objectId, o.frameIndex ?? 0)),
      }
      deletedTrackingIds.value[mediaId] = new Set((state.deletedTrackingIds ?? []).map(String))
      anomalyFrames.value = Array.isArray(state.anomalyFrames) ? state.anomalyFrames : []
      pausedAnomalies.value = Array.isArray(state.pausedAnomalies)
        ? (state.pausedAnomalies as typeof pausedAnomalies.value).map(normalizePauseNotice)
        : []
      lastPausedContext.value = state.lastPausedContext
        ? { mediaId, frameIndex: Number(state.lastPausedContext.frameIndex) }
        : null
      brightness.value = Number(state.display?.brightness) || 100
      contrast.value = Number(state.display?.contrast) || 100
      zoom.value = Number(state.display?.zoom) || 1
      if (state.editor?.activeTool && ['select', 'point', 'bbox'].includes(state.editor.activeTool)) {
        activeTool.value = state.editor.activeTool
      }
      if (typeof state.editor?.objectNameInput === 'string') objectNameInput.value = state.editor.objectNameInput
      selectedObjectId.value = typeof state.editor?.selectedObjectId === 'string'
        ? state.editor.selectedObjectId
        : null
      anomalyPanelVisible.value = pausedAnomalies.value.length > 0
      const frame = Math.max(0, Math.min(maxFrameIndex.value, Number(state.currentFrame) || 0))
      return frame
    } catch (error) {
      if (!isCurrent()) return null
      workspaceReadError.value = '工作区读取失败，请重新加载素材后再编辑，避免覆盖服务器状态'
      saveState.value = 'error'; saveError.value = workspaceReadError.value
      console.error('[annotation.workspace_load_failed]', { mediaId, error })
      return null
    } finally {
      if (isCurrent()) workspaceRestoreInProgress = false
    }
  }

  // 标注进度统计
  const annotatedFrameCount = computed(() => {
    const mediaId = currentMediaId.value
    return new Set((annotationsByMedia.value[mediaId] ?? []).map((o) => o.frameIndex ?? 0)).size
  })

  const clearSelection = () => {
    if (isAiBusy.value) return
    selectedObjectId.value = null
    statusMessage.value = '已取消选择'
  }

  const openFilePicker = (type: 'image' | 'video') => {
    if (type === 'image') fileInputRef.value?.click()
    else videoInputRef.value?.click()
  }

  const clearResetCache = (serverId:string, revision:number) => {
    const media=mediaAssets.value.find(m=>m.serverMediaId===serverId)
    if(!media)return
    const mid=media.id
    trackingJobs.delete(mid);rememberTracking(mid);delete trackingErrors.value[mid];isAiBusy.value=trackingJobs.size>0
    cancelScheduledSave(mid)
    annotationsByMedia.value={...annotationsByMedia.value,[mid]:[]};trackingFramesByMedia.value[mid]=[]
    deletedTrackingIds.value[mid]=new Set();deletedObjectIds.value[mid]=[];deletedFrameObjects.value[mid]=[]
    normalMotionSamples.value[mid]=[];trackingFeedbackEvents.value[mid]=[]
    savedResults.value=savedResults.value.filter(item=>item.mediaId!==mid&&item.mediaId!==serverId)
    loadedRemoteResultKeys.clear()
    delete videoDeletionHistory.value[mid]
    workspaceViews.delete(mid);frameCache.clear()
    for(const key of histories.keys())if(key.startsWith(`${mid}:`))histories.delete(key)
    workspaceWrites.setRevision(serverId,revision)
    try{sessionStorage.removeItem(controlsCacheKey(serverId))}catch{}
    selectedObjectId.value=null
  }
  const importJournalKey=()=>`annotation-reimport:${useAuth().user.value?.id}`
  const cancelDuplicateImport=()=>{
    if(duplicateImport.value?.pending)return
    duplicateImport.value=null;finishDuplicate?.(null);finishDuplicate=null
  }
  const reimportSaving=ref(false)
  const confirmDuplicateImport=async()=>{
    const intent=duplicateImport.value
    if(!intent||!intent.canOverwrite||reimportSaving.value)return
    reimportSaving.value=true
    intent.error='';intent.pending=true
    let dispatched=false
    try{
      sessionStorage.setItem(importJournalKey(),JSON.stringify(intent))
      dispatched=true
      const result=await reviewWorkflowApi.resetAnnotations(intent.mediaId,intent.workspaceRevision,intent.key)
      if(!result.ok||result.mediaId!==intent.mediaId)throw new Error('服务器未确认覆盖')
      clearResetCache(intent.mediaId,result.revision)
      sessionStorage.removeItem(importJournalKey())
      duplicateImport.value=null
      const resolve=finishDuplicate;finishDuplicate=null
      if(resolve)resolve(intent.media)
      else{await loadServerMedia();const media=mediaAssets.value.find(m=>m.serverMediaId===intent.mediaId);if(media){selectedMediaId.value=media.id;await nextTick();await resetAnnotationViewForMedia(media.id)}}
      showToast('旧工作区标注已清空，可以从第 1 帧重新标注')
    }catch(e:any){
      intent.error=e.message||'覆盖结果尚未确认，请重试原请求';console.error('[annotation.reimport_failed]',e)
      if(e.status&&e.status<500){intent.pending=false;intent.canOverwrite=false;sessionStorage.removeItem(importJournalKey())}
      if(!dispatched)intent.pending=false
    }finally{reimportSaving.value=false}
  }
  const restoreImportIntent=()=>{
    try{const intent=JSON.parse(sessionStorage.getItem(importJournalKey())||'null');if(intent?.pending&&intent.mediaId)duplicateImport.value={...intent,error:'上次覆盖结果尚未确认，请重试原请求。'}}catch(e){console.warn('[annotation.reimport_restore_failed]',e)}
  }
  const handleFiles = async (files: FileList | null, type: 'image' | 'video') => {
    if (!files?.length||mediaImportBusy.value||duplicateImport.value||editingBlocked.value) return
    mediaImportBusy.value=true;importError.value='';pausePlayback();cancelAnnotationGesture()
    let lastAddedId:string|null=null
    try{
      await persistWorkspaceState(currentMediaId.value,true)
      cancelScheduledSave(currentMediaId.value)
      for(const file of Array.from(files)){
        if(type==='image'&&!file.type.startsWith('image/'))continue
        if(type==='video'&&!isVideoFile(file))continue
        let uploaded:TrackUploadResponse|undefined
        if(type==='video'){
          try{uploaded=await trackApi.uploadVideo(file)}
          catch(e:any){
            if(e.code!=='DUPLICATE_VIDEO'||!e.duplicate)throw e
            duplicateImport.value={...e.duplicate,key:createRequestId(),pending:false,error:''}
            const existing=mediaAssets.value.find(m=>m.serverMediaId===e.duplicate.mediaId)
            if(existing){cancelScheduledSave(existing.id);await saveQueues.get(existing.id)?.catch(()=>{});const state=await trackApi.getWorkspaceState(e.duplicate.mediaId);duplicateImport.value!.workspaceRevision=state.revision||0}
            const replacement=await new Promise<TrackUploadResponse|null>(resolve=>{finishDuplicate=resolve})
            if(!replacement)continue
            uploaded=replacement
          }
        }
        let media=uploaded?mediaAssets.value.find(m=>m.serverMediaId===uploaded!.mediaId):undefined
        if(!media){
          media={id:`local-${createRequestId()}`,name:uploaded?.videoName||file.name,type,url:uploaded?.videoUrl||URL.createObjectURL(file),sizeBytes:file.size,
            ...(uploaded?{serverMediaId:uploaded.mediaId,serverVideoName:uploaded.videoName,width:uploaded.width,height:uploaded.height,fps:uploaded.fps,duration:uploaded.duration,frameCount:uploaded.frameCount}:{})}
          annotationsByMedia.value={...annotationsByMedia.value,[media.id]:[]};trackingFramesByMedia.value[media.id]=[];mediaAssets.value=[...mediaAssets.value,media]
        }
        lastAddedId=media.id
        if(type==='image')void api.uploadMedia({file})
      }
      if(lastAddedId){selectedMediaId.value=lastAddedId;await nextTick();await resetAnnotationViewForMedia(lastAddedId);statusMessage.value=`已导入：${selectedMedia.value?.name}`;showToast(statusMessage.value)}
    }catch(e:any){importError.value=e.message||'导入失败，请重试';statusMessage.value=importError.value;console.error('[annotation.import_failed]',e)}
    finally{mediaImportBusy.value=false;if(type==='image'&&fileInputRef.value)fileInputRef.value.value='';if(type==='video'&&videoInputRef.value)videoInputRef.value.value=''}
  }

  const onImageLoaded = () => {
    if (!imageRef.value) return
    if (selectedMedia.value) {
      selectedMedia.value.width = imageRef.value.naturalWidth
      selectedMedia.value.height = imageRef.value.naturalHeight
    }
  }

  const onVideoLoaded = async () => {
    const mediaId = currentMediaId.value
    const video = videoRef.value
    if (!video) return
    videoPlaybackFallback.value = false
    videoDuration.value = video.duration || videoDuration.value || 0
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media || mediaId !== currentMediaId.value || videoRef.value !== video) return
    media.duration = videoDuration.value
    // 以服务端/OpenCV 的源视频尺寸作为标注坐标系；精确逐帧模式直接显示服务端解码帧。
    if (!media.width) media.width = video.videoWidth
    if (!media.height) media.height = video.videoHeight
    videoFps.value = media.fps || 30
    media.frameCount = media.frameCount || (video.duration > 0 && videoFps.value > 0 ? Math.round(video.duration * videoFps.value) : undefined)
    await ensureVideoFirstFrame(mediaId)
    if (isPlaying.value) scheduleVideoFrameSync()
  }

  const stopFallbackPlayback = () => {
    fallbackPlaybackSerial += 1
    isPlaying.value = false
  }

  const onVideoError = async () => {
    const media = selectedMedia.value
    if (!media?.serverMediaId) return
    videoPlaybackFallback.value = true
    stopFallbackPlayback()
    if (media.frameCount && media.fps) {
      videoDuration.value = media.frameCount / media.fps
      videoFps.value = media.fps
    }
    // A native error can arrive while the saved frame is still decoding.
    // Do not replace that request with the temporary frame-zero view.
    if (!workspaceRestoring.value && !exactFrameLoading.value) await loadExactFrame(currentFrame.value, media.id)
    statusMessage.value = `当前浏览器不能直接播放 ${media.name}，已切换为原始视频逐帧预览（源文件未转换）`
  }

  const playFallbackFrames = async () => {
    const media = selectedMedia.value
    if (!media?.serverMediaId) return
    const serial = ++fallbackPlaybackSerial
    isPlaying.value = true
    const fps = Math.max(1, media.fps || videoFps.value || 30)
    const delay = Math.max(10, Math.round(1000 / (fps * playbackRate.value)))
    while (serial === fallbackPlaybackSerial && isPlaying.value && currentFrame.value < maxFrameIndex.value) {
      await new Promise((resolve) => setTimeout(resolve, delay))
      if (serial !== fallbackPlaybackSerial || !isPlaying.value) break
      const next = currentFrame.value + 1
      const ok = await loadExactFrame(next, media.id)
      if (!ok) break
    }
    if (serial === fallbackPlaybackSerial) isPlaying.value = false
  }

  const loadTrackingResult = async (mediaId: string, force = false, strict = false, required = false) => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media?.serverMediaId) return
    const serial = ++trackingLoadSerial
    const actor = useAuth().user.value?.id
    try {
      const result = await trackApi.getResult(media.serverMediaId, undefined, required)
      if (serial !== trackingLoadSerial || mediaId !== currentMediaId.value || actor !== useAuth().user.value?.id) return
      if ('state' in result && result.state === 'not_generated' && required) throw new Error('追踪任务应已生成结果，请管理员检查任务日志与存储')
      const frames = 'frames' in result ? result.frames : [result]
      if (media.frameCount && frames.some(f => f.frameIndex >= media.frameCount!)) throw new Error('追踪结果帧号超过原视频范围')
      // 过滤掉前端已删除的 AI tracking 对象
      const deleted = deletedTrackingIds.value[mediaId]
      const filtered = frames.map((f: any) => ({ ...f, annotations: f.annotations.filter((a: any) => !isObjectDeleted(mediaId, a.objectId, f.frameIndex) && !deleted?.has(a.id) && !deleted?.has(String(a.objectId))) }))
      trackingFramesByMedia.value[mediaId] = filtered

      // 将 AI tracking 结果合并到 annotationsByMedia（统一管理）
      // 策略：同帧同 objectId 的新结果覆盖旧结果，手动标注永不被覆盖
      const width = media.width || 1
      const height = media.height || 1
      const existing = (annotationsByMedia.value[mediaId] ?? []).filter(o => !isObjectDeleted(mediaId, o.objectId, o.frameIndex ?? 0))
      // 被手动修改过的 (frameIndex, objectId) 集合 — 精准保护, 只跳过用户改过的那一个
      const manualKeys = new Set(existing
        .filter((o) => o.source === 'manual' && o.objectId != null)
        .map((o) => `${o.frameIndex ?? 0}:${o.objectId}`))
      // 手动新建 (objectId=null) 也保留; 有 objectId 的才走 key 粒度保护
      // Full results are authoritative: absent AI rows belong to an obsolete branch.
      const existingByKey = new Map<string, AnnotationObject>()
      // 先把手动标注全量放进去，并清理旧版本重复保存的同帧同 ID 项。
      let merged: AnnotationObject[] = dedupeAnnotationObjects(
        existing.filter((o) => o.source === 'manual').map((o) => ({ ...o })),
      )

      for (const frame of filtered) {
        for (const ann of frame.annotations) {
          const key = `${frame.frameIndex}:${ann.objectId}`
          // 跳过被用户手动修改过的那一个 (其他同帧同 objectId 的正常更新)
          if (manualKeys.has(key)) continue
          const [x1, y1, x2, y2] = ann.bbox ?? [0, 0, 0, 0]
          const newObj: AnnotationObject = {
            id: `ai-${frame.frameIndex}-${ann.objectId}`,  // 强制唯一 id，不管后端返回什么
            objectId: ann.objectId,
            name: numberedObjectName(ann.name ?? 'rare sperm', Number(ann.objectId)),
            source: 'ai',
            confidence: ann.confidence,
            bbox: {
              x: (x1 / width) * 100,
              y: (y1 / height) * 100,
              width: ((x2 - x1) / width) * 100,
              height: ((y2 - y1) / height) * 100,
            },
            frameIndex: frame.frameIndex,
            timestampMs: frame.timestampMs,
            anomaly_level: ann.anomaly_level ?? undefined,
            anomaly_reasons: ann.anomaly_reasons ?? undefined,
            anomaly_details: ann.anomaly_details ?? undefined,
          }
          // 新结果覆盖旧结果（续接追踪时覆盖第一遍的结果）
          existingByKey.set(key, newObj)
        }
      }

      // ---------- 关键：用 AI tracking 的 objectId 补全手动标注 ----------
      // 策略：拿 AI 结果首帧的 bbox 和所有手动标注做 IoU 匹配
      // 必须在 finalAi push 之前做，否则同帧同 oid 的手动/AI 会共存
      const seedFrame = filtered.find((f: any) => f.annotations?.length)
      if (seedFrame) {
        const seedAiBoxes = seedFrame.annotations.map((a: any) => {
          const [x1, y1, x2, y2] = a.bbox ?? [0, 0, 0, 0]
          return {
            objectId: a.objectId,
            name: a.name,
            bbox: {
              x: (x1 / width) * 100,
              y: (y1 / height) * 100,
              width: ((x2 - x1) / width) * 100,
              height: ((y2 - y1) / height) * 100,
            },
          }
        })
        // IoU 计算
        const bboxIoU = (a: any, b: any) => {
          const ax2 = a.x + a.width, ay2 = a.y + a.height
          const bx2 = b.x + b.width, by2 = b.y + b.height
          const ix1 = Math.max(a.x, b.x), iy1 = Math.max(a.y, b.y)
          const ix2 = Math.min(ax2, bx2), iy2 = Math.min(ay2, by2)
          const iw = Math.max(0, ix2 - ix1), ih = Math.max(0, iy2 - iy1)
          const inter = iw * ih
          const areaA = a.width * a.height, areaB = b.width * b.height
          return areaA + areaB - inter > 0 ? inter / (areaA + areaB - inter) : 0
        }
        // 只处理没有 objectId 的手动标注
        merged = merged.map((obj) => {
          if (obj.source !== 'manual' || obj.objectId != null || !obj.bbox) return obj
          let bestIoU = 0, bestMatch: any = null
          for (const box of seedAiBoxes) {
            const iou = bboxIoU(obj.bbox, box.bbox)
            if (iou > bestIoU) { bestIoU = iou; bestMatch = box }
          }
          // IoU > 0.3 才认为是同一个物体
          if (bestMatch && bestIoU > 0.3) {
            return { ...obj, objectId: bestMatch.objectId }
          }
          return obj
        })
      }

      // 合并：手动标注 + 最终的 AI 标注
      // 手动已有的 (frame, objectId) 不再加 AI 的 — 避免同帧同 oid 两条共存
      const manualKeysAfterMatch = new Set(
        merged.filter((o) => o.objectId != null).map((o) => `${o.frameIndex ?? 0}:${o.objectId}`)
      )
      const finalAi = [...existingByKey.values()]
        .filter((o) => o.source === 'ai')
        .filter((o) => !manualKeysAfterMatch.has(`${o.frameIndex ?? 0}:${o.objectId}`))
      merged = dedupeAnnotationObjects([...merged, ...finalAi])

      annotationsByMedia.value = { ...annotationsByMedia.value, [mediaId]: merged }
    } catch (error) {
      if (serial !== trackingLoadSerial || mediaId !== currentMediaId.value || actor !== useAuth().user.value?.id) return
      console.warn('[annotation.tracking_load_failed]', { mediaId, error })
      if(strict)throw error
    }
  }

  const syncVideoFrameState = () => {
    const video = videoRef.value
    if (!video) return
    // currentTime 仅用于时间显示；currentFrame 只由实际呈现帧 metadata.mediaTime 更新。
    currentTime.value = Number.isFinite(video.currentTime) ? video.currentTime : 0
  }

  const scheduleVideoFrameSync = () => {
    const video = videoRef.value
    if (!video || typeof video.requestVideoFrameCallback !== 'function' || isSeekingVideo) return

    if (videoFrameCallbackId !== null) {
      try { video.cancelVideoFrameCallback(videoFrameCallbackId) } catch {}
      videoFrameCallbackId = null
    }

    const loop = (_now: number, metadata: VideoFrameCallbackMetadata) => {
      if (videoRef.value !== video || isSeekingVideo) return

      const mediaTime = Number.isFinite(metadata.mediaTime)
        ? metadata.mediaTime
        : video.currentTime

      // 唯一的真实当前帧来源：浏览器实际呈现帧的 mediaTime。
      commitPresentedFrame(mediaTime)

      if (!video.paused && !video.ended && !isSeekingVideo) {
        videoFrameCallbackId = video.requestVideoFrameCallback(loop)
      } else {
        videoFrameCallbackId = null
      }
    }

    videoFrameCallbackId = video.requestVideoFrameCallback(loop)
  }

  const onVideoTimeUpdate = () => {
    if (isSeekingVideo || !isPlaying.value) return
    syncVideoFrameState(); scheduleVideoFrameSync()
  }
  const requestedFrame = ref<number | null>(null)
  const seekVideo = async (time: number) => {
    if (!isVideo.value || isInteracting()) return
    videoRef.value?.pause()
    stopFallbackPlayback()
    const media = selectedMedia.value
    if (!media) return

    const targetFrame = Math.max(
      0,
      Math.min(maxFrameIndex.value, Math.round(time * Math.max(videoFps.value, 1)))
    )

    // 精确逐帧模式：暂停后不再让浏览器通过 currentTime 猜测目标帧，
    // 直接向后端请求 source video 的指定 frame_index。
    isSeekingVideo = true
    const serial = ++seekSerial
    requestedFrame.value = targetFrame
    try {
      if (media.serverMediaId) {
        const ok = await loadExactFrame(targetFrame, media.id)
        if (serial !== seekSerial) return
        if (!ok) return
        // 后端精确帧图像已经与 JSON 使用同一个 frame_index；同步视频元素位置仅供恢复播放使用。
        if (videoRef.value) {
          try { videoRef.value.currentTime = frameToTime(targetFrame) } catch {}
        }
        statusMessage.value = `已定位到第 ${targetFrame + 1} 帧`
      } else if (videoRef.value) {
        // 没有 serverMediaId 的旧本地视频只能回退到浏览器 video。
        videoRef.value.currentTime = frameToTime(targetFrame)
        currentFrame.value = targetFrame
        currentTime.value = frameToTime(targetFrame)
        frameInput.value = targetFrame
      }
    } finally {
      if (serial === seekSerial) { isSeekingVideo = false; requestedFrame.value = null }
    }
  }

  const seekToInputFrame = async () => {
    if (!isVideo.value || isAiBusy.value) return
    const frame = Math.max(0, Math.min(maxFrameIndex.value, Math.floor(Number(frameInput.value) || 0)))
    frameInput.value = frame
    await seekVideo(frameToTime(frame))

  }

  const seekByFrame = async (delta: number) => {
    if (!isVideo.value || isAiBusy.value) return
    // 用 currentFrame (整数帧号) 做基准, 不用 currentTime (可能有小数误差)
    const target = Math.max(0, Math.min(maxFrameIndex.value, (requestedFrame.value ?? currentFrame.value) + delta))
    if (target === currentFrame.value && requestedFrame.value === null) return
    await seekVideo(frameToTime(target))
  }

  const togglePlayback = async () => {
    if (isSeekingVideo || exactFrameLoading.value || isAiBusy.value || isInteracting()) return
    if (videoPlaybackFallback.value) {
      if (isPlaying.value) {
        stopFallbackPlayback()
        await loadExactFrame(currentFrame.value)
      } else {
        void playFallbackFrames()
      }
      return
    }
    if (!videoRef.value) return
    if (videoRef.value.paused) {
      try {
        videoRef.value.currentTime = frameToTime(currentFrame.value)
        revokeExactFrameUrl()
        videoRef.value.playbackRate = playbackRate.value
        await videoRef.value.play()
        isPlaying.value = true
        scheduleVideoFrameSync()
      } catch {
        await onVideoError()
        void playFallbackFrames()
      }
    } else {
      videoRef.value.pause()
      isPlaying.value = false
      const target = currentFrame.value
      syncVideoFrameState()
      if (selectedMedia.value?.serverMediaId) await loadExactFrame(target)
    }
  }


  const pausePlayback = () => {
    videoRef.value?.pause(); stopFallbackPlayback()
    if (videoFrameCallbackId !== null && videoRef.value) videoRef.value.cancelVideoFrameCallback?.(videoFrameCallbackId)
    videoFrameCallbackId = null
  }
  watch(playbackRate, value => { if (videoRef.value) videoRef.value.playbackRate = value })
  const onVideoEnded = async () => {
    stopFallbackPlayback()
    if (videoFrameCallbackId !== null && videoRef.value && typeof videoRef.value.cancelVideoFrameCallback === 'function') {
      try { videoRef.value.cancelVideoFrameCallback(videoFrameCallbackId) } catch {}
      videoFrameCallbackId = null
    }
    syncVideoFrameState()
    await loadExactFrame(currentFrame.value)
  }

  const onTimelineClick = async (event: MouseEvent) => {
    if (!isVideo.value || !videoDuration.value || isAiBusy.value) return
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))
    await seekVideo(ratio * videoDuration.value)
  }

  const runAiSegment = async () => {
    const mediaId = currentMediaId.value
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media) return
    const mediaType = media.type
    isAiBusy.value = true
    statusMessage.value = mediaType === 'video' ? `AI 正在处理视频第 ${currentFrame.value + 1} 帧……` : 'AI 正在处理图片……'
    try {
      const result = await api.segment({
        mediaId,
        mediaType,
        prompt: objectNameInput.value.trim() || 'rare sperm',
        frameIndex: mediaType === 'video' ? currentFrame.value : undefined,
        timestampMs: mediaType === 'video' ? Math.round(currentTime.value * 1000) : undefined,
      })
      // 关键隔离：AI 返回期间如果用户切换了素材，绝不能把旧素材的结果写到新素材。
      if (mediaId !== currentMediaId.value) return
      const existing = annotationsByMedia.value[mediaId] ?? []
      let nextId = getNextObjectId(mediaId)
      const normalizedObjects = result.objects.map((obj: AnnotationObject) => {
        const id = typeof obj.objectId === 'number' ? obj.objectId : nextId++
        return normalizeAnnotationObject({ ...obj, objectId: id }, id)
      })
      annotationsByMedia.value = {...annotationsByMedia.value,[mediaId]:[...existing, ...normalizedObjects]}
      selectedObjectId.value = normalizedObjects[0]?.id ?? null
      statusMessage.value = `AI 完成：${normalizedObjects.length} 个目标${mediaType === 'video' ? '（当前帧）' : ''}`
    } catch (error) {
      if (mediaId === currentMediaId.value) statusMessage.value = error instanceof Error ? error.message : 'AI 处理失败'
    } finally {
      isAiBusy.value = false
    }
  }

  type TrackingSeedSnapshot = {frameIndex:number;timestampMs:number;objects:AnnotationObject[]}
  const buildCurrentFrameSam3Objects = async (mediaId: string, snapshot?: TrackingSeedSnapshot) => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media) return []
    const frameIndex = snapshot?.frameIndex ?? currentFrame.value, timestampMs = snapshot?.timestampMs ?? Math.round(currentTime.value * 1000)
    const objects = snapshot?.objects ?? (annotationsByMedia.value[mediaId] ?? []).filter((obj) => (obj.frameIndex ?? 0) === currentFrame.value)
    if (!objects.length) throw new Error(`第 ${currentFrame.value + 1} 帧没有人工标注`)

    // ID 完整性是断点续追的硬约束：不能在生成 seed JSON 时丢失 objectId，
    // 更不能让后端按 annotations 数组顺序重新编号。否则下一轮会出现 15→8 之类的 identity switch。
    const seenIds = new Set<number>()
    for (const obj of objects) {
      if (!Number.isInteger(obj.objectId) || (obj.objectId as number) <= 0) {
        throw new Error(`第 ${currentFrame.value + 1} 帧存在缺少有效 objectId 的标注（${obj.name || obj.id}），请删除后重新框选`)
      }
      if (seenIds.has(obj.objectId as number)) {
        throw new Error(`第 ${currentFrame.value + 1} 帧存在重复 objectId=${obj.objectId}，请修正后再进行 AI Tracking`)
      }
      seenIds.add(obj.objectId as number)
    }

    const { width, height } = await getMediaPixelSize(media)
    const clampX = (v: number) => Math.min(width, Math.max(0, v))
    const clampY = (v: number) => Math.min(height, Math.max(0, v))
    return objects.map((obj) => {
      const objectId = obj.objectId as number
      const item: Record<string, unknown> = {
        id: obj.id,
        object_id: objectId,
        objectId,
        name: obj.name,
        source: obj.source,
        frameIndex,
        timestampMs,
      }
      if (obj.bbox) {
        item.bbox = [
          Math.round(clampX(obj.bbox.x / 100 * width)),
          Math.round(clampY(obj.bbox.y / 100 * height)),
          Math.round(clampX((obj.bbox.x + obj.bbox.width) / 100 * width)),
          Math.round(clampY((obj.bbox.y + obj.bbox.height) / 100 * height)),
        ]
      }
      if (obj.point) item.point = [
        Math.round(clampX(obj.point.x / 100 * width)),
        Math.round(clampY(obj.point.y / 100 * height)),
      ]
      return item
    }).filter((item: any) => Array.isArray(item.bbox) && item.bbox.length === 4)
  }

  const findNextUnannotatedFrame = (fromFrame = currentFrame.value) => {
    const all = annotationsByMedia.value[currentMediaId.value] ?? []
    const annotatedFrames = new Set(all.filter((o) => o.bbox).map((o) => o.frameIndex ?? 0))
    for (let frame = Math.max(0, fromFrame); frame <= maxFrameIndex.value; frame += 1) {
      if (!annotatedFrames.has(frame)) return frame
    }
    return Math.min(Math.max(0, fromFrame), maxFrameIndex.value)
  }

  const goToNextUnannotatedFrame = async () => {
    if (!isVideo.value) return
    const next = findNextUnannotatedFrame(currentFrame.value + 1)
    await seekVideo(frameToTime(next))
    statusMessage.value = next === currentFrame.value ? `当前已是最后可标注帧：${next + 1}` : `下一个未标注帧：${next + 1}`
  }

  /**
   * 获取当前素材的实际像素尺寸。
   * 视频优先读取 HTMLVideoElement.videoWidth/videoHeight；图片读取 naturalWidth/naturalHeight；
   * 最后才回退到 MediaAsset 中已保存的 width/height。
   */
  const getMediaPixelSize = async (media: MediaAsset) => {
    if (media.type === 'video') {
      const video = videoRef.value
      if (video && video.videoWidth > 0 && video.videoHeight > 0) {
        return { width: video.videoWidth, height: video.videoHeight }
      }
      // AVI/MKV 等容器可能被后端 OpenCV 正常解码，却不被浏览器的
      // <video> 原生支持。此时界面会切换到 /api/track/frame 的逐帧预览，
      // videoWidth 会一直是 0；上传接口已保存的真实尺寸才是可信来源。
      if ((media.width || 0) > 0 && (media.height || 0) > 0) {
        return { width: media.width as number, height: media.height as number }
      }
      throw new Error('视频宽高不可用：请确认视频已成功上传且后端可以读取其帧数据')
    }

    const image = imageRef.value
    if (image && image.naturalWidth > 0 && image.naturalHeight > 0) {
      return { width: image.naturalWidth, height: image.naturalHeight }
    }
    if ((media.width || 0) > 0 && (media.height || 0) > 0) {
      return { width: media.width as number, height: media.height as number }
    }
    throw new Error('图片实际宽高尚未获取，请等待图片加载完成后再生成 JSON')
  }

  const formatMetric = (label: string, value: unknown) => {
    const number = Number(value)
    return Number.isFinite(number) ? `${label} ${number.toFixed(2)}` : ''
  }

  const objectDisplayName = (objectId: number, preferred?: string | null) => {
    if (preferred?.trim()) return preferred.trim()
    const local = (annotationsByMedia.value[currentMediaId.value] ?? [])
      .find((obj) => obj.objectId === objectId && obj.name?.trim())
    return local?.name?.trim() || `object-${objectId}`
  }

  const isGeometryReason = (reason: string) => /^(?:manual|geometry)_(?:area_ratio|width_ratio|height_ratio|aspect_change)=/.test(reason)
  const canLearnNormalMotion = (reasons: string[]) => reasons.some(reason => reason.startsWith('adjacent_center_shift='))
    && reasons.every(reason => reason.startsWith('adjacent_center_shift=') || reason.startsWith('motion_') || isGeometryReason(reason))

  const normalizePauseNotice = (item: typeof pausedAnomalies.value[number]) => {
    // Older saved pauses have only the displayed title and canLearn flag.
    // This restores their explanation; the server validates the real evidence.
    const acceptsGeometry = !!item.acceptsGeometry || (item.rawReasons ?? []).some(isGeometryReason)
      || ['框相对最近人工标注明显缩小', '框相对最近人工标注明显扩大或变形'].includes(item.title)
    return {
      ...item,
      canLearn: item.rawReasons?.length ? canLearnNormalMotion(item.rawReasons) : !!item.canLearn,
      acceptsGeometry,
      geometryReferenceFrame: item.geometryReferenceFrame ?? (acceptsGeometry ? item.baselineFrame : undefined),
      geometryReferenceSource: item.geometryReferenceSource ?? (acceptsGeometry ? 'manual' as const : undefined),
      rawReasons: item.rawReasons ?? [],
    }
  }

  const explainPausedObject = (item: NonNullable<Awaited<ReturnType<typeof trackApi.getStatus>>['pausedObjects']>[number]) => {
    const details = item.details ?? {}
    const reasons = item.reasons ?? []
    const type = item.type
    const displayName = objectDisplayName(item.object_id, item.display_name || item.name)
    const baselineFrame = Number(item.manualBaselineFrame ?? details.manual_baseline_frame ?? NaN)
    const geometryReferenceFrame = Number(details.geometry_reference_frame ?? item.manualBaselineFrame ?? details.manual_baseline_frame ?? NaN)
    const geometryReferenceSource = details.geometry_reference_source === 'confirmed-normal' ? 'confirmed-normal' as const : 'manual' as const
    const referenceName = geometryReferenceSource === 'confirmed-normal' ? '已确认正常的框' : '最近人工标注'
    const geometryReference = {
      geometryReferenceFrame: Number.isFinite(geometryReferenceFrame) ? geometryReferenceFrame : undefined,
      geometryReferenceSource,
    }
    const reviewStart = Number(item.reviewStartFrame ?? details.review_start_frame)
    const reviewEnd = Number(item.reviewEndFrame ?? details.review_end_frame)
    const lookback = Number(item.reviewLookbackFrames ?? details.review_lookback_frames ?? 5)
    const reviewRange = Number.isFinite(reviewStart) && Number.isFinite(reviewEnd)
      ? `第 ${reviewStart + 1}～${reviewEnd + 1} 帧`
      : undefined
    const reviewNotice = `该错误可能在触发暂停前已经逐渐出现，请同时检查前 ${Number.isFinite(lookback) ? lookback : 5} 帧的框是否已经开始变形、缩小或扩大。`
    const metrics = [
      formatMetric('面积倍率', item.metrics?.areaRatio ?? details.area_ratio ?? item.ratio),
      formatMetric('宽度倍率', item.metrics?.widthRatio ?? details.width_ratio),
      formatMetric('高度倍率', item.metrics?.heightRatio ?? details.height_ratio),
      formatMetric('长宽比倍率', item.metrics?.aspectRatio ?? details.aspect_ratio_ratio),
      formatMetric('中心偏移(px)', details.center_shift),
      formatMetric('重叠覆盖率', item.metrics?.overlapCoverage ?? details.overlap_coverage),
    ].filter(Boolean)

    if (type === 'overlap' || reasons.some((reason) => reason.includes('bbox_overlap_with='))) {
      const otherId = Number(item.other_object_id ?? details.other_object_id)
      const otherName = Number.isFinite(otherId) ? objectDisplayName(otherId, item.other_display_name) : '另一个对象'
      return {
        objectId: item.object_id,
        displayName,
        title: '对象重叠异常',
        summary: `${displayName} 与 ${otherName} 的框高度重叠，可能发生 ID 合并。`,
        metrics,
        baselineFrame: Number.isFinite(baselineFrame) ? baselineFrame : undefined,
        reviewRange,
        suggestion: '检查两个对象的边界和 object ID；分别修正框后，从当前帧重新执行 AI Tracking。',
      }
    }
    if (type === 'size_shrink') {
      return {
        objectId: item.object_id,
        displayName,
        title: `框相对${referenceName}明显缩小`,
        ...geometryReference,
        summary: `${displayName} 的框相对${referenceName}明显缩小，可能已经丢失精子尾部。`,
        metrics,
        baselineFrame: Number.isFinite(baselineFrame) ? baselineFrame : undefined,
        reviewRange,
        reviewNotice,
        suggestion: '检查当前帧与前几帧；框准确时可直接确认无异常，有误时先重新框住完整目标再确认。',
      }
    }
    if (type === 'size_growth' || type === 'shape_change') {
      return {
        objectId: item.object_id,
        displayName,
        title: `框相对${referenceName}明显扩大或变形`,
        ...geometryReference,
        summary: `${displayName} 的框相对${referenceName}明显扩大或变形，可能框入了杂质或其他精子。`,
        metrics,
        baselineFrame: Number.isFinite(baselineFrame) ? baselineFrame : undefined,
        reviewRange,
        reviewNotice,
        suggestion: '框准确时可直接确认无异常；若框入其他对象，先修正为只覆盖当前目标，再确认。',
      }
    }
    if (type === 'disappearance' || reasons.some((reason) => reason.includes('disappearance'))) {
      return {
        objectId: item.object_id,
        displayName,
        title: '对象在画面内消失',
        summary: `${displayName} 未在边界附近被检测到，无法确认其是否正常离场。`,
        metrics,
        baselineFrame: Number.isFinite(baselineFrame) ? baselineFrame : undefined,
        reviewRange,
        suggestion: '确认对象是否被遮挡、合并到其他对象或确实离开画面；必要时补框或删除该对象。',
      }
    }
    return {
      objectId: item.object_id,
      displayName,
      title: '追踪运动异常',
      summary: `${displayName} 相对上一帧发生异常位置跳变。`,
      metrics,
      baselineFrame: Number.isFinite(baselineFrame) ? baselineFrame : undefined,
      reviewRange,
      suggestion: '检查当前帧对象身份和位置，修正框后从当前帧继续追踪。',
    }
  }

  /**
   * AI Tracking 开始前自动把当前帧的人工标注写入数据库。
   * 每次 AI Tracking 点击时自动提交当前帧人工标注。
   */
  const persistCurrentFrameManualAnnotations = async (media: MediaAsset, mediaId: string, snapshot?: TrackingSeedSnapshot) => {
    const frameIndex = snapshot?.frameIndex ?? currentFrame.value, timestampMs = snapshot?.timestampMs ?? Math.round(currentTime.value * 1000)
    const current = (snapshot?.objects ?? annotationsByMedia.value[mediaId] ?? []).filter(
      (obj) => (obj.frameIndex ?? 0) === frameIndex && obj.source === 'manual'
    )
    if (!current.length) return { count: 0, batchId: undefined as string | undefined }

    const pixel = await getMediaPixelSize(media).catch(() => null)
    const canonicalMediaId = media.serverMediaId || mediaId
    const result = await api.saveManualAnnotation({
      mediaId: canonicalMediaId,
      generationId: workspaceGenerations.get(mediaId),
      mediaType: media.type,
      mediaName: media.name,
      mediaWidth: pixel?.width ?? media.width,
      mediaHeight: pixel?.height ?? media.height,
      frameIndex: media.type === 'video' ? frameIndex : undefined,
      timestampMs: media.type === 'video' ? timestampMs : undefined,
      objects: JSON.parse(JSON.stringify(current)),
      annotationVersion: 'annotation-v8-ai-tracking-autosave',
    })

    // 让标注结果页/工作区本地结果同步显示本次自动入库批次。
    const savedResult = {
      mediaId: canonicalMediaId,
      mediaName: media.name,
      mediaType: media.type,
      frameIndex: media.type === 'video' ? frameIndex : undefined,
      timestampMs: media.type === 'video' ? timestampMs : undefined,
      savedAt: new Date().toISOString(),
      filename: '',
      batchId: result.batchId,
      objects: JSON.parse(JSON.stringify(current)),
    } as SavedAnnotationFile
    savedResults.value = [
      savedResult,
      ...savedResults.value.filter((item) => !(
        item.mediaId === canonicalMediaId
        && (item.frameIndex ?? 0) === frameIndex
        && item.username === savedResult.username
      )),
    ]

    return { count: current.length, batchId: result.batchId }
  }

  const trackingJournalKey = (mediaId:string)=>`annotation-tracking:${useAuth().user.value?.id}:${mediaId}`
  const rememberTracking = (mediaId:string) => {
    try{const job=trackingJobs.get(mediaId);if(job)sessionStorage.setItem(trackingJournalKey(mediaId),JSON.stringify(job));else sessionStorage.removeItem(trackingJournalKey(mediaId))}
    catch(error){console.warn('[annotation.tracking_journal_failed]',{mediaId,error})}
  }
  const performTracking = async (resume=false, restartIntent?: TrackingRestartIntent) => {
    if(trackingRetryBusy.value)return
    const mediaId = currentMediaId.value
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media || media.type !== 'video' || !media.serverMediaId) {
      statusMessage.value = '请先上传视频'
      return
    }

    let job=resume?trackingJobs.get(mediaId):undefined
    const startFrame = job?.startFrame ?? restartIntent?.body.startFrame ?? currentFrame.value
    const restartSerial = restartEpoch
    let preparingRestartSeed = !!restartIntent
    const restartSeedSnapshot: TrackingSeedSnapshot | undefined = restartIntent ? {frameIndex:startFrame,timestampMs:Math.round(frameToTime(startFrame)*1000),objects:clone((annotationsByMedia.value[mediaId]??[]).filter(object=>(object.frameIndex??0)===startFrame))} : undefined
    const ensureRestartCurrent = () => {
      if (restartIntent && (restartSerial !== restartEpoch || restartIntent.actorId !== useAuth().user.value?.id || mediaId !== currentMediaId.value)) throw new DOMException('账号或素材已切换，已停止本页续追', 'AbortError')
      if (preparingRestartSeed && (currentFrame.value !== startFrame || exactFrameLoading.value || !!frameError.value)) throw new Error('重追起始帧已变化或尚未读取完成，未继续写入起始框；请重试从原起点追踪')
      if (preparingRestartSeed && submissionBusy.value) throw new Error('当前有送审操作尚未结束，已停止准备起始框；请完成送审操作后再继续原起点追踪')
    }
    let phase='保存工作区'
    let seed: Record<string, unknown>[]
    if (pendingDeletion.value || pendingFeedback.value || (pendingRestart.value && pendingRestart.value !== restartIntent) || workspaceReadError.value || (isAiBusy.value&&!resume)) return
    if (!job&&lastPausedContext.value?.mediaId === mediaId && pausedAnomalies.value.some(item => !isObjectDeleted(mediaId, item.objectId, lastPausedContext.value!.frameIndex))) {
      anomalyPanelVisible.value = true
      showToast('请先逐项确认暂停对象，或修正框后确认，再继续追踪')
      return
    }
    if (!job&&!currentObjects.value.some(object => object.bbox)) { showToast('当前帧没有可追踪的框，请先绘制或选择有框的帧'); return }
    anomalyObjectIds.value = []
    pausedAnomalies.value = []
    anomalyPanelVisible.value = false
    isAiBusy.value = true
    trackingRetryBusy.value=true

    try {
      let task: {taskId:string;maxFrames?:number;trackFrames?:number}
      if(job){
        phase=job.taskId?'查询追踪任务':'确认启动结果'
        task=job.taskId?{taskId:job.taskId,maxFrames:job.maxFrames}:await trackApi.run(job.input,job.key)
        job.taskId=task.taskId;job.maxFrames=task.maxFrames;rememberTracking(mediaId)
      }else{
      // 固化完整工作区（人工框、稳定 ID/名称、人工基准和异常状态），
      // 确保后端本轮 Tracking 与以后重新加载使用同一份人工基准。
      if (!restartIntent) await persistWorkspaceState(mediaId)
      ensureRestartCurrent()
      // ① AI Tracking 点击即自动落库：当前帧人工标注先进入数据库，
      // 然后才写 seed JSON / 启动 SAM3。任何一步失败都不会“假保存”。
      statusMessage.value = `① 正在将第 ${startFrame + 1} 帧人工标注写入数据库……`
      phase='保存人工标注记录'
      const persisted = await persistCurrentFrameManualAnnotations(media, mediaId, restartSeedSnapshot)
      ensureRestartCurrent()
      if (persisted.count) {
        showToast(`第 ${startFrame + 1} 帧 ${persisted.count} 个人工标注已写入数据库`)
      }

      // 关键：如果这是对旧 Tracking 结果的人工回退/修正，从当前帧重新开分支。
      // 服务端只删除 tracker_results.json 中 frame > startFrame 的旧结果。
      // 当前帧保留，作为新的分支锚点；其上的 AI 框和人工修改/新增框会一起成为新 seed。
      statusMessage.value = `①b 正在从第 ${startFrame + 1} 帧切断旧 Tracking 未来分支……`
      phase='整理旧追踪分支'
      const rewind = restartIntent ? null : await trackApi.rewind({
        mediaId: media.serverMediaId,
        generationId: workspaceGenerations.get(mediaId),
        startFrame,
      })
      ensureRestartCurrent()
      // The future branch is now replaced. Old AI snapshots can no longer be
      // durably restored by an editor-only undo, so invalidate that history.
      delete videoDeletionHistory.value[mediaId]
      for (const key of histories.keys()) if (key.startsWith(`${mediaId}:`)) histories.delete(key)
      historyRevision.value++; rememberControls(mediaId)
      const existingAfterRewind = annotationsByMedia.value[mediaId] ?? []
      // 当前帧 N 是新的分支锚点：保留此前所有结果以及 N 帧现有 AI/人工框。
      // 只有 N 之后的旧未来轨迹被清掉；随后 buildCurrentFrameSam3Objects() 会
      // 把 N 帧当前页面上最终存在的全部 bbox（旧 AI + 人工新增/修改）作为新 seed。
      annotationsByMedia.value = {
        ...annotationsByMedia.value,
        [mediaId]: existingAfterRewind.filter((obj) => obj.source === 'manual' || (obj.frameIndex ?? 0) <= startFrame),
      }
      trackingFramesByMedia.value[mediaId] = (trackingFramesByMedia.value[mediaId] ?? [])
        .filter((frame) => frame.frameIndex <= startFrame)
      console.info(
        `[ai-track] ${restartIntent ? 'confirmed restart' : 'rewind'} branch at frame=${startFrame}: removedRows=${rewind?.removedRows ?? 0}, ` +
        `deletedFutureSeedFiles=${rewind?.deletedFutureSeedFiles ?? 0}`
      )

      seed = await buildCurrentFrameSam3Objects(mediaId, restartSeedSnapshot)
      ensureRestartCurrent()
      if (!seed.length) {
        throw new Error(`第 ${startFrame + 1} 帧没有人工 bbox 标注`)
      }
      // 不能直接使用 media.width || 0：在 AVI 浏览器播放降级为逐帧预览时，
      // 尺寸来自后端上传元数据。统一从该函数获取，保证 seed 与 Tracking
      // 请求使用同一套像素坐标系。
      const pixelSize = await getMediaPixelSize(media)
      ensureRestartCurrent()

      // Phase 2: 把“当前页面正在看的这一帧”固化成后端 JSON seed。
      statusMessage.value = `② 正在生成第 ${startFrame + 1} 帧标注 JSON……`
      phase='保存追踪起始框'
      await trackApi.saveFrameAnnotations({
        mediaId: media.serverMediaId,
        generationId: workspaceGenerations.get(mediaId),
        mediaName: media.name,
        mediaWidth: pixelSize.width,
        mediaHeight: pixelSize.height,
        frameIndex: startFrame,
        timestampMs: restartSeedSnapshot?.timestampMs ?? Math.round(currentTime.value * 1000),
        annotations: seed,
      })
      ensureRestartCurrent()

      // 后端以 SAM3_TRACK_FRAMES（包含 seed 帧）和剩余视频帧数为上限。
      statusMessage.value = `③ SAM3 将从第 ${startFrame + 1} 帧持续向后追踪，直到异常、单轮上限或视频末尾……`
      const input = {
        mediaId: media.serverMediaId,
        generationId: workspaceGenerations.get(mediaId),
        mediaName: media.name,
        mediaWidth: pixelSize.width,
        mediaHeight: pixelSize.height,
        startFrame,
        annotations: seed,
      }
      phase='启动追踪任务'
      preparingRestartSeed = false
      trackingJobs.set(mediaId,{mediaId,startFrame,input,key:createRequestId()})
      job=trackingJobs.get(mediaId)!;rememberTracking(mediaId)
      if (restartIntent && pendingRestart.value === restartIntent) {
        // The durable tracking journal now owns recovery. Replaying a restart
        // after this point must never truncate newly generated model results.
        rememberRestart(restartIntent, true); pendingRestart.value = null; trackingRestartError.value = ''
      }
      task=await trackApi.run(input,job.key)
      ensureRestartCurrent()
      job.taskId=task.taskId;job.maxFrames=task.maxFrames;rememberTracking(mediaId)
      }

      trackingFrameCount.value = task.maxFrames || task.trackFrames || 1
      statusMessage.value =
        `SAM3 正在运行：处理第 ${startFrame + 1}～${Math.min(maxFrameIndex.value, startFrame + trackingFrameCount.value - 1) + 1} 帧`

      let completedStatus: TrackStatusResponse|undefined
      for (;;) {
        phase='查询追踪任务'
        const status = await trackApi.getStatus(task.taskId)
        ensureRestartCurrent()
        trackingProgress.value=status.stage==='decoding'?'正在读取本轮原始帧':status.stage==='preparing_model'?'正在准备模型和输入':status.stage==='saving_results'?'正在保存追踪结果':status.lastProcessedFrame!=null?`正在追踪第 ${status.lastProcessedFrame+1} 帧`:status.status==='queued'?'等待模型任务启动':'正在追踪'

        trackingWarningSummary.value = status.warningSummary ?? []
        if (status.status === 'success') {completedStatus=status;break}
        if (status.status === 'failed') {
          trackingJobs.delete(mediaId);rememberTracking(mediaId)
          phase='模型追踪'
          throw new Error(status.message || 'SAM3 Tracking 失败')
        }
        if (status.status === 'paused' && status.paused) {
          const pauseFrame = status.pausedFrame ?? startFrame
          phase='读取追踪结果'
          await loadTrackingResult(mediaId, true, true, true)
          ensureRestartCurrent()

          if (mediaId === currentMediaId.value) {
            await seekVideo(frameToTime(pauseFrame))
            const levels = status.anomalyLevels || {}
            anomalyObjectIds.value = Object.entries(levels)
              .filter(([, v]) => v !== 'normal')
              .map(([k]) => Number(k))
            const pausedObjs = status.pausedObjects || []
            pausedAnomalies.value = pausedObjs.map(item => ({
              ...explainPausedObject(item),
              rawReasons: item.reasons ?? [],
              canLearn: canLearnNormalMotion(item.reasons ?? []),
              acceptsGeometry: ['size_shrink', 'size_growth', 'shape_change'].includes(item.type ?? '') || (item.reasons ?? []).some(isGeometryReason),
            }))
            lastPausedContext.value = { mediaId, frameIndex: pauseFrame }
            anomalyPanelVisible.value = true
            const reasons = pausedAnomalies.value.map((item) => `${item.displayName}：${item.title}`)
            const level = Object.values(levels).find((v) => v && v !== 'normal') || 'anomaly'
            anomalyFrames.value = [
              ...anomalyFrames.value,
              { frame_index: pauseFrame, level, reasons },
            ]
            statusMessage.value = `⚠️ Tracking 暂停在第 ${pauseFrame + 1} 帧: ${reasons.join('; ')}`
            showToast(`检测到异常，已暂停在第 ${pauseFrame + 1} 帧`)
          }
          trackingJobs.delete(mediaId);rememberTracking(mediaId);delete trackingErrors.value[mediaId]
          return
        }
        await new Promise((resolve) => setTimeout(resolve, 700))
      }

      phase='读取追踪结果'
      await loadTrackingResult(mediaId, true, true, true)
      ensureRestartCurrent()

      if (mediaId === currentMediaId.value) {
        const finalStatus = completedStatus!
        trackingWarningSummary.value = finalStatus.warningSummary ?? []
        const lastProcessedFrame = finalStatus.lastProcessedFrame ?? startFrame
        await seekVideo(frameToTime(lastProcessedFrame))
        if (finalStatus.reachedVideoEnd || lastProcessedFrame >= maxFrameIndex.value) {
          statusMessage.value = `完成：已追踪到视频末尾第 ${lastProcessedFrame + 1} 帧。`
          showToast('已追踪到视频末尾')
        } else {
          statusMessage.value = `完成：SAM3 已追踪第 ${startFrame + 1}～${lastProcessedFrame + 1} 帧。请检查/修正最终帧后再次点击 AI Tracking 继续。`
          showToast(`已到达单轮上限第 ${lastProcessedFrame + 1} 帧，可检查后继续`)
        }
      }
      trackingJobs.delete(mediaId);rememberTracking(mediaId);delete trackingErrors.value[mediaId]
    } catch (error) {
      if (restartIntent && (restartIntent.actorId !== useAuth().user.value?.id || restartSerial !== restartEpoch || mediaId !== currentMediaId.value)) return
      if (job&&(definiteRejection(error)||messageOf(error,'').includes('此服务器未启用 AI Tracking'))) {trackingJobs.delete(mediaId);rememberTracking(mediaId)}
      const message=phase==='查询追踪任务'&&Number((error as {status?:number})?.status)===404?'后台任务已不存在，服务器可能已重启。请检查已保存的结果，再重新追踪。':messageOf(error,'AI Tracking 失败')
      trackingErrors.value[mediaId]={phase,frameIndex:startFrame,message}
      console.error('[annotation.tracking_failed]',{mediaId:media.serverMediaId,frameIndex:startFrame,phase,taskId:job?.taskId,requestKey:job?.key,error})
      if (mediaId === currentMediaId.value) {
        statusMessage.value = `${phase}失败：${message}`
        showToast(statusMessage.value)
      }
    } finally {
      // A restarted request can finish after this page/account has moved on.
      // Its cleanup must not release a newer operation's lock or erase progress.
      if (!restartIntent || (restartIntent.actorId === useAuth().user.value?.id && restartSerial === restartEpoch && mediaId === currentMediaId.value)) {
        isAiBusy.value = trackingJobs.size>0
        trackingRetryBusy.value=false
        trackingProgress.value = ''
        if(!isAiBusy.value)scheduleWorkspaceStateSave(mediaId)
      }
    }
  }
  const runAiTrack = () => performTracking()
  const trackingRetryLabel = computed(()=>trackingJobs.get(currentMediaId.value)?.taskId?'重新查询追踪任务':trackingJobs.has(currentMediaId.value)?'重试原追踪请求':'重试追踪')
  const retryTracking = () => performTracking(true)

  /**
   * Build a browser-downloadable SAM3 annotations JSON for the current media.
   * Kept for the existing JSON export action; it is independent of AI Tracking autosave.
   */
  const buildSam3AnnotationsJson = async (mediaId: string) => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media) throw new Error('当前素材不存在，无法生成 JSON')
    const objects = annotationsByMedia.value[mediaId] ?? []
    if (!objects.length) throw new Error('当前素材没有可导出的标注')

    const { width, height } = await getMediaPixelSize(media)
    const frameIndex = media.type === 'video' ? currentFrame.value : 0
    const timestampMs = media.type === 'video' ? Math.round(currentTime.value * 1000) : 0
    const clampX = (value: number) => Math.min(width, Math.max(0, value))
    const clampY = (value: number) => Math.min(height, Math.max(0, value))
    const toPixel = (value: number) => Math.round(value)

    const annotations = objects.map((obj) => {
      const item: Record<string, unknown> = {
        id: obj.id,
        object_id: obj.objectId,
        objectId: obj.objectId,
        name: obj.name,
        source: obj.source,
      }
      if (obj.bbox) {
        item.bbox = [
          toPixel(clampX((obj.bbox.x / 100) * width)),
          toPixel(clampY((obj.bbox.y / 100) * height)),
          toPixel(clampX(((obj.bbox.x + obj.bbox.width) / 100) * width)),
          toPixel(clampY(((obj.bbox.y + obj.bbox.height) / 100) * height)),
        ]
      }
      if (obj.point) {
        item.point = [
          toPixel(clampX((obj.point.x / 100) * width)),
          toPixel(clampY((obj.point.y / 100) * height)),
        ]
      }
      if (media.type === 'video') {
        item.frameIndex = obj.frameIndex ?? frameIndex
        item.timestampMs = obj.timestampMs ?? timestampMs
      }
      return item
    })

    const payload = {
      version: '1.0',
      format: 'sam3-annotations',
      media: { id: media.id, name: media.name, type: media.type, width, height },
      frame: { frameIndex, timestampMs },
      coordinateSystem: { source: 'frontend-percent', target: 'pixel', bbox: '[x1, y1, x2, y2]' },
      annotations,
    }
    return new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
  }

  const generateAnnotationsJson = async () => {
    const mediaId = currentMediaId.value
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media) return showToast('当前素材不存在')
    try {
      const { width, height } = await getMediaPixelSize(media)
      if (media.type === 'video') {
        if (!media.serverMediaId) throw new Error('该视频尚未成功上传到后端，请重新上传视频')
        const annotations = await buildCurrentFrameSam3Objects(mediaId)
        const frameIndex = currentFrame.value
        const timestampMs = Math.round(currentTime.value * 1000)
        statusMessage.value = `正在保存第 ${frameIndex + 1} 帧 annotations.json……`
        const result = await trackApi.saveFrameAnnotations({
          mediaId: media.serverMediaId,
          generationId: workspaceGenerations.get(mediaId),
          mediaName: media.name,
          mediaWidth: width,
          mediaHeight: height,
          frameIndex,
          timestampMs,
          annotations,
        })
        statusMessage.value = `第 ${frameIndex + 1} 帧 JSON 已写入 ${result.filename}`
        showToast(`第 ${frameIndex + 1} 帧 JSON 已保存到视频目录`)
      } else {
        const blob = await buildSam3AnnotationsJson(mediaId)
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url; link.download = 'annotations.json'; link.click()
        setTimeout(() => URL.revokeObjectURL(url), 1000)
        statusMessage.value = 'JSON 生成成功：annotations.json'
        showToast('图片 annotations.json 已生成')
      }
    } catch (error) {
      statusMessage.value = error instanceof Error ? error.message : 'JSON 生成失败'
      showToast(statusMessage.value)
    }
  }


  const loadSavedResults = async () => {
    if (remoteResultsLoaded) return
    remoteResultsLoaded = true
    try {
      const { listResults } = await import('../api/httpAnnotationApi')
      const res = await listResults('default')
      const rows = (res.items ?? []) as any[]
      const groups = new Map<string, any[]>()
      for (const row of rows) {
        const fileIdentity = String(row.media_name || row.media_id || '').trim().toLocaleLowerCase()
        const key = `${row.media_type || 'video'}:${fileIdentity}:${row.frame_index ?? 0}:${row.user_id ?? 0}`
        groups.set(key, [...(groups.get(key) ?? []), row])
      }
      const remote = Array.from(groups.entries()).map(([key, list]) => {
        const head = list[0]
        const isVideo = head.media_type === 'video'
        const objects = list.map((r) => {
          const objectIdRaw = r.object_id && /^\d+$/.test(String(r.object_id)) ? Number(r.object_id) : undefined
          const isBox = r.shape_type === 'bbox'
          return normalizeAnnotationObject({
            id: r.object_id ?? String(r.id), objectId: objectIdRaw, name: r.object_name ?? 'rare sperm', source: (r.source === 'ai' ? 'ai' : 'manual'),
            bbox: isBox && r.pct_x !== null ? { x: r.pct_x, y: r.pct_y ?? 0, width: r.pct_w ?? 0, height: r.pct_h ?? 0 } : undefined,
            point: !isBox && r.pct_x !== null ? { x: r.pct_x, y: r.pct_y ?? 0 } : undefined,
            frameIndex: isVideo ? r.frame_index : undefined, timestampMs: isVideo ? r.timestamp_ms : undefined,
          } as AnnotationObject, objectIdRaw ?? Number(r.id))
        })
        return { key, batchId: head.batch_id || undefined, mediaId: head.media_id, mediaName: head.media_name ?? head.media_id,
          mediaType: (isVideo ? 'video' : 'image') as 'video' | 'image',
          frameIndex: isVideo ? head.frame_index : undefined, timestampMs: isVideo ? head.timestamp_ms : undefined,
          savedAt: new Date(String(head.created_at).replace(' ', 'T')).toISOString(), filename: '后端记录', username: head.username || null, objects } as SavedAnnotationFile & { key: string }
      })
      const existingKeys = new Set(savedResults.value.map((r: any) => r.batchId ? `batch:${r.batchId}` : `local:${r.mediaId}:${r.savedAt}:${r.objects.map((o: any) => o.id).join(',')}`))
      const fresh = remote.filter((r) => {
        const dedupeKey = r.batchId ? `batch:${r.batchId}` : `remote:${r.key}`
        if (loadedRemoteResultKeys.has(dedupeKey) || existingKeys.has(dedupeKey)) return false
        loadedRemoteResultKeys.add(dedupeKey)
        return true
      }).map(({ key: _key, ...r }) => r)
      if (fresh.length) savedResults.value = [...fresh, ...savedResults.value]
    } catch (error) {
      remoteResultsLoaded = false
      console.warn('从后端加载标注结果失败：', error)
    }
  }

  const openEffectFolderPicker = () => {
    effectFolderInputRef.value?.click()
  }

  const handleEffectFolder = (files: FileList | null) => {
    if (!files?.length) return
    const videos = Array.from(files).filter((file) => file.type.startsWith('video/'))
    if (!videos.length) {
      statusMessage.value = '所选文件夹中没有可查看的视频文件'
      return
    }

    const now = Date.now()
    const localEffects: EffectResult[] = videos.map((file, index) => ({
      id: `local-effect-${now}-${index}`,
      sourceMediaId: `local-effect-media-${now}-${index}`,
      sourceMediaName: file.name,
      resultName: `${file.name} · 本地效果查看`,
      resultVideoUrl: URL.createObjectURL(file),
      createdAt: new Date().toISOString(),
      status: 'completed',
      summary: { detectedObjects: 0, extractedCandidates: 0, durationSeconds: 0 },
    }))

    effectResults.value = [...localEffects, ...effectResults.value]
    selectedEffectId.value = localEffects[0]?.id ?? selectedEffectId.value
    statusMessage.value = `已加载 ${videos.length} 个本地视频，可直接查看效果`
  }

  const loadEffects = async () => {
    try {
      const result = await api.listEffects()
      effectResults.value = result.items
      selectedEffectId.value = result.items[0]?.id ?? null
    } catch (error) {
      statusMessage.value = error instanceof Error ? error.message : '效果列表加载失败'
    }
  }

  const onEffectTimeUpdate = () => {
    if (effectVideoRef.value) effectTime.value = effectVideoRef.value.currentTime
  }

  const toggleEffectPlayback = async () => {
    if (!effectVideoRef.value) return
    if (effectVideoRef.value.paused) {
      await effectVideoRef.value.play()
      effectPlaying.value = true
    } else {
      effectVideoRef.value.pause()
      effectPlaying.value = false
    }
  }

  const selectEffect = (effectId: string) => {
    selectedEffectId.value = effectId
    effectTime.value = 0
    effectPlaying.value = false
    nextTick(() => {
      if (effectVideoRef.value) {
        effectVideoRef.value.pause()
        effectVideoRef.value.currentTime = 0
      }
    })
  }

  const effectOverlayObjects = computed(() => {
    if (!selectedEffect.value) return []
    const t = effectTime.value
    return Array.from({ length: Math.min(4, selectedEffect.value.summary.detectedObjects) }, (_, index) => ({
      id: `effect-${index}`,
      x: 22 + ((t * (4 + index) + index * 17) % 58),
      y: 25 + ((Math.sin(t * 1.5 + index) + 1) * 22),
      width: 6 + index * 0.5,
      height: 5 + index * 0.4,
    }))
  })

  let restoreSerial = 0
  const resetAnnotationViewForMedia = async (mediaId = currentMediaId.value) => {
    if (mediaId !== currentMediaId.value) return
    const serial = ++restoreSerial
    const actor = useAuth().user.value?.id
    const isCurrent = () => serial === restoreSerial && mediaId === currentMediaId.value && actor === useAuth().user.value?.id
    restartController?.abort(); restartController = null; restartEpoch++
    trackingRestartBusy.value = false; pendingRestart.value = null; trackingRestartError.value = ''; trackingRestartStorageWarning.value = ''
    cancelAnnotationGesture(); frameCache.clear(); exactFrameRequestSerial++; seekSerial++
    exactFrameLoading.value = false; isSeekingVideo = false; requestedFrame.value = null; frameError.value = ''
    workspaceRestoring.value = true
    saveState.value = 'idle'; saveError.value = ''
    trackingWarningSummary.value = []
    objectDeletionError.value = ''; trackingFeedbackError.value = ''
    revokeExactFrameUrl()
    selectedObjectId.value = null
    currentFrame.value = 0
    currentTime.value = 0
    frameInput.value = 0
    tempBbox.value = null
    bboxStart = null
    activeTool.value = 'bbox'
    anomalyObjectIds.value = []
    anomalyFrames.value = []
    pausedAnomalies.value = []
    lastPausedContext.value = null
    zoom.value = 1
    videoPlaybackFallback.value = false
    stopFallbackPlayback()
    // A local image has no server workspace to reload. In particular, the
    // sample image after a real login must not inherit the account-change lock.
    if (!isVideo.value) workspaceReadError.value = ''
    if (isVideo.value && mediaId === currentMediaId.value) {
      // 后端上传元数据在 <video> loadedmetadata/error 之前已可用。先采用
      // 它，避免不同 FPS 的 AVI 逐帧预览仍沿用默认 30 FPS 计算时间轴。
      const media = mediaAssets.value.find((item) => item.id === mediaId)
      if (media?.fps && media.fps > 0) videoFps.value = media.fps
      if (media?.frameCount && media.fps) videoDuration.value = media.frameCount / media.fps
      const restoredFrame = await restoreWorkspaceState(mediaId, isCurrent)
      if (!isCurrent()) return
      const targetFrame = restoredFrame ?? 0
      if (workspaceReadError.value) { workspaceRestoring.value = false; return }
      await nextTick()
      if (!isCurrent()) return
      revokeExactFrameUrl()
      const video = videoRef.value
      if (video) {
        try { video.pause(); video.currentTime = frameToTime(targetFrame) } catch {}
      }
      await loadExactFrame(targetFrame, mediaId)
      if (!isCurrent()) return
      try { await loadTrackingResult(mediaId, true, true) }
      catch(error) {
        if(isCurrent()) {
          workspaceReadError.value=`追踪结果读取失败：${messageOf(error,'服务器响应异常')}。请重新读取工作区后再编辑，避免将未知数据当成空帧`
          saveState.value='error';saveError.value=workspaceReadError.value
          workspaceRestoring.value=false
        }
        return
      }
      if (!isCurrent()) return
      try{
        const job=JSON.parse(sessionStorage.getItem(trackingJournalKey(mediaId))||'null') as TrackingJob|null
        if(job?.key&&job.mediaId===mediaId&&job.input.mediaId===media?.serverMediaId){
          trackingJobs.set(mediaId,job);isAiBusy.value=true
          trackingErrors.value[mediaId]={frameIndex:job.startFrame,phase:'恢复任务状态',message:'上次追踪结果尚未确认，请查询原任务，避免重复启动。'}
        }
      }catch(error){console.warn('[annotation.tracking_restore_failed]',{mediaId,error})}
      restoreRestart(mediaId)
    }
    if (isCurrent()) workspaceRestoring.value = false
  }

  /**
   * 启动时从后端加载已有视频素材，刷新页面后素材列表不丢失。
   * 关键修复：通过 serverMediaId 匹配已持久化（localStorage）的素材，
   * 复用原 id（如 local-xxx），这样 annotationsByMedia 的 key 才能对上。
   */
  const loadServerMedia = async () => {
    try {
      const cachedIds = new Set(mediaAssets.value.filter(item => item.serverMediaId).map(item => item.id))
      const res = await trackApi.listMedia()
      const existingIds = new Set(res.items.map(item => item.mediaId))
      // Uploads may finish while the list request is in flight. Only reconcile
      // entries that were already cached when this request began.
      mediaAssets.value = mediaAssets.value.filter(item => !cachedIds.has(item.id) || !item.serverMediaId || existingIds.has(item.serverMediaId))
      // 建立 serverMediaId -> MediaAsset 的索引
      const byServerId = new Map<string, MediaAsset>()
      for (const m of mediaAssets.value) {
        if (m.serverMediaId) byServerId.set(m.serverMediaId, m)
      }

      const additions: MediaAsset[] = []
      for (const item of res.items) {
        const existing = byServerId.get(item.mediaId)
        if (existing) {
          // 已有记录：刷新后 blob: URL 必然失效，用后端新地址覆盖 url；保留原 id
          if (item.videoUrl) existing.url = item.videoUrl
          existing.serverVideoName = item.sourceVideoName || existing.serverVideoName
          if (item.fps) existing.fps = item.fps
          if (item.width) existing.width = item.width
          if (item.height) existing.height = item.height
          if (item.frameCount) existing.frameCount = item.frameCount
          if (item.frameCount && item.fps) existing.duration = item.duration || item.frameCount / item.fps

          continue
        }
        // 新素材：创建条目
        const newAsset: MediaAsset = {
          id: closedMediaFrontendIds[item.mediaId] || `server-${item.mediaId}`,
          serverMediaId: item.mediaId,
          serverVideoName: item.sourceVideoName,
          name: item.videoName,
          type: 'video',
          url: item.videoUrl,
          fps: item.fps || undefined,
          width: item.width || undefined,
          height: item.height || undefined,
          duration: item.duration || (item.frameCount && item.fps ? item.frameCount / item.fps : undefined),
          frameCount: item.frameCount || undefined,
        }
        additions.push(newAsset)
        if (closedMediaFrontendIds[item.mediaId]) delete closedMediaFrontendIds[item.mediaId]
        trackingFramesByMedia.value[newAsset.id] = []
      }
      if (additions.length) {
        mediaAssets.value = [...mediaAssets.value, ...additions]
        persistClosedMediaFrontendIds()
      }

      // 如果当前选中的 media 不存在了（被删），重置到第一个
      if (selectedMediaId.value && !mediaAssets.value.find((m) => m.id === selectedMediaId.value)) {
        selectedMediaId.value = mediaAssets.value[0]?.id ?? ''
      }
      const active = mediaAssets.value.find((item) => item.id === selectedMediaId.value)
      if (active?.serverMediaId) await resetAnnotationViewForMedia(active.id)
    } catch (error) {
      console.error('[annotation.media_list_failed]', error)
      statusMessage.value = '素材列表同步失败，请刷新重试；本机列表已保留'
    }
  }

  watch(() => useAuth().user.value?.id, (next, previous) => {
    if (next === previous) return
    restoreSerial++; trackingLoadSerial++
    workspaceRestoreInProgress = false
    for (const timer of workspaceSaveTimers.values()) clearTimeout(timer)
    workspaceSaveTimers.clear()
    pendingDeletion.value = null; pendingFeedback.value = null
    restartController?.abort(); restartController = null; restartEpoch++
    pendingRestart.value = null; trackingRestartBusy.value = false; trackingRestartError.value = ''; trackingRestartStorageWarning.value = ''
    videoDeletionHistory.value = {}; histories.clear(); historyRevision.value++
    objectDeletionError.value = ''; trackingFeedbackError.value = ''
    trackingJobs.clear();trackingErrors.value={};isAiBusy.value=false
    workspaceReadError.value = '账号已切换，请重新加载素材'
  })

  return {
    restartTrackingFromEarlierFrame, retryTrackingRestart, cancelTrackingRestartRequest, trackingRestartBusy, trackingRestartError, trackingRestartPendingAction, trackingRestartStorageWarning,
    trackingError,trackingProgress,trackingRetryLabel,trackingRetryBusy,retryTracking,workspaceRecoveryRequired,saveRecoveryLabel,retryWorkspaceSave,reloadWorkspaceFromServer,
    getObjectDeletionSummary, removeObjectAcrossVideo, undoVideoObjectDeletion, retryObjectDeletion, canUndoVideoDeletion, lastVideoObjectDeletion, objectDeletionBusy, objectDeletionError, objectDeletionPendingAction, confirmTrackingAnomaly, retryTrackingAnomalyFeedback, trackingFeedbackBusy, trackingFeedbackError, trackingFeedbackPending, trackingFeedbackPendingAction, trackingCalibrationSummary, resetTrackingCalibration, trackingWarningSummary,
    deleteMedia, deletingMediaId, mediaDeleteError, persistWorkspaceState, retryExactFrame, saveState, saveError, playbackRate, pausePlayback, displayObjects, editingBlocked, workspaceRestoring, frameError, loadExactFrame, nudgeSelected, cancelAnnotationGesture, canUndo, canRedo,
    api, mediaAssets, selectedMediaId, submissionLocks, submissionBusy, mediaImportBusy, importError, duplicateImport, reimportSaving, cancelDuplicateImport, confirmDuplicateImport, restoreImportIntent, activeTool, objectNameInput, selectedObjectId, currentFrame, currentTime, videoDuration, videoFps, frameInput, isPlaying, isAiBusy, trackingFrameCount, statusMessage, toastMessage, showToast, zoom, zoomIn, zoomOut, zoomReset, savedResults, loadedRemoteResultKeys, effectResults, selectedEffectId, effectTime, effectPlaying, effectVideoRef, imageRef, videoRef, exactFrameImageRef, exactFrameUrl, exactFrameLoading, videoPlaybackFallback, annotationHitRef, fileInputRef, videoInputRef, effectFolderInputRef, annotationsByMedia, trackingFramesByMedia, anomalyObjectIds, anomalyFrames, pausedAnomalies, anomalyPanelVisible, trackingPausedFrame, closeAnomalyPanel, selectedMedia, isVideo, maxFrameIndex, currentMediaId, currentObjects, selectedObject, selectedEffect, formatTime, timeToFrame, frameToTime, getStagePoint, addObject, resetVideoViewToFirstFrame, ensureVideoFirstFrame, selectTool, onStageClick, tempBbox, onBboxDown, onBboxMove, onBboxUp, onObjectDropdownChange, selectObject, removeObject, renameObject, undo, redo, copyPreviousFrame, brightness, contrast, mediaFilterStyle, resetMediaFilter, annotatedFrameCount, clearSelection, openFilePicker, handleFiles, onImageLoaded, onVideoLoaded, onVideoTimeUpdate, onVideoError, loadTrackingResult, seekVideo, seekToInputFrame, seekByFrame, togglePlayback, onVideoEnded, onTimelineClick, runAiSegment, runAiTrack, getMediaPixelSize, buildSam3AnnotationsJson, generateAnnotationsJson, loadSavedResults, openEffectFolderPicker, handleEffectFolder, loadEffects, onEffectTimeUpdate, toggleEffectPlayback, selectEffect, effectOverlayObjects, resetAnnotationViewForMedia, loadServerMedia
  }
}

const workspace = createWorkspace()
export const useWorkspace = () => workspace
