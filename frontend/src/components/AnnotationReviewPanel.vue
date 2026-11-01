<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import ReviewZoomPreview from './ReviewZoomPreview.vue'
import { useAnnotationReview, reviewStatusLabel } from '../stores/annotationReview'
import { useAuth } from '../stores/auth'
import type { CheckResult, ItemKind, ReviewItem } from '../api/annotationReviewApi'
import { reviewCategory, reviewLabels, reviewLocationText } from '../annotation/reviewPresentation'

const props = defineProps<{
  mediaId: string | null; currentFrame: number; blocked: boolean; trackingPending: boolean; flush: () => Promise<unknown>
  previewSource: HTMLImageElement | HTMLVideoElement | null; previewFilter: string
  previewMediaWidth: number; previewMediaHeight: number; actionError?: string
}>()
const emit = defineEmits<{ locate: [item: ReviewItem]; adopt: [item: ReviewItem]; 'add-head': [item: ReviewItem] }>()
const review = useAnnotationReview()
const auth = useAuth()
const { state, busy, error, status, nextItem } = review

// One short explanation per category, shown once instead of on every row.
const groups: { kind: ItemKind; category?: string; label: string; hint: string }[] = [
  { kind: 'unknown', label: '未标注帧', hint: '这些帧没有任何框。补标，或确认确实没有精子。' },
  { kind: 'doubt', category: 'missing', label: reviewLabels.missing, hint: '小圆圈提示可能漏标的位置；请核对是否有未框出的精子头。' },
  { kind: 'doubt', category: 'association', label: reviewLabels.association, hint: '强调的框可能重复或跟错目标，结合前后帧检查。' },
  { kind: 'doubt', category: 'position', label: reviewLabels.position, hint: '检查强调的框是否贴合精子头，可直接微调。' },
  { kind: 'doubt', category: 'static', label: reviewLabels.static, hint: '位置几乎不动的目标，可能是杂质，也可能是不活动的精子。整段视频同一位置只问一次。' },
  { kind: 'doubt', category: 'check', label: reviewLabels.check, hint: '自动检查未完成，请人工核对本段画面。' },
  { kind: 'changed', label: '通过后的改动', hint: '上次审查通过后改过的帧，确认改得对。' },
  { kind: 'count', label: '计数突变', hint: '框数和前后帧差 3 个以上，常见于漏标、重复框或跟错。' },
  { kind: 'coverage', label: '整帧查漏标', hint: '看完整幅画面，找没有框的精子头。' },
  { kind: 'sample', label: '随机抽检', hint: '随机挑的帧，检查每个框和有没有漏标。' },
]
const groupOf = (item: ReviewItem) => groups.find(g => g.kind === item.kind && (item.kind !== 'doubt' || g.category === reviewCategory(item)))!
const round = computed(() => state.value?.status === 'open' ? state.value.review : null)
const items = computed(() => round.value?.items ?? [])
const grouped = computed(() => groups.map(g => ({ ...g, items: items.value.filter(i => groupOf(i) === g) })).filter(g => g.items.length))
const fixedSamples = computed(() => items.value.filter(i => i.kind === 'sample' && i.check?.result === 'fixed').length)
const remaining = computed(() => items.value.filter(i => !i.check).length)
const canPass = computed(() => !!round.value && remaining.value === 0)
const concurrentReviewer = computed(() => {
  const value = round.value
  if (!value || value.lastActorId == null) return null
  const me = Number(auth.user.value?.id)
  if (!Number.isFinite(me) || value.lastActorId === me || !value.lastActivityAt) return null
  const age = Date.now() - Date.parse(value.lastActivityAt)
  if (!Number.isFinite(age) || age < 0 || age > 10 * 60 * 1000) return null
  return { actor: value.lastActorId, minutes: Math.max(1, Math.floor(age / 60000)) }
})

// The card always shows one item: the one last located, else the next unchecked one.
const selectedId = ref<string | null>(null)
const current = computed(() => items.value.find(i => i.id === selectedId.value) ?? nextItem.value)
watch(() => round.value?.reviewId, () => { selectedId.value = null })

