<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from '../router'
import { reviewApi, type ReviewSession, type BaselineFrame } from '../api/reviewApi'
import { http, tokenStore } from '../api/http'

const router = useRouter()

// ── Toast / 全局错误 ──
const toast = ref<{ type: 'ok' | 'err'; msg: string } | null>(null)
function showToast(type: 'ok' | 'err', msg: string) {
  toast.value = { type, msg }
  setTimeout(() => { if (toast.value?.msg === msg) toast.value = null }, 3500)
}

// ── 列表视图 ──
const view = ref<'list' | 'session'>('list')
const sessions = ref<ReviewSession[]>([])
const loading = ref(false)
const filter = ref<'all' | 'pending' | 'in_progress' | 'reviewed'>('all')
const mediaAssets = ref<Array<{ id: string; name: string; width: number; height: number; frameCount?: number; fps?: number; duration?: number }>>([])
const listSearch = ref('')

const filteredSessions = computed(() => {
  let list = sessions.value
  if (filter.value !== 'all') list = list.filter(s => s.state === filter.value)
  if (listSearch.value.trim()) {
    const q = listSearch.value.toLowerCase()
    list = list.filter(s =>
      (s as any).mediaName?.toLowerCase()?.includes(q) ||
      (s as any).mediaId?.toLowerCase()?.includes(q) ||
      s.id.toLowerCase().includes(q)
    )
  }
  return list
})

async function loadAll() {
  loading.value = true
  try {
    const [ses, mediasResp] = await Promise.all([
      reviewApi.listSessions().catch(() => ({ items: [] as ReviewSession[] })),
      http.get<any>('/track/media').catch(() => ({ items: [] })),
    ])
    sessions.value = ses.items
    const items = Array.isArray(mediasResp) ? mediasResp : (mediasResp?.items || [])
    mediaAssets.value = items.map((m: any) => ({
      id: m.mediaId || m.id,
      name: m.videoName || m.name || m.mediaId,
      width: m.width, height: m.height,
      frameCount: m.frameCount,
      fps: m.fps, duration: m.duration,
    }))
  } catch (e: any) {
    showToast('err', e?.message || '加载失败')
  } finally { loading.value = false }
}
onMounted(loadAll)

// ── 新建 ──
const createDialogOpen = ref(false)
const currentMediaId = ref('')
const creating = ref(false)

async function createSession() {
  if (!currentMediaId.value) { showToast('err', '请选择素材'); return }
  creating.value = true
  try {
    const anns = await http.get<any[]>(`/annotations/${currentMediaId.value}`).catch(() => [])
    const frameMap = new Map<number, any[]>()
    for (const a of anns) {
      const fi = a.frameIndex ?? 0
      if (!frameMap.has(fi)) frameMap.set(fi, [])
      frameMap.get(fi)!.push({ objectId: a.objectId ?? 1, bbox: a.bbox, classKey: a.name || 'sperm' })
    }
    if (frameMap.size === 0) {
      const media = mediaAssets.value.find(m => m.id === currentMediaId.value)
      const fc = media?.frameCount ?? 30
      for (let i = 0; i < Math.min(fc, 10); i++) frameMap.set(i, [])
    }
    const frames: BaselineFrame[] = Array.from(frameMap.entries()).sort((a, b) => a[0] - b[0])
      .map(([fi, objs]) => ({ frameIndex: fi, coverage: 'objects', objects: objs }))

    const freezeRes = await reviewApi.freezeBaseline({ mediaId: currentMediaId.value, frames })
    const sessRes = await reviewApi.createSession({ baselineId: freezeRes.baseline.id })
    await reviewApi.claimSession(sessRes.session.id)
    const full = await reviewApi.getSession(sessRes.session.id)
    await openSession(full.id, true)
    showToast('ok', `创建审查会话成功！帧: ${full.frameCount}`)
  } catch (e: any) {
    showToast('err', e?.message || '创建失败')
  } finally { creating.value = false; createDialogOpen.value = false }
}

async function openSession(id: string, isNew = false) {
  loading.value = true
  try {
    await reviewApi.claimSession(id).catch(() => {})
    const sess = await reviewApi.getSession(id)
    await enterSession(sess, isNew)
  } catch (e: any) {
    showToast('err', e?.message || '打开失败')
  } finally { loading.value = false }
}

// ── Session 视图状态 ──
const activeSession = ref<ReviewSession | null>(null)
const currentFrameIndex = ref(0)
const draftPatch = ref<Array<{ objectId: number; bbox: number[] }>>([])
const submitting = ref(false)

// 原型风格新状态
const showResumeDialog = ref(false)   // 继续上次审查弹窗
const videoPlaying = ref(false)       // 模拟播放
let playbackTimer: number | null = null
const zoomLevel = ref(1)              // 缩放
const showRawToggle = ref(false)      // 原始显示（只看 A 原稿）

// 撤销历史（草稿级别，不影响已提交帧）
const draftHistory = ref<Array<{ patch: typeof draftPatch.value; frameIndex: number }>>([])

function pushUndo() {
  draftHistory.value.push({ patch: JSON.parse(JSON.stringify(draftPatch.value)), frameIndex: currentFrameIndex.value })
  if (draftHistory.value.length > 20) draftHistory.value.shift()
}
function undoDraft() {
  const last = draftHistory.value.pop()
  if (!last || last.frameIndex !== currentFrameIndex.value) {
    showToast('err', '没有可撤销的操作')
    return
  }
  draftPatch.value = last.patch
  showToast('ok', '已撤销')
}

async function enterSession(sess: ReviewSession, isNew = false) {
  activeSession.value = sess
  const sessMediaId = (sess as any).mediaId
  if (sessMediaId) {
    currentMediaId.value = sessMediaId
  } else {
    const blRes = await http.get<{ items: any[] }>('/review/baselines').catch(() => ({ items: [] }))
    const bl = blRes.items.find((b: any) => b.id === sess.baselineId)
    if (bl) currentMediaId.value = bl.mediaId
  }
  await loadMediaFrames()
  await loadBaselineFrames()

  // 原型：有未提交帧则弹"继续上次审查"
  const firstUnsubmitted = sess.frames?.find(f => f.state !== 'submitted')?.frameIndex ?? 0
  const shouldShowResume = !isNew && sess.state === 'in_progress' && firstUnsubmitted > 0
  view.value = 'session'
  // 先让 session 视图渲染出来（解决 Vue 3 v-if 首次渲染 ref 读取顺序问题）
  await nextTick()
  if (shouldShowResume) {
    // 先跳到停留帧位置（弹窗显示它），但不提交
    currentFrameIndex.value = firstUnsubmitted
    showResumeDialog.value = true
  } else {
    currentFrameIndex.value = firstUnsubmitted
  }
}

