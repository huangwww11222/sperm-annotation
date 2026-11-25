// Microfluidic mode: the list of videos, the one being judged, and the answers on their way to the server.
// Answers show at once and are sent one after another; what could not be sent stays marked until it is.
import { computed, ref } from 'vue'
import { flowApi, type FlowDetail, type FlowFailure, type FlowView } from '../api/flowApi'
import { trackApi } from '../api/trackApi'
import { firstOpenPage, nextCard, nextTodo, toggleDropped, type FlowAnswer, type FlowItem, type FlowProgress, type FlowStep } from '../flow/flowLogic'
import { SaveQueue } from '../flow/saveQueue'
import { askConfirm } from '../utils/confirmDialog'
import { useAuth } from './auth'
import { showToast } from './workspace/state'

const items = ref<FlowItem[]>([]), listReady = ref(false), listError = ref('')
const currentId = ref<string | null>(null), detail = ref<FlowDetail | null>(null), loading = ref(false), loadError = ref('')
const step = ref<FlowStep>('wall'), page = ref(0), card = ref(0)
/** 'recheck': the same steps over a second, blind round of a finished video. */
const mode = ref<'judge' | 'recheck'>('judge')
const view = ref<FlowView>('e'), playing = ref(true)
/** What the person has chosen so far; the server copy is in ``detail``. */
const picked = ref<Record<number, string[]>>({}), answers = ref<Record<string, FlowAnswer>>({}), dropped = ref<Record<string, number[]>>({}), points = ref<number[][]>([])

const current = computed(() => items.value.find(item => item.mediaId === currentId.value) ?? null)
const cards = computed(() => detail.value?.second ?? [])
const finding = computed(() => items.value.some(item => item.state === 'finding'))

// ------------------------------------------------------------------ time really spent

let spent = 0, lastMove = Date.now()
/** Counts only while the page is visible and someone moved or typed in the last 15 seconds. */
function startClock() {
  const move = () => { lastMove = Date.now() }
  const events = ['mousemove', 'mousedown', 'keydown', 'wheel'] as const
  events.forEach(name => window.addEventListener(name, move, { passive: true }))
  const timer = setInterval(() => { if (!document.hidden && Date.now() - lastMove < 15000) spent += 250 }, 250)
  return () => { clearInterval(timer); events.forEach(name => window.removeEventListener(name, move)) }
}
const takeTime = () => { const ms = spent; spent = 0; return ms }

// ------------------------------------------------------------------ drafts: choices not handed in yet survive a reload

const draftKey = (d: FlowDetail) => `flow-draft:${useAuth().user.value?.id ?? ''}:${d.mediaId}:${d.progress.runId}`
function readDraft(d: FlowDetail): { picks: Record<string, string[]>; points: number[][] } {
  try { const value = JSON.parse(localStorage.getItem(draftKey(d)) || 'null'); if (value?.picks && Array.isArray(value.points)) return value } catch (error) { console.warn('[flow.draft_unreadable]', error) }
  return { picks: {}, points: [] }
}
function writeDraft() {
  const d = detail.value
  if (!d) return
  const picks: Record<string, string[]> = {}
  for (const [number, keys] of Object.entries(picked.value)) if (JSON.stringify(keys) !== JSON.stringify(d.picks[number]?.picked ?? [])) picks[number] = keys
  const draft = { picks, points: d.scan.done ? [] : points.value }
  try {
    if (Object.keys(picks).length || draft.points.length) localStorage.setItem(draftKey(d), JSON.stringify(draft))
    else localStorage.removeItem(draftKey(d))
  } catch (error) { console.warn('[flow.draft_not_kept]', error) }
}

// ------------------------------------------------------------------ list

let listSeq = 0
async function load() {
  const seq = ++listSeq
  try {
    const { items: next } = await flowApi.list()
    if (seq !== listSeq) return
    items.value = next; listError.value = ''; listReady.value = true
  } catch (error) { if (seq === listSeq) listError.value = (error as FlowFailure).message }
}
/** Quick while something is being searched, slow otherwise so teammates' progress shows up. */
function startPolling() {
  let timer: ReturnType<typeof setTimeout> | null = null, stopped = false
  const loop = async () => { if (!document.hidden) await load(); if (!stopped) timer = setTimeout(loop, finding.value ? 1500 : 20000) }
  void loop()
  const wake = () => { if (!document.hidden) void load() }
  document.addEventListener('visibilitychange', wake)
  return () => { stopped = true; if (timer) clearTimeout(timer); document.removeEventListener('visibilitychange', wake) }
}
function showProgress(mediaId: string, progress: FlowProgress) {
  if (detail.value?.mediaId === mediaId) detail.value.progress = progress
  const item = items.value.find(entry => entry.mediaId === mediaId)
  if (item && progress.runId.includes('~')) { if (item.recheck) item.recheck.progress = progress; return }
  if (item && item.state !== 'finding') { item.progress = { ...progress, lastActorName: useAuth().user.value?.name ?? item.progress?.lastActorName }; item.state = progress.state }
}

