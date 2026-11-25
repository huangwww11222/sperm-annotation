<script setup lang="ts">
// Some videos start with one whole frame and no candidates on it: what people click here and the
// program never found is what the program misses.
import { computed, onUnmounted, ref, watch } from 'vue'
import { planBlocks } from '../annotation/scanBlocks'
import { flowApi } from '../api/flowApi'
import { useFlow } from '../stores/flow'
import { useSize } from './useSize'

const flow = useFlow()
const { detail, points, view } = flow
const area = ref<HTMLElement | null>(null), { width, height } = useSize(area)
const scan = computed(() => detail.value?.scan ?? null)
const blocks = computed(() => scan.value ? planBlocks(scan.value.width, scan.value.height, width.value - 24, height.value - 12) : [])
const at = ref(0)
watch(() => blocks.value.length, count => { at.value = Math.min(at.value, Math.max(0, count - 1)) })
watch(() => detail.value?.progress.runId, () => { at.value = 0 })
const block = computed(() => blocks.value[at.value] ?? null)
const box = computed(() => {
  if (!scan.value || !block.value) return null
  const x = block.value.x0 * scan.value.width, y = block.value.y0 * scan.value.height
  const w = (block.value.x1 - block.value.x0) * scan.value.width, h = (block.value.y1 - block.value.y0) * scan.value.height
  const s = Math.min((width.value - 24) / w, (height.value - 12) / h)
  return { x, y, w, h, s }
})
const shown = computed(() => points.value.map(([x, y], index) => ({ x, y, index })).filter(p => box.value && p.x >= box.value.x && p.x <= box.value.x + box.value.w && p.y >= box.value.y && p.y <= box.value.y + box.value.h))

const url = ref(''), failed = ref(false)
let seq = 0
const release = () => { if (url.value) URL.revokeObjectURL(url.value); url.value = '' }
async function load() {
  const d = detail.value, mine = ++seq
  if (!d) return
  failed.value = false
  let next = ''
  try {
    next = URL.createObjectURL(await flowApi.scanImage(d.mediaId, d.progress.runId, view.value))
    const image = new Image()
    image.src = next
    await image.decode()
    if (mine !== seq) { URL.revokeObjectURL(next); return }
    release(); url.value = next
  } catch (error) {
    if (next) URL.revokeObjectURL(next)
    if (mine === seq) { failed.value = true; console.error('[flow.scan_image_failed]', error) }
  }
}
watch([() => detail.value?.mediaId, () => detail.value?.progress.runId, view], load, { immediate: true })
onUnmounted(() => { seq++; release() })

function click(event: MouseEvent) {
  const r = (event.currentTarget as HTMLElement).getBoundingClientRect(), b = box.value
  if (!b || !url.value) return
  flow.addPoint(Math.min(scan.value!.width, Math.max(0, b.x + (event.clientX - r.left) / b.s)), Math.min(scan.value!.height, Math.max(0, b.y + (event.clientY - r.top) / b.s)))
}
const last = computed(() => at.value + 1 >= blocks.value.length)
const busy = ref(false)
async function next() {
  if (!url.value || busy.value) return
  if (!last.value) { at.value++; return }
  busy.value = true
  try { await flow.finishScan() } finally { busy.value = false }
}
function previous() { if (at.value > 0) at.value-- }
defineExpose({ next, previous })
const columns = computed(() => Math.max(1, ...blocks.value.map(entry => entry.col + 1)))
const grid = computed(() => blocks.value.map((entry, index) => ({ ...entry, index })).sort((a, b) => a.row - b.row || a.col - b.col))
</script>

<template>
  <div ref="area" class="flow-main">
    <div v-if="box && url" :key="at" class="flow-scan flow-in" :style="{ width: `${Math.floor(box.w * box.s)}px`, height: `${Math.floor(box.h * box.s)}px` }" data-testid="flow-scan" @click="click">
      <img :src="url" alt="" draggable="false" :style="{ width: `${scan!.width * box.s}px`, left: `${-box.x * box.s}px`, top: `${-box.y * box.s}px` }" />
      <button v-for="point in shown" :key="point.index" class="flow-ring" :style="{ left: `${(point.x! - box.x) * box.s}px`, top: `${(point.y! - box.y) * box.s}px` }" aria-label="去掉这个点" @click.stop="flow.removePoint(point.index)" />
    </div>
    <p v-else-if="failed" class="flow-note">整帧图片没加载出来 <button class="flow-quiet" @click="load">重试</button></p>
    <p v-else class="flow-note">正在加载…</p>
  </div>
  <footer class="flow-foot">
    <div class="flow-row">
      <div class="flow-blocks" :style="{ '--cols': columns }" aria-hidden="true"><i v-for="entry in grid" :key="entry.index" :class="{ done: entry.index < at, now: entry.index === at }" /></div>
      <button class="flow-quiet" :disabled="at === 0" @click="previous">上一块</button>
      <button class="flow-primary" data-testid="flow-scan-next" :disabled="!url || busy" @click="next">{{ last ? '看完了' : '下一块' }} <kbd>Enter</kbd></button>
      <span class="flow-count">点了 {{ points.length }} 个</span>
    </div>
  </footer>
</template>