const frames = (i: ReviewItem) => i.startFrame === i.endFrame ? `第 ${i.startFrame + 1} 帧` : `第 ${i.startFrame + 1}–${i.endFrame + 1} 帧`
function objectsText(i: ReviewItem) {
  const ids = i.objectIds ?? (i.objectId != null ? [i.objectId] : [])
  if (!ids.length) return ''
  return ids.length <= 4 ? `对象 ${ids.join('、')}` : `对象 ${ids.slice(0, 3).join('、')} 等 ${ids.length} 个`
}
const missing = (i: ReviewItem) => i.kind === 'doubt' && ['missing', 'static'].includes(reviewCategory(i))
const rowText = (i: ReviewItem) => i.source === 'track_ended'
  ? `对象 #${i.objectId} 在第 ${i.frame + 1} 帧起消失：若它仍在画面内，请补框；灰色虚线是上一帧位置。`
  : missing(i) ? reviewLocationText(i) : i.kind === 'doubt' ? objectsText(i) : i.kind === 'count' ? i.message : ''
const sameFrameLocations = computed(() => current.value && missing(current.value) ? items.value.filter(i => missing(i) && i.frame === current.value!.frame).sort((a,b) => (a.index ?? 0)-(b.index ?? 0)) : [])
const currentLocation = computed(() => sameFrameLocations.value.findIndex(i => i.id === current.value?.id))
function nearbyLocation(delta: number) { const item = sameFrameLocations.value[currentLocation.value+delta]; if (item) locate(item) }
const resultLabel: Record<CheckResult, string> = { ok: '无误', fixed: '已修正', empty: '无目标', not_target: '不是精子' }
const adoptsTrack = (i: ReviewItem) => i.kind === 'doubt' && reviewCategory(i) === 'missing' && ['reverse', 'disagreement'].includes(i.source ?? '')
const zoomPreview = computed(() => {
  const item = current.value
  if (!item || item.kind !== 'doubt') return null
  if (!(missing(item) || item.source === 'track_ended')) return null
  const observation = item.observations?.find(o => o.frameIndex === props.currentFrame)
  const box = observation?.candidates[0] ?? item.anchor ?? item.lastBox
  if (!box?.length) return null
  const frame = observation ? props.currentFrame : item.frame
  const sequence = item.index && item.countInFrame ? `${item.index}/${item.countInFrame}` : ''
  const position = [item.region, sequence].filter(Boolean).join(' · ')
  return { box, label: `第 ${frame + 1} 帧${position ? ` · ${position}` : ''}${observation ? '' : ' 的位置'}` }
})

function locate(item: ReviewItem) { selectedId.value = item.id; emit('locate', item) }
async function saved() {
  try { await props.flush(); return true }
  catch (e) { error.value = `标注保存失败，未记录本次审查操作：${(e as Error)?.message || '请重试'}`; return false }
}
async function start() {
  if (!props.mediaId || !await saved()) return
  if (await review.start(props.mediaId) && nextItem.value) locate(nextItem.value)
}
async function mark(item: ReviewItem, result: CheckResult) {
  // Corrections live in the shared workspace; save them before recording the check.
  if (!props.mediaId || !await saved()) return
  if (!await review.check(props.mediaId, item, result)) return
  // Corrections are made before marking, so every result moves on to the next unchecked item.
  if (nextItem.value) locate(nextItem.value)
}
async function pass() {
  if (!props.mediaId || !await saved()) return
  await review.pass(props.mediaId)
}
function actionable() { return !busy.value && !props.blocked && current.value }
function primary() {
  const item = current.value
  if (!actionable() || !item) return
  if (item.kind === 'unknown') return void mark(item, 'empty')
  if (missing(item)) {
    if (adoptsTrack(item)) emit('adopt', item)
    else emit('add-head', item)
    return
  }
  void mark(item, 'ok')
}
function secondary() {
  const item = current.value
  if (!actionable() || !item) return
  void mark(item, item.kind === 'unknown' ? 'fixed' : missing(item) ? 'not_target' : 'fixed')
}
function tertiary() {
  const item = current.value
  if (!actionable() || !item || !missing(item)) return
  void mark(item, 'fixed')
}
function next() {
  const all = items.value
  if (!all.length) return
  const index = all.findIndex(i => i.id === current.value?.id)
  const ordered = [...all.slice(index + 1), ...all.slice(0, Math.max(0, index + 1))]
  const item = ordered.find(i => !i.check) ?? ordered[0]
  if (item) locate(item)
}
defineExpose({ primary, secondary, tertiary, next })
</script>

