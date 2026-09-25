import { computed, nextTick, ref, watch } from 'vue'
import type { AnnotationObject, AnnotationTool, EffectResult, MediaAsset, SavedAnnotationFile } from '../types/annotation'
// 登录、人工标注、视频目录、SAM3 Tracking 均走真实后端
import { httpAnnotationApi, exportDataset as apiExportDataset } from '../api/httpAnnotationApi'
import { trackApi } from '../api/trackApi'
import type { TrackingFrameObject, TrackingFrameResult } from '../types/annotation'

const createWorkspace = () => {
  const VIDEO_EXTENSIONS = new Set([
    '.mp4', '.avi', '.mov', '.mkv', '.webm', '.m4v', '.mpg', '.mpeg', '.wmv', '.flv',
    '.ts', '.m2ts', '.mts', '.3gp', '.ogv', '.ogg', '.asf', '.vob', '.divx', '.xvid',
  ])
  const isVideoFile = (file: File) => file.type.startsWith('video/') || VIDEO_EXTENSIONS.has(`.${file.name.split('.').pop()?.toLowerCase() || ''}`)
  const isVideoName = (name: string) => VIDEO_EXTENSIONS.has(`.${name.split('.').pop()?.toLowerCase() || ''}`)
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

  const selectedMediaId = ref(mediaAssets.value[0]?.id ?? '')
  let workspaceRestoreInProgress = false
  let persistWorkspaceState: (mediaId: string, useCurrentUiState?: boolean) => Promise<void> = async () => {}
  let scheduleWorkspaceStateSave: (mediaId: string) => void = () => {}
  const activeTool = ref<AnnotationTool>('point')
  const objectNameInput = ref('rare sperm')
  const selectedObjectId = ref<string | null>(null)
  const currentFrame = ref(0)
  const currentTime = ref(0)
  const videoDuration = ref(0)
  const videoFps = ref(30)
  const frameInput = ref(0)
  const isPlaying = ref(false)
  const isAiBusy = ref(false)
  const trackingFrameCount = ref(5)
  const statusMessage = ref('就绪')
  const zoom = ref(1)
  const isInteracting = () => !!tempBbox.value || !!draggingObjectId || !!bboxStart
  const zoomIn = (step = 0.1) => { if (isInteracting()) return; zoom.value = Math.min(5, +(zoom.value + step).toFixed(2)) }
  const zoomOut = (step = 0.1) => { if (isInteracting()) return; zoom.value = Math.max(0.25, +(zoom.value - step).toFixed(2)) }
  const zoomReset = () => { zoom.value = 1 }
  const closeMedia = async (mediaId: string) => {
    const media = mediaAssets.value.find((m) => m.id === mediaId)
    if (!media) return

    if (media.serverMediaId && mediaId === currentMediaId.value) {
      try { await persistWorkspaceState(mediaId, true) }
      catch (error) { console.warn('关闭前保存工作区状态失败：', error) }
    }

    // 素材列表中的按钮只代表关闭当前浏览器视图。后端原视频、人工标注、
    // tracker_results.json、数据集以及数据库记录全部保留，之后可通过
    // “加载标注”或刷新后端素材列表重新打开。
    if (media.url.startsWith('blob:')) URL.revokeObjectURL(media.url)
    if (media.serverMediaId) {
      closedMediaFrontendIds[media.serverMediaId] = media.id
      persistClosedMediaFrontendIds()
    }
    mediaAssets.value = mediaAssets.value.filter((m) => m.id !== mediaId)
    delete trackingFramesByMedia.value[mediaId]
    if (selectedMediaId.value === mediaId) {
      selectedMediaId.value = mediaAssets.value.length ? mediaAssets.value[0].id : ''
    }
    statusMessage.value = `已关闭素材：${media.name}；后端文件和标注记录均已保留`
    showToast(`已关闭「${media.name}」，后端记录未删除`)
  }
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
  const annotationFolderInputRef = ref<HTMLInputElement | null>(null)
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
  const annotationsByMedia = ref<Record<string, AnnotationObject[]>>(loadAnnotations())

  // 自动持久化到 localStorage（debounce 300ms）
  let saveTimer: ReturnType<typeof setTimeout> | null = null
  watch(annotationsByMedia, (val) => {
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      try { localStorage.setItem('annotationsByMedia', JSON.stringify(val)) } catch {}
      if (!workspaceRestoreInProgress) scheduleWorkspaceStateSave(selectedMediaId.value)
    }, 300)
  }, { deep: true })

  // ── 撤销/重做栈 ──
  const undoStack: string[] = []
  const redoStack: string[] = []
  const MAX_UNDO = 50
  let undoSnapshotInProgress = false

  const snapshotUndo = () => {
    if (undoSnapshotInProgress) return
    undoStack.push(JSON.stringify(annotationsByMedia.value))
    if (undoStack.length > MAX_UNDO) undoStack.shift()
    redoStack.length = 0
  }
  const undo = () => {
    if (!undoStack.length) { showToast('没有可撤销的操作'); return }
    redoStack.push(JSON.stringify(annotationsByMedia.value))
    const prev = undoStack.pop()!
    undoSnapshotInProgress = true
    annotationsByMedia.value = JSON.parse(prev)
    undoSnapshotInProgress = false
    statusMessage.value = '已撤销'
  }
  const redo = () => {
    if (!redoStack.length) { showToast('没有可重做的操作'); return }
    undoStack.push(JSON.stringify(annotationsByMedia.value))
    const next = redoStack.pop()!
    undoSnapshotInProgress = true
    annotationsByMedia.value = JSON.parse(next)
    undoSnapshotInProgress = false
    statusMessage.value = '已重做'
  }

  // SAM3 逐帧结果缓存。key=前端 media.id，value=服务器 tracking_result.json 的 frames。
  const trackingFramesByMedia = ref<Record<string, TrackingFrameResult[]>>({})
  /** 记录前端删除的 AI tracking 对象 id，避免重新加载后又出现 */
  const deletedTrackingIds = ref<Record<string, Set<string>>>({})
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
    reviewRange?: string
    reviewNotice?: string
    suggestion: string
  }>>([])
  const anomalyPanelVisible = ref(false)
  const closeAnomalyPanel = () => { anomalyPanelVisible.value = false }
  const lastPausedContext = ref<{ mediaId: string; frameIndex: number } | null>(null)
  let trackingLoadSerial = 0
  let lastTrackingPollAt = 0
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
    return maxId + 1
  }
  const currentObjects = computed(() => {
    const all = annotationsByMedia.value[currentMediaId.value] ?? []
    if (!isVideo.value) return all
    return all.filter((item) => (item.frameIndex ?? 0) === currentFrame.value)
  })
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
    if (isAiBusy.value) { showToast('SAM3 Tracking 运行中，暂时禁止人工标注'); return }
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
    statusMessage.value = `已添加：${name}${media.type === 'video' ? `（第 ${currentFrame.value} 帧）` : ''}`
  }

  const revokeExactFrameUrl = () => {
    if (exactFrameUrl.value) {
      try { URL.revokeObjectURL(exactFrameUrl.value) } catch {}
      exactFrameUrl.value = null
    }
  }

  const loadExactFrame = async (frameIndex: number, mediaId = currentMediaId.value) => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media || media.type !== 'video' || !media.serverMediaId) return false
    const serial = ++exactFrameRequestSerial
    exactFrameLoading.value = true
    try {
      const blob = await trackApi.getFrameBlob(media.serverMediaId, frameIndex)
      if (serial !== exactFrameRequestSerial || mediaId !== currentMediaId.value) return false
      const url = URL.createObjectURL(blob)
      revokeExactFrameUrl()
      exactFrameUrl.value = url
      currentFrame.value = frameIndex
      frameInput.value = frameIndex
      currentTime.value = frameToTime(frameIndex)
      await nextTick()
      return true
    } finally {
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
    if (isAiBusy.value) { showToast('SAM3 Tracking 运行中，暂时禁止人工标注'); return }
    activeTool.value = tool
    statusMessage.value = isVideo.value
      ? `已切换到${toolLabel(tool)} · 当前第 ${currentFrame.value} 帧`
      : `已切换到${toolLabel(tool)}`
  }

  const onStageClick = (event: MouseEvent) => {
    if (isAiBusy.value) return
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

  const HANDLE_HIT = 2.5 // 角点命中范围（百分比坐标）
  const pointInBbox = (px: number, py: number, b: { x: number; y: number; width: number; height: number }) =>
    px >= b.x && px <= b.x + b.width && py >= b.y && py <= b.y + b.height

  const getResizeHandle = (px: number, py: number, b: { x: number; y: number; width: number; height: number }) => {
    const { x, y, width: w, height: h } = b
    if (Math.abs(px - x) < HANDLE_HIT && Math.abs(py - y) < HANDLE_HIT) return 'nw'
    if (Math.abs(px - (x + w)) < HANDLE_HIT && Math.abs(py - y) < HANDLE_HIT) return 'ne'
    if (Math.abs(px - x) < HANDLE_HIT && Math.abs(py - (y + h)) < HANDLE_HIT) return 'sw'
    if (Math.abs(px - (x + w)) < HANDLE_HIT && Math.abs(py - (y + h)) < HANDLE_HIT) return 'se'
    return null
  }

  const onBboxDown = (event: PointerEvent) => {
    if (isAiBusy.value) return
    event.preventDefault()
    event.stopPropagation()
    bboxPointerId = event.pointerId
    const stage = annotationHitRef.value
    if (stage) {
      try { stage.setPointerCapture(event.pointerId) } catch { }
    }
    const point = getStagePoint(event)

    // 选择框模式：点击选中，同时允许对命中的框直接拖拽移动；
    // 点击四角手柄可缩放。此模式不会创建新框。
    if (activeTool.value === 'select') {
      const hit = [...currentObjects.value].reverse().find((obj) => obj.bbox && pointInBbox(point.x, point.y, obj.bbox))
      if (!hit?.bbox) {
        clearSelection()
        bboxPointerId = null
        hitExisting = false
        return
      }

      selectedObjectId.value = hit.id
      draggingObjectId = hit.id
      dragStartPoint = point
      dragStartBbox = { ...hit.bbox }
      dragMoved = false
      resizeHandle = getResizeHandle(point.x, point.y, hit.bbox)
      hitExisting = true
      statusMessage.value = resizeHandle
        ? `已选中 ${hit.name}，拖拽角点可调整框大小`
        : `已选中 ${hit.name}，拖拽可移动框位置`
      return
    }

    // 1. 优先检测：已选中框的角点（允许框外一定范围命中）
    if (selectedObjectId.value) {
      const sel = currentObjects.value.find((o) => o.id === selectedObjectId.value && o.bbox)
      if (sel?.bbox) {
        const handle = getResizeHandle(point.x, point.y, sel.bbox)
        if (handle) {
          draggingObjectId = sel.id
          dragStartPoint = point
          dragStartBbox = { ...sel.bbox }
          dragMoved = false
          resizeHandle = handle
          hitExisting = true
          statusMessage.value = `已选中 ${sel.name}，拖拽角点可调整框大小`
          return
        }
      }
    }

    // 2. 检测是否点中已有框（移动）
    const hit = [...currentObjects.value].reverse().find((obj) => obj.bbox && pointInBbox(point.x, point.y, obj.bbox))
    if (hit) {
      draggingObjectId = hit.id
      dragStartPoint = point
      dragStartBbox = { ...hit.bbox! }
      dragMoved = false
      resizeHandle = null
      hitExisting = true
      selectedObjectId.value = hit.id
      statusMessage.value = `已选中 ${hit.name}，拖拽可移动框位置`
      return
    }

    // 3. 未命中已有框：只有框标注模式才绘制新框
    if (activeTool.value !== 'bbox') return
    bboxStart = point
    tempBbox.value = { x: point.x, y: point.y, width: 0, height: 0 }
  }

  const onBboxMove = (event: PointerEvent) => {
    if (bboxPointerId !== event.pointerId) return
    const point = getStagePoint(event)

    // 拖拽已有框：移动或调整大小
    if (draggingObjectId && dragStartPoint && dragStartBbox) {
      const dx = point.x - dragStartPoint.x
      const dy = point.y - dragStartPoint.y
      if (Math.abs(dx) > 0.3 || Math.abs(dy) > 0.3) dragMoved = true
      const startBbox = dragStartBbox
      let newBbox = { ...startBbox }

      if (resizeHandle) {
        // 调整大小
        let { x, y, width: w, height: h } = startBbox
        if (resizeHandle === 'nw') { x = startBbox.x + dx; y = startBbox.y + dy; w = startBbox.width - dx; h = startBbox.height - dy }
        if (resizeHandle === 'ne') { y = startBbox.y + dy; w = startBbox.width + dx; h = startBbox.height - dy }
        if (resizeHandle === 'sw') { x = startBbox.x + dx; w = startBbox.width - dx; h = startBbox.height + dy }
        if (resizeHandle === 'se') { w = startBbox.width + dx; h = startBbox.height + dy }
        // 保证最小尺寸
        if (w < 1) { if (resizeHandle.includes('w')) x = startBbox.x + startBbox.width - 1; w = 1 }
        if (h < 1) { if (resizeHandle.includes('n')) y = startBbox.y + startBbox.height - 1; h = 1 }
        newBbox = {
          x: Math.max(0, Math.min(100 - w, x)),
          y: Math.max(0, Math.min(100 - h, y)),
          width: w,
          height: h,
        }
      } else {
        // 移动
        newBbox = {
          x: Math.max(0, Math.min(100 - startBbox.width, startBbox.x + dx)),
          y: Math.max(0, Math.min(100 - startBbox.height, startBbox.y + dy)),
          width: startBbox.width,
          height: startBbox.height,
        }
      }

      const mediaId = currentMediaId.value
      const allObjs = annotationsByMedia.value[mediaId] ?? []
      annotationsByMedia.value = {
        ...annotationsByMedia.value,
        [mediaId]: allObjs.map((obj) =>
          obj.id === draggingObjectId && obj.bbox
            // 用户修改任何 bbox 都升级为 source='manual', 防止被 loadTrackingResult 用 API 旧 AI 结果覆盖
            ? { ...obj, bbox: newBbox, source: 'manual' as const }
            : obj
        ),
      }
      return
    }

    if (!bboxStart) return
    tempBbox.value = {
      x: Math.min(bboxStart.x, point.x),
      y: Math.min(bboxStart.y, point.y),
      width: Math.abs(point.x - bboxStart.x),
      height: Math.abs(point.y - bboxStart.y),
    }
  }

  const onBboxUp = (event: PointerEvent) => {
    if (bboxPointerId !== event.pointerId) return
    const stage = annotationHitRef.value
    if (stage) {
      try { stage.releasePointerCapture(event.pointerId) } catch { }
    }

    // 结束拖拽
    if (draggingObjectId) {
      const obj = currentObjects.value.find((o) => o.id === draggingObjectId)
      if (obj && dragMoved) snapshotUndo()
      if (obj) {
        const action = resizeHandle ? (dragMoved ? '已调整' : '已选中') : (dragMoved ? '已移动' : '已选中')
        statusMessage.value = `${action} ${obj.name}`
      }
      draggingObjectId = null
      dragStartPoint = null
      dragStartBbox = null
      dragMoved = false
      resizeHandle = null
      bboxPointerId = null
      return
    }

    // 只有真正拖拽出一定大小的框才创建
    if (tempBbox.value && tempBbox.value.width > 2 && tempBbox.value.height > 2) {
      const center = { x: tempBbox.value.x + tempBbox.value.width / 2, y: tempBbox.value.y + tempBbox.value.height / 2 }
      addObject(center, { ...tempBbox.value })
    } else if (selectedObjectId.value) {
      // 点击空白处且没画框：取消选中
      clearSelection()
    }
    bboxPointerId = null
    bboxStart = null
    tempBbox.value = null
  }

  const onObjectDropdownChange = () => {
    if (selectedObjectId.value) selectObject(selectedObjectId.value)
  }

  const selectObject = (objectId: string) => {
    selectedObjectId.value = objectId
    const object = currentObjects.value.find((item) => item.id === objectId)
    if (!object) return

    statusMessage.value = isVideo.value
      ? `已定位：${object.name} · 第 ${currentFrame.value} 帧`
      : `已定位：${object.name}`
  }

  const removeObject = (objectId: string) => {
    if (isAiBusy.value) { showToast('SAM3 Tracking 运行中，暂时禁止修改标注'); return }
    snapshotUndo()
    const mediaId = currentMediaId.value

    // 只删除当前帧的该标注，不影响其他帧
    const list = annotationsByMedia.value[mediaId] ?? []
    const filtered = list.filter((obj) => !(obj.id === objectId && (obj.frameIndex ?? 0) === currentFrame.value))
    if (filtered.length !== list.length) {
      annotationsByMedia.value = { ...annotationsByMedia.value, [mediaId]: filtered }
      statusMessage.value = `已删除第 ${currentFrame.value} 帧的标注`
    } else {
      statusMessage.value = '未找到要删除的标注'
    }

    // 记录到已删除集合，防止 loadTrackingResult 重新加载后又出现
    if (!deletedTrackingIds.value[mediaId]) deletedTrackingIds.value[mediaId] = new Set()
    deletedTrackingIds.value[mediaId].add(objectId)
    scheduleWorkspaceStateSave(mediaId)

    if (selectedObjectId.value === objectId) selectedObjectId.value = null
  }

  const renameObject = () => {
    if (isAiBusy.value) { showToast('SAM3 Tracking 运行中，暂时禁止修改标注'); return }
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
    if (isAiBusy.value) { showToast('SAM3 Tracking 运行中'); return }
    if (!isVideo.value) { showToast('仅视频支持复制上一帧'); return }
    const mediaId = currentMediaId.value
    const prevFrame = currentFrame.value - 1
    if (prevFrame < 0) { showToast('已是第一帧'); return }
    const allObjs = annotationsByMedia.value[mediaId] ?? []
    const prevObjs = allObjs.filter((o) => (o.frameIndex ?? 0) === prevFrame)
    if (!prevObjs.length) { showToast(`第 ${prevFrame} 帧没有标注`); return }
    snapshotUndo()
    const newObjs = prevObjs.map((o) => ({
      ...o,
      id: `manual-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      frameIndex: currentFrame.value,
      timestampMs: Math.round(currentTime.value * 1000),
      source: 'manual' as const,
    }))
    annotationsByMedia.value = { ...annotationsByMedia.value, [mediaId]: [...allObjs, ...newObjs] }
    statusMessage.value = `已从第 ${prevFrame} 帧复制 ${newObjs.length} 个标注`
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

  persistWorkspaceState = async (mediaId: string, useCurrentUiState = false) => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media?.serverMediaId || workspaceRestoreInProgress) return
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
    await trackApi.saveWorkspaceState(media.serverMediaId, state)
  }

  scheduleWorkspaceStateSave = (mediaId: string) => {
    if (!mediaId || workspaceRestoreInProgress) return
    const previous = workspaceSaveTimers.get(mediaId)
    if (previous) clearTimeout(previous)
    workspaceSaveTimers.set(mediaId, setTimeout(() => {
      workspaceSaveTimers.delete(mediaId)
      void persistWorkspaceState(mediaId).catch((error) => console.warn('工作区状态保存失败：', error))
    }, 800))
  }

  // Switching assets happens before the new asset resets the shared editor
  // refs. Persist the old asset with the still-current UI values so its frame,
  // anomaly panel and display/editor state are not overwritten by defaults.
  watch(selectedMediaId, (nextId, previousId) => {
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

  const restoreWorkspaceState = async (mediaId: string): Promise<number | null> => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media?.serverMediaId) return null
    try {
      const state = await trackApi.getWorkspaceState(media.serverMediaId)
      if (!state.exists) return null
      workspaceRestoreInProgress = true
      const restoredManual = (state.manualAnnotations ?? [])
        .filter((value): value is AnnotationObject => !!value && typeof value === 'object')
        .map((value) => ({ ...value, source: 'manual' as const }))
      const retainedAi = (annotationsByMedia.value[mediaId] ?? []).filter((obj) => obj.source === 'ai')
      annotationsByMedia.value = {
        ...annotationsByMedia.value,
        [mediaId]: dedupeAnnotationObjects([...retainedAi, ...restoredManual]),
      }
      deletedTrackingIds.value[mediaId] = new Set((state.deletedTrackingIds ?? []).map(String))
      anomalyFrames.value = Array.isArray(state.anomalyFrames) ? state.anomalyFrames : []
      pausedAnomalies.value = Array.isArray(state.pausedAnomalies)
        ? state.pausedAnomalies as typeof pausedAnomalies.value
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
      console.warn('工作区状态加载失败，将使用 Tracking/本地缓存恢复：', error)
      return null
    } finally {
      workspaceRestoreInProgress = false
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

  const handleFiles = async (files: FileList | null, type: 'image' | 'video') => {
    if (!files?.length) return
    let lastAddedId: string | null = null

    for (const file of Array.from(files)) {
      if (type === 'image' && !file.type.startsWith('image/')) continue
      if (type === 'video' && !isVideoFile(file)) continue

      const media: MediaAsset = {
        id: `local-${crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`}`,
        name: file.name,
        type,
        url: URL.createObjectURL(file),
        fps: type === 'video' ? 30 : undefined,
        sizeBytes: file.size,
      }

      // 每个导入素材都创建独立的标注容器，绝不复用示例素材的数组。
      annotationsByMedia.value[media.id] = []
      mediaAssets.value = [...mediaAssets.value, media]
      lastAddedId = media.id

      // 视频必须先落到后端；后端原样保存用户上传的源视频，后续 annotations / tracking JSON 都写入独立目录。
      if (type === 'video') {
        try {
          const uploaded = await trackApi.uploadVideo(file)
          media.serverMediaId = uploaded.mediaId
          media.serverVideoName = uploaded.videoName
          media.url = uploaded.videoUrl
          if (uploaded.width) media.width = uploaded.width
          if (uploaded.height) media.height = uploaded.height
          if (uploaded.duration) media.duration = uploaded.duration
          if (uploaded.fps) media.fps = uploaded.fps
          if ((uploaded as any).frameCount) media.frameCount = (uploaded as any).frameCount
          if (uploaded.duration) media.duration = uploaded.duration
          trackingFramesByMedia.value[media.id] = []
        } catch (error) {
          console.error('视频上传到 Tracking 后端失败:', error)
          statusMessage.value = error instanceof Error ? error.message : '视频上传到后端失败'
        }
      } else {
        void api.uploadMedia({ file })
      }
    }

    if (lastAddedId) {
      // 先切换素材 ID，再等待 DOM 根据 key 创建全新的媒体元素，避免旧视频的异步事件覆盖新视频状态。
      selectedMediaId.value = lastAddedId
      await nextTick()
      await resetAnnotationViewForMedia(lastAddedId)
      const name = mediaAssets.value.find((m) => m.id === lastAddedId)?.name || ''
      statusMessage.value = `已导入并切换到新素材：${name}；已创建独立标注记录`
      showToast(`已导入「${name}」，标注记录已独立创建`)
    }
    if (type === 'image' && fileInputRef.value) fileInputRef.value.value = ''
    if (type === 'video' && videoInputRef.value) videoInputRef.value.value = ''
  }

  const parseTrackerResults = (text: string): { rows: any[]; meta: any } => {
    const trimmed = text.trim()
    if (!trimmed) return { rows: [], meta: {} }
    try {
      const parsed = JSON.parse(trimmed)
      if (Array.isArray(parsed)) return { rows: parsed, meta: {} }
      if (parsed?.frames && Array.isArray(parsed.frames)) return { rows: parsed.frames, meta: parsed }
      if (parsed?.results && Array.isArray(parsed.results)) return { rows: parsed.results, meta: parsed }
      if (parsed && typeof parsed === 'object' && 'frame_index' in parsed) return { rows: [parsed], meta: parsed }
    } catch { /* JSONL */ }
    const rows = trimmed.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).flatMap((line) => {
      try {
        const value = JSON.parse(line)
        return value?.frame_index !== undefined || value?.frameIndex !== undefined ? [value] : []
      } catch { return [] }
    })
    return { rows, meta: {} }
  }

  const openExistingBackendMedia = async (folderName: string, videoFile: File): Promise<boolean> => {
    const normalizedFolderName = folderName.trim()
    if (!normalizedFolderName) return false

    const response = await trackApi.listMedia()
    const serverItem = response.items.find((item) => {
      const sameDirectory = item.mediaId === normalizedFolderName || item.directoryName === normalizedFolderName
      const sameVideo = !item.sourceVideoName || item.sourceVideoName === videoFile.name
      return sameDirectory && sameVideo
    })
    if (!serverItem) return false

    let media = mediaAssets.value.find((item) => item.serverMediaId === serverItem.mediaId)
    const alreadyOpen = !!media
    if (!media) {
      media = {
        id: closedMediaFrontendIds[serverItem.mediaId] || `server-${serverItem.mediaId}`,
        serverMediaId: serverItem.mediaId,
        serverVideoName: serverItem.sourceVideoName || videoFile.name,
        name: serverItem.videoName || serverItem.sourceVideoName || videoFile.name,
        type: 'video',
        url: serverItem.videoUrl,
        fps: serverItem.fps || undefined,
        width: serverItem.width || undefined,
        height: serverItem.height || undefined,
        duration: serverItem.duration || (serverItem.frameCount && serverItem.fps ? serverItem.frameCount / serverItem.fps : undefined),
        frameCount: serverItem.frameCount || undefined,
        sizeBytes: videoFile.size,
      }
      mediaAssets.value = [...mediaAssets.value, media]
      trackingFramesByMedia.value[media.id] = []
    } else {
      media.url = serverItem.videoUrl
      media.serverVideoName = serverItem.sourceVideoName || videoFile.name
      if (serverItem.fps) media.fps = serverItem.fps
      if (serverItem.width) media.width = serverItem.width
      if (serverItem.height) media.height = serverItem.height
      if (serverItem.frameCount) media.frameCount = serverItem.frameCount
      if (serverItem.duration || (serverItem.frameCount && serverItem.fps)) {
        media.duration = serverItem.duration || (serverItem.frameCount as number) / (serverItem.fps as number)
      }
    }

    if (closedMediaFrontendIds[serverItem.mediaId]) {
      delete closedMediaFrontendIds[serverItem.mediaId]
      persistClosedMediaFrontendIds()
    }

    selectedMediaId.value = media.id
    await nextTick()
    await resetAnnotationViewForMedia(media.id)

    if (alreadyOpen) {
      statusMessage.value = `该视频已经打开，已切换到：${media.name}`
      showToast(`「${media.name}」已经打开，已为你切换`)
    } else {
      statusMessage.value = `已加载现有标注目录：${serverItem.directoryName || serverItem.mediaId}（未新建目录）`
      showToast(`已继续使用现有目录「${serverItem.directoryName || serverItem.mediaId}」`)
    }
    return true
  }

  const loadTrackerFolder = async (videoFile: File, trackerFile: File | null, sourceFolderName = '') => {
    // 浏览器不会暴露绝对路径，但 directory picker / webkitRelativePath
    // 都能提供顶层目录名；后端目录名本身就是稳定 mediaId。
    if (sourceFolderName && await openExistingBackendMedia(sourceFolderName, videoFile)) return
    if (!trackerFile) throw new Error('所选文件夹不是已有后端素材目录，且缺少 tracker_results.json')

    const { rows, meta } = parseTrackerResults(await trackerFile.text())
    if (!rows.length) throw new Error('tracker_results.json 中没有可用的逐帧标注')

    const mediaId = `loaded-${crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`} `
    const safeMediaId = mediaId.trim()
    const mediaMeta = meta?.media || {}
    const trackingMeta = meta?.tracking || {}
    // 文件夹导入也统一把原始视频送到后端：原格式原样保存，不转码；
    // 这样 AVI/MKV 等浏览器不能直接播放的格式仍可通过逐帧预览和 Tracking 使用。
    const uploaded = await trackApi.uploadVideo(videoFile)
    const videoUrl = uploaded.videoUrl
    const width = Number(mediaMeta.width || trackingMeta.width || rows.find((r: any) => r.width)?.width || uploaded.width || 0) || undefined
    const height = Number(mediaMeta.height || trackingMeta.height || rows.find((r: any) => r.height)?.height || uploaded.height || 0) || undefined
    const fps = Number(mediaMeta.fps || trackingMeta.fps || rows.find((r: any) => r.fps)?.fps || uploaded.fps || 15) || 15
    const frames: TrackingFrameResult[] = []
    const annotations: AnnotationObject[] = []
    const usedObjectIds = new Set<number>()
    let fallbackObjectId = 1

    for (const row of rows) {
      const frameIndex = Number(row.frame_index ?? row.frameIndex ?? 0)
      const timestampMs = Number(row.timestamp_ms ?? row.timestampMs ?? Math.round(frameIndex * 1000 / fps))
      const anns: TrackingFrameObject[] = []
      for (const raw of (row.objects || row.annotations || [])) {
        let objectId = Number(raw.object_id ?? raw.objectId)
        if (!Number.isFinite(objectId)) {
          while (usedObjectIds.has(fallbackObjectId)) fallbackObjectId += 1
          objectId = fallbackObjectId++
        }
        usedObjectIds.add(objectId)
        const name = numberedObjectName(raw.name ?? raw.label ?? 'rare sperm', objectId)
        const bboxPx = Array.isArray(raw.bbox) && raw.bbox.length === 4 ? raw.bbox.map(Number) : null
        const hasDims = !!(width && height)
        const bbox = bboxPx && hasDims ? {
          x: Math.max(0, Math.min(100, bboxPx[0] / (width as number) * 100)),
          y: Math.max(0, Math.min(100, bboxPx[1] / (height as number) * 100)),
          width: Math.max(0, Math.min(100, (bboxPx[2] - bboxPx[0]) / (width as number) * 100)),
          height: Math.max(0, Math.min(100, (bboxPx[3] - bboxPx[1]) / (height as number) * 100)),
        } : undefined
        anns.push({
          id: `ai-${frameIndex}-${objectId}`, objectId, name, source: 'ai',
          confidence: raw.score ?? raw.confidence,
          bbox: bboxPx ? [bboxPx[0], bboxPx[1], bboxPx[2], bboxPx[3]] : undefined,
          frameIndex, timestampMs,
          anomaly: raw.anomaly,
        })
        annotations.push(normalizeAnnotationObject({
          id: `ai-${frameIndex}-${objectId}`, objectId, name, source: 'ai',
          confidence: raw.score ?? raw.confidence, bbox, frameIndex, timestampMs,
          anomaly: raw.anomaly, anomaly_level: raw.anomaly_level,
          anomaly_reasons: raw.anomaly_reasons, anomaly_details: raw.anomaly_details,
        }, objectId))
      }
      frames.push({ frameIndex, timestampMs, annotations: anns })
    }

    const media: MediaAsset = {
      id: safeMediaId, name: videoFile.name, type: 'video', url: videoUrl,
      serverMediaId: uploaded.mediaId, serverVideoName: uploaded.videoName,
      width, height, fps, duration: uploaded.duration || (uploaded.frameCount && fps ? uploaded.frameCount / fps : undefined), frameCount: uploaded.frameCount, sizeBytes: videoFile.size,
    }
    annotationsByMedia.value[safeMediaId] = annotations
    trackingFramesByMedia.value[safeMediaId] = frames
    mediaAssets.value = [...mediaAssets.value, media]
    selectedMediaId.value = safeMediaId
    await nextTick()
    await resetAnnotationViewForMedia(safeMediaId)
    statusMessage.value = `已加载文件夹：${videoFile.name} + tracker_results.json，共 ${frames.length} 帧标注`
    showToast(`已加载 ${videoFile.name} 的 Tracking 标注`)
  }

  const openAnnotationFolderPicker = async () => {
    const picker = (window as any).showDirectoryPicker
    if (!picker) { annotationFolderInputRef.value?.click(); return }
    try {
      const dir = await picker({ mode: 'read' })
      let videoFile: File | null = null
      let trackerFile: File | null = null
      for await (const entry of (dir as any).values()) {
        if (entry.kind !== 'file') continue
        if (!videoFile && isVideoName(entry.name) && !/_overlay\.[^.]+$/i.test(entry.name)) videoFile = await entry.getFile()
        if (!trackerFile && entry.name.toLowerCase() === 'tracker_results.json') trackerFile = await entry.getFile()
      }
      if (!videoFile) throw new Error('所选文件夹中没有可识别的视频文件')
      await loadTrackerFolder(videoFile, trackerFile, String(dir.name || ''))
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      statusMessage.value = error instanceof Error ? error.message : '标注文件夹加载失败'
      showToast(statusMessage.value)
    }
  }

  const handleAnnotationFolderFiles = async (files: FileList | null) => {
    if (!files?.length) return
    const arr = Array.from(files)
    const videoFile = arr.find((f) => isVideoFile(f) && !/_overlay\.[^.]+$/i.test(f.name))
    const trackerFile = arr.find((f) => f.name.toLowerCase() === 'tracker_results.json')
    if (!videoFile) {
      statusMessage.value = '所选文件夹中没有可识别的视频文件'
      showToast(statusMessage.value)
      return
    }
    const sourceFolderName = arr.map((file) => file.webkitRelativePath).find(Boolean)?.split(/[\\/]/)[0] || ''
    try { await loadTrackerFolder(videoFile, trackerFile || null, sourceFolderName) }
    catch (error) { statusMessage.value = error instanceof Error ? error.message : '标注文件夹加载失败'; showToast(statusMessage.value) }
    if (annotationFolderInputRef.value) annotationFolderInputRef.value.value = ''
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
    await loadExactFrame(currentFrame.value, media.id)
    statusMessage.value = `当前浏览器不能直接播放 ${media.name}，已切换为原始视频逐帧预览（源文件未转换）`
  }

  const playFallbackFrames = async () => {
    const media = selectedMedia.value
    if (!media?.serverMediaId) return
    const serial = ++fallbackPlaybackSerial
    isPlaying.value = true
    const fps = Math.max(1, media.fps || videoFps.value || 30)
    const delay = Math.max(10, Math.round(1000 / fps))
    while (serial === fallbackPlaybackSerial && isPlaying.value && currentFrame.value < maxFrameIndex.value) {
      await new Promise((resolve) => setTimeout(resolve, delay))
      if (serial !== fallbackPlaybackSerial || !isPlaying.value) break
      const next = currentFrame.value + 1
      const ok = await loadExactFrame(next, media.id)
      if (!ok) break
      await loadTrackingResult(media.id, true)
    }
    if (serial === fallbackPlaybackSerial) isPlaying.value = false
  }

  const loadTrackingResult = async (mediaId: string, force = false) => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media?.serverMediaId) return
    const serial = ++trackingLoadSerial
    try {
      const result = await trackApi.getResult(media.serverMediaId)
      if (serial !== trackingLoadSerial || mediaId !== currentMediaId.value) return
      const frames = 'frames' in result ? result.frames : [result]
      // 过滤掉前端已删除的 AI tracking 对象
      const deleted = deletedTrackingIds.value[mediaId]
      const filtered = deleted && deleted.size
        ? (frames || []).map((f: any) => ({ ...f, annotations: f.annotations.filter((a: any) => !deleted.has(a.id) && !deleted.has(String(a.objectId))) }))
        : (frames || [])
      trackingFramesByMedia.value[mediaId] = filtered

      // 将 AI tracking 结果合并到 annotationsByMedia（统一管理）
      // 策略：同帧同 objectId 的新结果覆盖旧结果，手动标注永不被覆盖
      const width = media.width || 1
      const height = media.height || 1
      const existing = annotationsByMedia.value[mediaId] ?? []
      // 被手动修改过的 (frameIndex, objectId) 集合 — 精准保护, 只跳过用户改过的那一个
      const manualKeys = new Set(existing
        .filter((o) => o.source === 'manual' && o.objectId != null)
        .map((o) => `${o.frameIndex ?? 0}:${o.objectId}`))
      // 手动新建 (objectId=null) 也保留; 有 objectId 的才走 key 粒度保护
      const keyOf = (o: AnnotationObject) => `${o.frameIndex ?? 0}:${o.objectId ?? o.id}`
      const existingByKey = new Map(existing.map((o) => [keyOf(o), o]))
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
    } catch {
      // 尚未生成 tracking_result.json 时静默处理：该帧不显示 AI 框。
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
    // currentTime 只用于时间显示；跳帧期间绝不启动全局帧同步，避免和单次 seek 回调竞争。
    syncVideoFrameState()
    if (isVideo.value && Date.now() - lastTrackingPollAt > 1000) {
      lastTrackingPollAt = Date.now()
      void loadTrackingResult(currentMediaId.value, true)
    }
    if (!isSeekingVideo) scheduleVideoFrameSync()
  }

  const seekVideo = async (time: number) => {
    if (!isVideo.value) return
    stopFallbackPlayback()
    const media = selectedMedia.value
    if (!media) return

    const targetFrame = Math.max(
      0,
      Math.min(maxFrameIndex.value, Math.round(time * Math.max(videoFps.value, 1)))
    )

    // 精确逐帧模式：暂停后不再让浏览器通过 currentTime 猜测目标帧，
    // 直接向后端请求 source video 的指定 frame_index。
    const wasPlaying = isPlaying.value
    if (wasPlaying && videoRef.value) {
      videoRef.value.pause()
      isPlaying.value = false
    }

    isSeekingVideo = true
    const serial = ++seekSerial
    try {
      if (media.serverMediaId) {
        const ok = await loadExactFrame(targetFrame, media.id)
        if (serial !== seekSerial) return
        if (!ok) throw new Error(`无法读取第 ${targetFrame} 帧`)
        // 后端精确帧图像已经与 JSON 使用同一个 frame_index；同步视频元素位置仅供恢复播放使用。
        if (videoRef.value) {
          try { videoRef.value.currentTime = frameToTime(targetFrame) } catch {}
        }
        await loadTrackingResult(media.id, true)
        statusMessage.value = `已定位到第 ${targetFrame} 帧`
      } else if (videoRef.value) {
        // 没有 serverMediaId 的旧本地视频只能回退到浏览器 video。
        videoRef.value.currentTime = frameToTime(targetFrame)
        currentFrame.value = targetFrame
        currentTime.value = frameToTime(targetFrame)
        frameInput.value = targetFrame
        await loadTrackingResult(media.id, true)
      }
    } finally {
      if (serial === seekSerial) isSeekingVideo = false
    }
  }

  const seekToInputFrame = async () => {
    if (!isVideo.value) return
    const frame = Math.max(0, Math.min(maxFrameIndex.value, Math.floor(Number(frameInput.value) || 0)))
    frameInput.value = frame
    await seekVideo(frameToTime(frame))
    await loadTrackingResult(currentMediaId.value, true)
    statusMessage.value = `已跳转到第 ${frame} 帧`
  }

  const seekByFrame = async (delta: number) => {
    if (!isVideo.value) return
    // 用 currentFrame (整数帧号) 做基准, 不用 currentTime (可能有小数误差)
    const target = Math.max(0, Math.min(maxFrameIndex.value, currentFrame.value + delta))
    if (target === currentFrame.value) return
    await seekVideo(frameToTime(target))
  }

  const togglePlayback = async () => {
    if (isSeekingVideo || exactFrameLoading.value) return
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
    if (!isVideo.value || !videoDuration.value) return
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
    statusMessage.value = mediaType === 'video' ? `AI 正在处理视频第 ${currentFrame.value} 帧……` : 'AI 正在处理图片……'
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
      annotationsByMedia.value[mediaId] = [...existing, ...normalizedObjects]
      selectedObjectId.value = normalizedObjects[0]?.id ?? null
      statusMessage.value = `AI 完成：${normalizedObjects.length} 个目标${mediaType === 'video' ? '（当前帧）' : ''}`
    } catch (error) {
      if (mediaId === currentMediaId.value) statusMessage.value = error instanceof Error ? error.message : 'AI 处理失败'
    } finally {
      isAiBusy.value = false
    }
  }

  const buildCurrentFrameSam3Objects = async (mediaId: string) => {
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media) return []
    const objects = (annotationsByMedia.value[mediaId] ?? []).filter((obj) => (obj.frameIndex ?? 0) === currentFrame.value)
    if (!objects.length) throw new Error(`第 ${currentFrame.value} 帧没有人工标注`)

    // ID 完整性是断点续追的硬约束：不能在生成 seed JSON 时丢失 objectId，
    // 更不能让后端按 annotations 数组顺序重新编号。否则下一轮会出现 15→8 之类的 identity switch。
    const seenIds = new Set<number>()
    for (const obj of objects) {
      if (!Number.isInteger(obj.objectId) || (obj.objectId as number) <= 0) {
        throw new Error(`第 ${currentFrame.value} 帧存在缺少有效 objectId 的标注（${obj.name || obj.id}），请删除后重新框选`)
      }
      if (seenIds.has(obj.objectId as number)) {
        throw new Error(`第 ${currentFrame.value} 帧存在重复 objectId=${obj.objectId}，请修正后再进行 AI Tracking`)
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
        frameIndex: currentFrame.value,
        timestampMs: Math.round(currentTime.value * 1000),
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
    statusMessage.value = next === currentFrame.value ? `当前已是最后可标注帧：${next}` : `下一个未标注帧：${next}`
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

  const explainPausedObject = (item: NonNullable<Awaited<ReturnType<typeof trackApi.getStatus>>['pausedObjects']>[number]) => {
    const details = item.details ?? {}
    const reasons = item.reasons ?? []
    const type = item.type
    const displayName = objectDisplayName(item.object_id, item.display_name || item.name)
    const baselineFrame = Number(item.manualBaselineFrame ?? details.manual_baseline_frame)
    const reviewStart = Number(item.reviewStartFrame ?? details.review_start_frame)
    const reviewEnd = Number(item.reviewEndFrame ?? details.review_end_frame)
    const lookback = Number(item.reviewLookbackFrames ?? details.review_lookback_frames ?? 5)
    const reviewRange = Number.isFinite(reviewStart) && Number.isFinite(reviewEnd)
      ? `第 ${reviewStart}～${reviewEnd} 帧`
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
        title: '框相对最近人工标注明显缩小',
        summary: `${displayName} 的框相对最近人工标注明显缩小，可能已经丢失精子尾部。`,
        metrics,
        baselineFrame: Number.isFinite(baselineFrame) ? baselineFrame : undefined,
        reviewRange,
        reviewNotice,
        suggestion: '检查当前帧与前几帧；重新框住完整的精子头部和尾部后再继续追踪。',
      }
    }
    if (type === 'size_growth' || type === 'shape_change') {
      return {
        objectId: item.object_id,
        displayName,
        title: '框相对最近人工标注明显扩大或变形',
        summary: `${displayName} 的框相对最近人工标注明显扩大，可能框入了杂质或其他精子。`,
        metrics,
        baselineFrame: Number.isFinite(baselineFrame) ? baselineFrame : undefined,
        reviewRange,
        reviewNotice,
        suggestion: '将框修正为只覆盖当前精子；若对象已分离，请分别确认框和 ID 后再续追。',
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
  const persistCurrentFrameManualAnnotations = async (media: MediaAsset, mediaId: string) => {
    const current = (annotationsByMedia.value[mediaId] ?? []).filter(
      (obj) => (obj.frameIndex ?? 0) === currentFrame.value && obj.source === 'manual'
    )
    if (!current.length) return { count: 0, batchId: undefined as string | undefined }

    const pixel = await getMediaPixelSize(media).catch(() => null)
    const canonicalMediaId = media.serverMediaId || mediaId
    const result = await api.saveManualAnnotation({
      mediaId: canonicalMediaId,
      mediaType: media.type,
      mediaName: media.name,
      mediaWidth: pixel?.width ?? media.width,
      mediaHeight: pixel?.height ?? media.height,
      frameIndex: media.type === 'video' ? currentFrame.value : undefined,
      timestampMs: media.type === 'video' ? Math.round(currentTime.value * 1000) : undefined,
      objects: JSON.parse(JSON.stringify(current)),
      annotationVersion: 'annotation-v8-ai-tracking-autosave',
    })

    // 让标注结果页/工作区本地结果同步显示本次自动入库批次。
    const savedResult = {
      mediaId: canonicalMediaId,
      mediaName: media.name,
      mediaType: media.type,
      frameIndex: media.type === 'video' ? currentFrame.value : undefined,
      timestampMs: media.type === 'video' ? Math.round(currentTime.value * 1000) : undefined,
      savedAt: new Date().toISOString(),
      filename: '',
      batchId: result.batchId,
      objects: JSON.parse(JSON.stringify(current)),
    } as SavedAnnotationFile
    savedResults.value = [
      savedResult,
      ...savedResults.value.filter((item) => !(
        item.mediaId === canonicalMediaId
        && (item.frameIndex ?? 0) === currentFrame.value
        && item.username === savedResult.username
      )),
    ]

    return { count: current.length, batchId: result.batchId }
  }

  const runAiTrack = async () => {
    const mediaId = currentMediaId.value
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    if (!media || media.type !== 'video' || !media.serverMediaId) {
      statusMessage.value = '请先上传视频'
      return
    }

    const startFrame = currentFrame.value
    let seed: Record<string, unknown>[]
    const confirmedPausedIds = new Set(
      lastPausedContext.value?.mediaId === mediaId && lastPausedContext.value.frameIndex === startFrame
        ? pausedAnomalies.value.map((item) => item.objectId)
        : [],
    )
    if (confirmedPausedIds.size > 0) {
      const existing = annotationsByMedia.value[mediaId] ?? []
      annotationsByMedia.value = {
        ...annotationsByMedia.value,
        [mediaId]: existing.map((obj) =>
          (obj.frameIndex ?? 0) === startFrame && obj.objectId != null && confirmedPausedIds.has(obj.objectId) && obj.bbox
            ? { ...obj, source: 'manual' as const }
            : obj,
        ),
      }
      // A direct retry is the user's confirmation that the paused boxes are
      // valid. saveFrameAnnotations will persist them as the newest baseline.
      lastPausedContext.value = null
    }
    anomalyObjectIds.value = []
    pausedAnomalies.value = []
    anomalyPanelVisible.value = false
    isAiBusy.value = true

    try {
      // 固化完整工作区（人工框、稳定 ID/名称、人工基准和异常状态），
      // 确保后端本轮 Tracking 与以后重新加载使用同一份人工基准。
      await persistWorkspaceState(mediaId)
      // ① AI Tracking 点击即自动落库：当前帧人工标注先进入数据库，
      // 然后才写 seed JSON / 启动 SAM3。任何一步失败都不会“假保存”。
      statusMessage.value = `① 正在将第 ${startFrame} 帧人工标注写入数据库……`
      const persisted = await persistCurrentFrameManualAnnotations(media, mediaId)
      if (confirmedPausedIds.size > 0) {
        showToast(`已将 ${confirmedPausedIds.size} 个异常框确认为新的人工基准`)
      } else if (persisted.count) {
        showToast(`第 ${startFrame} 帧 ${persisted.count} 个人工标注已写入数据库`)
      }

      // 关键：如果这是对旧 Tracking 结果的人工回退/修正，从当前帧重新开分支。
      // 服务端只删除 tracker_results.json 中 frame > startFrame 的旧结果。
      // 当前帧保留，作为新的分支锚点；其上的 AI 框和人工修改/新增框会一起成为新 seed。
      statusMessage.value = `①b 正在从第 ${startFrame} 帧切断旧 Tracking 未来分支……`
      const rewind = await trackApi.rewind({
        mediaId: media.serverMediaId,
        startFrame,
      })
      const existingAfterRewind = annotationsByMedia.value[mediaId] ?? []
      // 当前帧 N 是新的分支锚点：保留此前所有结果以及 N 帧现有 AI/人工框。
      // 只有 N 之后的旧未来轨迹被清掉；随后 buildCurrentFrameSam3Objects() 会
      // 把 N 帧当前页面上最终存在的全部 bbox（旧 AI + 人工新增/修改）作为新 seed。
      annotationsByMedia.value = {
        ...annotationsByMedia.value,
        [mediaId]: existingAfterRewind.filter((obj) => (obj.frameIndex ?? 0) <= startFrame),
      }
      trackingFramesByMedia.value[mediaId] = (trackingFramesByMedia.value[mediaId] ?? [])
        .filter((frame) => frame.frameIndex <= startFrame)
      console.info(
        `[ai-track] rewind branch at frame=${startFrame}: removedRows=${rewind.removedRows}, ` +
        `deletedFutureSeedFiles=${rewind.deletedFutureSeedFiles}`
      )

      seed = await buildCurrentFrameSam3Objects(mediaId)
      if (!seed.length) {
        throw new Error(`第 ${startFrame} 帧没有人工 bbox 标注`)
      }
      // 不能直接使用 media.width || 0：在 AVI 浏览器播放降级为逐帧预览时，
      // 尺寸来自后端上传元数据。统一从该函数获取，保证 seed 与 Tracking
      // 请求使用同一套像素坐标系。
      const pixelSize = await getMediaPixelSize(media)

      // Phase 2: 把“当前页面正在看的这一帧”固化成后端 JSON seed。
      statusMessage.value = `② 正在生成第 ${startFrame} 帧标注 JSON……`
      await trackApi.saveFrameAnnotations({
        mediaId: media.serverMediaId,
        mediaName: media.name,
        mediaWidth: pixelSize.width,
        mediaHeight: pixelSize.height,
        frameIndex: startFrame,
        timestampMs: Math.round(currentTime.value * 1000),
        annotations: seed,
      })

      // 后端以 SAM3_TRACK_FRAMES（包含 seed 帧）和剩余视频帧数为上限。
      statusMessage.value = `③ SAM3 将从第 ${startFrame} 帧持续向后追踪，直到异常、单轮上限或视频末尾……`
      const task = await trackApi.run({
        mediaId: media.serverMediaId,
        mediaName: media.name,
        mediaWidth: pixelSize.width,
        mediaHeight: pixelSize.height,
        startFrame,
        annotations: seed,
      })

      trackingFrameCount.value = task.maxFrames || task.trackFrames || 1
      statusMessage.value =
        `SAM3 正在运行：处理第 ${startFrame}～${Math.min(maxFrameIndex.value, startFrame + trackingFrameCount.value - 1)} 帧`

      for (;;) {
        const status = await trackApi.getStatus(task.taskId)

        if (status.status === 'success') break
        if (status.status === 'failed') {
          throw new Error(status.message || 'SAM3 Tracking 失败')
        }
        if (status.status === 'paused' && status.paused) {
          const pauseFrame = status.pausedFrame ?? startFrame
          await loadTrackingResult(mediaId, true)

          if (mediaId === currentMediaId.value) {
            await seekVideo(frameToTime(pauseFrame))
            const levels = status.anomalyLevels || {}
            anomalyObjectIds.value = Object.entries(levels)
              .filter(([, v]) => v !== 'normal')
              .map(([k]) => Number(k))
            const pausedObjs = status.pausedObjects || []
            pausedAnomalies.value = pausedObjs.map(explainPausedObject)
            lastPausedContext.value = { mediaId, frameIndex: pauseFrame }
            anomalyPanelVisible.value = true
            const reasons = pausedAnomalies.value.map((item) => `${item.displayName}：${item.title}`)
            const level = Object.values(levels).find((v) => v && v !== 'normal') || 'anomaly'
            anomalyFrames.value = [
              ...anomalyFrames.value,
              { frame_index: pauseFrame, level, reasons },
            ]
            statusMessage.value = `⚠️ Tracking 暂停 @ frame ${pauseFrame}: ${reasons.join('; ')}`
            showToast(`检测到异常，已暂停在第 ${pauseFrame} 帧`)
          }
          return
        }
        await new Promise((resolve) => setTimeout(resolve, 700))
      }

      await loadTrackingResult(mediaId, true)

      if (mediaId === currentMediaId.value) {
        const finalStatus = await trackApi.getStatus(task.taskId)
        const lastProcessedFrame = finalStatus.lastProcessedFrame ?? startFrame
        await seekVideo(frameToTime(lastProcessedFrame))
        if (finalStatus.reachedVideoEnd || lastProcessedFrame >= maxFrameIndex.value) {
          statusMessage.value = `完成：已追踪到视频末尾第 ${lastProcessedFrame} 帧。`
          showToast('已追踪到视频末尾')
        } else {
          statusMessage.value = `完成：SAM3 已追踪 ${startFrame}～${lastProcessedFrame}。请检查/修正最终帧后再次点击 AI Tracking 继续。`
          showToast(`已到达单轮上限第 ${lastProcessedFrame} 帧，可检查后继续`)
        }
      }
    } catch (error) {
      if (mediaId === currentMediaId.value) {
        statusMessage.value = error instanceof Error ? error.message : 'AI Tracking 失败'
        showToast(statusMessage.value)
      }
    } finally {
      isAiBusy.value = false
    }
  }

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
        statusMessage.value = `正在保存第 ${frameIndex} 帧 annotations.json……`
        const result = await trackApi.saveFrameAnnotations({
          mediaId: media.serverMediaId,
          mediaName: media.name,
          mediaWidth: width,
          mediaHeight: height,
          frameIndex,
          timestampMs,
          annotations,
        })
        statusMessage.value = `第 ${frameIndex} 帧 JSON 已写入 ${result.filename}`
        showToast(`第 ${frameIndex} 帧 JSON 已保存到视频目录`)
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


  /**
   * 导出训练数据集（COCO / YOLO / both）
   * 接收格式选择 + 划分设置，从 annotationsByMedia 中提取当前素材所有帧标注发给后端，
   * 后端抽视频帧、转格式、打包 zip 返回。
   */
  const exportDataset = async (opts: {
    format: 'coco' | 'yolo' | 'both'
    splitRatio: number   // 0 = 不划分，0.8 = 80/20
  }) => {
    const mediaId = currentMediaId.value
    const media = mediaAssets.value.find((item) => item.id === mediaId)
    const objects = annotationsByMedia.value[mediaId] ?? []

    if (!media) { showToast('请先选择素材'); return }
    if (!objects.length) { showToast('当前素材没有标注可导出'); return }

    const pixel = await getMediaPixelSize(media).catch(() => null)
    const allFrames = new Set(objects.map((o: any) => o.frameIndex ?? 0))
    const allNames = Array.from(new Set(objects.map((o: any) => o.name).filter(Boolean))) as string[]

    isAiBusy.value = true
    statusMessage.value = `正在导出数据集（${allFrames.size} 帧 × ${allNames.length} 类别）...`

    try {
      const blob = await apiExportDataset({
        mediaId,
        mediaType: media.type,
        mediaName: media.name,
        mediaWidth: pixel?.width ?? media.width,
        mediaHeight: pixel?.height ?? media.height,
        format: opts.format,
        splitRatio: opts.splitRatio,
        classNames: allNames,
        annotations: JSON.parse(JSON.stringify(objects)),
      })

      // 下载 zip
      const filename = `${media.name}_dataset.zip`
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      setTimeout(() => URL.revokeObjectURL(url), 2000)

      const size_mb = (blob.size / 1024 / 1024).toFixed(2)
      statusMessage.value = `✅ 数据集已导出：${filename} (${size_mb} MB, ${allFrames.size} 帧)`
      showToast(`导出成功：${filename}`)
    } catch (error: any) {
      statusMessage.value = `导出失败：${error?.message ?? '未知错误'}`
      showToast(statusMessage.value)
    } finally {
      isAiBusy.value = false
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

  const resetAnnotationViewForMedia = async (mediaId = currentMediaId.value) => {
    selectedObjectId.value = null
    currentFrame.value = 0
    currentTime.value = 0
    frameInput.value = 0
    tempBbox.value = null
    bboxStart = null
    activeTool.value = 'select'
    anomalyObjectIds.value = []
    anomalyFrames.value = []
    pausedAnomalies.value = []
    lastPausedContext.value = null
    zoom.value = 1
    videoPlaybackFallback.value = false
    stopFallbackPlayback()
    if (isVideo.value && mediaId === currentMediaId.value) {
      // 后端上传元数据在 <video> loadedmetadata/error 之前已可用。先采用
      // 它，避免不同 FPS 的 AVI 逐帧预览仍沿用默认 30 FPS 计算时间轴。
      const media = mediaAssets.value.find((item) => item.id === mediaId)
      if (media?.fps && media.fps > 0) videoFps.value = media.fps
      if (media?.frameCount && media.fps) videoDuration.value = media.frameCount / media.fps
      const restoredFrame = await restoreWorkspaceState(mediaId)
      const targetFrame = restoredFrame ?? 0
      await nextTick()
      revokeExactFrameUrl()
      const video = videoRef.value
      if (video) {
        try { video.pause(); video.currentTime = frameToTime(targetFrame) } catch {}
      }
      await loadExactFrame(targetFrame, mediaId)
      await loadTrackingResult(mediaId, true)
    }
  }

  /**
   * 启动时从后端加载已有视频素材，刷新页面后素材列表不丢失。
   * 关键修复：通过 serverMediaId 匹配已持久化（localStorage）的素材，
   * 复用原 id（如 local-xxx），这样 annotationsByMedia 的 key 才能对上。
   */
  const loadServerMedia = async () => {
    try {
      const res = await trackApi.listMedia()
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
    } catch {
      // 后端未启动时静默处理
    }
  }

  return {
    api, mediaAssets, selectedMediaId, activeTool, objectNameInput, selectedObjectId, currentFrame, currentTime, videoDuration, videoFps, frameInput, isPlaying, isAiBusy, trackingFrameCount, statusMessage, toastMessage, showToast, zoom, zoomIn, zoomOut, zoomReset, closeMedia, savedResults, loadedRemoteResultKeys, effectResults, selectedEffectId, effectTime, effectPlaying, effectVideoRef, imageRef, videoRef, exactFrameImageRef, exactFrameUrl, exactFrameLoading, videoPlaybackFallback, annotationHitRef, fileInputRef, videoInputRef, annotationFolderInputRef, effectFolderInputRef, annotationsByMedia, trackingFramesByMedia, anomalyObjectIds, anomalyFrames, pausedAnomalies, anomalyPanelVisible, closeAnomalyPanel, selectedMedia, isVideo, maxFrameIndex, currentMediaId, currentObjects, selectedObject, selectedEffect, formatTime, timeToFrame, frameToTime, getStagePoint, addObject, resetVideoViewToFirstFrame, ensureVideoFirstFrame, selectTool, onStageClick, tempBbox, onBboxDown, onBboxMove, onBboxUp, onObjectDropdownChange, selectObject, removeObject, renameObject, undo, redo, copyPreviousFrame, brightness, contrast, mediaFilterStyle, resetMediaFilter, annotatedFrameCount, clearSelection, openFilePicker, handleFiles, openAnnotationFolderPicker, handleAnnotationFolderFiles, onImageLoaded, onVideoLoaded, onVideoTimeUpdate, onVideoError, loadTrackingResult, seekVideo, seekToInputFrame, seekByFrame, togglePlayback, onVideoEnded, onTimelineClick, runAiSegment, runAiTrack, getMediaPixelSize, buildSam3AnnotationsJson, generateAnnotationsJson, exportDataset, loadSavedResults, openEffectFolderPicker, handleEffectFolder, loadEffects, onEffectTimeUpdate, toggleEffectPlayback, selectEffect, effectOverlayObjects, resetAnnotationViewForMedia, loadServerMedia
  }
}

const workspace = createWorkspace()
export const useWorkspace = () => workspace
