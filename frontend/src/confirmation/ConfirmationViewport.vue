<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { Change, FrameContext } from '../api/confirmationApi'
import { useViewportNavigation } from '../workbench/useViewportNavigation'
import { canvasZoomKey } from '../workbench/viewTransform'
import ConfirmationImage from './ConfirmationImage.vue'

const p=withDefaults(defineProps<{src:string;width:number;height:number;mediaId:string;item?:Change;context:FrameContext|null;zoom:number;selectableObjectIds:number[];interactive:boolean;showBoxes:boolean;blocked?:boolean}>(),{blocked:false})
const emit=defineEmits<{select:[objectId:number]}>()
const viewport=ref<HTMLElement|null>(null),stage=ref<HTMLElement|null>(null),scale=ref(1),sized=ref(false)
let followsFit=true
const unavailable=computed(()=>p.blocked||!p.src||!p.context)
const nav=useViewportNavigation({viewport,stage,scale,minScale:.02,maxScale:8,blocked:()=>unavailable.value,beforeZoom:()=>{followsFit=false}})
const stageStyle=computed(()=>({width:`${p.width*scale.value}px`,height:`${p.height*scale.value}px`}))
let observer:ResizeObserver|null=null, fittedMedia=''
function fit(){
  const el=viewport.value
  if(!el||!p.width||!p.height||el.clientWidth<=0||el.clientHeight<=0)return
  nav.cancelPan();followsFit=true
  scale.value=Math.min(el.clientWidth/p.width,el.clientHeight/p.height,8)
  sized.value=true;fittedMedia=p.mediaId
  void nextTick(()=>{if(viewport.value){viewport.value.scrollLeft=0;viewport.value.scrollTop=0}})
}
function zoomBy(delta:number){void nav.zoomBy(delta)}
function wheel(e:WheelEvent){
  if((!e.ctrlKey&&!e.metaKey)||unavailable.value||document.querySelector('[data-user-guide][open]'))return
  e.preventDefault()
  if(e.deltaY)void nav.zoomBy(e.deltaY<0?.1:-.1,{x:e.clientX,y:e.clientY})
}
function auxiliaryClick(e:MouseEvent){if(e.button===1)e.preventDefault()}
function keyDown(e:KeyboardEvent){
  if(unavailable.value||e.isComposing||e.keyCode===229)return false
  const command=canvasZoomKey(e)
  if(command==='in'){e.preventDefault();zoomBy(.1);return true}
  if(command==='out'){e.preventDefault();zoomBy(-.1);return true}
  if(command==='fit'){e.preventDefault();fit();return true}
  if(e.code==='Space'||e.key===' '){e.preventDefault();nav.spaceHeld.value=true;return true}
  return false
}
watch(()=>p.mediaId,()=>{sized.value=false;followsFit=true;void nextTick(fit)})
watch(()=>p.src,()=>{if(p.src&&(!sized.value||fittedMedia!==p.mediaId))void nextTick(fit)})
onMounted(()=>{
  observer=new ResizeObserver(()=>{if(!sized.value||followsFit)fit()})
  if(viewport.value)observer.observe(viewport.value)
  void nextTick(fit)
})
onUnmounted(()=>{observer?.disconnect();nav.cancelPan()})
function captureAnchor(){nav.cancelPan();return {point:nav.captureAnchor(),fitting:followsFit}}
async function restoreAnchor(saved:{point:ReturnType<typeof nav.captureAnchor>;fitting:boolean}|null){
  if(!saved)return
  if(saved.fitting){await nextTick();fit()}
  else {followsFit=false;await nav.restoreAnchor(saved.point)}
}
defineExpose({zoomBy,fit,keyDown,keyUp:nav.keyUp,blur:nav.blur,captureAnchor,restoreAnchor})
</script>

<template>
  <div class="c-frame-viewer">
    <div ref="viewport" class="c-frame-viewport" :class="{'is-space-held':nav.spaceHeld.value,'is-panning':nav.panning.value}" role="region" aria-label="完整帧画面" :data-scale="scale" :data-view-ready="sized" @pointerdown="nav.startPan" @pointermove="nav.movePan" @pointerup="nav.endPan" @pointercancel="nav.endPan" @lostpointercapture="nav.cancelPan" @wheel="wheel" @click.capture="nav.consumeClick" @auxclick="auxiliaryClick">
      <div ref="stage" class="c-frame-stage" :style="stageStyle">
        <ConfirmationImage v-if="src&&context" :src="src" :width="width" :height="height" :item="item" :context="context" variant="full" :zoom="zoom" :show-boxes="showBoxes" :selectable-object-ids="selectableObjectIds" :interactive="interactive" @select="emit('select',$event)"/>
      </div>
      <p v-if="!src||!context" class="c-empty">当前帧图像不可用，请重试加载。</p>
    </div>
    <div class="c-frame-controls" role="group" aria-label="画面缩放">
      <span>完整帧</span><button aria-label="缩小画面" :disabled="unavailable||scale<=.02" @click="zoomBy(-.1)">−</button><output aria-label="画面倍率">{{ Math.round(scale*100) }}%</output><button aria-label="放大画面" :disabled="unavailable||scale>=8" @click="zoomBy(.1)">+</button><button aria-label="适应画面" :disabled="unavailable" @click="fit">适应</button>
    </div>
  </div>
</template>
