<script setup lang="ts">
// First pass: a page of small films, click the ones that look like a sperm.
import { computed, ref, watch } from 'vue'
import { flowApi } from '../api/flowApi'
import { useFlow } from '../stores/flow'
import { fitGrid } from './flowLogic'
import FlowStrip from './FlowStrip.vue'
import { useSize } from './useSize'

const flow = useFlow()
const { detail, page, picked, view, playing, unsaved } = flow
const area = ref<HTMLElement | null>(null), { width, height } = useSize(area)
const tiles = computed(() => detail.value?.pages[page.value] ?? [])
const picks = computed(() => new Set(picked.value[page.value] ?? []))
const fit = computed(() => fitGrid(tiles.value.length, width.value - 24, height.value - 16, 10))

// A page counts as seen only when every picture on it could be seen.
const loaded = ref(new Set<string>()), broken = ref(new Set<string>())
watch([page, () => detail.value?.progress.runId], () => { loaded.value = new Set(); broken.value = new Set() })
function ready(key: string, ok: boolean) {
  const [add, drop] = ok ? [loaded, broken] : [broken, loaded]
  add.value = new Set(add.value).add(key)
  if (drop.value.has(key)) { const rest = new Set(drop.value); rest.delete(key); drop.value = rest }
}
const complete = computed(() => tiles.value.every(tile => loaded.value.has(tile.key)))
const last = computed(() => page.value + 1 >= (detail.value?.pages.length ?? 0))
const label = computed(() => {
  if (broken.value.size) return '有图没加载出来'
  if (!complete.value) return '图片加载中…'
  return `${picks.value.size ? `选了 ${picks.value.size} 个` : '都不像'}，${last.value ? '看完了' : '下一页'}`
})
const busy = ref(false)
async function next() {
  if (!complete.value || busy.value) return
  busy.value = true
  try { await flow.nextPage() } finally { busy.value = false }
}
defineExpose({ next, previous: flow.previousPage })

// The next page is usually asked for within a second.
watch([page, view, complete], () => {
  const d = detail.value
  if (!d || !complete.value) return
  for (const tile of d.pages[page.value + 1] ?? []) void flowApi.strip(d.mediaId, d.progress.runId, tile.key, view.value).catch(() => { /* Shown as broken when the page opens. */ })
}, { immediate: true })

const big = ref(''), origin = ref('50% 50%')
let timer: ReturnType<typeof setTimeout> | null = null
function enter(event: MouseEvent, key: string) {
  const tile = event.currentTarget as HTMLElement
  timer = setTimeout(() => {
    const r = tile.getBoundingClientRect(), m = area.value!.getBoundingClientRect()
    const along = (from: number, to: number, size: number) => from < size * .45 ? '0%' : to < size * .45 ? '100%' : '50%'
    origin.value = `${along(r.left - m.left, m.right - r.right, r.width)} ${along(r.top - m.top, m.bottom - r.bottom, r.height)}`
    big.value = key
  }, 380)
}
function leave() { if (timer) clearTimeout(timer); timer = null; big.value = '' }
watch(page, leave)
</script>

<template>
  <div ref="area" class="flow-main">
    <div v-if="detail" :key="page" class="flow-wall flow-in" :style="{ '--cols': fit.cols, '--tile': `${fit.size}px` }" data-testid="flow-wall">
      <button v-for="(tile, index) in tiles" :key="tile.key" class="flow-tile" :class="{ on: picks.has(tile.key), big: big === tile.key }" :style="big === tile.key ? { transformOrigin: origin } : undefined" :aria-pressed="picks.has(tile.key)" :aria-label="`第 ${index + 1} 个`" data-testid="flow-tile" @click="flow.togglePick(tile.key)" @mouseenter="enter($event, tile.key)" @mouseleave="leave">
        <FlowStrip :media-id="detail.mediaId" :run="detail.progress.runId" :tile="tile" :view="view" :playing="playing || big === tile.key" @ready="ready(tile.key, $event)" />
      </button>
    </div>
  </div>
  <footer v-if="detail" class="flow-foot">
    <div class="flow-bar"><i :style="{ width: `${Math.round(page / detail.pages.length * 100)}%` }" /></div>
    <div class="flow-row">
      <button class="flow-quiet" :disabled="page === 0" @click="flow.previousPage">上一页</button>
      <button class="flow-primary" data-testid="flow-next-page" :disabled="!complete || busy" @click="next">{{ busy && last ? '正在准备下一步…' : label }} <kbd>Enter</kbd></button>
      <span class="flow-count">{{ page + 1 }} / {{ detail.pages.length }}</span>
    </div>
  </footer>
</template>
