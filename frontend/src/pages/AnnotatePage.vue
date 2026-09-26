<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import SendToReview from '../components/SendToReview.vue'
import AppIcon from '../components/AppIcon.vue'
import AnnotationOverlay from '../components/AnnotationOverlay.vue'
import { useWorkspace } from '../stores/workspace'
import { addLeaveGuard } from '../router'
import '../annotation/annotation.css'
const {
  mediaAssets, selectedMediaId, activeTool, objectNameInput, selectedObjectId, currentFrame, currentTime, videoDuration, videoFps, frameInput, isPlaying,
  isAiBusy, statusMessage, toastMessage, imageRef, videoRef, exactFrameImageRef, exactFrameUrl, exactFrameLoading, videoPlaybackFallback, annotationHitRef, fileInputRef, videoInputRef, annotationFolderInputRef,
  annotationsByMedia, selectedMedia, isVideo, maxFrameIndex, currentMediaId, currentObjects, selectedObject, displayObjects, editingBlocked, workspaceRestoring,
  anomalyObjectIds, anomalyFrames, pausedAnomalies, anomalyPanelVisible, closeAnomalyPanel, formatTime, selectTool, onStageClick, tempBbox, onBboxDown, onBboxMove, onBboxUp,
  selectObject, removeObject, renameObject, undo, redo, canUndo, canRedo, copyPreviousFrame, brightness, contrast, mediaFilterStyle, resetMediaFilter, annotatedFrameCount,
  clearSelection, openFilePicker, handleFiles, onImageLoaded, onVideoLoaded, onVideoTimeUpdate, onVideoError, seekToInputFrame, togglePlayback, onVideoEnded,
  onTimelineClick, runAiTrack, resetAnnotationViewForMedia, seekByFrame, zoom, zoomIn, zoomOut, zoomReset, closeMedia, openAnnotationFolderPicker, handleAnnotationFolderFiles,
  nudgeSelected, cancelAnnotationGesture, frameError, retryExactFrame, saveState, saveError, persistWorkspaceState, playbackRate, pausePlayback,
} = useWorkspace()
const scrollContainerRef = ref<HTMLDivElement | null>(null), stageRef = ref<HTMLDivElement | null>(null), loupe = ref<HTMLCanvasElement | null>(null)
const help = ref(false), helpDialog = ref<HTMLElement | null>(null), focused = ref(false), labels = ref(true), hiddenBoxes = ref(false), magnifier = ref(false), inverted = ref(false), enhancing = ref(false), spaceHeld = ref(false)
const search = ref(''), objectSearch = ref(''), jumpFrame = ref(1)
const size = ref({ w: 0, h: 0 }), base = ref({ w: 800, h: 450 })
const mousePixel = ref<{ x: number; y: number } | null>(null)
const stageSize = computed(() => ({ w: Math.round(base.value.w * zoom.value), h: Math.round(base.value.h * zoom.value) }))
const visibleMedia = computed(() => mediaAssets.value.filter(m => m.name.toLowerCase().includes(search.value.toLowerCase())))
const visibleObjects = computed(() => currentObjects.value.filter(o => o.name.toLowerCase().includes(objectSearch.value.toLowerCase())))
const filter = computed(() => `${mediaFilterStyle.value} ${inverted.value ? 'invert(1)' : ''}`)
const saveLabel = computed(() => selectedMedia.value?.serverMediaId ? ({ idle:'自动保存', saving:'保存中…', saved:'已保存', error:'保存失败' }[saveState.value]) : '本机自动保存')
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
  if(document.querySelector('[data-user-guide][open]'))return

  if(e.isComposing||document.querySelector('[data-completion-dialog]'))return
  if(help.value){if(e.key==='Escape'){help.value=false;e.preventDefault()}if(e.key==='Tab'){const nodes=Array.from(helpDialog.value?.querySelectorAll<HTMLElement>('button,input,[tabindex="0"]')||[]);if(nodes.length){e.preventDefault();const i=nodes.indexOf(document.activeElement as HTMLElement);nodes[(i+(e.shiftKey?-1:1)+nodes.length)%nodes.length]?.focus()}}return}
  if((e.target as HTMLElement)?.closest('input,textarea,select,[contenteditable="true"]'))return
  if(e.key==='Escape'){e.preventDefault();if(pointerActive||pan){cancelPointer();return}if(focused.value){void toggleFocus();return}cancelPointer();hiddenBoxes.value=false;clearSelection();return}
  if(e.key.toLowerCase()==='f'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!e.repeat){e.preventDefault();void toggleFocus();return}
  if(isAiBusy.value||workspaceRestoring.value)return
  if(e.key===' '){e.preventDefault();spaceHeld.value=true;return}
  if((e.ctrlKey||e.metaKey)&&!e.altKey){if(e.key.toLowerCase()==='z'){e.preventDefault();e.shiftKey?redo():undo()}else if(e.key.toLowerCase()==='y'){e.preventDefault();redo()}return}
  if(e.altKey){if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();const step=e.shiftKey?10:1;nudgeSelected(e.key==='ArrowLeft'?-step:e.key==='ArrowRight'?step:0,e.key==='ArrowUp'?-step:e.key==='ArrowDown'?step:0)}return}
  if(e.repeat)return
  const key=e.key.toLowerCase()
  if(key==='arrowleft'||key==='arrowright'){e.preventDefault();void seekByFrame((key==='arrowleft'?-1:1)*(e.shiftKey?10:1))}
  else if(key==='delete'||key==='backspace'){if(selectedObjectId.value){e.preventDefault();removeObject(selectedObjectId.value)}}
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
function beforeUnload(e:BeforeUnloadEvent){if(saveState.value==='saving'||saveState.value==='error'){e.preventDefault();e.returnValue=''}}
watch(help,async value=>{if(value){helpPriorFocus=document.activeElement as HTMLElement;await nextTick();helpDialog.value?.querySelector<HTMLElement>('button')?.focus()}else helpPriorFocus?.focus()})
watch(currentFrame,value=>{jumpFrame.value=value+1;mousePixel.value=null})
watch(selectedMediaId,async id=>{cancelPointer();objectSearch.value='';mousePixel.value=null;fit();await nextTick();await resetAnnotationViewForMedia(id);scrollContainerRef.value?.scrollTo(0,0)})
watch(()=>[selectedMedia.value?.width,selectedMedia.value?.height],fit)
onMounted(()=>{
  observer=new ResizeObserver(()=>{const scroller=scrollContainerRef.value;if(scroller)size.value={w:scroller.clientWidth,h:scroller.clientHeight};if(zoom.value===1)fit()})
  if(scrollContainerRef.value)observer.observe(scrollContainerRef.value)
  window.addEventListener('keydown',keys);window.addEventListener('keyup',keyup);window.addEventListener('blur',blur);window.addEventListener('beforeunload',beforeUnload)
  removeGuard=addLeaveGuard(async()=>{if(isAiBusy.value)return false;cancelPointer();pausePlayback();try{await persistWorkspaceState(currentMediaId.value,true);return true}catch{return false}})
  if(selectedMedia.value)void resetAnnotationViewForMedia(selectedMediaId.value)
})
onUnmounted(()=>{if(focused.value)restorePage();observer?.disconnect();cancelPointer();pausePlayback();removeGuard?.();window.removeEventListener('keydown',keys);window.removeEventListener('keyup',keyup);window.removeEventListener('blur',blur);window.removeEventListener('beforeunload',beforeUnload)})
</script>
<template>
  <section class="annotation-page" :class="{focused}" data-testid="annotation-page">
    <input ref="fileInputRef" type="file" accept="image/*" multiple hidden @change="handleFiles(($event.target as HTMLInputElement).files,'image')" />
    <input ref="videoInputRef" type="file" accept="video/*" multiple hidden @change="handleFiles(($event.target as HTMLInputElement).files,'video')" />
    <input ref="annotationFolderInputRef" type="file" webkitdirectory directory multiple hidden @change="handleAnnotationFolderFiles(($event.target as HTMLInputElement).files)" />
    <header v-if="!focused" class="annotation-heading"><div><span class="eyebrow">ANNOTATION STUDIO</span><h2>人工标注 <span>让每一帧都更准确</span></h2></div><div class="heading-actions"><span class="save-indicator" :class="saveState" role="status"><i />{{ saveLabel }}</span><button class="quiet-button" @click="help=true"><AppIcon name="help" :size="16" />快捷键</button><SendToReview /></div></header>
    <div v-if="saveError" class="error-banner" role="alert">{{ saveError }} <button class="quiet-button" @click="retrySave">重试保存</button></div>
    <div class="annotation-layout">
      <aside class="panel asset-panel"><div class="panel-title"><span>素材库</span><span class="badge">{{ mediaAssets.length }}</span></div>
        <div class="asset-actions"><button class="btn-primary" :disabled="isAiBusy" @click="openFilePicker('video')"><AppIcon name="upload" :size="15" />导入视频</button><button class="btn-secondary" :disabled="isAiBusy" @click="openFilePicker('image')">图片</button></div>
        <div class="asset-search"><input v-model="search" class="input" placeholder="查找素材…" aria-label="查找素材" /></div>
        <div class="asset-list"><div v-for="media in visibleMedia" :key="media.id" class="asset-card" :class="{selected:selectedMediaId===media.id}" role="button" :tabindex="isAiBusy?-1:0" :aria-disabled="isAiBusy" @click="!isAiBusy&&(selectedMediaId=media.id)" @keydown.enter="!isAiBusy&&(selectedMediaId=media.id)" @keydown.space.prevent.stop="!isAiBusy&&(selectedMediaId=media.id)">
          <span class="asset-type">{{ media.type==='video'?'VID':'IMG' }}</span><div><strong :title="media.name">{{ media.name }}</strong><small>{{ media.type==='video'?`${media.frameCount || '—'} 帧 · ${media.fps?.toFixed(1) || 30} fps`:`${media.width || '—'} × ${media.height || '—'}` }}</small></div><button class="asset-close icon-button" :disabled="isAiBusy" title="关闭素材（不会删除后端文件和标注）" aria-label="关闭素材" @click.stop="closeMedia(media.id)"><AppIcon name="close" :size="13" /></button>
        </div><p v-if="!visibleMedia.length" class="sidebar-empty">{{ search?'没有匹配素材':'导入视频或图片开始标注' }}</p></div>
        <button class="asset-import quiet-button" :disabled="isAiBusy" @click="openAnnotationFolderPicker"><AppIcon name="folder" :size="16" />加载标注</button>
      </aside>
      <section class="panel annotation-workbench">
        <div class="workbench-title"><div><strong :title="selectedMedia?.name">{{ selectedMedia?.name || '开始一个新的标注' }}</strong><span>{{ selectedMedia?.width || '—' }} × {{ selectedMedia?.height || '—' }}<template v-if="isVideo"> · {{ videoFps.toFixed(1) }} fps</template></span></div><div class="workbench-actions"><template v-if="focused"><span class="save-indicator" :class="saveState" role="status"><i />{{ saveLabel }}</span><button class="quiet-button" :disabled="editingBlocked||!isVideo||!currentObjects.some(o=>o.bbox)" @click="runAiTrack">{{ isAiBusy?'正在追踪…':'AI Tracking' }}</button><button class="quiet-button" @click="help=true" aria-label="快捷键"><AppIcon name="help" :size="16" /></button><SendToReview /></template><button ref="focusButton" class="quiet-button" :aria-pressed="focused" :title="focused?'退出专注模式 (F / Esc)':'专注模式 (F)'" @click="toggleFocus"><AppIcon name="fit" :size="16" />{{ focused?'退出专注':'专注' }}<kbd v-if="focused">Esc</kbd></button></div></div>
        <div class="annotation-toolbar" aria-label="标注工具">
          <div class="tool-group"><button v-for="tool in ([['select','cursor','选择','V'],['bbox','box','画框','B'],['point','point','标点','P']] as const)" :key="tool[0]" class="tool-btn" :class="{active:activeTool===tool[0]}" :aria-pressed="activeTool===tool[0]" :disabled="editingBlocked" :title="`${tool[2]} (${tool[3]})`" @click="selectTool(tool[0])"><AppIcon :name="tool[1]" :size="16" />{{ tool[2] }}<kbd>{{ tool[3] }}</kbd></button></div>
          <div class="tool-group"><button class="icon-button" title="撤销本帧操作 (Ctrl/⌘ Z)" aria-label="撤销本帧操作" :disabled="editingBlocked||!canUndo" @click="undo"><AppIcon name="undo" :size="17" /></button><button class="icon-button" title="重做本帧操作 (Ctrl/⌘ Shift Z)" aria-label="重做本帧操作" :disabled="editingBlocked||!canRedo" @click="redo"><AppIcon name="redo" :size="17" /></button></div>
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
        <div v-if="anomalyPanelVisible && anomalyObjectIds.length" class="tracking-anomaly"><div><strong>追踪暂停 · 请检查第 {{ currentFrame+1 }} 帧</strong><button class="quiet-button" @click="closeAnomalyPanel">收起</button></div><details><summary>{{ pausedAnomalies.length }} 个异常提示 · 查看详情</summary><p>确认这些框没有问题后可继续 AI Tracking；本帧异常框会被确认为新的人工基准。</p><div v-for="item in pausedAnomalies" :key="item.objectId"><b>{{ item.displayName }} · {{ item.title }}</b><p>{{ item.summary }} {{ item.metrics.join(' · ') }}</p><p v-if="item.baselineFrame!=null">最近人工基准：第 {{ item.baselineFrame+1 }} 帧</p><p v-if="item.reviewRange">建议检查范围：{{ item.reviewRange }}</p><p>{{ item.reviewNotice }} {{ item.suggestion }}</p></div></details></div>
        <div v-if="isVideo" class="annotation-timeline"><div class="timeline-controls"><button class="icon-button" aria-label="上一帧" :disabled="isAiBusy||currentFrame===0" @click="seekByFrame(-1)">←</button><button class="play-button" :aria-label="isPlaying?'暂停':'播放'" :disabled="isAiBusy||exactFrameLoading" @click="togglePlayback"><AppIcon :name="isPlaying?'pause':'play'" :size="15" /></button><button class="icon-button" aria-label="下一帧" :disabled="isAiBusy||currentFrame===maxFrameIndex" @click="seekByFrame(1)">→</button><label class="frame-jump">第 <input v-model.number="jumpFrame" aria-label="跳转帧号" type="number" min="1" :max="maxFrameIndex+1" :disabled="isAiBusy" @change="jump" @keydown.enter="jump" /> / {{ maxFrameIndex+1 }} 帧</label><select v-model.number="playbackRate" aria-label="播放速度" class="speed-select"><option v-for="r in [.25,.5,1,2]" :key="r" :value="r">{{ r }}×</option></select><button class="quiet-button copy-previous" :disabled="editingBlocked||currentFrame===0" title="只补充当前帧缺少的对象 (C)" @click="copyPreviousFrame">复制上一帧 <kbd>C</kbd></button></div>
          <div class="timeline-track" role="slider" aria-label="视频时间轴" tabindex="0" :aria-valuenow="currentFrame+1" :aria-valuemin="1" :aria-valuemax="maxFrameIndex+1" @click="onTimelineClick"><i class="timeline-base" /><i class="timeline-progress" :style="{width:`${maxFrameIndex?currentFrame/maxFrameIndex*100:0}%`}"/><i v-for="f in markers" :key="f" class="timeline-marker" :style="{left:`${maxFrameIndex?f/maxFrameIndex*100:0}%`}"/><i v-for="a in anomalyFrames" :key="a.frame_index" class="timeline-anomaly" :title="`第 ${a.frame_index+1} 帧：${a.reasons.join(' · ')}`" :style="{left:`${maxFrameIndex?a.frame_index/maxFrameIndex*100:0}%`}"/><i class="timeline-cursor" :style="{left:`${maxFrameIndex?currentFrame/maxFrameIndex*100:0}%`}"/></div><div class="timeline-caption"><span>{{ formatTime(currentTime) }} / {{ formatTime(videoDuration) }}</span><span>{{ annotatedFrameCount }} 帧含标注 <i />绿色为已有标注</span></div>
        </div>
        <footer class="annotation-status"><span :title="statusMessage">{{ statusMessage }}</span><span v-if="mousePixel">X {{ mousePixel.x }} · Y {{ mousePixel.y }} px</span><span v-else>原图坐标 · 显示增强不影响标注</span></footer>
      </section>
      <aside class="annotation-inspector">
        <section class="panel name-panel"><div class="panel-title"><span>{{ selectedObject?'当前对象':'新对象名称' }}</span><span v-if="selectedObject" class="badge">#{{ selectedObject.objectId }}</span></div><div class="inspector-body"><input v-model="objectNameInput" class="input" aria-label="对象名称" placeholder="例如 rare sperm" /><button v-if="selectedObject" class="btn-secondary" :disabled="editingBlocked" @click="renameObject">应用名称</button><div v-if="selectedObject?.bbox" class="object-measure"><span>位置 {{ Math.round(selectedObject.bbox.x*(selectedMedia?.width||0)/100) }}, {{ Math.round(selectedObject.bbox.y*(selectedMedia?.height||0)/100) }}</span><span>{{ (selectedObject.bbox.width*(selectedMedia?.width||0)/100).toFixed(1) }} × {{ (selectedObject.bbox.height*(selectedMedia?.height||0)/100).toFixed(1) }} px</span></div></div></section>
        <section class="panel object-panel"><div class="panel-title"><span>本帧对象 <span class="badge">{{ currentObjects.length }}</span></span><label class="labels-toggle"><input v-model="labels" type="checkbox" />名称</label></div><div v-if="currentObjects.length>8" class="object-search"><input v-model="objectSearch" class="input" placeholder="查找对象…" aria-label="查找对象" /></div><div class="object-list"><div v-for="obj in visibleObjects" :key="obj.id" class="object-row" :class="{selected:selectedObjectId===obj.id}" @click="selectObject(obj.id)"><button class="object-select" @click.stop="selectObject(obj.id)"><i :class="obj.source"/><span><strong>{{ obj.name }}</strong><small>{{ obj.source==='ai'?'AI 追踪':'人工标注' }}{{ obj.confidence?` · ${Math.round(obj.confidence*100)}%`:'' }}</small></span></button><button class="icon-button remove-object" :aria-label="`删除 ${obj.name}`" :disabled="editingBlocked" @click.stop="removeObject(obj.id)"><AppIcon name="trash" :size="15" /></button></div><div v-if="!visibleObjects.length" class="sidebar-empty"><AppIcon name="box" :size="28"/><p>{{ objectSearch?'没有匹配对象':'本帧还没有标注' }}</p><small>按 B 画框，按 P 标点</small></div></div><div class="object-legend"><span><i class="manual"/>人工</span><span><i class="ai"/>AI</span><span><i class="chosen"/>选中</span></div></section>
        <section class="panel ai-panel"><div class="ai-heading"><AppIcon name="spark" :size="18"/><strong>AI 辅助追踪</strong></div><p>以当前帧为起点，自动追踪已有目标。</p><button class="btn-primary" :disabled="editingBlocked||!isVideo||!currentObjects.some(o=>o.bbox)" @click="runAiTrack">{{ isAiBusy?'正在追踪…':'AI Tracking' }}</button><small>发现异常时暂停，检查后可继续。</small></section>
      </aside>
    </div>
    <Teleport to="body"><div v-if="help" class="dialog-mask" @click.self="help=false"><section ref="helpDialog" class="app-dialog annotation-help" role="dialog" aria-modal="true" aria-labelledby="annotation-help-title"><h2 id="annotation-help-title">标注快捷键</h2><dl><div v-for="[key,desc] in [['V / B / P','选择 / 画框 / 标点'],['← / →','上一帧 / 下一帧'],['Shift + ← / →','前后跳 10 帧'],['空格 + 拖动','平移放大后的画面'],['Ctrl/⌘ + 滚轮','围绕指针缩放'],['+ / − / 0','放大 / 缩小 / 适应画面'],['Alt + 方向键','选中框微调 1 原图像素'],['Alt + Shift + 方向键','选中框微调 10 原图像素'],['Ctrl/⌘ + Z','撤销本帧操作'],['Ctrl/⌘ + Shift + Z / Ctrl + Y','重做本帧操作'],['C','复制上一帧中当前帧缺少的对象'],['Del / Backspace','删除选中对象'],['H / L / F','隐藏框 / 放大镜 / 专注模式'],['K','播放 / 暂停'],['Esc','先取消当前拖动；无拖动时退出专注或取消选择'],['?','打开快捷键说明']]" :key="key"><dt><kbd>{{ key }}</kbd></dt><dd>{{ desc }}</dd></div></dl><p class="muted">输入框中不触发标注快捷键。播放或加载帧时暂停编辑，避免图像与标注错位。</p><div class="app-dialog-actions"><button class="btn-primary" @click="help=false">知道了</button></div></section></div></Teleport>
  </section>
  <transition name="toast"><div v-if="toastMessage" class="status-toast" role="status">{{ toastMessage }}</div></transition>
</template>