function continueResume(fromBegin = false) {
  showResumeDialog.value = false
  if (fromBegin) {
    currentFrameIndex.value = 0
  }
  // else 保持 enterSession 里算好的 firstUnsubmitted
}

async function loadMediaFrames() {
  mediaFrameCache.value = {}
  if (!currentMediaId.value || !activeSession.value) return
  for (const f of activeSession.value.frames ?? []) {
    mediaFrameCache.value[f.frameIndex] =
      `/api/track/frame/${currentMediaId.value}/${f.frameIndex}?token=${tokenStore.get()}`
  }
}
const mediaFrameCache = ref<Record<number, string>>({})

const fullVideoUrl = computed(() =>
  `/api/track/video/${currentMediaId.value}?token=${tokenStore.get()}`
)

// ── baseline 原始 bbox（A） ──
const baselineFrames = ref<Map<number, Array<{ objectId: number; bbox: number[] }>>>(new Map())
async function loadBaselineFrames() {
  baselineFrames.value = new Map()
  if (!activeSession.value) return
  try {
    const blRes = await http.get<any>(`/review/baselines/${activeSession.value.baselineId}`).catch(() => null)
    if (blRes?.frames) {
      for (const f of blRes.frames) {
        baselineFrames.value.set(f.frameIndex, (f.objects ?? []).map((o: any) => ({ objectId: o.objectId, bbox: o.bbox })))
      }
    }
  } catch { /* ignore */ }
}

// ── 视频播放模拟 ──
function togglePlay() {
  if (videoPlaying.value) {
    videoPlaying.value = false
    if (playbackTimer) { clearInterval(playbackTimer); playbackTimer = null }
  } else {
    if (!activeSession.value) return
    videoPlaying.value = true
    playbackTimer = window.setInterval(() => {
      if (!activeSession.value) return
      const next = currentFrameIndex.value + 1
      if (next >= activeSession.value.frameCount) {
        videoPlaying.value = false
        if (playbackTimer) { clearInterval(playbackTimer); playbackTimer = null }
        return
      }
      currentFrameIndex.value = next
    }, 100) // 模拟 10fps 视觉
  }
}
onUnmounted(() => { if (playbackTimer) clearInterval(playbackTimer) })

function seekFrame(fi: number) {
  if (!activeSession.value) return
  currentFrameIndex.value = Math.max(0, Math.min(fi, activeSession.value.frameCount - 1))
}

function zoomIn() { zoomLevel.value = Math.min(2, +(zoomLevel.value + 0.25).toFixed(2)) }
function zoomOut() { zoomLevel.value = Math.max(0.5, +(zoomLevel.value - 0.25).toFixed(2)) }
function zoomReset() { zoomLevel.value = 1 }

// ── bbox 拖动（原型交互：拖动框可移动位置） ──
const dragging = ref<{ idx: number; offX: number; offY: number } | null>(null)
function onPatchPointerDown(idx: number, e: PointerEvent) {
  e.stopPropagation(); e.preventDefault()
  pushUndo()
  const p = draftPatch.value[idx]
  dragging.value = { idx, offX: e.clientX - p.bbox[0], offY: e.clientY - p.bbox[1] }
  ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
}
function onPatchPointerMove(e: PointerEvent) {
  if (!dragging.value) return
  const p = draftPatch.value[dragging.value.idx]
  if (!p) return
  const nx = Math.max(0, Math.round(e.clientX - dragging.value.offX))
  const ny = Math.max(0, Math.round(e.clientY - dragging.value.offY))
  const w = p.bbox[2] - p.bbox[0], h = p.bbox[3] - p.bbox[1]
  const newBBox = [nx, ny, nx + w, ny + h]
  // 直接替换数组引用 → Vue 3 能追踪
  p.bbox.splice(0, 4, ...newBBox)
  // 强制触发一次 computed 重算（draftPatch 数组引用不变但内部变了）
  draftPatch.value = draftPatch.value.slice()
}
function onPatchPointerUp() {
  dragging.value = null
}

// ── 已提交帧 reopen（原型：重新编辑本帧） ──
async function reopenCurrentFrame() {
  if (!activeSession.value || !currentFrameInfo.value) return
  if (currentFrameInfo.value.state !== 'submitted') { showToast('err', '当前帧未提交'); return }
  // 从 baseline 重置：清空 draftPatch（回到原始 A）
  pushUndo()
  draftPatch.value = []
  showToast('ok', '已 reopen，可重新编辑本帧')
}


const currentFrameInfo = computed(() => activeSession.value?.frames?.find(f => f.frameIndex === currentFrameIndex.value))
const allSubmitted = computed(() => activeSession.value?.frames?.every(f => f.state === 'submitted') ?? false)
const frameProgress = computed(() => {
  const s = activeSession.value; if (!s) return 0
  return s.frameCount ? Math.round(s.submittedFrames / s.frameCount * 100) : 0
})

