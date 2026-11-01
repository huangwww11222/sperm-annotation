<script setup lang="ts">
import { nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  source: HTMLImageElement | HTMLVideoElement | null
  box: number[] | null
  /** Optional second box (pixel xyxy) drawn dashed, e.g. another user's version. */
  compare?: number[] | null
  /** Draw both boxes as rectangles (solid = box, dashed = compare). */
  comparing?: boolean
  mediaWidth: number
  mediaHeight: number
  filter?: string
  size?: number
  frameKey?: string | number
}>(), { compare: null, comparing: false, filter: '', size: 160, frameKey: '' })
const canvas = ref<HTMLCanvasElement | null>(null)
const waiting = ref(true)

let boundSource: HTMLImageElement | HTMLVideoElement | null = null
const sourceEvents = ['load', 'error', 'loadeddata', 'seeked', 'emptied', 'loadstart']
function bindSource(source: HTMLImageElement | HTMLVideoElement | null) {
  for (const event of sourceEvents) boundSource?.removeEventListener(event, redraw)
  boundSource = source
  for (const event of sourceEvents) source?.addEventListener(event, redraw)
}
function sourceReady() {
  const source = props.source
  return source instanceof HTMLImageElement ? source.complete && source.naturalWidth > 0 : !!source && source.readyState >= 2 && !source.seeking
}

function draw() {
  const source = props.source, ctx = canvas.value?.getContext('2d')
  const valid = (b: number[] | null | undefined): b is number[] => !!b && b.length === 4 && b.every(Number.isFinite) && b[2] > b[0] && b[3] > b[1]
  const compare = valid(props.compare) ? props.compare : null
  // With a comparison, centre the crop on both boxes so neither is cut off.
  const box = valid(props.box) && compare ? [Math.min(props.box[0], compare[0]), Math.min(props.box[1], compare[1]), Math.max(props.box[2], compare[2]), Math.max(props.box[3], compare[3])] : valid(props.box) ? props.box : compare
  waiting.value = true
  ctx?.clearRect(0, 0, props.size, props.size)
  if (!sourceReady()) return
  if (!source || !ctx || !box || props.mediaWidth <= 0 || props.mediaHeight <= 0) return
  const [x1, y1, x2, y2] = box
  const boxW = x2 - x1, boxH = y2 - y1
  if (boxW <= 0 || boxH <= 0) return
  const maxCrop = Math.min(props.mediaWidth, props.mediaHeight)
  const crop = Math.min(maxCrop, Math.max(boxW, boxH) * 4)
  const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2
  const sx = Math.max(0, Math.min(props.mediaWidth - crop, cx - crop / 2))
  const sy = Math.max(0, Math.min(props.mediaHeight - crop, cy - crop / 2))
  const side = props.size
  ctx.save()
  ctx.clearRect(0, 0, side, side)
  ctx.fillStyle = '#0d1726'
  ctx.fillRect(0, 0, side, side)
  try { ctx.filter = props.filter || 'none' } catch {}
  try { ctx.drawImage(source, sx, sy, crop, crop, 0, 0, side, side) } catch { ctx.restore(); return }
  ctx.restore()
  waiting.value = false
  if (props.comparing) {
    const rect = (b: number[], color: string, dash: number[]) => {
      ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dash)
      ctx.strokeRect((b[0] - sx) / crop * side, (b[1] - sy) / crop * side, (b[2] - b[0]) / crop * side, (b[3] - b[1]) / crop * side)
      ctx.restore()
    }
    if (valid(props.box)) rect(props.box, '#ffb547', [])
    if (compare) rect(compare, '#5fd3f3', [5, 4])
    return
  }
  const targetCx = (cx - sx) / crop * side, targetCy = (cy - sy) / crop * side
  const radiusX = boxW / crop * side / 2, radiusY = boxH / crop * side / 2
  ctx.save()
  ctx.strokeStyle = '#ffb547'
  ctx.lineWidth = 2
  ctx.beginPath()
  ctx.ellipse(targetCx, targetCy, Math.max(2, radiusX), Math.max(2, radiusY), 0, 0, Math.PI * 2)
  ctx.stroke()
  ctx.restore()
}

async function redraw() {
  waiting.value = true
  await nextTick()
  draw()
}

watch(() => [props.source, props.box, props.compare, props.comparing, props.mediaWidth, props.mediaHeight, props.filter, props.frameKey], redraw, { deep: true })
watch(() => props.source, bindSource, { immediate: true })
onBeforeUnmount(() => bindSource(null))
onMounted(redraw)
</script>

<template>
  <div class="review-zoom-preview">
    <canvas ref="canvas" :width="size" :height="size" :style="{ width: `${size}px`, height: `${size}px` }" />
    <span v-if="waiting">正在读取画面…</span>
  </div>
</template>

<style scoped>
.review-zoom-preview { position: relative; width: fit-content; max-width: 100%; }
.review-zoom-preview canvas { display: block; max-width: 100%; border: 1px solid var(--line); border-radius: 7px; background: #0d1726; }
.review-zoom-preview span { position: absolute; inset: 0; display: grid; place-items: center; color: #d6e3ef; background: #0d1726aa; border-radius: 7px; font-size: 12px; }
</style>
