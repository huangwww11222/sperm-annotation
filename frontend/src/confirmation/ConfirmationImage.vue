<script setup lang="ts">
import { computed } from 'vue'
import { createRequestId } from '../utils/browserCompat'
import type { Change, FrameContext } from '../api/confirmationApi'
import { comparisonCrop, rect } from './geometry'
const p=withDefaults(defineProps<{src:string;width:number;height:number;item?:Change;context:FrameContext;variant:'A'|'B'|'overlay'|'full';zoom:number;selectableObjectIds?:number[];interactive?:boolean}>(),{interactive:true})
const emit=defineEmits<{select:[objectId:number]}>()
const view=computed(()=>p.variant==='full'||!p.item?`0 0 ${p.width} ${p.height}`:comparisonCrop(p.item.beforeBbox,p.item.afterBbox,p.width,p.height,p.zoom))
const cropClipId=`confirmation-crop-${createRequestId()}`
const viewRect=computed(()=>{const [x,y,width,height]=view.value.split(' ').map(Number);return {x,y,width,height}})
const selectable=computed(()=>new Set(p.selectableObjectIds||[]))
const locator=computed(()=>{
  if(!p.item)return null
  const a=p.item.beforeBbox,b=p.item.afterBbox,pad=Math.max(p.width,p.height)*.008
  return rect([Math.max(0,Math.min(a[0],b[0])-pad),Math.max(0,Math.min(a[1],b[1])-pad),Math.min(p.width,Math.max(a[2],b[2])+pad),Math.min(p.height,Math.max(a[3],b[3])+pad)])
})
function select(objectId:number){if(p.interactive&&selectable.value.has(objectId))emit('select',objectId)}
</script>
<template>
  <svg :viewBox="view" class="confirmation-image" :data-variant="variant" :role="variant==='full'?'group':'img'" :aria-label="variant==='full'?'当前帧完整上下文':variant==='overlay'?'A 与 B 叠加对比':variant==='A'?'原始标注 A 局部':'审查标注 B 局部'">
    <defs><clipPath :id="cropClipId"><rect v-bind="viewRect" style="fill:white;stroke:none" /></clipPath></defs>
    <g :clip-path="`url(#${cropClipId})`">
    <image :href="src" :width="width" :height="height" />
    <g v-if="variant==='full'" class="context-boxes">
      <rect v-for="o in context.baselineObjects" :key="o.objectId" :data-object-id="o.objectId" v-bind="rect(o.bbox)" :opacity="o.objectId===item?.objectId?0:selectable.has(o.objectId)?.7:.25" :class="{'context-selectable':selectable.has(o.objectId)&&interactive}" :role="selectable.has(o.objectId)?'button':undefined" :tabindex="selectable.has(o.objectId)&&interactive?0:undefined" :aria-label="selectable.has(o.objectId)?`查看对象 #${o.objectId} 的修改`:undefined" :aria-disabled="selectable.has(o.objectId)?!interactive:undefined" @click="select(o.objectId)" @keydown.enter.prevent.stop="select(o.objectId)" @keydown.space.prevent.stop="select(o.objectId)" />
    </g>
    <rect v-if="variant==='full'&&locator" v-bind="locator" class="context-current" aria-hidden="true" />
    <rect v-if="item && variant!=='B'" v-bind="rect(item.beforeBbox)" class="confirm-box-a" />
    <rect v-if="item && variant!=='A'" v-bind="rect(item.afterBbox)" class="confirm-box-b" />
    </g>
  </svg>
</template>
