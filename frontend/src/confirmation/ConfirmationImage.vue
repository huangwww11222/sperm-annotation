<script setup lang="ts">
import { computed } from 'vue'
import type { Change, FrameContext } from '../api/confirmationApi'
import { comparisonCrop, rect } from './geometry'
const p=defineProps<{src:string;width:number;height:number;item?:Change;context:FrameContext;variant:'A'|'B'|'overlay'|'full';zoom:number}>()
const emit=defineEmits<{select:[objectId:number]}>()
const view=computed(()=>p.variant==='full'||!p.item?`0 0 ${p.width} ${p.height}`:comparisonCrop(p.item.beforeBbox,p.item.afterBbox,p.width,p.height,p.zoom))
</script>
<template>
  <svg :viewBox="view" class="confirmation-image" :data-variant="variant" role="img" :aria-label="variant==='full'?'当前帧完整上下文':variant==='overlay'?'A 与 B 叠加对比':variant==='A'?'原始标注 A 局部':'审查标注 B 局部'">
    <image :href="src" :width="width" :height="height" />
    <g v-if="variant==='full'" class="context-boxes">
      <rect v-for="o in context.baselineObjects" :key="o.objectId" v-bind="rect(o.bbox)" :opacity="o.objectId===item?.objectId?0:.35" @click="emit('select',o.objectId)" />
    </g>
    <rect v-if="item && variant!=='B'" v-bind="rect(item.beforeBbox)" class="confirm-box-a" />
    <rect v-if="item && variant!=='A'" v-bind="rect(item.afterBbox)" class="confirm-box-b" />
  </svg>
</template>