// ------------------------------------------------------------------ opening a video

let openSeq = 0
async function open(mediaId: string | null): Promise<boolean> {
  if (!(await flush())) return false
  mode.value = 'judge'
  currentId.value = mediaId
  detail.value = null; loadError.value = ''
  picked.value = {}; answers.value = {}; dropped.value = {}; points.value = []
  if (mediaId) await reload()
  return true
}
async function reload(keepPlace = false) {
  const mediaId = currentId.value, seq = ++openSeq
  if (!mediaId) return
  loading.value = true
  try {
    const next = await flowApi.detail(mediaId, mode.value === 'recheck')
    if (seq !== openSeq || currentId.value !== mediaId) return
    const sameRun = keepPlace && detail.value?.progress.runId === next.progress.runId
    detail.value = next; loadError.value = ''
    const draft = readDraft(next)
    picked.value = Object.fromEntries(next.pages.map((tiles, number) => [number, next.picks[number]?.picked ?? (draft.picks[number] ?? []).filter(key => tiles.some(tile => tile.key === key))]))
    answers.value = Object.fromEntries(Object.entries(next.answers).map(([key, value]) => [key, value.answer]))
    dropped.value = Object.fromEntries(Object.entries(next.answers).map(([key, value]) => [key, value.dropped]))
    points.value = next.scan.done ? next.scan.points : draft.points
    secondOutdated = false
    if (!sameRun) place(next.progress.state)
    else if (step.value === 'cards') card.value = Math.min(card.value, Math.max(0, cards.value.length - 1))
  } catch (error) {
    const failure = error as FlowFailure
    if (seq !== openSeq || currentId.value !== mediaId) return
    if (failure.code === 'FLOW_RECHECK_GONE' && mode.value === 'recheck') { mode.value = 'judge'; loading.value = false; void reload(); return }
    // Still being searched, or never searched: the list says so, and the page shows that instead.
    loadError.value = failure.code === 'FLOW_NOT_READY' ? '' : failure.message
    if (failure.code === 'FLOW_NOT_READY') detail.value = null
  } finally { if (seq === openSeq) loading.value = false }
}
/** Show one step; where in it follows from what is still open. */
function place(next: FlowStep) {
  const d = detail.value
  if (!d) return
  step.value = next; takeTime()
  if (next === 'wall') page.value = Math.max(0, firstOpenPage(d.pages.length, d.picks))
  if (next === 'cards') card.value = Math.max(0, nextCard(cards.value.map(tile => tile.key), key => key in answers.value, -1))
}
/** The second pass depends on the wall, and only the server knows its list. */
let secondOutdated = false
async function goTo(next: FlowStep) {
  if (next === 'cards' && (secondOutdated || queue.size)) {
    if (!(await flush())) return
    await reload(true)
  }
  place(next)
}

// ------------------------------------------------------------------ saving, one after another

const unsaved = ref(0), sending = ref(false), saveError = ref('')
const OUTDATED = new Set(['FLOW_CONFLICT', 'FLOW_RUN_CHANGED', 'FLOW_NOT_IN_SECOND', 'FLOW_NOT_READY', 'INVALID_FLOW_PAGE', 'FLOW_NOT_FOUND', 'FLOW_RECHECKING', 'FLOW_RECHECK_SHOWN', 'FLOW_RECHECK_GONE'])
const queue = new SaveQueue({
  changed: (waiting, busy, error) => { unsaved.value = waiting; sending.value = busy; saveError.value = error },
  outdated: error => OUTDATED.has((error as FlowFailure)?.code ?? ''),
  // Someone else changed it, or the targets were found again: show the server's copy, never overwrite it.
  onOutdated: async (error, id) => {
    showToast((error as FlowFailure).message, 'warning')
    await reload(true)
    const [kind, what] = id.split(':'), at = kind === 'answer' ? cards.value.findIndex(tile => tile.key === what) : -1
    if (at >= 0 && step.value === 'cards') card.value = at
    if (kind === 'page' && step.value === 'wall' && detail.value?.pages[Number(what)]) page.value = Number(what)
    await load()
  },
})
const flush = () => queue.flush()

