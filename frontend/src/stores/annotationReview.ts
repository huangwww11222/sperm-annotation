import { computed, ref } from 'vue'
import { annotationReviewApi as api, type CheckResult, type Failure, type ReviewItem, type ReviewState, type ReviewStatus } from '../api/annotationReviewApi'

// One review state for the selected server video, shared by the review panel, the
// frame strip, the header status and the export gate.
const state = ref<ReviewState | null>(null)
const summary = ref<Record<string, ReviewStatus>>({})
const busy = ref(false)
const error = ref('')
let loadSeq = 0

const status = computed<ReviewStatus | null>(() => state.value?.status ?? null)
const items = computed<ReviewItem[]>(() => state.value?.status === 'open' ? state.value.review?.items ?? [] : [])
const nextItem = computed(() => items.value.find(item => !item.check) ?? null)

function describe(e: unknown) {
  const f = e as Failure
  return `${f?.message || '审查请求失败，请重试'}${f?.requestId ? `（记录号 ${f.requestId}）` : ''}`
}

function apply(next: ReviewState) {
  state.value = next
  summary.value = { ...summary.value, [next.mediaId]: next.status }
}

async function load(mediaId: string | null | undefined) {
  const seq = ++loadSeq
  if (!mediaId) { state.value = null; return }
  if (state.value?.mediaId !== mediaId) state.value = null
  try {
    const next = await api.status(mediaId)
    if (seq === loadSeq) { apply(next); error.value = '' }
  } catch (e) {
    if (seq === loadSeq) error.value = describe(e)
  }
}

async function run(action: () => Promise<ReviewState>) {
  if (busy.value) return false
  busy.value = true; error.value = ''
  try { apply(await action()); return true }
  catch (e) { error.value = describe(e); return false }
  finally { busy.value = false }
}

async function loadSummary() {
  try { summary.value = (await api.summary()).items }
  catch (e) { console.warn('[annotation_review.summary_failed]', e) }
}

export function useAnnotationReview() {
  return {
    state, summary, busy, error, status, items, nextItem,
    load, loadSummary,
    start: (mediaId: string) => run(() => api.start(mediaId)),
    check: (mediaId: string, item: ReviewItem, result: CheckResult) => run(() => api.check(mediaId, state.value!.review!.reviewId, item.id, result)),
    pass: (mediaId: string) => run(() => api.pass(mediaId, state.value!.review!.reviewId)),
  }
}

export const reviewStatusLabel: Record<ReviewStatus, string> = {
  none: '待审查', open: '审查中', passed: '审查通过', stale: '需重新审查',
}