<template>
  <section class="shell-card review-panel" aria-label="审查清单" data-testid="review-panel">
    <div class="shell-card-head">
      <AppIcon name="check" :size="16" /><strong>审查</strong>
      <span v-if="status" class="review-status" :class="status" data-testid="review-status">{{ reviewStatusLabel[status] }}</span>
    </div>
    <p v-if="!mediaId" class="review-note">选择一个视频开始审查。</p>
    <p v-else-if="!state && !error" class="review-note">正在读取…</p>
    <template v-else-if="state">
      <template v-if="status === 'none' || status === 'stale'">
        <p class="review-note">{{ status === 'stale' ? '通过后标注又改过，只需检查改动的地方。' : '系统会挑出最可能出错的帧，逐个确认；发现错误直接在画面上改。' }}</p>
        <p v-if="state.notReady" class="review-warning" role="status">{{ state.notReady }}</p>
        <p v-if="trackingPending" class="review-warning" role="status">AI 追踪还在等待核对，请先回到标注页处理。</p>
        <button class="btn-primary review-wide" data-testid="review-start" :disabled="busy || blocked || trackingPending || !!state.notReady" @click="start">{{ busy ? '正在生成清单…' : status === 'stale' ? '开始重新审查' : '开始审查' }}</button>
      </template>

      <template v-else-if="status === 'passed' && state.review">
        <p class="review-note">已通过，可以导出训练数据集。之后再改标注需要重新审查。</p>
        <p v-if="state.review.changes" class="review-changes" data-testid="review-changes">本轮修改：移动 {{ state.review.changes.moved }} · 新增 {{ state.review.changes.added }} · 删除 {{ state.review.changes.removed }}</p>
      </template>

      <template v-else-if="round">
        <div class="review-progress" aria-live="polite">
          <progress :max="round.total" :value="round.checked" /><span data-testid="review-progress">{{ round.checked }} / {{ round.total }}</span>
        </div>
        <p v-if="concurrentReviewer" class="review-warning" role="status" data-testid="review-concurrent">用户 #{{ concurrentReviewer.actor }} 也在审查这段视频（最近一次操作 {{ concurrentReviewer.minutes }} 分钟前），同时修改可能互相覆盖。</p>
        <p v-if="round.escalated" class="review-warning" role="status">抽检发现 {{ fixedSamples }} 帧有错，已加抽 10 帧。</p>

        <div v-if="current" class="review-current" data-testid="review-current" :data-item="current.id">
          <div class="review-current-head">
            <span class="review-kind">{{ groupOf(current).label }}</span>
            <button class="review-frame" :title="`跳到${frames(current)}`" @click="locate(current)">{{ frames(current) }}</button>
            <span v-if="current.check" class="review-result" :class="current.check.result">{{ resultLabel[current.check.result] }}</span>
          </div>
          <p v-if="current.check" class="review-checked" data-testid="review-checked">已判为：{{ resultLabel[current.check.result] }}，可改判</p>
          <p v-if="rowText(current)" class="review-what" data-testid="review-location-text">{{ rowText(current) }}</p>
          <p class="review-hint">{{ groupOf(current).hint }}</p>
          <figure v-if="zoomPreview" class="review-zoom" data-testid="review-zoom-preview">
            <ReviewZoomPreview :source="previewSource" :box="zoomPreview.box" :media-width="previewMediaWidth" :media-height="previewMediaHeight" :filter="previewFilter" :frame-key="`${current.id}:${currentFrame}:${zoomPreview.label}`" />
            <figcaption>{{ zoomPreview.label }}</figcaption>
          </figure>
          <div v-if="missing(current) || objectsText(current)" class="review-location-actions">
            <button v-if="sameFrameLocations.length > 1" class="btn-secondary" :disabled="currentLocation <= 0" @click="nearbyLocation(-1)">上一处</button>
            <button class="btn-secondary" data-testid="review-relocate" @click="locate(current)">{{ missing(current) ? '定位此处' : '定位对象' }}</button>
            <button v-if="sameFrameLocations.length > 1" class="btn-secondary" :disabled="currentLocation >= sameFrameLocations.length-1" @click="nearbyLocation(1)">下一处</button>
          </div>
          <div class="review-actions review-actions-many">
            <template v-if="current.kind === 'unknown'">
              <button class="btn-primary" :disabled="busy || blocked" title="确认无目标（快捷键 1）" @click="mark(current, 'empty')">确认无目标</button>
              <button class="btn-secondary" :disabled="busy || blocked" title="已补标（快捷键 2）" @click="mark(current, 'fixed')">已补标</button>
            </template>
            <template v-else-if="missing(current)">
              <button v-if="adoptsTrack(current)" class="btn-primary" data-testid="review-adopt-track" :disabled="busy || blocked" title="采用这段轨迹（快捷键 1）" @click="emit('adopt', current)">采用这段轨迹</button>
              <button v-else class="btn-primary" data-testid="review-add-head" :disabled="busy || blocked" title="在当前候选位置补一个头部框（快捷键 1）" @click="emit('add-head', current)">是精子，在这里补框</button>
              <button class="btn-secondary" :disabled="busy || blocked" title="记住这个位置，以后不再提示（快捷键 2）" @click="mark(current, 'not_target')">不是精子</button>
              <button class="btn-secondary" title="已手动补框后记录（快捷键 3）" :disabled="busy || blocked" @click="mark(current, 'fixed')">已手动补</button>
            </template>
            <template v-else>
              <button class="btn-primary" :disabled="busy || blocked" title="无误（快捷键 1）" @click="mark(current, 'ok')">无误</button>
              <button class="btn-secondary" title="先在画面上改好，再点这里（快捷键 2）" :disabled="busy || blocked" @click="mark(current, 'fixed')">已修正</button>
            </template>
          </div>
          <p v-if="adoptsTrack(current) && current.adoptObjectId" class="review-hint" data-testid="review-adopt-note">将在 {{ current.observations?.length ?? 0 }} 帧补上 SAM3 反向追踪的框，编号 #{{ current.adoptObjectId }}；可用“删除全视频中此对象”撤回。</p>
          <p v-if="current.detectorAgrees" class="review-hint">检测器也在这里看到了头部。</p>
          <p v-if="!adoptsTrack(current) && missing(current)" class="review-hint">这里只补第 {{ current.frame + 1 }} 帧；其他帧若也缺，请逐帧补，或等待“只追踪新对象”功能。</p>
          <p v-if="actionError" class="review-warning" role="alert" data-testid="review-action-error">{{ actionError }}</p>
        </div>

        <button class="btn-primary review-wide" data-testid="review-pass" :disabled="!canPass || busy || blocked" @click="pass">{{ busy ? '正在处理…' : canPass ? '审查通过' : `还剩 ${remaining} 项` }}</button>

        <div class="review-groups">
          <details v-for="g in grouped" :key="g.category || g.kind" class="review-group" :data-review-group="g.category || g.kind" :open="g.items.some(i => !i.check) || g.items.some(i => i.id === current?.id)">
            <summary><span>{{ g.label }}</span><small>{{ g.items.filter(i => i.check).length }} / {{ g.items.length }}</small></summary>
            <div v-for="item in g.items" :key="item.id" class="review-row" :class="{ done: item.check, current: item.id === current?.id }" :data-review-item="item.id" role="button" tabindex="0" :aria-current="item.id === current?.id" @click="locate(item)" @keydown.enter.prevent="locate(item)" @keydown.space.prevent="locate(item)">
              <i class="review-dot" :class="item.check?.result" aria-hidden="true" />
              <span class="review-row-frame">{{ frames(item) }}</span>
              <span class="review-row-text">{{ rowText(item) }}</span>
              <small v-if="item.check">{{ resultLabel[item.check.result] }}</small>
              <button v-if="item.check" class="review-override" title="改判这一项" @click.stop="locate(item)">改判</button>
            </div>
          </details>
        </div>
      </template>
    </template>
    <div v-if="error" class="error-banner" role="alert">{{ error }} <button class="quiet-button" :disabled="busy" @click="review.load(mediaId)">重新读取</button></div>
  </section>
