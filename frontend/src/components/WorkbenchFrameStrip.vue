<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

// One cell per source frame, coloured by a page-defined state; long videos are
// merged into buckets (highest-priority state wins) so the strip stays one row.
// Clicking seeks only; it never edits, submits or chooses anything.
const props = defineProps<{
  frameCount: number
  current: number
  disabled?: boolean
  priority: string[]
  kindOf: (frame: number) => string
  flagsOf?: (frame: number) => string[]
  titleOf: (frame: number) => string
}>()
const emit = defineEmits<{ seek: [frame: number] }>()

// Each cell needs at least 3 px (1 px + 2 px gap); merge frames when the strip is narrower.
const MAX_CELLS = 240
const root = ref<HTMLElement | null>(null), width = ref(0)
let observer: ResizeObserver | null = null
onMounted(() => { observer = new ResizeObserver(() => { width.value = root.value?.clientWidth || 0 }); if (root.value) observer.observe(root.value) })
onUnmounted(() => observer?.disconnect())
const capacity = computed(() => width.value ? Math.max(1, Math.min(MAX_CELLS, Math.floor(width.value / 3))) : MAX_CELLS)
const bucket = computed(() => Math.max(1, Math.ceil(props.frameCount / capacity.value)))
const rank = (kind: string) => { const i = props.priority.indexOf(kind); return i < 0 ? props.priority.length : i }
const cells = computed(() => {
  const out: { start: number; kind: string; flags: string[]; title: string }[] = []
  for (let start = 0; start < props.frameCount; start += bucket.value) {
    const end = Math.min(props.frameCount - 1, start + bucket.value - 1)
    let kind = '', flags = new Set<string>()
    for (let f = start; f <= end; f++) {
      const k = props.kindOf(f)
      if (!kind || rank(k) < rank(kind)) kind = k
      for (const flag of props.flagsOf?.(f) ?? []) flags.add(flag)
    }
    const title = start === end ? props.titleOf(start) : `第 ${start + 1}–${end + 1} 帧`
    out.push({ start, kind, flags: [...flags], title })
  }
  return out
})
const currentCell = computed(() => Math.floor(props.current / bucket.value))

function pick(event: MouseEvent) {
  if (props.disabled || !props.frameCount) return
  const r = (event.currentTarget as HTMLElement).getBoundingClientRect()
  emit('seek', Math.min(props.frameCount - 1, Math.max(0, Math.floor((event.clientX - r.left) / r.width * props.frameCount))))
}
</script>

<template>
  <div ref="root" class="frame-strip" :class="{ disabled }" role="slider" tabindex="-1" aria-label="帧条：点击跳到对应帧" :aria-valuenow="current + 1" :aria-valuemin="1" :aria-valuemax="frameCount" data-testid="frame-strip" @click="pick">
    <i v-for="(cell, i) in cells" :key="cell.start" :class="[cell.kind, ...cell.flags, { current: i === currentCell }]" :title="cell.title" />
  </div>
</template>

<style scoped>
.frame-strip{flex:1;min-width:0;height:26px;display:flex;gap:2px;align-items:stretch;cursor:pointer;padding:2px 0;overflow:hidden}
.frame-strip.disabled{cursor:not-allowed;opacity:.6}
.frame-strip i{flex:1;min-width:1px;border-radius:2px;background:var(--surface-subtle);border:1px solid var(--line);position:relative}
.frame-strip i.ai,.frame-strip i.submitted{background:var(--b-soft);border-color:var(--b-line)}
.frame-strip i.manual{background:var(--a-soft);border-color:var(--a-line)}
.frame-strip i.draft{background:var(--warning-soft);border-color:var(--warning)}
.frame-strip i.anomaly::after,.frame-strip i.returned::after{content:'';position:absolute;left:0;right:0;bottom:-1px;height:3px;background:var(--danger);border-radius:0 0 2px 2px}
.frame-strip i.paused{outline:2px solid var(--warning);outline-offset:0}
.frame-strip i.current{border:2px solid var(--text);transform:scaleY(1.12)}
</style>