const fps = computed(() => mediaAssets.value.find(m => m.id === currentMediaId.value)?.fps ?? 15)
function frameToTime(fi: number) {
  const totalSec = fi / fps.value
  const m = Math.floor(totalSec / 60); const s = Math.floor(totalSec % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

// 当前帧 baseline bbox（A）
const currentBaselineObjs = computed(() => baselineFrames.value.get(currentFrameIndex.value) ?? [])

// B (baseline + patch 合成)
const currentBObjs = computed(() => {
  const a = currentBaselineObjs.value.map(o => ({ ...o }))
  const patchMap = new Map(draftPatch.value.map(p => [p.objectId, p]))
  for (let i = 0; i < a.length; i++) {
    if (patchMap.has(a[i].objectId)) a[i].bbox = patchMap.get(a[i].objectId)!.bbox
  }
  for (const p of draftPatch.value) {
    if (!a.some(o => o.objectId === p.objectId)) a.push({ objectId: p.objectId, bbox: p.bbox })
  }
  return a
})

// 当前帧逐项对比（A vs B）——原型左下对比表格
const perObjectCompare = computed(() => {
  const rows: Array<{
    objectId: number;
    a: number[]; b: number[];
    state: 'unchanged' | 'modified';
    iou: number | null;
    dx: number; dy: number;
    dw: number; dh: number;
  }> = []
  const aMap = new Map(currentBaselineObjs.value.map(o => [o.objectId, o.bbox]))
  const bMap = new Map(currentBObjs.value.map(o => [o.objectId, o.bbox]))
  const ids = new Set([...aMap.keys(), ...bMap.keys()])
  for (const id of ids) {
    const a = aMap.get(id); const b = bMap.get(id)
    if (!a || !b) continue
    const modified = a[0] !== b[0] || a[1] !== b[1] || a[2] !== b[2] || a[3] !== b[3]
    rows.push({
      objectId: id,
      a, b,
      state: modified ? 'modified' : 'unchanged',
      iou: modified ? computeIoU(a, b) : null,
      dx: +(b[0] - a[0]).toFixed(1), dy: +(b[1] - a[1]).toFixed(1),
      dw: +((b[2]-b[0]) - (a[2]-a[0])).toFixed(1),
      dh: +((b[3]-b[1]) - (a[3]-a[1])).toFixed(1),
    })
  }
  return rows
})

// 当前帧统计（对齐原型）
const currentFrameStats = computed(() => {
  const total = perObjectCompare.value.length
  const changed = perObjectCompare.value.filter(r => r.state === 'modified').length
  const ious = perObjectCompare.value.filter(r => r.iou !== null).map(r => r.iou!)
  const shifts = perObjectCompare.value.map(r => {
    const acx = (r.a[0]+r.a[2])/2, acy = (r.a[1]+r.a[3])/2
    const bcx = (r.b[0]+r.b[2])/2, bcy = (r.b[1]+r.b[3])/2
    return ((acx-bcx)**2 + (acy-bcy)**2)**0.5
  })
  return {
    total, changed, unchanged: total - changed,
    avgIoU: ious.length ? +(ious.reduce((a,b)=>a+b,0)/ious.length).toFixed(3) : null,
    maxShift: shifts.length ? +Math.max(...shifts).toFixed(1) : null,
  }
})

// 圆环进度（原型）
const ringProgress = computed(() => {
  const s = activeSession.value; if (!s) return 0
  return Math.round(s.submittedFrames / s.frameCount * 100)
})
const ringRadius = 32
const ringCircum = ringRadius * 2 * Math.PI

function computeIoU(a: number[], b: number[]): number {
  const x1 = Math.max(a[0], b[0]); const y1 = Math.max(a[1], b[1])
  const x2 = Math.min(a[2], b[2]); const y2 = Math.min(a[3], b[3])
  const inter = Math.max(0, x2-x1) * Math.max(0, y2-y1)
  const areaA = (a[2]-a[0])*(a[3]-a[1]); const areaB = (b[2]-b[0])*(b[3]-b[1])
  const union = areaA + areaB - inter
  return union > 0 ? +(inter/union).toFixed(3) : 0
}

// ── 帧切换载入已有 patch + 压撤销栈 ──
watch(currentFrameIndex, async (fi, prev) => {
  if (!activeSession.value) return
  if (prev !== undefined && JSON.stringify(draftPatch.value) !== '[]') {
    // 切帧前不自动 pushUndo —— 每次手动编辑时 push
  }
  draftHistory.value = [] // 换帧清撤销栈
  const fr = activeSession.value.frames?.find(f => f.frameIndex === fi)
  if (fr?.state === 'submitted' && (fr as any).patch) {
    draftPatch.value = JSON.parse(JSON.stringify((fr as any).patch))
  } else {
    draftPatch.value = []
  }
  // 如果切到了已提交帧，patch 会自动显示为 A（空 draft），但 currentBObjs 会基于 draftPatch 合成
  // 已提交帧应该显示已提交的 patch —— 所以上面那段 ((fr as any).patch) 从后端取就对了
})

// ── patch 编辑：每次手动 pushUndo ──
function addPatch() {
  pushUndo()
  const objId = draftPatch.value.length > 0 ? Math.max(...draftPatch.value.map(o => o.objectId)) + 1 : 1
  draftPatch.value.push({ objectId: objId, bbox: [10, 10, 80, 60] })
}
function updatePatchBBox(idx: number, axis: 'x1'|'y1'|'x2'|'y2', val: number) {
  pushUndo()
  const p = draftPatch.value[idx]
  if (axis === 'x1') p.bbox[0] = val
  else if (axis === 'y1') p.bbox[1] = val
  else if (axis === 'x2') p.bbox[2] = val
  else if (axis === 'y2') p.bbox[3] = val
  draftPatch.value = [...draftPatch.value] // 触发响应式
}
function removePatchItem(idx: number) { pushUndo(); draftPatch.value.splice(idx, 1) }
function clearPatch() { pushUndo(); draftPatch.value = [] }

// ── 提交 / 冻结 ──
async function submitCurrentFrame() {
  if (!activeSession.value) return
  submitting.value = true
  try {
    await reviewApi.submitFrame(activeSession.value.id, currentFrameIndex.value, { patch: draftPatch.value })
    activeSession.value = await reviewApi.getSession(activeSession.value.id)
    showToast('ok', `帧 ${currentFrameIndex.value} 提交成功`)
    draftHistory.value = []
    if (activeSession.value.state === 'reviewed') {
      showToast('ok', '所有帧已提交，冻结审查版本中...')
      setTimeout(() => router.push('/confirm'), 500)
    }
  } catch (e: any) {
    const m = e?.message || '提交失败'
    if (m.includes('revision') || m.includes('conflict') || e?.status === 409) {
      showToast('err', '冲突，正在刷新...')
      activeSession.value = await reviewApi.getSession(activeSession.value.id)
    } else showToast('err', m)
  } finally { submitting.value = false }
}

async function manualFreeze() {
  if (!activeSession.value) return
  if (!allSubmitted.value) { showToast('err', '还有帧未提交'); return }
  submitting.value = true
  try {
    const res = await reviewApi.freezeSession(activeSession.value.id)
    showToast('ok', `冻结成功！修改项: ${res.totalChanges}`)
    setTimeout(() => router.push('/confirm'), 500)
  } catch (e: any) { showToast('err', e?.message || '冻结失败') }
  finally { submitting.value = false }
}


function stateCount(state: string) { return sessions.value.filter(s => s.state === state).length }
function sessionLastFrame(s: ReviewSession) {
  const fc = s.frameCount || 0
  const done = s.frames?.filter(f => f.state === 'submitted').length ?? 0
  if (fc <= 0) return 0
  const val = Math.min(done, fc - 1)
  return Number.isFinite(val) ? val : 0
}

function backToList() {
  if (playbackTimer) { clearInterval(playbackTimer); playbackTimer = null }
  videoPlaying.value = false
  view.value = 'list'
  activeSession.value = null
}
</script>
<template>
  <!-- Toast -->
  <div v-if="toast"
       class="fixed top-20 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-lg text-sm shadow-2xl border backdrop-blur transition-opacity"
       :class="toast.type === 'ok' ? 'bg-emerald-900/80 border-emerald-500/50 text-emerald-200' : 'bg-red-900/80 border-red-500/50 text-red-200'">
    {{ toast.msg }}
  </div>

  <!-- 继续上次审查弹窗（提至顶层避免 v-if 时序问题） -->
  <div v-if="view === 'session' && activeSession && showResumeDialog"
       class="fixed top-28 left-1/2 -translate-x-1/2 z-[100] w-[420px] rounded-xl border border-indigo-500/40 bg-slate-900/95 backdrop-blur-xl p-4 shadow-2xl">
    <div class="flex items-start gap-3">
      <div class="w-8 h-8 rounded-full bg-indigo-500/20 flex items-center justify-center text-indigo-300 text-sm">ℹ</div>
      <div class="flex-1">
        <div class="text-sm font-medium text-slate-100">继续上次审查？</div>
        <div class="mt-1 text-[11px] text-slate-400 leading-relaxed">
          上次停留帧 <span class="text-indigo-300 font-mono">第 {{ currentFrameIndex }} 帧</span>
          · 已完成 <span class="text-emerald-300 font-mono">{{ activeSession.submittedFrames }} / {{ activeSession.frameCount }} 帧</span>
        </div>
      </div>
      <button class="text-slate-500 hover:text-slate-300 text-lg leading-none" @click="showResumeDialog = false">×</button>
    </div>
    <div class="mt-3 flex gap-2">
      <button class="flex-1 text-xs px-3 py-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800" @click="continueResume(true)">从头查看</button>
      <button class="flex-1 text-xs px-3 py-2 rounded-lg bg-gradient-to-br from-indigo-500 to-indigo-600 text-white font-medium hover:brightness-110" @click="continueResume(false)">继续审查</button>
    </div>
  </div>

  <!-- ============ 列表视图 ============ -->
  <div v-if="view === 'list'" class="space-y-5">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold tracking-wide text-slate-200">待审查视频列表（已标注）</h2>
      <div class="flex items-center gap-3">
        <div class="flex items-center rounded-lg border border-slate-700 bg-slate-900/70 p-0.5">
          <button v-for="f in (['all','pending','in_progress','reviewed'] as const)" :key="f"
                  class="px-3 py-1 text-xs rounded-md transition-colors"
                  :class="filter === f ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-400 hover:text-slate-200'"
                  @click="filter = f">
            {{ {all:`全部(${sessions.length})`, pending:`待审(${stateCount('pending')})`, in_progress:`审中(${stateCount('in_progress')})`, reviewed:`已审(${stateCount('reviewed')})`}[f] }}
          </button>
        </div>
        <button class="btn-primary" @click="createDialogOpen = true">＋ 新建审查</button>
      </div>
    </div>

    <!-- 搜索框 + 计数 -->
    <div class="flex items-center gap-3">
      <div class="relative flex-1 max-w-md">
        <span class="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 text-xs">🔍</span>
        <input v-model="listSearch" placeholder="搜索视频名称 / ID..."
               class="w-full rounded-lg border border-slate-700 bg-slate-900/70 pl-9 pr-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-indigo-500 focus:outline-none" />
      </div>
      <span class="text-xs text-slate-500">共 {{ filteredSessions.length }} 项</span>
    </div>

    <!-- 新建对话框 -->
    <div v-if="createDialogOpen" class="rounded-xl border border-indigo-500/40 bg-slate-900/80 p-4 backdrop-blur">
      <div class="mb-3 text-sm text-slate-300">选择要审查的素材（将自动冻结原稿 baseline）</div>
      <select v-model="currentMediaId" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 focus:border-indigo-500 focus:outline-none">
        <option value="">-- 选择素材 --</option>
        <option v-for="m in mediaAssets" :key="m.id" :value="m.id">{{ m.name }} · {{ m.width }}×{{ m.height }} · {{ m.frameCount }}帧</option>
      </select>
      <div class="mt-3 flex gap-2">
        <button class="btn-primary" :disabled="creating || !currentMediaId" @click="createSession">
          {{ creating ? '创建中...' : '创建并开始审查' }}
        </button>
        <button class="btn-secondary" @click="createDialogOpen = false">取消</button>
      </div>
    </div>

    <div v-if="loading && sessions.length === 0" class="text-center text-slate-500 py-10">加载中...</div>
    <div v-else-if="filteredSessions.length === 0" class="rounded-xl border border-dashed border-slate-800 p-12 text-center text-slate-500">
      暂无{{ filter === 'all' ? '' : { pending:'待审查', in_progress:'审查中', reviewed:'已审查' }[filter] }}会话
    </div>

    <div v-else class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      <div v-for="s in filteredSessions" :key="s.id"
           class="group cursor-pointer rounded-xl border border-slate-800 bg-slate-900/50 p-4 transition-all hover:border-indigo-500/60 hover:bg-slate-900">
        <div class="flex items-center justify-between">
          <span class="text-[11px] font-mono text-slate-500 truncate max-w-[180px]">{{ (s as any).mediaId || s.id.slice(-10) }}</span>
          <span :class="[
            'rounded-full border px-2 py-0.5 text-[10px] font-medium shrink-0',
            s.state === 'reviewed' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' :
            s.state === 'in_progress' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' :
            'bg-slate-700/50 text-slate-400 border-slate-600'
          ]">{{ {pending:'待审查', in_progress:'审查中', reviewed:'已审查'}[s.state] }}</span>
        </div>
        <div class="mt-3 flex items-center justify-between text-[11px] text-slate-400">
          <span>帧进度</span>
          <span class="font-mono text-slate-300">{{ s.submittedFrames }}/{{ s.frameCount }}</span>
        </div>
        <div class="mt-1.5 h-1.5 rounded-full bg-slate-800 overflow-hidden">
          <div class="h-full bg-gradient-to-r from-indigo-500 to-emerald-500 transition-all"
               :style="{ width: (s.frameCount ? s.submittedFrames / s.frameCount * 100 : 0) + '%' }"></div>
        </div>
        <div class="mt-2 flex items-center justify-between text-[10px] text-slate-500">
          <span>总帧数 {{ s.frameCount || 0 }} · 上次停留 第 {{ sessionLastFrame(s) }} 帧</span>
        </div>
        <button class="mt-3 w-full btn-primary" @click="openSession(s.id)">继续审查 →</button>
      </div>
    </div>
  </div>

  <!-- ============ Session 视图：三栏原型布局 ============ -->
  <div v-else-if="view === 'session' && activeSession" class="h-[calc(100vh-80px)] overflow-hidden flex flex-col">

    <!-- 顶部面包屑 + 文件名栏 -->
    <div class="flex items-center gap-3 px-4 py-2 border-b border-slate-800 bg-slate-900/50">
      <button class="btn-secondary" @click="backToList">← 返回列表</button>
      <div class="text-sm text-slate-200 font-medium">
        <span class="font-mono">{{ (activeSession as any).mediaId?.slice(0,10) || activeSession.id.slice(0,10) }}.avi</span>
        <span class="text-slate-500"> · 第 {{ currentFrameIndex + 1 }} / {{ activeSession.frameCount }} 帧</span>
        <span class="text-slate-500"> · 时间 {{ frameToTime(currentFrameIndex) }} / {{ frameToTime(activeSession.frameCount - 1) }}</span>
      </div>
      <span :class="[
        'rounded-full border px-2 py-0.5 text-[11px]',
        activeSession.state === 'reviewed' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' :
        activeSession.state === 'in_progress' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' :
        'bg-slate-700/50 text-slate-400 border-slate-600'
      ]">{{ {pending:'待审查', in_progress:'审查中', reviewed:'已审查', closed:'已关闭'}[activeSession.state] }}</span>
      <span class="ml-auto text-xs text-slate-500">
        第 {{ currentFrameIndex + 1 }} / {{ activeSession.frameCount }} 帧 · revision {{ activeSession.revision }} · 进度 {{ frameProgress }}%
      </span>
    </div>

    <!-- 主体三栏 -->
    <div class="flex-1 grid gap-0 overflow-hidden" style="grid-template-columns: 200px 1fr 300px;">

      <!-- 左：帧列表（原型风格） -->
      <div class="border-r border-slate-800 bg-slate-950/50 flex flex-col">
        <div class="flex items-center justify-between px-3 py-2 border-b border-slate-800">
          <span class="text-xs font-medium text-slate-400">帧列表</span>
          <span class="text-[10px] text-slate-600">{{ activeSession.frames?.length || 0 }}帧</span>
        </div>
        <div class="flex-1 overflow-y-auto p-2 space-y-1">
          <div v-for="f in activeSession.frames" :key="f.frameIndex"
               class="flex items-center gap-2 rounded-md border px-2 py-1.5 text-[11px] cursor-pointer transition-colors"
               :class="[
                 currentFrameIndex === f.frameIndex ? 'border-indigo-500 bg-indigo-500/10 text-indigo-200' :
                 f.state === 'submitted' ? 'border-emerald-800/50 hover:border-emerald-500/60 bg-emerald-500/5 text-slate-300' :
                 f.state === 'draft' ? 'border-amber-800/50 bg-amber-500/5 text-amber-200 hover:border-amber-500/60' :
                 'border-slate-800 hover:border-slate-600 text-slate-500'
               ]"
               @click="seekFrame(f.frameIndex)">
            <span class="font-mono w-10 text-slate-500">{{ String(f.frameIndex).padStart(3,'0') }}</span>
            <span class="ml-auto" :class="[
              f.state === 'submitted' ? 'text-emerald-400' : f.state === 'draft' ? 'text-amber-400' : 'text-slate-600'
            ]">
              {{ {submitted:'✓', draft:'◌', unreviewed:'—', pending:'—'}[f.state] }}
            </span>
          </div>
        </div>
      </div>

      <!-- 中：预览 + 视频控件 + 对比面板 -->
      <div class="flex flex-col overflow-hidden">

        <!-- 顶栏：帧号 + 原始显示 + 快捷键 + 缩放 -->
        <div class="flex items-center gap-2 px-4 py-2 border-b border-slate-800 bg-slate-900/30">
          <span class="text-sm font-medium text-slate-200">Frame {{ currentFrameIndex }}
            <span class="ml-2 text-[11px] text-slate-500">时间 {{ frameToTime(currentFrameIndex) }} / {{ frameToTime(activeSession.frameCount - 1) }}</span>
          </span>
          <div class="ml-auto flex items-center gap-1">
            <!-- 原始显示 -->
            <button :class="[
              'text-[11px] px-2 py-1 rounded-md border transition-colors',
              showRawToggle ? 'border-indigo-500 text-indigo-300 bg-indigo-500/10' : 'border-slate-700 text-slate-400 hover:text-slate-200'
            ]" @click="showRawToggle = !showRawToggle">原始显示</button>
            <span class="text-[10px] text-slate-600 px-2">快捷键 F1-F5</span>
            <!-- 缩放 -->
            <button class="text-[11px] px-2 py-1 rounded border border-slate-700 text-slate-400 hover:text-slate-200" @click="zoomOut">−</button>
            <button class="text-[11px] px-2 py-1 rounded border border-slate-700 font-mono text-slate-300 w-14" @click="zoomReset">{{ Math.round(zoomLevel * 100) }}%</button>
            <button class="text-[11px] px-2 py-1 rounded border border-slate-700 text-slate-400 hover:text-slate-200" @click="zoomIn">+</button>
          </div>
        </div>

        <!-- 预览区 -->
        <div class="relative flex-1 overflow-auto bg-black" :style="{ transform: `scale(${zoomLevel})`, transformOrigin: 'top left' }">
          <!-- 帧图 -->
          <div class="relative inline-block">
            <img v-if="mediaFrameCache[currentFrameIndex]"
                 :src="mediaFrameCache[currentFrameIndex]"
                 class="block"
                 @pointermove="onPatchPointerMove"
                 @pointerup="onPatchPointerUp" />
            <div v-else class="flex items-center justify-center text-slate-600 text-sm h-[300px] w-[640px]">帧图加载中...</div>

            <!-- 原始显示关 → 显示 A/B bbox 叠加 -->
            <template v-if="!showRawToggle">
              <!-- A 原稿框：绿色（原型配色）带编号 -->
              <template v-for="o in currentBaselineObjs" :key="'A'+o.objectId">
                <div class="absolute border-2 border-green-400 bg-green-400/8"
                     :style="{ left: o.bbox[0]+'px', top: o.bbox[1]+'px', width: (o.bbox[2]-o.bbox[0])+'px', height: (o.bbox[3]-o.bbox[1])+'px' }">
                  <span class="absolute -top-4 left-0 text-[10px] bg-green-500 text-black px-1 rounded-sm font-mono">#{{ String(o.objectId).padStart(2,'0') }}</span>
                </div>
              </template>
              <!-- B 审查修改框：红色（原型配色）带编号 -->
              <template v-for="(p, pi) in draftPatch" :key="'B'+p.objectId">
                <div class="absolute border-2 border-red-500 bg-red-500/10 cursor-move select-none"
                     :style="{ left: p.bbox[0]+'px', top: p.bbox[1]+'px', width: (p.bbox[2]-p.bbox[0])+'px', height: (p.bbox[3]-p.bbox[1])+'px' }"
                     @pointerdown="onPatchPointerDown(pi, $event)"
                     @pointermove="onPatchPointerMove"
                     @pointerup="onPatchPointerUp"
                     @pointercancel="onPatchPointerUp">
                  <span class="absolute -top-4 left-0 text-[10px] bg-red-500 text-white px-1 rounded-sm font-mono pointer-events-none">#{{ String(p.objectId).padStart(2,'0') }}</span>
                </div>
              </template>
            </template>
          </div>


        </div>

        <!-- 视频播放器控件（原型风格） -->
        <div class="flex items-center gap-3 px-4 py-2 border-t border-slate-800 bg-slate-900/30">
          <button class="w-8 h-8 rounded-full bg-indigo-500/20 text-indigo-300 hover:bg-indigo-500/30 transition-colors flex items-center justify-center"
                  :class="videoPlaying ? '!bg-red-500/20 !text-red-300' : ''"
                  @click="togglePlay">
            <span v-if="!videoPlaying">▶</span><span v-else>❚❚</span>
          </button>
          <span class="text-xs font-mono text-slate-300 w-[72px] text-right">{{ frameToTime(currentFrameIndex) }}</span>
          <!-- 进度条（可点击跳转帧） -->
          <div class="relative flex-1 h-1.5 rounded-full bg-slate-800 overflow-hidden cursor-pointer group"
               @click="(e: MouseEvent) => {
                 const el = e.currentTarget as HTMLDivElement
                 const pct = (e.clientX - el.getBoundingClientRect().left) / el.offsetWidth
                 seekFrame(Math.round(pct * ((activeSession?.frameCount ?? 1) - 1)))
               }">
            <!-- 已提交部分 -->
            <div class="absolute inset-y-0 left-0 bg-emerald-500/40"
                 :style="{ width: (activeSession.frameCount ? activeSession.submittedFrames / activeSession.frameCount * 100 : 0) + '%' }"></div>
            <!-- 已修改部分 -->
            <div class="absolute inset-y-0 left-0 bg-indigo-500/60"
                 :style="{ width: (activeSession.frameCount ? ((activeSession as any).changedFrames || 0) / activeSession.frameCount * 100 : 0) + '%' }"></div>
            <!-- 当前帧指针 -->
            <div class="absolute top-1/2 -translate-y-1/2 w-3 h-3 rounded-full bg-white shadow pointer-events-none group-hover:scale-110 transition-transform"
                 :style="{ left: (activeSession.frameCount ? currentFrameIndex / activeSession.frameCount * 100 : 0) + '%', marginLeft: '-6px' }"></div>
          </div>
          <span class="text-xs font-mono text-slate-300 w-[72px]">{{ frameToTime(activeSession.frameCount - 1) }}</span>
          <!-- 帧跳转 -->
          <!-- ◀ 上一帧 / 下一帧 ▶ -->
          <button class="btn-secondary !py-1 !px-2 text-[10px]" :disabled="currentFrameIndex === 0"
                  @click="seekFrame(currentFrameIndex - 1)">◀ 上一帧</button>
          <button class="btn-secondary !py-1 !px-2 text-[10px]" :disabled="currentFrameIndex >= activeSession.frameCount - 1"
                  @click="seekFrame(currentFrameIndex + 1)">下一帧 ▶</button>
          <!-- 跳转到 -->
          <div class="flex items-center gap-1 text-[11px] text-slate-500">
            <span>跳转到</span>
            <input type="number" :value="currentFrameIndex"
                   @change="(e: Event) => seekFrame(+((e.target as HTMLInputElement).value) || 0)"
                   class="w-12 rounded border border-slate-700 bg-slate-950 px-2 py-0.5 font-mono text-slate-200 text-xs text-center focus:border-indigo-500 focus:outline-none" />
          </div>
          <span class="text-xs text-slate-600">帧</span>
          <span class="text-[11px] font-mono text-slate-500">{{ currentFrameIndex }} / {{ activeSession.frameCount }} 帧</span>
          <!-- 缩放 + 全屏 -->
          <div class="flex items-center gap-0.5 text-[11px] text-slate-400 ml-2">
            <button class="px-1.5 py-0.5 rounded border border-slate-700 hover:text-slate-200" @click="zoomOut">−</button>
            <span class="font-mono w-10 text-center">{{ Math.round(zoomLevel * 100) }}%</span>
            <button class="px-1.5 py-0.5 rounded border border-slate-700 hover:text-slate-200" @click="zoomIn">+</button>
            <button class="ml-1 px-1.5 py-0.5 rounded border border-slate-700 hover:text-slate-200" title="全屏">⛶</button>
          </div>
        </div>

        <!-- 左下：A vs B 对比面板（原型核心） -->
        <div class="border-t border-slate-800 bg-slate-950/30">
          <div class="flex items-center gap-4 px-4 py-2">
            <span class="text-xs font-medium text-slate-400">当前帧对比（原标注 vs 审查后）</span>
            <span class="text-[10px] text-slate-600">{{ perObjectCompare.length }} 个框 · {{ perObjectCompare.filter(r => r.state === 'modified').length }} 修改</span>
          </div>

          <!-- A/B 缩略图 -->
          <div class="flex items-start gap-4 px-4 pb-3">
            <div class="flex items-center gap-2">
              <div class="text-[10px] text-green-400 font-medium">原标注 (A)</div>
              <div class="w-[100px] h-[80px] rounded border border-green-500/40 overflow-hidden bg-black relative shrink-0">
                <img v-if="mediaFrameCache[currentFrameIndex]" :src="mediaFrameCache[currentFrameIndex]" class="w-full h-full object-contain" />
                <div v-for="o in currentBaselineObjs" :key="'TA'+o.objectId" class="absolute border border-green-400"
                     :style="{
                       left: o.bbox[0]+'px', top: o.bbox[1]+'px',
                       width: (o.bbox[2]-o.bbox[0])+'px', height: (o.bbox[3]-o.bbox[1])+'px',
                       transform: 'scale(100/640)', transformOrigin: 'top left',
                     }"></div>
              </div>
            </div>
            <div class="text-slate-500 text-lg leading-none">→</div>
            <div class="flex items-center gap-2">
              <div class="w-[100px] h-[80px] rounded border border-red-500/40 overflow-hidden bg-black relative shrink-0">
                <img v-if="mediaFrameCache[currentFrameIndex]" :src="mediaFrameCache[currentFrameIndex]" class="w-full h-full object-contain" />
                <div v-for="p in (showRawToggle ? [] : draftPatch)" :key="'TB'+p.objectId" class="absolute border border-red-500"
                     :style="{
                       left: p.bbox[0]+'px', top: p.bbox[1]+'px',
                       width: (p.bbox[2]-p.bbox[0])+'px', height: (p.bbox[3]-p.bbox[1])+'px',
                       transform: 'scale(100/640)', transformOrigin: 'top left',
                     }"></div>
              </div>
              <div class="text-[10px] text-red-400 font-medium">审查后 (当前)</div>
            </div>

            <!-- Patch 数值对比表（原型风格） -->
            <div class="flex-1 min-w-0 ml-4 rounded-lg border border-slate-800 overflow-hidden">
              <table class="w-full text-[10px]">
                <thead class="bg-slate-950 text-slate-600">
                  <tr>
                    <th class="px-2 py-1 text-left">ID</th>
                    <th class="px-2 py-1 text-left">原标注坐标 (A)</th>
                    <th class="px-2 py-1 text-left">审后坐标 (B)</th>
                    <th class="px-2 py-1 text-left">状态</th>
                    <th class="px-2 py-1 text-left">IoU</th>
                    <th class="px-2 py-1 text-left">位置</th>
                    <th class="px-2 py-1 text-left">尺寸</th>
                    <th class="px-2 py-1 text-left">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-if="perObjectCompare.length === 0" class="text-center text-slate-600">
                    <td colspan="8" class="py-2">当前帧无标注框</td>
                  </tr>
                  <tr v-for="r in perObjectCompare" :key="r.objectId" class="border-t border-slate-800 hover:bg-slate-800/30">
                    <td class="px-2 py-1 font-mono text-slate-300">#{{ String(r.objectId).padStart(2,'0') }}</td>
                    <td class="px-2 py-1 font-mono text-green-400">x:{{ r.a[0] }} y:{{ r.a[1] }} w:{{ r.a[2]-r.a[0] }} · h:{{ r.a[3]-r.a[1] }}</td>
                    <td class="px-2 py-1 font-mono text-red-400">x:{{ r.b[0] }} y:{{ r.b[1] }} w:{{ r.b[2]-r.b[0] }} · h:{{ r.b[3]-r.b[1] }}</td>
                    <td class="px-2 py-1">
                      <span :class="[
                        'rounded px-1.5 py-0.5 text-[9px] font-medium',
                        r.state === 'modified' ? 'bg-red-500/20 text-red-300' : 'bg-emerald-500/20 text-emerald-300'
                      ]">{{ r.state === 'modified' ? '已修改' : '未修改' }}</span>
                    </td>
                    <td class="px-2 py-1 font-mono" :class="r.iou === null ? 'text-slate-600' : r.iou > 0.5 ? 'text-emerald-400' : 'text-amber-400'">
                      {{ r.iou === null ? '—' : r.iou.toFixed(3) }}
                    </td>
                    <td class="px-2 py-1 font-mono text-slate-400">({{ r.dx }}, {{ r.dy }})</td>
                    <td class="px-2 py-1 font-mono text-slate-400">({{ r.dw }}, {{ r.dh }})</td>
                    <td class="px-2 py-1 text-slate-600">—</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- reopen 提示（已提交帧） -->
          <div v-if="currentFrameInfo?.state === 'submitted'"
               class="rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2 mb-2 flex items-center justify-between">
            <div class="text-[11px] text-amber-300">本帧已提交 · 可重新编辑</div>
            <button class="btn-secondary !py-1 !px-2 text-[10px]" @click="reopenCurrentFrame">重新编辑本帧</button>
          </div>
          <!-- 可编辑 Patch 区（原型的可修改 bbox） -->
          <div v-if="!showRawToggle" class="px-4 pb-3 space-y-2">
            <div class="flex items-center gap-2">
              <span class="text-[11px] font-medium text-slate-400">修改标注框 (B)</span>
              <span class="text-[10px] text-slate-600">只允许移动/缩放已存在的 A 框，不允许新增或删除</span>
              <div class="ml-auto flex gap-1">
                <button class="text-[10px] px-2 py-0.5 rounded border border-slate-700 text-slate-400 hover:text-slate-200" @click="addPatch">+ 添加</button>
                <button class="text-[10px] px-2 py-0.5 rounded border border-slate-700 text-red-400 hover:text-red-300 hover:border-red-500/50" @click="clearPatch">清空</button>
              </div>
            </div>

            <div class="rounded-lg border border-slate-800 overflow-hidden">
              <table class="w-full text-[11px]">
                <thead class="bg-slate-950 text-slate-500">
                  <tr>
                    <th class="px-2 py-1 text-left w-12">ID</th>
                    <th class="px-2 py-1 text-left">x1</th><th class="px-2 py-1 text-left">y1</th>
                    <th class="px-2 py-1 text-left">x2</th><th class="px-2 py-1 text-left">y2</th>
                    <th class="px-2 py-1 text-left w-14">状态</th>
                    <th class="px-2 py-1 text-left w-8"></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-if="draftPatch.length === 0" class="text-center text-slate-600">
                    <td colspan="7" class="py-2">点击 + 添加 修改已有框坐标</td>
                  </tr>
                  <tr v-for="(p, i) in draftPatch" :key="'P'+i" class="border-t border-slate-800">
                    <td class="px-2 py-1.5 font-mono text-slate-300">#{{ String(p.objectId).padStart(2,'0') }}</td>
                    <td class="px-1 py-1"><input type="number" :value="p.bbox[0]" @input="(e:any)=>updatePatchBBox(i,'x1',+e.target.value)" class="w-full rounded bg-slate-950 border border-slate-700 px-1 py-0.5 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"></td>
                    <td class="px-1 py-1"><input type="number" :value="p.bbox[1]" @input="(e:any)=>updatePatchBBox(i,'y1',+e.target.value)" class="w-full rounded bg-slate-950 border border-slate-700 px-1 py-0.5 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"></td>
                    <td class="px-1 py-1"><input type="number" :value="p.bbox[2]" @input="(e:any)=>updatePatchBBox(i,'x2',+e.target.value)" class="w-full rounded bg-slate-950 border border-slate-700 px-1 py-0.5 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"></td>
                    <td class="px-1 py-1"><input type="number" :value="p.bbox[3]" @input="(e:any)=>updatePatchBBox(i,'y2',+e.target.value)" class="w-full rounded bg-slate-950 border border-slate-700 px-1 py-0.5 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"></td>
                    <td class="px-2 py-1 text-[10px]">
                      <span :class="[
                        'rounded px-1.5 py-0.5 font-medium',
                        currentBaselineObjs.find(o=>o.objectId===p.objectId) ? 'bg-amber-500/20 text-amber-300' : 'bg-blue-500/20 text-blue-300'
                      ]">{{ currentBaselineObjs.find(o=>o.objectId===p.objectId) ? '已改' : '新增' }}</span>
                    </td>
                    <td class="px-2 py-1"><button class="text-red-400 hover:text-red-300 text-sm" @click="removePatchItem(i)">✕</button></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <!-- 右：统计 + 操作 + 圆环进度（原型风格） -->
      <div class="border-l border-slate-800 bg-slate-950/50 flex flex-col overflow-y-auto">

        <!-- 当前帧统计 -->
        <div class="p-3 border-b border-slate-800">
          <h3 class="mb-2 text-[11px] font-medium text-slate-400 uppercase tracking-wider">当前帧审查统计</h3>
          <div class="space-y-1.5">
            <div class="flex items-center justify-between text-[11px]">
              <span class="text-slate-500">当前标注框数</span>
              <span class="font-mono text-slate-200 text-sm">{{ currentFrameStats.total }}</span>
            </div>
            <div class="flex items-center justify-between text-[11px]">
              <span class="text-slate-500">已修改</span>
              <span class="font-mono text-sm" :class="currentFrameStats.changed ? 'text-red-400' : 'text-slate-400'">{{ currentFrameStats.changed }}</span>
            </div>
            <div class="flex items-center justify-between text-[11px]">
              <span class="text-slate-500">未修改</span>
              <span class="font-mono text-sm text-emerald-400">{{ currentFrameStats.unchanged }}</span>
            </div>
            <div class="mt-2 pt-2 border-t border-slate-800 space-y-1.5">
              <div class="flex items-center justify-between text-[11px]">
                <span class="text-slate-500">平均 IoU (修改框)</span>
                <span class="font-mono text-sm"
                      :class="currentFrameStats.avgIoU === null ? 'text-slate-600' : currentFrameStats.avgIoU > 0.7 ? 'text-emerald-400' : currentFrameStats.avgIoU > 0.4 ? 'text-amber-400' : 'text-red-400'">
                  {{ currentFrameStats.avgIoU === null ? '—' : currentFrameStats.avgIoU.toFixed(3) }}
                </span>
              </div>
              <div class="flex items-center justify-between text-[11px]">
                <span class="text-slate-500">最大位置偏移</span>
                <span class="font-mono text-sm"
                      :class="currentFrameStats.maxShift === null ? 'text-slate-600' : currentFrameStats.maxShift > 20 ? 'text-red-400' : 'text-slate-200'">
                  {{ currentFrameStats.maxShift === null ? '—' : currentFrameStats.maxShift + ' px' }}
                </span>
              </div>
            </div>
          </div>
        </div>

        <!-- 审查操作（原型风格） -->
        <div class="p-3 border-b border-slate-800 space-y-2">
          <h3 class="text-[11px] font-medium text-slate-400 uppercase tracking-wider">审查操作</h3>

          <div class="rounded-lg border border-slate-800 bg-slate-900/50 p-2 space-y-1">
            <div class="flex items-center gap-2 text-[11px] text-slate-200">
              <span class="text-amber-400">✏️</span> 直接修改 bbox 坐标进行位置调整
            </div>
            <div class="text-[10px] text-slate-600 leading-snug">
              只能修改已有的标注框，无法新增或删除
            </div>
          </div>

          <!-- 撤销按钮 -->
          <button class="w-full text-xs px-3 py-2 rounded-lg border border-slate-700 bg-slate-900/50 text-slate-300 hover:bg-slate-800 hover:text-slate-100 hover:border-slate-500 transition-colors flex items-center justify-center gap-2"
                  :disabled="draftHistory.length === 0" @click="undoDraft">
            <span>↺</span> 撤销 <span class="text-slate-500">({{ draftHistory.length }})</span>
          </button>

          <!-- 提交本帧（原型大按钮） -->
          <button class="w-full text-xs px-3 py-2.5 rounded-lg bg-gradient-to-br from-indigo-500 to-indigo-600 text-white font-medium hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-md shadow-indigo-500/20 flex items-center justify-center gap-2"
                  :disabled="submitting" @click="submitCurrentFrame">
            <span>✓</span> {{ submitting ? '提交中...' : (currentFrameInfo?.state === 'submitted' ? '重新提交本帧' : '提交本帧') }}
          </button>

          <button v-if="allSubmitted && activeSession.state === 'in_progress'"
                  class="w-full text-xs px-3 py-2 rounded-lg border border-indigo-500/50 text-indigo-300 hover:bg-indigo-500/10 transition-colors"
                  :disabled="submitting" @click="manualFreeze">
            🔒 冻结审查版本
          </button>

          <div v-if="activeSession.state === 'reviewed'" class="text-center text-[11px] text-emerald-400 pt-1">
            ✓ 已冻结，可进入对比确认
            <button class="block w-full mt-2 btn-primary !py-2" @click="router.push('/confirm')">→ 对比确认页</button>
          </div>
        </div>

        <!-- 视频审查进度（原型圆环风格） -->
        <div class="p-3 flex-1">
          <h3 class="text-[11px] font-medium text-slate-400 uppercase tracking-wider mb-3">视频审查进度</h3>

          <!-- 圆环 -->
          <div class="flex items-center gap-3">
            <div class="relative w-[80px] h-[80px] shrink-0">
              <svg class="w-full h-full -rotate-90" viewBox="0 0 80 80">
                <circle cx="40" cy="40" :r="ringRadius" fill="none" stroke="rgb(30,41,59)" stroke-width="8" />
                <circle cx="40" cy="40" :r="ringRadius" fill="none"
                        stroke="url(#ringGrad)" stroke-width="8" stroke-linecap="round"
                        :stroke-dasharray="ringCircum"
                        :stroke-dashoffset="ringCircum * (1 - ringProgress / 100)"
                        class="transition-all duration-500" />
                <defs>
                  <linearGradient id="ringGrad" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0%" stop-color="#6366f1" />
                    <stop offset="100%" stop-color="#10b981" />
                  </linearGradient>
                </defs>
              </svg>
              <div class="absolute inset-0 flex items-center justify-center text-sm font-mono font-semibold text-slate-200">
                {{ ringProgress }}%
              </div>
            </div>
            <div class="flex-1 space-y-1">
              <div class="flex items-center justify-between text-[10px]">
                <span class="text-slate-500">已审查帧数</span>
                <span class="font-mono text-slate-200">{{ activeSession.submittedFrames }} / {{ activeSession.frameCount }}</span>
              </div>
              <div class="flex items-center justify-between text-[10px]">
                <span class="text-slate-500">修改帧数</span>
                <span class="font-mono text-indigo-300">{{ (activeSession as any).changedFrames || 0 }}</span>
              </div>
            </div>
          </div>

          <div class="mt-3 text-[10px] text-emerald-400 flex items-center gap-1">
            <span>✓</span> 完成全部审查后，该视频标记为"已审查"
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
.btn-primary {
  @apply px-3 py-1.5 text-xs font-medium rounded-lg bg-gradient-to-br from-indigo-500 to-indigo-600 text-white shadow-md shadow-indigo-500/20 hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-all;
}
.btn-secondary {
  @apply px-3 py-1.5 text-xs font-medium rounded-lg border border-slate-700 bg-slate-900/60 text-slate-300 hover:bg-slate-800/60 hover:text-slate-100 disabled:opacity-50 disabled:cursor-not-allowed transition-colors;
}
</style>