const timeOf = new Map<string, number>()
/** Queue one save. Time spent adds up until a save that carries it has arrived. */
function save(id: string, send: (d: FlowDetail, ms: number) => Promise<void>) {
  const d = detail.value
  if (!d) return
  const timeKey = `${d.mediaId}:${id}`
  timeOf.set(timeKey, (timeOf.get(timeKey) ?? 0) + takeTime())
  queue.add(id, async () => {
    const ms = timeOf.get(timeKey) ?? 0
    // A reload may have replaced the copy this save started from.
    const now = detail.value?.mediaId === d.mediaId && detail.value.progress.runId === d.progress.runId ? detail.value : d
    await send(now, ms)
    timeOf.set(timeKey, (timeOf.get(timeKey) ?? 0) - ms)
  })
}
function savePage(number: number) {
  save(`page:${number}`, async (d, ms) => {
    const result = await flowApi.savePage(d.mediaId, number, { runId: d.progress.runId, picked: picked.value[number] ?? [], ms, revision: d.picks[number]?.revision ?? 0 })
    d.picks[number] = { picked: result.picked, revision: result.revision }
    secondOutdated = true
    showProgress(d.mediaId, result.progress); writeDraft()
  })
}
function saveAnswer(key: string) {
  save(`answer:${key}`, async (d, ms) => {
    const result = await flowApi.saveAnswer(d.mediaId, key, { runId: d.progress.runId, answer: answers.value[key]!, dropped: dropped.value[key] ?? [], ms, revision: d.answers[key]?.revision ?? 0 })
    d.answers[key] = { answer: result.answer, dropped: result.dropped, revision: result.revision }
    showProgress(d.mediaId, result.progress)
  })
}

// ------------------------------------------------------------------ judging

function togglePick(key: string) {
  const keys = picked.value[page.value] ?? []
  picked.value[page.value] = keys.includes(key) ? keys.filter(other => other !== key) : [...keys, key]
  writeDraft()
  // A page handed in before is corrected at once; a new one is handed in with "next".
  if (detail.value && String(page.value) in detail.value.picks) savePage(page.value)
}
/** Hand in this page. After the last one the second pass opens. */
async function nextPage() {
  const d = detail.value
  if (!d) return
  savePage(page.value)
  if (page.value + 1 < d.pages.length) { page.value++; return }
  if (!(await flush())) return
  await reload(true)
  if (detail.value) place(detail.value.progress.state)
}
function previousPage() { if (page.value > 0) page.value-- }

async function answer(value: FlowAnswer) {
  const tile = cards.value[card.value]
  if (!tile) return
  answers.value[tile.key] = value
  saveAnswer(tile.key)
  const next = nextCard(cards.value.map(entry => entry.key), key => key in answers.value, card.value)
  if (next >= 0) { card.value = next; return }
  if (card.value + 1 < cards.value.length) { card.value++; return }  // Looking through answered cards again.
  if (await flush() && detail.value) { place(detail.value.progress.state); void load() }
}
function previousCard() { if (card.value > 0) card.value-- }
/** This frame is not the same thing: never exported as it. */
function dropFrame(frame: number) {
  const tile = cards.value[card.value]
  if (!tile) return
  const before = dropped.value[tile.key] ?? [], after = toggleDropped(tile.frames, before, frame)
  if (after === before) { showToast('至少要留一帧'); return }
  dropped.value[tile.key] = after
  if (tile.key in answers.value) saveAnswer(tile.key)
}

function addPoint(x: number, y: number) { points.value = [...points.value, [Math.round(x), Math.round(y)]]; writeDraft() }
function removePoint(index: number) { points.value = points.value.filter((_, i) => i !== index); writeDraft() }
async function finishScan() {
  save('scan', async (d, ms) => {
    const result = await flowApi.saveScan(d.mediaId, { runId: d.progress.runId, points: points.value, ms })
    d.scan = { ...d.scan, points: result.points, done: true }
    showProgress(d.mediaId, result.progress); writeDraft()
  })
  if (await flush() && detail.value) place(detail.value.progress.state)
}

// ------------------------------------------------------------------ optional re-check

