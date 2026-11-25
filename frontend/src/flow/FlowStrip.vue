<script setup lang="ts">
// One target as a strip of small pictures: standing on one frame, or playing through them.
import { computed, onUnmounted, ref, watch } from 'vue'
import { flowApi, type FlowView } from '../api/flowApi'
import { cellPosition, stepFrame, type FlowTile } from './flowLogic'
import { tick, useTicker } from './ticker'

const props = withDefaults(defineProps<{ mediaId: string; run: string; tile: FlowTile; view: FlowView; playing?: boolean; cell?: number | null; skip?: number[] }>(), { playing: false, cell: null, skip: () => [] })
const emit = defineEmits<{ ready: [ok: boolean]; frame: [index: number] }>()
const url = ref(''), failed = ref(false), at = ref(props.cell ?? props.tile.best)
useTicker()
let seq = 0
const release = () => { if (url.value) URL.revokeObjectURL(url.value); url.value = '' }
async function load() {
  const mine = ++seq
  failed.value = false
  let next = ''
  try {
    next = URL.createObjectURL(await flowApi.strip(props.mediaId, props.run, props.tile.key, props.view))
    const image = new Image()
    image.src = next
    await image.decode()  // A damaged picture must not pass for an empty one.
    if (mine !== seq) { URL.revokeObjectURL(next); return }
    release()
    url.value = next
    emit('ready', true)
  } catch (error) {
    if (next) URL.revokeObjectURL(next)
    if (mine !== seq) return
    console.error('[flow.strip_failed]', { mediaId: props.mediaId, key: props.tile.key, view: props.view, error })
    failed.value = true
    emit('ready', false)
  }
}
watch(() => [props.mediaId, props.run, props.tile.key, props.view], load, { immediate: true })
onUnmounted(() => { seq++; release() })

const skipped = computed(() => new Set(props.skip))
watch(tick, () => {
  if (props.cell !== null) return
  const next = props.playing ? stepFrame(props.tile.n, at.value, skipped.value) : props.tile.best
  if (next !== at.value) { at.value = next; emit('frame', next) }
})
const style = computed(() => ({
  backgroundImage: url.value ? `url("${url.value}")` : 'none',
  backgroundSize: `${props.tile.n * 100}% 100%`,
  backgroundPosition: cellPosition(props.tile.n, props.cell ?? at.value),
}))
</script>

<template>
  <span class="flow-strip" :class="{ loading: !url && !failed }" :style="style">
    <span v-if="failed" class="flow-strip-failed" role="button" tabindex="0" @click.stop="load" @keydown.enter.stop.prevent="load">没加载出来<b>点这里重试</b></span>
  </span>
</template>
