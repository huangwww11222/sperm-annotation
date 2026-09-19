from pathlib import Path
p=Path('frontend/src/stores/workspace.ts');s=p.read_text()
s=s.replace("import { computed, nextTick, ref, watch } from 'vue'", "import { computed, nextTick, ref, watch } from 'vue'\nimport { FrameCache } from '../annotation/frameCache'\nimport { hitHandle, moveBox, resizeBox, type Box } from '../annotation/geometry'")
s=s.replace("const activeTool = ref<AnnotationTool>('point')", "const activeTool = ref<AnnotationTool>('bbox')")
s=s.replace('const zoomReset = () => { zoom.value = 1 }', 'const zoomReset = () => { if (!isInteracting()) zoom.value = 1 }')
a=s.index('  const undoStack: string[] = []');b=s.index('  // SAM3',a)
s=s[:a]+'''  type HistoryState = { objects: AnnotationObject[]; deleted: string[] }
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
  })
  const snapshotUndo = () => {
    const h = history(); h.undo.push(captureHistory()); h.redo.length = 0
    if (h.undo.length > 50) h.undo.shift()
    historyRevision.value++
  }
  const canUndo = computed(() => { void historyRevision.value; return history().undo.length > 0 })
  const canRedo = computed(() => { void historyRevision.value; return history().redo.length > 0 })
  const restoreHistory = (state: HistoryState) => {
    const mid = currentMediaId.value, fi = currentFrame.value
    const rest = (annotationsByMedia.value[mid] ?? []).filter(o => (o.frameIndex ?? 0) !== fi)
    annotationsByMedia.value = { ...annotationsByMedia.value, [mid]: [...rest, ...state.objects] }
    deletedTrackingIds.value[mid] = new Set(state.deleted)
    if (!state.objects.some(o => o.id === selectedObjectId.value)) selectedObjectId.value = null
    historyRevision.value++; scheduleWorkspaceStateSave(mid)
  }
  const undo = () => {
    if (editingBlocked.value || isInteracting()) return
    const h = history(); if (!h.undo.length) return
    h.redo.push(captureHistory()); restoreHistory(h.undo.pop()!); statusMessage.value = '已撤销本帧操作'
  }
  const redo = () => {
    if (editingBlocked.value || isInteracting()) return
    const h = history(); if (!h.redo.length) return
    h.undo.push(captureHistory()); restoreHistory(h.redo.pop()!); statusMessage.value = '已重做本帧操作'
  }

'''+s[b:]
a=s.index('  const currentObjects = computed(');b=s.index('  const selectedObject =',a)
s=s[:a]+'''  // Index membership only when annotations change; ordinary frame navigation does not scan the whole video.
  const objectsByFrame = computed(() => {
    const index = new Map<number, AnnotationObject[]>()
    for (const obj of annotationsByMedia.value[currentMediaId.value] ?? []) {
      const fi = obj.frameIndex ?? 0
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
  const editingBlocked = computed(() => isAiBusy.value || exactFrameLoading.value || isPlaying.value || workspaceRestoring.value)
  const workspaceRestoring = ref(false)
'''+s[b:]
s=s.replace("if (isAiBusy.value) { showToast('SAM3 Tracking 运行中，暂时禁止人工标注'); return }", "if (editingBlocked.value) return")
a=s.index('  const loadExactFrame = async');b=s.index('  const resetVideoViewToFirstFrame',a)
s=s[:a]+'''  const frameCache = new FrameCache()
  const frameError = ref('')
  const loadExactFrame = async (frameIndex: number, mediaId = currentMediaId.value) => {
    const media = mediaAssets.value.find(item => item.id === mediaId)
    if (!media || media.type !== 'video' || !media.serverMediaId) return false
    const serial = ++exactFrameRequestSerial, started = performance.now()
    exactFrameLoading.value = true; frameError.value = ''
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
      if (serial === exactFrameRequestSerial && mediaId === currentMediaId.value) {
        frameError.value = `第 ${frameIndex + 1} 帧读取失败，请重试`; statusMessage.value = frameError.value
        console.error('[annotation.frame_failed]', { mediaId, frameIndex, error })
      }
      return false
    } finally {
      if (url) URL.revokeObjectURL(url)
      if (serial === exactFrameRequestSerial) exactFrameLoading.value = false
    }
  }

'''+s[b:]
s=s.replace('    if (isAiBusy.value) return\n    // 如果本次', '    if (editingBlocked.value) return\n    // 如果本次')
a=s.index('  const HANDLE_HIT');b=s.index('  const onObjectDropdownChange',a)
s=s[:a]+'''  const pointInBbox = (px: number, py: number, b: Box) => px >= b.x && px <= b.x+b.width && py >= b.y && py <= b.y+b.height
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

'''+s[b:]
s=s.replace("if (isAiBusy.value) { showToast('SAM3 Tracking 运行中，暂时禁止修改标注'); return }", 'if (editingBlocked.value || isInteracting()) return')
s=s.replace("if (isAiBusy.value) { showToast('SAM3 Tracking 运行中'); return }", 'if (editingBlocked.value || isInteracting()) return')
s=s.replace("const prevObjs = allObjs.filter((o) => (o.frameIndex ?? 0) === prevFrame)", "const present = new Set(currentObjects.value.map(o => o.objectId ?? o.id))\n    const prevObjs = allObjs.filter(o => (o.frameIndex ?? 0) === prevFrame && !present.has(o.objectId ?? o.id))")
s=s.replace('第 ${prevFrame} 帧没有标注', '上一帧没有可补充的对象')
s=s.replace('const newObjs = prevObjs.map((o) => ({\n      ...o,', 'const newObjs = prevObjs.map((o) => ({\n      ...JSON.parse(JSON.stringify(o)),')
s=s.replace('已从第 ${prevFrame} 帧复制', '已从上一帧补充')
# Remove repeated whole-result requests during navigation; keep initial load and explicit tracking updates.
s=s.replace('      await loadTrackingResult(media.id, true)\n    }\n    if (serial === fallbackPlaybackSerial)', '    }\n    if (serial === fallbackPlaybackSerial)')
s=s.replace('  let lastTrackingPollAt = 0\n', '')
a=s.index('  const onVideoTimeUpdate =');b=s.index('  const seekVideo',a)
s=s[:a]+'''  const onVideoTimeUpdate = () => {
    if (isSeekingVideo || !isPlaying.value) return
    syncVideoFrameState(); scheduleVideoFrameSync()
  }
  const requestedFrame = ref<number | null>(null)
'''+s[b:]
s=s.replace("    if (!isVideo.value) return\n    stopFallbackPlayback()\n    const media", "    if (!isVideo.value || isAiBusy.value || isInteracting()) return\n    videoRef.value?.pause()\n    stopFallbackPlayback()\n    const media",1)
s=s.replace('    const serial = ++seekSerial\n    try', '    const serial = ++seekSerial\n    requestedFrame.value = targetFrame\n    try')
s=s.replace('        if (!ok) throw new Error(`无法读取第 ${targetFrame} 帧`)', '        if (!ok) return')
s=s.replace('        await loadTrackingResult(media.id, true)\n', '')
s=s.replace("if (serial === seekSerial) isSeekingVideo = false", "if (serial === seekSerial) { isSeekingVideo = false; requestedFrame.value = null }")
s=s.replace('    await loadTrackingResult(currentMediaId.value, true)\n    statusMessage.value = `已跳转到第 ${frame} 帧`', '')
s=s.replace('currentFrame.value + delta))', '(requestedFrame.value ?? currentFrame.value) + delta))')
s=s.replace('if (target === currentFrame.value) return', 'if (target === currentFrame.value && requestedFrame.value === null) return')
s=s.replace('    if (isSeekingVideo || exactFrameLoading.value) return', '    if (isSeekingVideo || exactFrameLoading.value || isAiBusy.value || isInteracting()) return')
s=s.replace('    } catch {\n      // 尚未生成 tracking_result.json 时静默处理：该帧不显示 AI 框。\n    }', "    } catch (error) {\n      console.warn('[annotation.tracking_load_failed]', { mediaId, error })\n    }")
# Invalidate asynchronous work on asset switches; only current media may restore global editor state.
s=s.replace('      if (!state.exists) return null', '      if (!state.exists || mediaId !== currentMediaId.value) return null')
s=s.replace('  const resetAnnotationViewForMedia = async (mediaId = currentMediaId.value) => {\n', '''  let restoreSerial = 0
  const resetAnnotationViewForMedia = async (mediaId = currentMediaId.value) => {
    if (mediaId !== currentMediaId.value) return
    const serial = ++restoreSerial
    cancelAnnotationGesture(); frameCache.clear(); exactFrameRequestSerial++; seekSerial++
    exactFrameLoading.value = false; requestedFrame.value = null; frameError.value = ''
    workspaceRestoring.value = true
    revokeExactFrameUrl()
''')
s=s.replace("    activeTool.value = 'select'\n", "    activeTool.value = 'bbox'\n")
s=s.replace('      const targetFrame = restoredFrame ?? 0', '      if (serial !== restoreSerial || mediaId !== currentMediaId.value) return\n      const targetFrame = restoredFrame ?? 0')
s=s.replace('      await loadTrackingResult(mediaId, true)\n    }\n  }\n\n  /**', '      await loadTrackingResult(mediaId, true)\n    }\n    if (serial === restoreSerial) workspaceRestoring.value = false\n  }\n\n  /**')
s=s.replace('    persistWorkspaceState,\n', '    persistWorkspaceState, displayObjects, editingBlocked, workspaceRestoring, frameError, loadExactFrame, nudgeSelected, cancelAnnotationGesture, canUndo, canRedo,\n')
p.write_text(s)