</template>

<style scoped>
.review-panel { display: flex; flex-direction: column; gap: 10px; min-height: 0; flex: 1 1 0; overflow-y: auto; overscroll-behavior: contain; }
.review-status { margin-left: auto; font-size: 12px; padding: 1px 8px; border-radius: 10px; background: var(--surface-subtle); color: var(--muted); }
.review-status.open { background: var(--accent-soft); color: var(--accent); }
.review-status.passed { background: var(--b-soft); color: var(--success); }
.review-status.stale { background: var(--warning-soft); color: var(--warning); }
.review-note { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.5; }
.review-warning { margin: 0; background: var(--warning-soft); color: var(--warning); border-radius: 6px; padding: 6px 8px; font-size: 12px; }
.review-changes { margin: 0; font-size: 12px; color: var(--text); }
.review-wide { width: 100%; }
.review-progress { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.review-progress progress { flex: 1; height: 6px; }
.review-current { border: 1px solid var(--accent); border-radius: 8px; padding: 10px; background: var(--surface); display: flex; flex-direction: column; gap: 6px; }
.review-current-head { display: flex; align-items: center; gap: 8px; }
.review-kind { font-size: 12px; font-weight: 600; color: var(--accent); }
.review-frame { font-size: 13px; font-weight: 600; color: var(--text); background: none; border: 0; padding: 0; cursor: pointer; text-decoration: underline dotted; text-underline-offset: 3px; }
.review-what { margin: 0; font-size: 13px; color: var(--text); line-height: 1.45; }
.review-hint { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.45; }
.review-zoom { margin: 0; display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
.review-zoom figcaption { font-size: 11px; color: var(--muted); }
.review-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 2px; }
.review-actions-many { grid-template-columns: repeat(auto-fit,minmax(96px,1fr)); }
.review-location-actions { display: flex; gap: 6px; }
.review-location-actions button { flex: 1; min-width: 0; padding: 5px 6px; font-size: 12px; }
.review-result { margin-left: auto; font-size: 11px; color: var(--success); }
.review-result.fixed { color: var(--warning); }
.review-checked { margin: 0; font-size: 12px; color: var(--success); }
.review-override { flex: none; padding: 2px 5px; border: 1px solid var(--line); border-radius: 5px; background: var(--surface); color: var(--text); font-size: 11px; }
.review-groups { display: flex; flex-direction: column; gap: 6px; flex: none; min-height: 0; }
.review-group summary { display: flex; justify-content: space-between; align-items: center; cursor: pointer; font-size: 12px; font-weight: 600; color: var(--text); padding: 4px 2px; }
.review-group summary small { font-weight: 400; color: var(--muted); font-variant-numeric: tabular-nums; }
.review-row { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 30px; padding: 4px 6px; border: 0; border-radius: 5px; background: none; text-align: left; font-size: 12px; color: var(--text); cursor: pointer; }
.review-row:hover { background: var(--surface-hover); }
.review-row.current { background: var(--accent-soft); }
.review-row.done { color: var(--muted); }
.review-row-frame { flex: none; font-variant-numeric: tabular-nums; }
.review-row-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.review-row small { flex: none; color: var(--muted); }
.review-dot { flex: none; width: 8px; height: 8px; border-radius: 50%; border: 1.5px solid var(--muted); }
.review-dot.ok, .review-dot.empty { background: var(--success); border-color: var(--success); }
.review-dot.fixed { background: var(--warning); border-color: var(--warning); }
</style>