async function setMode(next: 'judge' | 'recheck'): Promise<boolean> {
  if (!(await flush())) return false
  mode.value = next; detail.value = null
  await reload(); void load()
  return true
}
/** Start one, or go on with the one already there. */
async function openRecheck() {
  const mediaId = currentId.value
  if (!mediaId || !(await flush())) return
  try { await flowApi.startRecheck(mediaId); await setMode('recheck') } catch (error) { showToast((error as FlowFailure).message, 'warning') }
}
const leaveRecheck = () => setMode('judge')
async function cancelRecheck() {
  const mediaId = currentId.value, recheck = current.value?.recheck ?? detail.value?.recheck
  if (!mediaId || !recheck) return
  if (!(await askConfirm({ title: '作废这次复查？', body: '复查的答案不再算数，原来的判定不变。', confirm: '作废', danger: true }))) return
  if (!(await flush())) return
  try { await flowApi.cancelRecheck(mediaId, recheck.id) } catch (error) { showToast((error as FlowFailure).message, 'warning') }
  await setMode('judge')
}
/** Show what the two rounds disagree on; after this neither can be changed. */
async function compare() {
  const d = detail.value
  if (!d?.recheck || !(await flush())) return
  try { const next = await flowApi.compareRecheck(d.mediaId, d.recheck.id); if (detail.value?.mediaId === d.mediaId) detail.value = next } catch (error) { showToast((error as FlowFailure).message, 'warning') }
  void load()
}
function settle(key: string, value: FlowAnswer, off: number[]) {
  const d = detail.value, entry = d?.differences?.find(item => item.key === key)
  if (!d?.recheck || !entry) return
  const recheckId = d.recheck.id, revision = entry.settled?.revision ?? 0
  entry.settled = { answer: value, dropped: off, revision }
  save(`settle:${key}`, async now => {
    const live = now.differences?.find(item => item.key === key)
    if (!live?.settled) return
    const result = await flowApi.settle(now.mediaId, recheckId, key, { answer: live.settled.answer, dropped: live.settled.dropped, revision: live.settled.revision })
    live.settled = { answer: result.answer, dropped: result.dropped, revision: result.revision }
    now.recheck = result.recheck
    const item = items.value.find(other => other.mediaId === now.mediaId)
    if (item) item.recheck = result.recheck
  })
}

// ------------------------------------------------------------------ videos

async function act(work: () => Promise<unknown>): Promise<boolean> {
  try { await work(); await load(); return true } catch (error) { showToast((error as FlowFailure).message || '操作失败，请重试'); await load(); return false }
}
const add = (mediaIds: string[], sample = '') => act(() => flowApi.add(mediaIds, sample))
async function find(item: FlowItem) {
  const judged = !!item.progress && (item.progress.pagesDone > 0 || item.progress.secondDone > 0 || item.progress.scanDone)
  if (judged && !(await askConfirm({ title: '重新找目标？', body: '找到的目标如果和现在不同，这一段要从头判。', confirm: '重新找' }))) return
  if (currentId.value === item.mediaId && !(await flush())) return
  if (await act(() => flowApi.find(item.mediaId, judged)) && currentId.value === item.mediaId) detail.value = null
}
async function moveBack(item: FlowItem) {
  if (!(await askConfirm({ title: `把「${item.videoName}」移回标注列表？`, confirm: '移回' }))) return
  if (await act(() => flowApi.remove([item.mediaId])) && currentId.value === item.mediaId) await open(null)
}
async function removeVideo(item: FlowItem) {
  if (!(await askConfirm({ title: `删除「${item.videoName}」？`, body: '视频和它的判定一起删除，其他人也看不到了。', confirm: '删除', danger: true }))) return
  if (currentId.value === item.mediaId && !(await flush())) return
  if (await act(() => trackApi.deleteMedia(item.mediaId)) && currentId.value === item.mediaId) await open(null)
}
const openNext = () => { const next = nextTodo(items.value, currentId.value); return next ? open(next.mediaId) : Promise.resolve(false) }

export const useFlow = () => ({
  items, listReady, listError, current, currentId, detail, loading, loadError, step, page, card, cards, view, playing,
  picked, answers, dropped, points, unsaved, sending, saveError, finding,
  load, startPolling, startClock, open, openNext, reload, goTo, flush,
  togglePick, nextPage, previousPage, answer, previousCard, dropFrame, addPoint, removePoint, finishScan,
  add, find, moveBack, removeVideo, mode, openRecheck, leaveRecheck, cancelRecheck, compare, settle,
})
