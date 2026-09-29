<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import SendToReview from '../components/SendToReview.vue'
import AppIcon from '../components/AppIcon.vue'
import AnnotationOverlay from '../components/AnnotationOverlay.vue'
import AnnotationTrackingFeedback from '../components/AnnotationTrackingFeedback.vue'
import UserGuide from '../components/UserGuide.vue'
import userGuide from '../help/user-guide.md?raw'
import { useWorkspace } from '../stores/workspace'
import { addLeaveGuard } from '../router'
import '../annotation/annotation.css'
const {
  mediaAssets, selectedMediaId, activeTool, objectNameInput, selectedObjectId, currentFrame, currentTime, videoDuration, videoFps, frameInput, isPlaying,
  isAiBusy, statusMessage, toastMessage, imageRef, videoRef, exactFrameImageRef, exactFrameUrl, exactFrameLoading, videoPlaybackFallback, annotationHitRef, fileInputRef, videoInputRef, annotationFolderInputRef,
  annotationsByMedia, selectedMedia, isVideo, maxFrameIndex, currentMediaId, currentObjects, selectedObject, displayObjects, editingBlocked, workspaceRestoring,
  anomalyFrames, pausedAnomalies, trackingPausedFrame, formatTime, selectTool, onStageClick, tempBbox, onBboxDown, onBboxMove, onBboxUp,
  selectObject, removeObject, renameObject, undo, redo, canUndo, canRedo, copyPreviousFrame, brightness, contrast, mediaFilterStyle, resetMediaFilter, annotatedFrameCount,
  clearSelection, openFilePicker, handleFiles, onImageLoaded, onVideoLoaded, onVideoTimeUpdate, onVideoError, seekToInputFrame, togglePlayback, onVideoEnded,
  onTimelineClick, runAiTrack, resetAnnotationViewForMedia, seekByFrame, zoom, zoomIn, zoomOut, zoomReset, closeMedia, openAnnotationFolderPicker, handleAnnotationFolderFiles,
  deleteMedia, deletingMediaId, mediaDeleteError, nudgeSelected, cancelAnnotationGesture, frameError, retryExactFrame, saveState, saveError, persistWorkspaceState, playbackRate, pausePlayback,
  getObjectDeletionSummary, removeObjectAcrossVideo, undoVideoObjectDeletion, canUndoVideoDeletion, lastVideoObjectDeletion,
  objectDeletionBusy, objectDeletionError, objectDeletionPendingAction, retryObjectDeletion,
  confirmTrackingAnomaly, trackingFeedbackBusy, trackingFeedbackError, trackingFeedbackPending, trackingFeedbackPendingAction, retryTrackingAnomalyFeedback,
  trackingCalibrationSummary, resetTrackingCalibration, trackingWarningSummary,
} = useWorkspace()
const scrollContainerRef = ref<HTMLDivElement | null>(null), stageRef = ref<HTMLDivElement | null>(null), loupe = ref<HTMLCanvasElement | null>(null)
const help = ref(false), helpDialog = ref<HTMLElement | null>(null), focused = ref(false), labels = ref(true), hiddenBoxes = ref(false), magnifier = ref(false), inverted = ref(false), enhancing = ref(false), spaceHeld = ref(false)
const mediaBusy = computed(() => isAiBusy.value || !!deletingMediaId.value || objectDeletionBusy.value || trackingFeedbackBusy.value || !!objectDeletionPendingAction.value || trackingFeedbackPending.value)
const search = ref(''), objectSearch = ref(''), jumpFrame = ref(1)
const size = ref({ w: 0, h: 0 }), base = ref({ w: 800, h: 450 })
const mousePixel = ref<{ x: number; y: number } | null>(null)
const stageSize = computed(() => ({ w: Math.round(base.value.w * zoom.value), h: Math.round(base.value.h * zoom.value) }))
const visibleMedia = computed(() => mediaAssets.value.filter(m => m.name.toLowerCase().includes(search.value.toLowerCase())))
const visibleObjects = computed(() => currentObjects.value.filter(o => o.name.toLowerCase().includes(objectSearch.value.toLowerCase()) || String(o.objectId).includes(objectSearch.value.trim().replace(/^#/,''))))
const filter = computed(() => `${mediaFilterStyle.value} ${inverted.value ? 'invert(1)' : ''}`)
const saveLabel = computed(() => selectedMedia.value?.serverMediaId ? ({ idle:'自动保存', saving:'保存中…', saved:'已保存', error:'保存失败' }[saveState.value]) : '本机自动保存')
// Shortcut help and the full guide share the same maintained Markdown source.
const shortcutSection = userGuide.split(/^## 快捷键速查\s*$/m)[1] || ''
const shortcutHelpNote = shortcutSection.trim().split(/\n\s*\n/)[0] || ''
const annotationShortcuts = (shortcutSection.match(/人工标注\s*\n\s*(\| 快捷键[\s\S]*?)(?=\n\s*审查模式)/)?.[1] || '')
  .trim().split('\n').slice(2).filter(row => row.startsWith('|')).map(row => row.split('|').slice(1,-1).map(cell => cell.trim()))
const selectedObjectCoverage = computed(() => {
  const id = selectedObject.value?.objectId
  if (id == null) return 0
  return new Set((annotationsByMedia.value[currentMediaId.value] || []).filter(object => object.objectId === id).map(object => object.frameIndex ?? 0)).size
})
const unresolvedAnomalies = computed(() => pausedAnomalies.value.filter(item => !item.resolved))
const feedbackTargetId = ref<number | null>(null), learnNormalMotion = ref(true)
const feedbackTarget = computed(() => {
  const pendingId = trackingFeedbackPendingAction.value?.objectId
  if (pendingId != null) return pausedAnomalies.value.find(item => item.objectId === pendingId) || null
  return unresolvedAnomalies.value.find(item => item.objectId === feedbackTargetId.value)
    || unresolvedAnomalies.value.find(item => item.objectId === selectedObject.value?.objectId)
    || unresolvedAnomalies.value[0] || null
})
const hasPausedTracking = computed(() => trackingPausedFrame.value != null && pausedAnomalies.value.length > 0)
const feedbackBlocked = computed(() => isAiBusy.value || !!deletingMediaId.value || exactFrameLoading.value || isPlaying.value || workspaceRestoring.value || !!frameError.value || objectDeletionBusy.value || trackingFeedbackBusy.value || (editingBlocked.value && !trackingFeedbackPending.value))
const deleteDialog = ref<HTMLDialogElement | null>(null), deletionDialogOpen = ref(false)
type DeletionSummary = Awaited<ReturnType<typeof getObjectDeletionSummary>>
const deletionSummary = ref<DeletionSummary | null>(null), deletionSummaryLoading = ref(false), deletionSummaryError = ref('')
const deletionTarget = ref<{objectId:number;name:string;mediaId:string;mediaName:string} | null>(null)
const frameDeletion = ref<{id:string;objectId?:number;name:string;mediaId:string;frameIndex:number;fingerprint:string} | null>(null)
let deletionSummarySerial = 0, deletionPriorFocus: HTMLElement | null = null, deletionPriorOverflow = ''
const frameFingerprint = () => JSON.stringify(currentObjects.value)
const frameDeletionVisible = computed(() => !!frameDeletion.value && frameDeletion.value.mediaId===currentMediaId.value && frameDeletion.value.frameIndex===currentFrame.value && frameDeletion.value.fingerprint===frameFingerprint())
const videoDeletionVisible = computed(() => !!lastVideoObjectDeletion.value && !(annotationsByMedia.value[currentMediaId.value] || []).some(object => object.objectId===lastVideoObjectDeletion.value?.objectId))
function deleteSelectedFrame() {
  const object = selectedObject.value
  if (!object || editingBlocked.value || objectDeletionBusy.value || trackingFeedbackPending.value || objectDeletionPendingAction.value) return
  removeObject(object.id)
  if (!currentObjects.value.some(item => item.id === object.id)) {
    clearSelection()
    frameDeletion.value = { id:object.id, objectId:object.objectId, name:object.name, mediaId:currentMediaId.value, frameIndex:currentFrame.value, fingerprint:frameFingerprint() }
  }
}
function undoFrameDeletion() { undo(); frameDeletion.value = null }
function selectFeedbackObject(objectId:number) {
  if (trackingFeedbackPending.value || feedbackBlocked.value) return
  feedbackTargetId.value = objectId
  const object = currentObjects.value.find(item => item.objectId === objectId)
  if (object) selectObject(object.id)
}
async function confirmFeedback(decision:'normal'|'corrected') {
  const target = feedbackTarget.value
  if (!target || feedbackBlocked.value || trackingFeedbackPending.value || currentFrame.value!==trackingPausedFrame.value) return
  await confirmTrackingAnomaly(target.objectId, decision, decision==='normal' && target.canLearn && learnNormalMotion.value)
}
async function returnToPausedFrame() {
  if (trackingPausedFrame.value==null || isAiBusy.value || trackingFeedbackBusy.value) return
  frameInput.value = trackingPausedFrame.value
  await seekToInputFrame()
}
function finishDeleteDialog() {
  deletionSummarySerial++
  deletionDialogOpen.value = false
  deletionSummaryLoading.value = false
  document.body.style.overflow = deletionPriorOverflow
  if (deletionPriorFocus?.isConnected && !(deletionPriorFocus as HTMLButtonElement).disabled) deletionPriorFocus.focus({preventScroll:true})
  else focusButton.value?.focus({preventScroll:true})
}
function closeDeleteDialog() { if (!objectDeletionBusy.value) deleteDialog.value?.close() }
async function loadDeletionSummary() {
  const target = deletionTarget.value
  if (!target || target.mediaId!==currentMediaId.value) return
  const serial = ++deletionSummarySerial
  deletionSummaryLoading.value = true; deletionSummaryError.value = ''; deletionSummary.value = null
  try {
    const result = await getObjectDeletionSummary(target.objectId)
    if (serial===deletionSummarySerial && deleteDialog.value?.open && target.mediaId===currentMediaId.value) deletionSummary.value = result
  } catch (error) {
    if (serial===deletionSummarySerial) deletionSummaryError.value = error instanceof Error ? error.message : '读取删除范围失败，请重试'
  } finally { if (serial===deletionSummarySerial) deletionSummaryLoading.value = false }
}
async function openVideoDeletion() {
  const pending = objectDeletionPendingAction.value
  if (objectDeletionBusy.value || trackingFeedbackBusy.value || trackingFeedbackPending.value || isAiBusy.value || (editingBlocked.value && !pending)) return
  if (pending && pending.action!=='delete') return
  if (!pending) {
    const object = selectedObject.value
    if (!object || !isVideo.value || !Number.isInteger(object.objectId)) return
    deletionTarget.value = {objectId:object.objectId!,name:object.name,mediaId:currentMediaId.value,mediaName:selectedMedia.value?.name || ''}
  } else if (!deletionTarget.value || deletionTarget.value.objectId!==pending.objectId) {
    deletionTarget.value = {objectId:pending.objectId,name:`对象 #${pending.objectId}`,mediaId:currentMediaId.value,mediaName:selectedMedia.value?.name || ''}
  }
  cancelPointer(); pausePlayback()
  deletionPriorFocus = document.activeElement as HTMLElement
  deletionPriorOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  await nextTick(); deleteDialog.value?.showModal(); deletionDialogOpen.value = true
  if (!pending) await loadDeletionSummary()
}
async function confirmVideoDeletion() {
  const target = deletionTarget.value
  if (!target || objectDeletionBusy.value || target.mediaId!==currentMediaId.value) return
  const pending = objectDeletionPendingAction.value
  if (!pending && (!deletionSummary.value || deletionSummary.value.frameCount===0 || deletionSummaryLoading.value)) return
  const success = pending ? await retryObjectDeletion() : await removeObjectAcrossVideo(target.objectId)
  if (success) { clearSelection(); frameDeletion.value = null; deleteDialog.value?.close() }
}
async function undoVideoDeletion() { if (await undoVideoObjectDeletion()) frameDeletion.value = null }
function deletionDialogKeys(event:KeyboardEvent) {
  if (event.key!=='Tab') return
  const nodes = Array.from(deleteDialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),[tabindex="0"]') || [])
  const first = nodes[0], last = nodes[nodes.length-1]
  if (event.shiftKey && document.activeElement===first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement===last) { event.preventDefault(); first?.focus() }
}
const markers = computed(() => {
  const rows = [...new Set((annotationsByMedia.value[currentMediaId.value] ?? []).map(o => o.frameIndex ?? 0))].sort((a,b)=>a-b)
  return rows.filter((_,i) => i % Math.max(1,Math.ceil(rows.length/200)) === 0)
})
let observer: ResizeObserver | null = null, removeGuard: (()=>void) | null = null, pointerRaf = 0
let pan: { x:number; y:number; left:number; top:number; id:number } | null = null, suppressClick = false, latestMove: PointerEvent | null = null
let helpPriorFocus: HTMLElement | null = null
const focusButton = ref<HTMLButtonElement | null>(null)
let pointerActive = false
let normalView: { mediaId: string; zoom: number; base: {w:number;h:number}; left: number; top: number; pageY: number } | null = null
let priorOverflow = '', priorHeaderInert = false
function restorePage() {
  document.body.style.overflow = priorOverflow
  const header = document.querySelector<HTMLElement>('.app-header')
  if (header) header.inert = priorHeaderInert
}
async function toggleFocus() {
  cancelPointer(); spaceHeld.value = false
  const scroller = scrollContainerRef.value
  if (!focused.value) {
    normalView = { mediaId: currentMediaId.value, zoom: zoom.value, base: {...base.value}, left: scroller?.scrollLeft || 0, top: scroller?.scrollTop || 0, pageY: window.scrollY }
    priorOverflow = document.body.style.overflow
    const header = document.querySelector<HTMLElement>('.app-header')
    priorHeaderInert = header?.inert || false
    if (header) header.inert = true
    document.body.style.overflow = 'hidden'
    focused.value = true
    zoomReset()
    await nextTick()
    size.value = { w: scroller?.clientWidth || 0, h: scroller?.clientHeight || 0 }
    fit(); scroller?.scrollTo(0, 0)
  } else {
    focused.value = false; restorePage()
    if (normalView?.mediaId === currentMediaId.value) {
      zoom.value = normalView.zoom; base.value = {...normalView.base}
    } else zoomReset()
    await nextTick()
    size.value = { w: scroller?.clientWidth || 0, h: scroller?.clientHeight || 0 }
    if (zoom.value === 1) fit()
    await nextTick()
    scroller?.scrollTo(normalView?.left || 0, normalView?.top || 0)
    window.scrollTo(0, normalView?.pageY || 0)
  }
  focusButton.value?.focus({preventScroll:true})
}
function fit() {
  const ratio = (selectedMedia.value?.width || 1600)/(selectedMedia.value?.height || 900)
  const w = Math.max(1, Math.min(size.value.w-40, (size.value.h-40)*ratio))
  base.value = {w,h:w/ratio}
}
function fitView() { cancelAnnotationGesture(); zoomReset(); fit(); scrollContainerRef.value?.scrollTo(0,0) }
async function zoomAt(delta:number, point?:{x:number;y:number}) {
  const el = stageRef.value, scroller = scrollContainerRef.value
  if (!el || !scroller) return
  const old = el.getBoundingClientRect(), vp = scroller.getBoundingClientRect()
  const p = point || {x:vp.left+vp.width/2,y:vp.top+vp.height/2}, nx=(p.x-old.left)/old.width, ny=(p.y-old.top)/old.height
  if(delta>0)zoomIn(delta);else zoomOut(-delta)
  await nextTick()
  const now=el.getBoundingClientRect();scroller.scrollBy(now.left+nx*now.width-p.x,now.top+ny*now.height-p.y)
}
function wheel(e:WheelEvent) { if(e.ctrlKey||e.metaKey) { e.preventDefault();void zoomAt(e.deltaY<0?.15:-.15,{x:e.clientX,y:e.clientY}) } }
function drawLoupe(e:PointerEvent) {
  const stage=stageRef.value, media=selectedMedia.value
  if(!stage||!media)return
  const rect=stage.getBoundingClientRect(),nx=(e.clientX-rect.left)/rect.width,ny=(e.clientY-rect.top)/rect.height
  if(nx<0||ny<0||nx>1||ny>1){mousePixel.value=null;return}
  const w=media.width||1,h=media.height||1
  mousePixel.value={x:Math.min(w-1,Math.floor(nx*w)),y:Math.min(h-1,Math.floor(ny*h))}
  if(!magnifier.value||!loupe.value)return
  const source=isVideo.value ? (exactFrameUrl.value && !isPlaying.value ? exactFrameImageRef.value : videoRef.value) : imageRef.value
  const ctx=loupe.value.getContext('2d');if(!source||!ctx)return
  ctx.fillStyle='#0d1726';ctx.fillRect(0,0,200,150)
  try {ctx.drawImage(source,nx*w-25,ny*h-18.75,50,37.5,0,0,200,150)} catch { return }
  ctx.strokeStyle='#ffd36d';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(92,75);ctx.lineTo(108,75);ctx.moveTo(100,67);ctx.lineTo(100,83);ctx.stroke()
}
function pointerDown(e:PointerEvent) {
  if((spaceHeld.value||e.button===1)&&!editingBlocked.value){
    e.preventDefault();const sc=scrollContainerRef.value!;pan={x:e.clientX,y:e.clientY,left:sc.scrollLeft,top:sc.scrollTop,id:e.pointerId};annotationHitRef.value?.setPointerCapture(e.pointerId);suppressClick=true;return
  }
  suppressClick=false
  if(hiddenBoxes.value){hiddenBoxes.value=false;suppressClick=true;return}
  pointerActive=true
  onBboxDown(e)
}
function processMove() {
  pointerRaf=0;const e=latestMove;if(!e)return
  if(pan){scrollContainerRef.value?.scrollTo(pan.left-(e.clientX-pan.x),pan.top-(e.clientY-pan.y))}
  else onBboxMove(e)
  drawLoupe(e)
}
function pointerMove(e:PointerEvent) {latestMove=e;if(!pointerRaf)pointerRaf=requestAnimationFrame(processMove)}
function pointerUp(e:PointerEvent) {
  pointerActive=false
  if(pointerRaf)cancelAnimationFrame(pointerRaf);pointerRaf=0
  if(pan){pan=null;annotationHitRef.value?.releasePointerCapture(e.pointerId);return}
  onBboxUp(e)
}
function cancelPointer(){if(pointerRaf)cancelAnimationFrame(pointerRaf);pointerRaf=0;pan=null;pointerActive=false;cancelAnnotationGesture()}
function stageClick(e:MouseEvent){if(suppressClick){suppressClick=false;return}if(!hiddenBoxes.value)onStageClick(e)}
async function jump(){frameInput.value=Math.max(0,Math.min(maxFrameIndex.value,Math.floor(Number(jumpFrame.value)||1)-1));await seekToInputFrame();jumpFrame.value=currentFrame.value+1}
async function retrySave(){try{await persistWorkspaceState(currentMediaId.value,true)}catch{/* The store logs and exposes the failure. */}}
function enhancement(preset:string){if(preset==='原图'){resetMediaFilter();inverted.value=false}else if(preset==='暗场'){brightness.value=150;contrast.value=125}else{brightness.value=100;contrast.value=160}}
function keys(e:KeyboardEvent){
  if(document.querySelector('[data-user-guide][open], [data-object-delete-dialog][open]'))return

  if(e.isComposing||document.querySelector('[data-completion-dialog]'))return
  if(help.value){if(e.key==='Escape'){help.value=false;e.preventDefault()}if(e.key==='Tab'){const nodes=Array.from(helpDialog.value?.querySelectorAll<HTMLElement>('button,input,[tabindex="0"]')||[]);if(nodes.length){e.preventDefault();const i=nodes.indexOf(document.activeElement as HTMLElement);nodes[(i+(e.shiftKey?-1:1)+nodes.length)%nodes.length]?.focus()}}return}
  if((e.target as HTMLElement)?.closest('input,textarea,select,[contenteditable="true"]'))return
  if(e.key==='Escape'){e.preventDefault();if(pointerActive||pan){cancelPointer();return}if(focused.value){void toggleFocus();return}cancelPointer();hiddenBoxes.value=false;clearSelection();return}
  if(e.key.toLowerCase()==='f'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!e.repeat){e.preventDefault();void toggleFocus();return}
  if(mediaBusy.value||workspaceRestoring.value)return
  if(e.key===' '){e.preventDefault();spaceHeld.value=true;return}
  if((e.ctrlKey||e.metaKey)&&!e.altKey){if(e.key.toLowerCase()==='z'){e.preventDefault();e.shiftKey?redo():undo()}else if(e.key.toLowerCase()==='y'){e.preventDefault();redo()}return}
  if(e.altKey){if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();const step=e.shiftKey?10:1;nudgeSelected(e.key==='ArrowLeft'?-step:e.key==='ArrowRight'?step:0,e.key==='ArrowUp'?-step:e.key==='ArrowDown'?step:0)}return}
  if(e.repeat)return
  const key=e.key.toLowerCase()
  if(key==='arrowleft'||key==='arrowright'){e.preventDefault();void seekByFrame((key==='arrowleft'?-1:1)*(e.shiftKey?10:1))}
  else if(key==='delete'||key==='backspace'){if(selectedObject.value){e.preventDefault();deleteSelectedFrame()}}
  else if(key==='v'||key==='p'||key==='b')selectTool(key==='v'?'select':key==='p'?'point':'bbox')
  else if(key==='c')copyPreviousFrame()
  else if(key==='h')hiddenBoxes.value=!hiddenBoxes.value
  else if(key==='l')magnifier.value=!magnifier.value
  else if(key==='k'){e.preventDefault();void togglePlayback()}
  else if(key==='='||key==='+'){e.preventDefault();void zoomAt(.25)}
  else if(key==='-'){e.preventDefault();void zoomAt(-.25)}
  else if(key==='0')fitView()
  else if(key==='?'){e.preventDefault();help.value=true}
}
function keyup(e:KeyboardEvent){if(e.key===' ')spaceHeld.value=false}
function blur(){spaceHeld.value=false;cancelPointer()}
function beforeUnload(e:BeforeUnloadEvent){if(saveState.value==='saving'||saveState.value==='error'||objectDeletionBusy.value||trackingFeedbackBusy.value||objectDeletionPendingAction.value||trackingFeedbackPending.value){e.preventDefault();e.returnValue=''}}
watch(help,async value=>{if(value){helpPriorFocus=document.activeElement as HTMLElement;await nextTick();helpDialog.value?.querySelector<HTMLElement>('button')?.focus()}else helpPriorFocus?.focus()})
watch(()=>selectedObject.value?.id,()=>{if(selectedObject.value)objectNameInput.value=selectedObject.value.name})
watch(selectedObjectId,()=>{if(!trackingFeedbackPending.value&&selectedObject.value?.objectId!=null)feedbackTargetId.value=selectedObject.value.objectId})
watch(()=>feedbackTarget.value?.objectId,()=>{if(!trackingFeedbackPending.value)learnNormalMotion.value=true})
watch(trackingPausedFrame,()=>{feedbackTargetId.value=null;if(!trackingFeedbackPending.value)learnNormalMotion.value=true})
watch(currentFrame,value=>{jumpFrame.value=value+1;mousePixel.value=null})
watch(selectedMediaId,async id=>{cancelPointer();objectSearch.value='';mousePixel.value=null;fit();await nextTick();await resetAnnotationViewForMedia(id);scrollContainerRef.value?.scrollTo(0,0)})
watch(()=>[selectedMedia.value?.width,selectedMedia.value?.height],fit)
onMounted(()=>{
  observer=new ResizeObserver(()=>{const scroller=scrollContainerRef.value;if(scroller)size.value={w:scroller.clientWidth,h:scroller.clientHeight};if(zoom.value===1)fit()})
  if(scrollContainerRef.value)observer.observe(scrollContainerRef.value)
  window.addEventListener('keydown',keys);window.addEventListener('keyup',keyup);window.addEventListener('blur',blur);window.addEventListener('beforeunload',beforeUnload)
  removeGuard=addLeaveGuard(async()=>{if(mediaBusy.value)return false;cancelPointer();pausePlayback();try{await persistWorkspaceState(currentMediaId.value,true);return true}catch{return false}})
  if(selectedMedia.value)void resetAnnotationViewForMedia(selectedMediaId.value)
})
onUnmounted(()=>{if(deleteDialog.value?.open){deleteDialog.value.close();document.body.style.overflow=deletionPriorOverflow}if(focused.value)restorePage();observer?.disconnect();cancelPointer();pausePlayback();removeGuard?.();window.removeEventListener('keydown',keys);window.removeEventListener('keyup',keyup);window.removeEventListener('blur',blur);window.removeEventListener('beforeunload',beforeUnload)})
</script>
<template>
  <section class="annotation-page" :class="{focused}" data-testid="annotation-page">
    <input ref="fileInputRef" type="file" accept="image/*" multiple hidden @change="handleFiles(($event.target as HTMLInputElement).files,'image')" />
    <input ref="videoInputRef" type="file" accept="video/*" multiple hidden @change="handleFiles(($event.target as HTMLInputElement).files,'video')" />
    <input ref="annotationFolderInputRef" type="file" webkitdirectory directory multiple hidden @change="handleAnnotationFolderFiles(($event.target as HTMLInputElement).files)" />
    <header v-if="!focused" class="annotation-heading"><div><span class="eyebrow">ANNOTATION STUDIO</span><h2>人工标注 <span>让每一帧都更准确</span></h2></div><div class="heading-actions"><span class="save-indicator" :class="saveState" role="status"><i />{{ saveLabel }}</span><button class="quiet-button" @click="help=true"><AppIcon name="help" :size="16" />快捷键</button><SendToReview /></div></header>
    <div v-if="saveError" class="error-banner" role="alert">{{ saveError }} <button class="quiet-button" @click="retrySave">重试保存</button></div>
    <div v-if="mediaDeleteError" class="error-banner" role="alert">{{ mediaDeleteError }}</div>
    <div v-if="objectDeletionError && !deletionDialogOpen" class="error-banner" role="alert">{{ objectDeletionError }} <button class="quiet-button" :disabled="objectDeletionBusy" @click="objectDeletionPendingAction?.action==='delete'?openVideoDeletion():retryObjectDeletion()">重试本次{{ objectDeletionPendingAction?.action==='undo'?'撤销':objectDeletionPendingAction?.action==='redo'?'重做':'删除' }}</button></div>
    <div class="annotation-layout">
      <aside class="panel asset-panel"><div class="panel-title"><span>素材库</span><span class="badge">{{ mediaAssets.length }}</span></div>
        <div class="asset-actions"><button class="btn-primary" :disabled="mediaBusy" @click="openFilePicker('video')"><AppIcon name="upload" :size="15" />导入视频</button><button class="btn-secondary" :disabled="mediaBusy" @click="openFilePicker('image')">图片</button></div>
        <div class="asset-search"><input v-model="search" class="input" placeholder="查找素材…" aria-label="查找素材" /></div>
        <div class="asset-list"><div v-for="media in visibleMedia" :key="media.id" class="asset-card" :class="{selected:selectedMediaId===media.id}" role="button" :tabindex="mediaBusy?-1:0" :aria-disabled="mediaBusy" @click="!mediaBusy&&(selectedMediaId=media.id)" @keydown.enter="!mediaBusy&&(selectedMediaId=media.id)" @keydown.space.prevent.stop="!mediaBusy&&(selectedMediaId=media.id)">
          <span class="asset-type">{{ media.type==='video'?'VID':'IMG' }}</span><div><strong :title="media.name">{{ media.name }}</strong><small>{{ media.type==='video'?`${media.frameCount || '—'} 帧 · ${media.fps?.toFixed(1) || 30} fps`:`${media.width || '—'} × ${media.height || '—'}` }}</small></div><button class="asset-close icon-button" :disabled="mediaBusy" title="关闭素材（不会删除后端文件和标注）" aria-label="关闭素材" @click.stop="closeMedia(media.id)"><AppIcon name="close" :size="13" /></button><button v-if="media.serverMediaId" class="asset-delete icon-button" :disabled="mediaBusy||workspaceRestoring" title="删除素材（从服务器删除，不能撤销）" :aria-label="`删除素材 ${media.name}`" @click.stop="deleteMedia(media.id)" @keydown.enter.stop @keydown.space.stop><AppIcon name="trash" :size="14" /></button>
        </div><p v-if="!visibleMedia.length" class="sidebar-empty">{{ search?'没有匹配素材':'导入视频或图片开始标注' }}</p></div>
        <button class="asset-import quiet-button" :disabled="mediaBusy" @click="openAnnotationFolderPicker"><AppIcon name="folder" :size="16" />加载标注</button>
      </aside>
      <section class="panel annotation-workbench">
        <div class="workbench-title"><div><strong :title="selectedMedia?.name">{{ selectedMedia?.name || '开始一个新的标注' }}</strong><span>{{ selectedMedia?.width || '—' }} × {{ selectedMedia?.height || '—' }}<template v-if="isVideo"> · {{ videoFps.toFixed(1) }} fps</template></span></div><div class="workbench-actions"><template v-if="focused"><span class="save-indicator" :class="saveState" role="status"><i />{{ saveLabel }}</span><button class="quiet-button" :disabled="editingBlocked||hasPausedTracking||trackingFeedbackPending||!!objectDeletionPendingAction||!isVideo||!currentObjects.some(o=>o.bbox)" @click="runAiTrack">{{ isAiBusy?'正在追踪…':'AI Tracking' }}</button><UserGuide page="/annotate" /><button class="quiet-button" @click="help=true" aria-label="快捷键"><AppIcon name="help" :size="16" /></button><SendToReview /></template><button ref="focusButton" class="quiet-button" :aria-pressed="focused" :title="focused?'退出专注模式 (F / Esc)':'专注模式 (F)'" @click="toggleFocus"><AppIcon name="fit" :size="16" />{{ focused?'退出专注':'专注' }}<kbd v-if="focused">Esc</kbd></button></div></div>
        <div class="annotation-toolbar" aria-label="标注工具">
          <div class="tool-group"><button v-for="tool in ([['select','cursor','选择','V'],['bbox','box','画框','B'],['point','point','标点','P']] as const)" :key="tool[0]" class="tool-btn" :class="{active:activeTool===tool[0]}" :aria-pressed="activeTool===tool[0]" :disabled="editingBlocked" :title="`${tool[2]} (${tool[3]})`" @click="selectTool(tool[0])"><AppIcon :name="tool[1]" :size="16" />{{ tool[2] }}<kbd>{{ tool[3] }}</kbd></button></div>
          <div class="tool-group"><button class="icon-button" title="撤销最近操作 (Ctrl/⌘ Z)" aria-label="撤销本帧操作" :disabled="editingBlocked||!!objectDeletionPendingAction||trackingFeedbackPending||!canUndo" @click="undo"><AppIcon name="undo" :size="17" /></button><button class="icon-button" title="重做最近操作 (Ctrl/⌘ Shift Z)" aria-label="重做本帧操作" :disabled="editingBlocked||!!objectDeletionPendingAction||trackingFeedbackPending||!canRedo" @click="redo"><AppIcon name="redo" :size="17" /></button><button class="icon-button delete-frame-tool" title="只删除本帧选中对象 (Delete / Backspace)" aria-label="删除本帧选中对象" :disabled="editingBlocked||!selectedObject||!!objectDeletionPendingAction||trackingFeedbackPending" @click="deleteSelectedFrame"><AppIcon name="trash" :size="16" /></button></div>
          <div class="tool-group display-tools"><button class="icon-button" :class="{active:magnifier}" :aria-pressed="magnifier" title="局部放大镜 (L)" aria-label="局部放大镜" @click="magnifier=!magnifier"><AppIcon name="zoom" /></button><button class="icon-button" :class="{active:hiddenBoxes}" :aria-pressed="hiddenBoxes" title="显示或隐藏标注 (H)" aria-label="显示或隐藏标注" @click="hiddenBoxes=!hiddenBoxes"><AppIcon name="eye" /></button><button class="quiet-button" :class="{active:enhancing}" :aria-expanded="enhancing" @click="enhancing=!enhancing"><AppIcon name="sun" :size="17" />画面增强</button></div>
        </div>
        <div v-if="enhancing" class="enhancement-bar"><button v-for="p in ['原图','暗场','低对比']" :key="p" class="quiet-button" @click="enhancement(p)">{{ p }}</button><label>亮度 <input v-model.number="brightness" aria-label="亮度" type="range" min="50" max="200" /></label><label>对比 <input v-model.number="contrast" aria-label="对比度" type="range" min="50" max="220" /></label><label><input v-model="inverted" type="checkbox" />反相</label><span>仅调整预览</span></div>
        <div class="annotation-canvas">
          <div ref="scrollContainerRef" class="canvas-scroll" @wheel="wheel">
            <div ref="stageRef" class="annotation-stage" :style="{width:`${stageSize.w}px`,height:`${stageSize.h}px`}">
              <template v-if="selectedMedia"><template v-if="isVideo">
                <video :key="selectedMedia.id" ref="videoRef" :src="selectedMedia.url" class="stage-media" :class="{'hidden-video':videoPlaybackFallback||(exactFrameUrl&&!isPlaying)}" :style="{filter}" preload="metadata" playsinline @loadedmetadata="onVideoLoaded" @timeupdate="onVideoTimeUpdate" @ended="onVideoEnded" @error="onVideoError" />
                <img v-if="exactFrameUrl&&(!isPlaying||videoPlaybackFallback)" ref="exactFrameImageRef" :src="exactFrameUrl" :alt="`${selectedMedia.name} 第 ${currentFrame+1} 帧`" class="stage-media exact-media" :style="{filter}" />
              </template><img v-else :key="selectedMedia.id" ref="imageRef" :src="selectedMedia.url" :alt="selectedMedia.name" class="stage-media" :style="{filter}" @load="onImageLoaded" /></template>
              <div v-else class="canvas-empty"><AppIcon name="upload" :size="36" /><h3>把注意力留给画面</h3><p>导入一段视频，开始精细标注。</p><button class="btn-primary" @click="openFilePicker('video')">导入视频</button></div>
              <div ref="annotationHitRef" data-testid="annotation-hit" class="annotation-hit" :class="{panning:spaceHeld||pan,selecting:activeTool==='select',locked:editingBlocked}" @click="stageClick" @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerUp" @pointercancel="cancelPointer" @lostpointercapture="cancelPointer" @pointerleave="mousePixel=null" />
              <AnnotationOverlay :objects="displayObjects" :selected="selectedObjectId" :width="stageSize.w" :height="stageSize.h" :draft="tempBbox" :labels="labels" :hidden="hiddenBoxes || (!!frameError && !exactFrameUrl)" />
            </div>
          </div>
          <div v-if="exactFrameLoading||workspaceRestoring||isAiBusy" class="canvas-status">{{ isAiBusy?'AI 正在追踪 · 标注暂时锁定':workspaceRestoring?'正在恢复工作区…':'正在读取帧…' }}</div>
          <div v-if="frameError" class="canvas-error" role="alert">{{ frameError }}<button class="btn-secondary" @click="retryExactFrame">重试读取</button></div>
          <div v-if="magnifier" class="loupe"><canvas ref="loupe" width="200" height="150" :style="{filter}" /><span>4× 原图局部 · {{ mousePixel?`${mousePixel.x}, ${mousePixel.y}`:'移动指针查看' }}</span></div>
          <div class="canvas-bottom"><span class="canvas-hint">{{ hiddenBoxes?'标注已隐藏 · H 恢复':spaceHeld?'拖动画面':activeTool==='bbox'?'拖拽画框 · 空格拖动画面':activeTool==='select'?'拖动移动 · 角点缩放':'单击添加标注点' }}</span><div class="canvas-zoom"><button aria-label="缩小画面" @click="zoomAt(-.25)">−</button><button title="适应画面 (0)" @click="fitView">{{ Math.round(zoom*100) }}%</button><button aria-label="放大画面" @click="zoomAt(.25)">＋</button><button aria-label="适应画面" @click="fitView"><AppIcon name="fit" :size="15" /></button></div></div>
        </div>
        <AnnotationTrackingFeedback v-if="hasPausedTracking || (trackingFeedbackError && trackingFeedbackPendingAction?.decision!=='reset')" :paused-frame="trackingPausedFrame" :current-frame="currentFrame" :notices="pausedAnomalies" :target="feedbackTarget" :remaining="unresolvedAnomalies.length" v-model:learn="learnNormalMotion" :disabled="feedbackBlocked" :busy="trackingFeedbackBusy" :pending="trackingFeedbackPending" :error="trackingFeedbackError" @select="selectFeedbackObject" @return="returnToPausedFrame" @confirm="confirmFeedback" @retry="retryTrackingAnomalyFeedback" />
        <div v-if="trackingWarningSummary.length && !hasPausedTracking" class="tracking-warning-summary" role="status"><AppIcon name="check" :size="14" /><span>{{ trackingWarningSummary.reduce((count,item)=>count+item.count,0) }} 次同类运动变化已合并提示，追踪继续。新的风险仍会检测。</span><details><summary>详情</summary><p v-for="item in trackingWarningSummary" :key="`${item.objectId}:${item.reason}`">#{{ item.objectId }} · 第 {{ item.firstFrame+1 }}–{{ item.lastFrame+1 }} 帧 · {{ item.count }} 次{{ item.calibrated?' · 参考已确认正常样本':'' }}</p></details></div>
        <div v-if="focused && trackingCalibrationSummary.length" class="focus-calibration"><span>异常检测持续开启 · 已有正常运动样本</span><button v-for="item in trackingCalibrationSummary" :key="item.objectId" class="quiet-button" :disabled="feedbackBlocked||trackingFeedbackPending" @click="resetTrackingCalibration(item.objectId)">#{{ item.objectId }} 恢复默认判断</button></div>
        <div v-if="focused && trackingFeedbackError && trackingFeedbackPendingAction?.decision==='reset'" class="focus-calibration tracking-feedback-error" role="alert"><span>{{ trackingFeedbackError }}</span><button class="quiet-button" :disabled="trackingFeedbackBusy" @click="retryTrackingAnomalyFeedback">重试恢复默认判断</button></div>
        <div v-if="focused && (frameDeletionVisible||videoDeletionVisible)" class="focus-calibration object-deletion-feedback" role="status"><span v-if="frameDeletionVisible">#{{ frameDeletion?.objectId }} 已删除本帧标注</span><span v-else-if="lastVideoObjectDeletion">#{{ lastVideoObjectDeletion.objectId }} 已删除 {{ lastVideoObjectDeletion.frameCount }} 帧标注</span><button class="quiet-button" :disabled="editingBlocked||!!objectDeletionPendingAction||trackingFeedbackPending||(frameDeletionVisible?!canUndo:!canUndoVideoDeletion)" @click="frameDeletionVisible?undoFrameDeletion():undoVideoDeletion()"><AppIcon name="undo" :size="13" />撤销</button></div>
        <div v-if="isVideo" class="annotation-timeline"><div class="timeline-controls"><button class="icon-button" aria-label="上一帧" :disabled="isAiBusy||currentFrame===0" @click="seekByFrame(-1)">←</button><button class="play-button" :aria-label="isPlaying?'暂停':'播放'" :disabled="isAiBusy||exactFrameLoading" @click="togglePlayback"><AppIcon :name="isPlaying?'pause':'play'" :size="15" /></button><button class="icon-button" aria-label="下一帧" :disabled="isAiBusy||currentFrame===maxFrameIndex" @click="seekByFrame(1)">→</button><label class="frame-jump">第 <input v-model.number="jumpFrame" aria-label="跳转帧号" type="number" min="1" :max="maxFrameIndex+1" :disabled="isAiBusy" @change="jump" @keydown.enter="jump" /> / {{ maxFrameIndex+1 }} 帧</label><select v-model.number="playbackRate" aria-label="播放速度" class="speed-select"><option v-for="r in [.25,.5,1,2]" :key="r" :value="r">{{ r }}×</option></select><button class="quiet-button copy-previous" :disabled="editingBlocked||currentFrame===0" title="只补充当前帧缺少的对象 (C)" @click="copyPreviousFrame">复制上一帧 <kbd>C</kbd></button></div>
          <div class="timeline-track" role="slider" aria-label="视频时间轴" tabindex="0" :aria-valuenow="currentFrame+1" :aria-valuemin="1" :aria-valuemax="maxFrameIndex+1" @click="onTimelineClick"><i class="timeline-base" /><i class="timeline-progress" :style="{width:`${maxFrameIndex?currentFrame/maxFrameIndex*100:0}%`}"/><i v-for="f in markers" :key="f" class="timeline-marker" :style="{left:`${maxFrameIndex?f/maxFrameIndex*100:0}%`}"/><i v-for="a in anomalyFrames" :key="a.frame_index" class="timeline-anomaly" :title="`第 ${a.frame_index+1} 帧：${a.reasons.join(' · ')}`" :style="{left:`${maxFrameIndex?a.frame_index/maxFrameIndex*100:0}%`}"/><i class="timeline-cursor" :style="{left:`${maxFrameIndex?currentFrame/maxFrameIndex*100:0}%`}"/></div><div class="timeline-caption"><span>{{ formatTime(currentTime) }} / {{ formatTime(videoDuration) }}</span><span>{{ annotatedFrameCount }} 帧含标注 <i />绿色为已有标注</span></div>
        </div>
        <footer class="annotation-status"><span :title="statusMessage">{{ statusMessage }}</span><span v-if="mousePixel">X {{ mousePixel.x }} · Y {{ mousePixel.y }} px</span><span v-else>原图坐标 · 显示增强不影响标注</span></footer>
      </section>
      <aside class="annotation-inspector">
        <section class="panel object-panel" aria-label="当前帧对象">
          <div class="panel-title"><span>当前帧对象 <span class="badge">{{ currentObjects.length }}</span></span><label class="labels-toggle"><input v-model="labels" type="checkbox" />名称</label></div>
          <div v-if="currentObjects.length>8" class="object-search"><input v-model="objectSearch" class="input" placeholder="查找名称或对象 ID…" aria-label="查找对象" /></div>
          <div class="object-list">
            <button v-for="obj in visibleObjects" :key="obj.id" class="object-row object-select" :class="{selected:selectedObjectId===obj.id}" :aria-pressed="selectedObjectId===obj.id" :aria-label="`选择对象 #${obj.objectId} ${obj.name}`" :disabled="editingBlocked||trackingFeedbackPending||!!objectDeletionPendingAction" @click="selectObject(obj.id)"><i :class="obj.source"/><span class="object-stable-id">#{{ obj.objectId }}</span><strong :title="obj.name">{{ obj.name }}</strong><span class="object-source">{{ obj.source==='ai'?'AI':'人工' }}</span><AppIcon v-if="selectedObjectId===obj.id" name="check" :size="13" /></button>
            <div v-if="!visibleObjects.length" class="sidebar-empty"><AppIcon name="box" :size="24"/><p>{{ objectSearch?'没有匹配对象':'本帧暂无标注对象' }}</p></div>
          </div>
          <div class="object-selection" data-testid="selected-object-actions">
            <template v-if="selectedObject">
              <div class="selection-context"><strong>操作 #{{ selectedObject.objectId }}</strong><span>{{ selectedObject.source==='ai'?'AI 追踪':'人工标注' }}{{ selectedObject.confidence?` · ${Math.round(selectedObject.confidence*100)}%`:'' }}</span></div>
              <div v-if="isVideo" class="object-coverage">有标注 {{ selectedObjectCoverage }} / {{ maxFrameIndex+1 }} 帧 · ID 在各帧保持不变</div>
              <div class="object-name-edit"><input v-model="objectNameInput" class="input" aria-label="对象名称" placeholder="例如 rare sperm" :disabled="editingBlocked||trackingFeedbackPending||!!objectDeletionPendingAction" /><button class="btn-secondary" :disabled="editingBlocked||trackingFeedbackPending||!!objectDeletionPendingAction" @click="renameObject">改名</button></div>
              <div v-if="selectedObject.bbox" class="object-measure"><span>位置 {{ Math.round(selectedObject.bbox.x*(selectedMedia?.width||0)/100) }}, {{ Math.round(selectedObject.bbox.y*(selectedMedia?.height||0)/100) }}</span><span>{{ (selectedObject.bbox.width*(selectedMedia?.width||0)/100).toFixed(1) }} × {{ (selectedObject.bbox.height*(selectedMedia?.height||0)/100).toFixed(1) }} px</span></div>
              <div class="object-delete-actions"><button class="btn-secondary" data-testid="delete-object-frame" title="仅删当前帧 (Delete / Backspace)" :disabled="editingBlocked||trackingFeedbackPending||!!objectDeletionPendingAction" @click="deleteSelectedFrame"><AppIcon name="trash" :size="13" />删除本帧</button><button class="btn-secondary delete-video-button" data-testid="delete-object-video" :disabled="editingBlocked||!isVideo||!selectedMedia?.serverMediaId||trackingFeedbackPending||!!objectDeletionPendingAction" @click="openVideoDeletion">删除全视频…</button></div>
            </template>
            <template v-else><p class="object-no-selection">点击列表或画面中的框选中对象。</p><label class="new-object-name">新对象名称<input v-model="objectNameInput" class="input" aria-label="对象名称" placeholder="例如 rare sperm" :disabled="editingBlocked||trackingFeedbackPending||!!objectDeletionPendingAction" /></label></template>
            <div v-if="frameDeletionVisible" class="object-deletion-feedback" role="status"><AppIcon name="check" :size="14"/><span>#{{ frameDeletion?.objectId }} 已删除本帧标注</span><button class="quiet-button" :disabled="editingBlocked||!canUndo||!!objectDeletionPendingAction||trackingFeedbackPending" @click="undoFrameDeletion"><AppIcon name="undo" :size="13" />撤销</button></div>
            <div v-else-if="videoDeletionVisible && lastVideoObjectDeletion" class="object-deletion-feedback" role="status"><AppIcon name="check" :size="14"/><span>#{{ lastVideoObjectDeletion.objectId }} 已删除 {{ lastVideoObjectDeletion.frameCount }} 帧标注</span><button class="quiet-button" :disabled="editingBlocked||!canUndoVideoDeletion||!!objectDeletionPendingAction||trackingFeedbackPending" @click="undoVideoDeletion"><AppIcon name="undo" :size="13" />撤销</button></div>
          </div>
          <div class="object-legend"><span><i class="manual"/>人工</span><span><i class="ai"/>AI</span><span><i class="chosen"/>选中</span></div>
        </section>
        <section class="panel ai-panel">
          <div class="ai-heading"><AppIcon name="spark" :size="18"/><strong>AI 辅助追踪</strong><span class="ai-state">{{ isAiBusy?'追踪中':hasPausedTracking?'待人工核对':'就绪' }}</span></div>
          <p>{{ hasPausedTracking?'先核对画布下的暂停对象，明确确认后继续。':'以当前帧为起点，自动追踪已有目标。' }}</p>
          <button class="btn-primary" :disabled="editingBlocked||hasPausedTracking||trackingFeedbackPending||!!objectDeletionPendingAction||!isVideo||!currentObjects.some(o=>o.bbox)" @click="runAiTrack">{{ isAiBusy?'正在追踪…':'AI Tracking' }}</button>
          <div class="tracking-calibration"><strong>异常检测持续开启</strong><p v-if="!trackingCalibrationSummary.length">确认正常运动后，可减少本视频同一对象的同类暂停。</p><div v-for="item in trackingCalibrationSummary" :key="item.objectId"><span>#{{ item.objectId }} · {{ item.sampleCount }} 次正常运动确认</span><button class="quiet-button" :disabled="feedbackBlocked||trackingFeedbackPending" @click="resetTrackingCalibration(item.objectId)">恢复默认判断</button></div><p>新的持续偏离、丢失、形状突变仍需核对。</p></div>
          <div v-if="trackingFeedbackError && trackingFeedbackPendingAction?.decision==='reset'" class="tracking-feedback-error" role="alert">{{ trackingFeedbackError }}<button class="quiet-button" :disabled="trackingFeedbackBusy" @click="retryTrackingAnomalyFeedback">重试恢复默认判断</button></div>
        </section>
      </aside>
    </div>
    <Teleport to="body">
      <dialog ref="deleteDialog" class="app-dialog object-delete-dialog" data-object-delete-dialog aria-labelledby="object-delete-title" aria-describedby="object-delete-description" @cancel.prevent="closeDeleteDialog" @close="finishDeleteDialog" @keydown.stop="deletionDialogKeys">
        <header class="object-delete-heading"><div><h2 id="object-delete-title">删除整段视频中的此对象？</h2><p id="object-delete-description">只删除「{{ deletionTarget?.mediaName }}」中稳定 ID 为 #{{ deletionTarget?.objectId }} 的标注。</p></div><button class="icon-button" aria-label="关闭删除确认" :disabled="objectDeletionBusy" @click="closeDeleteDialog"><AppIcon name="close" /></button></header>
        <div v-if="deletionSummaryLoading" class="object-delete-loading" role="status">正在核对全视频的实际删除范围…</div>
        <div v-else-if="deletionSummaryError" class="error-banner" role="alert">{{ deletionSummaryError }} <button class="quiet-button" @click="loadDeletionSummary">重试读取范围</button></div>
        <div v-else-if="deletionSummary" class="object-delete-scope"><div><strong>{{ deletionSummary.name || deletionTarget?.name }} · #{{ deletionSummary.objectId }}</strong><span v-if="deletionSummary.firstFrame!=null&&deletionSummary.lastFrame!=null">第 {{ deletionSummary.firstFrame+1 }}–{{ deletionSummary.lastFrame+1 }} 帧范围</span></div><dl><div><dt>涉及帧数</dt><dd>{{ deletionSummary.frameCount }}</dd></div><div><dt>人工标注</dt><dd>{{ deletionSummary.manualCount }}</dd></div><div><dt>AI 标注</dt><dd>{{ deletionSummary.aiCount }}</dd></div></dl></div>
        <p v-else-if="objectDeletionPendingAction" class="object-delete-loading">原删除请求仍待确认，请用原请求重试。</p>
        <ul class="object-delete-boundaries"><li>移除过去、当前、未来帧中此对象已有的人工框与 AI 标注。</li><li>此对象的续追种子一并排除；刷新、重新读取和续追不会自动恢复。</li><li>其他对象、原视频及已固定的 A/B/F 快照保留。</li></ul>
        <p class="object-delete-undo-note">误删可撤销整次操作，恢复原框与原对象 ID。最近一次全视频删除可在当前标签页刷新后继续撤销；关闭标签页或启动新追踪后不保证恢复。</p>
        <div v-if="objectDeletionError" class="error-banner" role="alert">{{ objectDeletionError }}<p v-if="objectDeletionPendingAction">删除结果尚未确认；重试沿用本次请求。</p></div>
        <div class="app-dialog-actions"><button class="btn-secondary" :disabled="objectDeletionBusy" @click="closeDeleteDialog">取消</button><button class="btn-primary confirm-delete-video" data-testid="confirm-delete-object-video" :disabled="objectDeletionBusy||(!objectDeletionPendingAction&&(deletionSummaryLoading||!deletionSummary||deletionSummary.frameCount===0))" @click="confirmVideoDeletion">{{ objectDeletionBusy?'正在确认删除…':objectDeletionPendingAction?`重试删除 #${deletionTarget?.objectId}`:`删除全部 ${deletionSummary?.frameCount || 0} 帧标注` }}</button></div>
      </dialog>
      <div v-if="help" class="dialog-mask" @click.self="help=false"><section ref="helpDialog" class="app-dialog annotation-help" role="dialog" aria-modal="true" aria-labelledby="annotation-help-title"><h2 id="annotation-help-title">标注快捷键</h2><dl><div v-for="[key,desc] in annotationShortcuts" :key="key"><dt><kbd>{{ key }}</kbd></dt><dd>{{ desc }}</dd></div></dl><p class="muted">{{ shortcutHelpNote }}</p><div class="app-dialog-actions"><button class="btn-primary" @click="help=false">知道了</button></div></section></div>
    </Teleport>
  </section>
  <transition name="toast"><div v-if="toastMessage" class="status-toast" role="status">{{ toastMessage }}</div></transition>
</template>
