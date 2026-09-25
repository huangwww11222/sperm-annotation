<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from '../router'
import { reviewApi, type ConfirmationSession, type ReviewChange, type FinalVersion } from '../api/reviewApi'
import { http, tokenStore } from '../api/http'

const router = useRouter()

// ── Toast ──
const toast = ref<{ type: 'ok' | 'err'; msg: string } | null>(null)
function showToast(type: 'ok' | 'err', msg: string) {
  toast.value = { type, msg }
  setTimeout(() => { if (toast.value?.msg === msg) toast.value = null }, 3500)
}

// ── 列表视图 ──
const view = ref<'list' | 'session'>('list')
const sessions = ref<ConfirmationSession[]>([])
const finalVersions = ref<FinalVersion[]>([])
const loading = ref(false)
const compareMode = ref<'side-by-side' | 'overlay'>('side-by-side')

// ── Session 视图核心状态 ──
const activeCs = ref<ConfirmationSession | null>(null)
const changes = ref<ReviewChange[]>([])
const activeIndex = ref(0)
const mediaFrameCache = ref<Record<number, string>>({})
const note = ref('')
const draftChoice = ref<'A' | 'B' | null>(null)

const currentChange = computed(() => changes.value[activeIndex.value])
const totalChanges = computed(() => changes.value.length)
const decidedCount = computed(() => changes.value.filter(c => c.decision).length)
const progressPct = computed(() => totalChanges.value ? Math.round(decidedCount.value / totalChanges.value * 100) : 0)
const allDecided = computed(() => totalChanges.value > 0 && decidedCount.value === totalChanges.value)

// ── 原型新增状态 ──
const leftFilter = ref<'all' | 'pending' | 'confirmed'>('all')
const leftSearch = ref('')
const zoomPct = ref(100)

const roi = computed(() => {
  const c = currentChange.value
  if (!c) return null
  const A = c.beforeBbox, B = c.afterBbox
  if (!A || !B) return null
  const x1 = Math.min(A[0], B[0]), y1 = Math.min(A[1], B[1])
  const x2 = Math.max(A[2], B[2]), y2 = Math.max(A[3], B[3])
  const pad = Math.max(8, Math.round(Math.max(A[2]-A[0], B[2]-B[0], A[3]-A[1], B[3]-B[1]) * 0.5))
  return { x1: Math.max(0, x1-pad), y1: Math.max(0, y1-pad), x2: x2+pad, y2: y2+pad }
})

const filteredLeft = computed(() => {
  let list = sessions.value
  if (leftFilter.value === 'pending') list = list.filter(s => s.state !== 'confirmed')
  else if (leftFilter.value === 'confirmed') list = list.filter(s => s.state === 'confirmed')
  if (leftSearch.value.trim()) {
    const q = leftSearch.value.toLowerCase()
    list = list.filter(s => (s as any).mediaId?.toLowerCase?.().includes(q) || s.id.toLowerCase().includes(q))
  }
  return list
})

const pendingCount = computed(() => totalChanges.value - decidedCount.value)
const adoptedBCount = computed(() => changes.value.filter(c => c.decision?.choice === 'B').length)
const keptACount = computed(() => changes.value.filter(c => c.decision?.choice === 'A').length)

function zoomIn() { zoomPct.value = Math.min(200, zoomPct.value + 25) }
function zoomOut() { zoomPct.value = Math.max(50, zoomPct.value - 25) }
function zoomReset() { zoomPct.value = 100 }

async function loadAll() {
  loading.value = true
  try {
    const [ses, fvs] = await Promise.all([
      reviewApi.listConfirmations().catch(() => ({ items: [] as ConfirmationSession[] })),
      reviewApi.listFinalVersions().catch(() => ({ items: [] as FinalVersion[] })),
    ])
    sessions.value = ses.items
    finalVersions.value = fvs.items
  } catch (e: any) {
    showToast('err', e?.message || '加载失败')
  } finally { loading.value = false }
}
onMounted(loadAll)

async function openCs(cs: ConfirmationSession) {
  loading.value = true
  try {
    activeCs.value = await reviewApi.getConfirmation(cs.id)
    const r = await reviewApi.listChanges(cs.id)
    changes.value = r.items
    activeIndex.value = 0
    await resolveMediaFrames()
    view.value = 'session'
  } catch (e: any) {
    showToast('err', e?.message || '打开失败')
  } finally { loading.value = false }
}

async function resolveMediaFrames() {
  mediaFrameCache.value = {}
  if (!activeCs.value) return
  const mediaId = (activeCs.value as any).mediaId
  if (!mediaId) return
  const uniqueFrames = new Set(changes.value.map(c => c.frameIndex))
  for (const fi of uniqueFrames) {
    mediaFrameCache.value[fi] = `/api/track/frame/${mediaId}/${fi}?token=${tokenStore.get()}`
  }
}

function selectDraft(choice: 'A' | 'B') {
  draftChoice.value = choice
}
async function submitChoice() {
  if (!draftChoice.value || !currentChange.value || !activeCs.value) {
    showToast('err', '请先选择保留 A 或采纳 B')
    return
  }
  const choice = draftChoice.value
  const cid = currentChange.value.changeId
  const oldDecision = currentChange.value.decision
  currentChange.value.decision = { choice, decidedAt: new Date().toISOString() }
  try {
    await reviewApi.setDecision(activeCs.value!.id, cid, choice)
    showToast('ok', `已选择 ${choice === 'A' ? '保留原标注 A' : '采用审查标注 B'}`)
    draftChoice.value = null
    activeCs.value = await reviewApi.getConfirmation(activeCs.value!.id)
    changes.value = await reviewApi.listChanges(activeCs.value!.id).then(r => r.items)
  } catch (e: any) {
    currentChange.value.decision = oldDecision
    showToast('err', e?.message || '提交失败')
  }
}

function prevChange() { if (activeIndex.value > 0) { activeIndex.value--; draftChoice.value = null } }
function nextChange() { if (activeIndex.value < totalChanges.value - 1) { activeIndex.value++; draftChoice.value = null } }

async function doFinalize() {
  if (!allDecided.value) { showToast('err', '还有未选择的修改项'); return }
  try {
    const res = await reviewApi.finalize(activeCs.value!.id)
    showToast('ok', `Finalize 成功！${res.frameCount} 帧`)
    await loadAll()
    view.value = 'list'
  } catch (e: any) { showToast('err', e?.message || 'Finalize 失败') }
}

const exportFormat = ref<'coco' | 'yolo' | 'both'>('both')
async function doExport(fvId: string) {
  try {
    const res = await reviewApi.exportDataset({ finalVersionIds: [fvId], format: exportFormat.value })
    const a = document.createElement('a')
    a.href = res.downloadUrl; a.download = ''; a.click()
    showToast('ok', `导出成功`)
  } catch (e: any) { showToast('err', e?.message || '导出失败') }
}

function back() { view.value = 'list'; activeCs.value = null; changes.value = []; loadAll() }

// ── Bbox 样式辅助 ──
function bboxStyle(bbox: number[] | undefined, _color: string) {
  if (!bbox || bbox.length < 4) return { position: "absolute" as const }
  return { left: bbox[0]+'px', top: bbox[1]+'px', width: (bbox[2]-bbox[0])+'px', height: (bbox[3]-bbox[1])+'px', position: 'absolute' as const }
}
function bboxStyleScaled(bbox: number[] | undefined, color: string) {
  if (!bbox || bbox.length < 4) return { position: "absolute" as const }
  return {
    left: Math.max(0, Math.min(100, bbox[0] / 640 * 100)) + '%',
    top: Math.max(0, Math.min(100, bbox[1] / 432 * 100)) + '%',
    width: Math.max(0.5, Math.min(100, (bbox[2]-bbox[0]) / 640 * 100)) + '%',
    height: Math.max(0.5, Math.min(100, (bbox[3]-bbox[1]) / 432 * 100)) + '%',
    borderColor: color, position: 'absolute' as const,
  }
}
function deltaDx(c: ReviewChange) { return c.afterBbox[0] - c.beforeBbox[0] }
function deltaDy(c: ReviewChange) { return c.afterBbox[1] - c.beforeBbox[1] }
function deltaW(c: ReviewChange) { return (c.afterBbox[2]-c.afterBbox[0]) - (c.beforeBbox[2]-c.beforeBbox[0]) }
function deltaH(c: ReviewChange) { return (c.afterBbox[3]-c.afterBbox[1]) - (c.beforeBbox[3]-c.beforeBbox[1]) }
</script>
<template>
  <!-- Toast -->
  <div v-if="toast"
       class="fixed top-20 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-lg text-sm shadow-2xl border backdrop-blur transition-opacity"
       :class="toast.type === 'ok' ? 'bg-emerald-900/80 border-emerald-500/50 text-emerald-200' : 'bg-red-900/80 border-red-500/50 text-red-200'">
    {{ toast.msg }}
  </div>

  <!-- ============ 左栏列表视图 ============ -->
  <div v-if="view === 'list' || !activeCs" class="space-y-5">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold tracking-wide text-slate-200">对比确认会话</h2>
      <div class="text-[11px] text-slate-500">共 {{ sessions.length }} 个会话</div>
    </div>

    <div v-if="loading && sessions.length === 0" class="text-center text-slate-500 py-10">加载中...</div>
    <div v-else-if="sessions.length === 0" class="rounded-xl border border-dashed border-slate-800 p-12 text-center">
      <div class="text-slate-500 mb-2">没有待确认的视频</div>
      <div class="text-[11px] text-slate-600">请先去「审查模式」提交并冻结审查结果</div>
    </div>

    <div v-else class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      <div v-for="cs in sessions" :key="cs.id"
           class="group cursor-pointer rounded-xl border border-slate-800 bg-slate-900/50 p-4 transition-all hover:border-indigo-500/60 hover:bg-slate-900"
           @click="openCs(cs)">
        <div class="flex items-center justify-between">
          <span class="text-[11px] font-mono text-slate-500 truncate max-w-[180px]">{{ (cs as any).mediaId || cs.id.slice(-10) }}</span>
          <span :class="[
            'rounded-full border px-2 py-0.5 text-[10px] font-medium shrink-0',
            cs.state === 'confirmed' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' :
            cs.state === 'blocked' ? 'bg-red-500/20 text-red-300 border-red-500/40' :
            'bg-indigo-500/20 text-indigo-300 border-indigo-500/40'
          ]">{{ {pending:'待确认',in_progress:'确认中',confirmed:'已确认',blocked:'阻塞',returned:'已退回'}[cs.state] }}</span>
        </div>
        <div class="mt-3 flex items-center justify-between text-[11px] text-slate-400">
          <span>修改项</span>
          <span class="font-mono text-slate-300">{{ (cs as any).totalChanges ?? 0 }}</span>
        </div>
        <div class="mt-1 flex items-center justify-between text-[11px] text-slate-400">
          <span>已确认</span>
          <span class="font-mono text-slate-300">{{ (cs as any).decidedCount ?? 0 }}/{{ (cs as any).totalChanges ?? 0 }}</span>
        </div>
        <div class="mt-1.5 h-1.5 rounded-full bg-slate-800 overflow-hidden">
          <div class="h-full bg-gradient-to-r from-indigo-500 to-emerald-500 transition-all"
               :style="{ width: (cs as any).totalChanges ? Math.round(((cs as any).decidedCount / (cs as any).totalChanges) * 100) + '%' : '0%' }"></div>
        </div>
        <button class="mt-3 w-full btn-primary" @click.stop="openCs(cs)">
          {{ cs.state === 'confirmed' ? '查看已确认' : '继续确认' }} →
        </button>
      </div>
    </div>

    <!-- FinalVersion 导出区 -->
    <div v-if="finalVersions.length > 0" class="pt-4 border-t border-slate-800">
      <h3 class="text-sm font-medium text-slate-300 mb-3">最终版本（已确认冻结）</h3>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div v-for="fv in finalVersions" :key="fv.id"
             class="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4">
          <div class="flex items-center justify-between">
            <span class="font-mono text-[11px] text-emerald-300">{{ fv.id.slice(-12) }}</span>
            <span class="text-[10px] text-emerald-400">✓ frozen</span>
          </div>
          <div class="mt-1 text-[11px] text-slate-500">帧 {{ fv.frameCount }} · 修改 {{ (fv as any).changedFrames ?? 0 }}</div>
          <div class="mt-3 flex gap-1">
            <button class="btn-primary !py-1 !px-2 text-[10px]" @click="doExport(fv.id)">📦 导出 COCO/YOLO</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ Session 视图（原型 2 & 3 共用） ============ -->
  <div v-else-if="view === 'session' && activeCs" class="h-[calc(100vh-80px)] overflow-hidden flex flex-col">

    <!-- 顶部面包屑 + 文件信息栏 -->
    <div class="flex items-center gap-3 px-4 py-2 border-b border-slate-800 bg-slate-900/50">
      <button class="btn-secondary" @click="back">← 返回列表</button>
      <div class="text-sm text-slate-200 font-medium">
        <span class="font-mono">{{ (activeCs as any).mediaId?.slice(0, 10) || activeCs.id.slice(0, 10) }}.avi</span>
        <span class="ml-3 text-[11px] text-slate-500">总修改项 <span class="text-slate-300 font-mono">{{ totalChanges }}</span>
          <span class="text-slate-600 mx-2">/</span>
          当前修改项 <span class="text-slate-300 font-mono">{{ activeIndex + 1 }} / {{ totalChanges }}</span>
          <span class="text-slate-600 mx-2">·</span>
          当前帧 <span class="text-slate-300 font-mono">#{{ currentChange?.frameIndex ?? 0 }}</span></span>
      </div>
      <span :class="[
        'rounded-full border px-2 py-0.5 text-[11px]',
        activeCs.state === 'confirmed' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' :
        activeCs.state === 'blocked' ? 'bg-red-500/20 text-red-300 border-red-500/40' :
        'bg-indigo-500/20 text-indigo-300 border-indigo-500/40'
      ]">{{ {pending:'待确认',in_progress:'确认中',confirmed:'已确认',blocked:'阻塞'}[activeCs.state] }}</span>
    </div>

    <!-- 主三栏 -->
    <div class="flex-1 grid gap-0 overflow-hidden" style="grid-template-columns: 240px 1fr 320px;">

      <!-- 左栏：已审核视频列表（原型风格） -->
      <div class="border-r border-slate-800 bg-slate-950/50 flex flex-col overflow-hidden">
        <div class="px-3 py-2 border-b border-slate-800">
          <h3 class="text-xs font-medium text-slate-400">已审核视频列表</h3>
          <div class="mt-1 flex items-center gap-1">
            <div class="flex items-center rounded border border-slate-700 bg-slate-900/70 flex-1 px-2 py-1">
              <span class="text-slate-600 text-[10px]">🔍</span>
              <input v-model="leftSearch" placeholder="搜索视频名称..."
                     class="flex-1 bg-transparent text-[11px] text-slate-200 placeholder-slate-600 focus:outline-none ml-1" />
            </div>
          </div>
          <div class="mt-1 flex items-center gap-0.5">
            <button v-for="f in (['all','pending','confirmed'] as const)" :key="f"
                    class="px-1.5 py-0.5 text-[10px] rounded transition-colors"
                    :class="leftFilter === f ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-500 hover:text-slate-300'"
                    @click="leftFilter = f">
              {{ {all:`全部(${sessions.length})`, pending:`待确认(${sessions.filter(s=>s.state!=='confirmed').length})`, confirmed:`已确认(${sessions.filter(s=>s.state==='confirmed').length})`}[f] }}
            </button>
          </div>
        </div>
        <div class="flex-1 overflow-y-auto p-2 space-y-1.5">
          <div v-for="cs in filteredLeft" :key="cs.id"
               class="rounded-lg border p-2 transition-colors cursor-pointer"
               :class="[
                 cs.id === activeCs.id ? 'border-indigo-500 bg-indigo-500/10' :
                 'border-slate-800 hover:border-slate-600 bg-slate-900/30'
               ]" @click="openCs(cs)">
            <div class="flex items-center gap-2">
              <div class="w-8 h-6 rounded bg-slate-800 overflow-hidden shrink-0 flex items-center justify-center text-slate-600 text-[10px]">帧</div>
              <div class="flex-1 min-w-0">
                <div class="text-[11px] text-slate-300 truncate">{{ (cs as any).mediaId || cs.id.slice(-12) }}</div>
                <div class="text-[9px] text-slate-500">已确认 {{ (cs as any).decidedCount ?? 0 }} / {{ (cs as any).totalChanges ?? 0 }}</div>
              </div>
            </div>
            <div class="mt-1 h-1 rounded-full bg-slate-800 overflow-hidden">
              <div class="h-full transition-all"
                   :class="cs.state === 'confirmed' ? 'bg-emerald-500' : 'bg-indigo-500'"
                   :style="{ width: (cs as any).totalChanges ? Math.round(((cs as any).decidedCount/(cs as any).totalChanges)*100)+'%' : '0%' }"></div>
            </div>
            <div class="mt-1 flex items-center justify-between">
              <span class="text-[9px] text-slate-600">总修改项 {{ (cs as any).totalChanges ?? 0 }}</span>
              <span :class="[
                'text-[9px] font-medium',
                cs.state === 'confirmed' ? 'text-emerald-400' : 'text-amber-400'
              ]">{{ cs.state === 'confirmed' ? '✓ 已确认' : '待确认' }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 中央主区 -->
      <div class="flex flex-col overflow-hidden bg-slate-900/20">

        <!-- 模式切换 Tab（原型顶部） -->
        <div class="flex items-center gap-2 px-4 py-2 border-b border-slate-800 bg-slate-900/30">
          <span class="text-[11px] text-slate-500 mr-1">顶部对比模式</span>
          <div class="flex items-center rounded border border-slate-700 overflow-hidden">
            <button class="px-3 py-1 text-[11px] transition-colors"
                    :class="compareMode === 'side-by-side' ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-400 hover:text-slate-200'"
                    @click="compareMode = 'side-by-side'">A/B 对比</button>
            <button class="px-3 py-1 text-[11px] transition-colors"
                    :class="compareMode === 'overlay' ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-400 hover:text-slate-200'"
                    @click="compareMode = 'overlay'">叠加对比</button>
          </div>
          <span class="ml-auto text-[10px] text-slate-600">提示：A = 原始标注（紫色虚线）· B = 审查修改（青色实线）</span>
        </div>

        <!-- 上：ROI 对比区（两种模式切换） -->
        <div class="border-b border-slate-800 bg-slate-950/30">

          <!-- A/B 并排（原型图 2） -->
          <template v-if="compareMode === 'side-by-side'">
            <div class="p-4 grid gap-4" style="grid-template-columns: 1fr 1fr;">
              <!-- A -->
              <div class="rounded-lg border border-indigo-500/30 bg-slate-900/50 overflow-hidden">
                <div class="px-3 py-1.5 border-b border-slate-800 flex items-center justify-between">
                  <span class="text-[11px] text-indigo-300 font-medium">A 原始标注（标注员）</span>
                  <span class="text-[10px] text-slate-500 font-mono">ID #{{ currentChange?.objectId ?? 0 }}</span>
                </div>
                <div class="relative bg-black flex items-center justify-center" style="min-height: 160px;">
                  <img v-if="roi && mediaFrameCache[currentChange?.frameIndex ?? -1]"
                       :src="mediaFrameCache[currentChange!.frameIndex]"
                       class="max-w-full max-h-[260px] object-contain"
                       :style="`object-position: center;`" />
                  <!-- ROI 裁剪：用负 margin 实现 -->
                  <div v-if="roi" class="absolute inset-0 overflow-hidden pointer-events-none">
                    <!-- 这里做 ROI 裁剪：把 img 用 transform 或直接裁剪 -->
                  </div>
                  <!-- A bbox 紫色虚线（相对当前全图坐标） -->
                  <div v-if="currentChange"
                       class="absolute border-2 border-dashed border-indigo-400 pointer-events-none"
                       :style="bboxStyle(currentChange.beforeBbox, '#818cf8')"></div>
                </div>
                <div class="px-3 py-1.5 text-[10px] text-slate-500 font-mono">
                  A = {{ (currentChange?.beforeBbox ?? []).map((v,i) => i < 2 ? `${v},` : v).join(' ') }}
                  <span class="ml-3 text-slate-600">w={{ (currentChange?.beforeBbox?.[2] ?? 0) - (currentChange?.beforeBbox?.[0] ?? 0) }} · h={{ (currentChange?.beforeBbox?.[3] ?? 0) - (currentChange?.beforeBbox?.[1] ?? 0) }}</span>
                </div>
              </div>

              <!-- B -->
              <div class="rounded-lg border border-emerald-500/30 bg-slate-900/50 overflow-hidden">
                <div class="px-3 py-1.5 border-b border-slate-800 flex items-center justify-between">
                  <span class="text-[11px] text-emerald-300 font-medium">B 审查修改（审查员）</span>
                  <span class="text-[10px] text-slate-500 font-mono">ID #{{ currentChange?.objectId ?? 0 }}</span>
                </div>
                <div class="relative bg-black flex items-center justify-center" style="min-height: 160px;">
                  <img v-if="roi && mediaFrameCache[currentChange?.frameIndex ?? -1]"
                       :src="mediaFrameCache[currentChange!.frameIndex]"
                       class="max-w-full max-h-[260px] object-contain" />
                  <div v-if="currentChange"
                       class="absolute border-2 border-emerald-400 pointer-events-none"
                       :style="bboxStyle(currentChange.afterBbox, '#34d399')"></div>
                </div>
                <div class="px-3 py-1.5 text-[10px] text-slate-500 font-mono">
                  B = {{ (currentChange?.afterBbox ?? []).map((v,i) => i < 2 ? `${v},` : v).join(' ') }}
                  <span class="ml-3 text-slate-600">w={{ (currentChange?.afterBbox?.[2] ?? 0) - (currentChange?.afterBbox?.[0] ?? 0) }} · h={{ (currentChange?.afterBbox?.[3] ?? 0) - (currentChange?.afterBbox?.[1] ?? 0) }}</span>
                </div>
              </div>
            </div>
          </template>

          <!-- 叠加对比（原型图 3） -->
          <template v-else-if="compareMode === 'overlay'">
            <div class="p-4">
              <div class="rounded-lg border border-slate-700 bg-slate-900/50 overflow-hidden">
                <div class="px-3 py-1.5 border-b border-slate-800 flex items-center justify-between">
                  <span class="text-[11px] text-slate-300 font-medium">叠加对比（同一目标的 A 与 B 标注）</span>
                  <div class="flex items-center gap-3 text-[10px]">
                    <span class="flex items-center gap-1"><span class="inline-block w-3 h-3 border-2 border-dashed border-indigo-400 rounded-sm"></span>A 原始标注 #{{ currentChange?.objectId ?? 0 }}</span>
                    <span class="flex items-center gap-1"><span class="inline-block w-3 h-3 border-2 border-emerald-400 rounded-sm"></span>B 审查修改 #{{ currentChange?.objectId ?? 0 }}</span>
                  </div>
                </div>
                <div class="relative bg-black flex items-center justify-center" style="min-height: 260px;">
                  <img v-if="mediaFrameCache[currentChange?.frameIndex ?? -1]"
                       :src="mediaFrameCache[currentChange!.frameIndex]"
                       class="max-w-full max-h-[320px] object-contain" />
                  <!-- A 紫色虚线 -->
                  <div v-if="currentChange"
                       class="absolute border-2 border-dashed border-indigo-400 pointer-events-none"
                       :style="bboxStyle(currentChange.beforeBbox, '#818cf8')"></div>
                  <!-- B 青色实线 -->
                  <div v-if="currentChange"
                       class="absolute border-2 border-emerald-400 pointer-events-none"
                       :style="bboxStyle(currentChange.afterBbox, '#34d399')"></div>
                  <!-- 标签外置 -->
                  <div v-if="currentChange"
                       class="absolute pointer-events-none">
                    <span class="bg-indigo-500 text-white text-[9px] px-1 rounded font-mono absolute"
                          :style="{ left: currentChange.beforeBbox[0]+'px', top: (currentChange.beforeBbox[1]-16)+'px' }">A#{{ currentChange.objectId }}</span>
                    <span class="bg-emerald-500 text-white text-[9px] px-1 rounded font-mono absolute"
                          :style="{ left: currentChange.afterBbox[0]+'px', top: (currentChange.afterBbox[1]-30)+'px' }">B#{{ currentChange.objectId }}</span>
                  </div>
                </div>
                <div class="px-3 py-1.5 text-[10px] text-slate-500 font-mono flex gap-6">
                  <span>A: x={{ currentChange?.beforeBbox?.[0] ?? 0 }} / y={{ currentChange?.beforeBbox?.[1] ?? 0 }} / w={{ (currentChange?.beforeBbox?.[2] ?? 0)-(currentChange?.beforeBbox?.[0] ?? 0) }} / h={{ (currentChange?.beforeBbox?.[3] ?? 0)-(currentChange?.beforeBbox?.[1] ?? 0) }}</span>
                  <span>B: x={{ currentChange?.afterBbox?.[0] ?? 0 }} / y={{ currentChange?.afterBbox?.[1] ?? 0 }} / w={{ (currentChange?.afterBbox?.[2] ?? 0)-(currentChange?.afterBbox?.[0] ?? 0) }} / h={{ (currentChange?.afterBbox?.[3] ?? 0)-(currentChange?.afterBbox?.[1] ?? 0) }}</span>
                </div>
              </div>
            </div>
          </template>
        </div>

        <!-- 下 1：当前帧全图（固定展示，原型要求始终展示） -->
        <div class="border-b border-slate-800 bg-slate-900/30">
          <div class="flex items-center gap-3 px-4 py-2">
            <span class="text-[11px] text-slate-400 font-medium">当前帧全图（固定展示）</span>
            <span class="text-[10px] text-slate-600">· 第 {{ currentChange?.frameIndex ?? 0 }} / {{ (activeCs as any).frameCount ?? '?' }} 帧 · 共 {{ changes.length }} 项修改</span>
            <span class="ml-auto text-[10px] text-slate-500">缩放
              <button class="px-1.5 py-0.5 rounded border border-slate-700 hover:text-slate-300" @click="zoomOut">−</button>
              <span class="mx-1 font-mono w-12 text-center">{{ zoomPct }}%</span>
              <button class="px-1.5 py-0.5 rounded border border-slate-700 hover:text-slate-300" @click="zoomIn">+</button>
              <button class="px-1.5 py-0.5 rounded border border-slate-700 hover:text-slate-300 ml-1" @click="zoomReset">重置</button>
            </span>
          </div>
          <div class="px-4 pb-3">
            <div class="relative rounded-lg border border-slate-700 bg-black overflow-hidden" :style="{ transform: `scale(${zoomPct/100})`, transformOrigin: 'top left' }">
              <img v-if="currentChange && mediaFrameCache[currentChange.frameIndex]"
                   :src="mediaFrameCache[currentChange.frameIndex]"
                   class="w-full h-auto object-contain" style="max-height: 320px;" />
              <div v-else class="h-[240px] flex items-center justify-center text-slate-600 text-xs">全帧加载中...</div>
              <!-- 当前帧所有修改项 bbox 叠加 -->
              <template v-if="currentChange">
                <template v-for="c in changes.filter(x => x.frameIndex === currentChange.frameIndex)" :key="c.changeId">
                  <!-- 未选的用低对比 -->
                  <div v-if="c.changeId !== currentChange.changeId"
                       class="absolute border border-slate-500/50 pointer-events-none"
                       :style="bboxStyle(c.decision?.choice === 'B' ? c.afterBbox : c.beforeBbox, '#64748b33')"></div>
                  <!-- 当前选中的 -->
                  <div v-else class="absolute border-2 pointer-events-none"
                       :class="currentChange.decision?.choice === 'A' ? 'border-indigo-400' : 'border-emerald-400'"
                       :style="bboxStyle(currentChange.decision?.choice === 'A' ? currentChange.beforeBbox : currentChange.afterBbox, currentChange.decision?.choice === 'A' ? '#818cf8' : '#34d399')">
                    <span class="absolute -top-3 left-0 text-[9px] font-mono bg-slate-900/80 text-slate-200 px-0.5 rounded pointer-events-none">#{{ String(currentChange.objectId).padStart(3,'0') }}</span>
                  </div>
                </template>
              </template>
            </div>
            <!-- 上一项 / 下一项 -->
            <div class="mt-3 flex items-center justify-center gap-2">
              <button class="btn-secondary !py-1 !px-3 text-[11px]" :disabled="activeIndex === 0" @click="prevChange">← 上一项</button>
              <span class="text-[11px] text-slate-500">{{ activeIndex + 1 }} / {{ totalChanges }}</span>
              <button class="btn-secondary !py-1 !px-3 text-[11px]" :disabled="activeIndex === totalChanges - 1" @click="nextChange">下一项 →</button>
            </div>
          </div>
        </div>

        <!-- 下 2：修改项列表（仅被修改的项） -->
        <div class="flex-1 overflow-y-auto bg-slate-950/20">
          <div class="px-4 py-2 flex items-center justify-between border-b border-slate-800">
            <span class="text-[11px] text-slate-400 font-medium">修改项列表（仅显示被审查员修改的部分）</span>
            <span class="text-[10px] text-slate-600">{{ changes.length }} 项 · {{ changes.filter(c=>c.decision).length }} 已确认</span>
          </div>
          <table class="w-full text-[11px]">
            <thead class="bg-slate-900/50 text-slate-500 sticky top-0 z-10">
              <tr>
                <th class="px-2 py-1.5 text-left w-10"></th>
                <th class="px-2 py-1.5 text-left w-14">ID</th>
                <th class="px-2 py-1.5 text-left w-16">帧号</th>
                <th class="px-2 py-1.5 text-left w-16">原缩略图</th>
                <th class="px-2 py-1.5 text-left w-16">审后缩略图</th>
                <th class="px-2 py-1.5 text-left w-14">IoU</th>
                <th class="px-2 py-1.5 text-left w-16">位置变化(px)</th>
                <th class="px-2 py-1.5 text-left w-14">尺寸变化</th>
                <th class="px-2 py-1.5 text-left w-24">当前确认结果</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(c, i) in changes" :key="c.changeId"
                  class="border-b border-slate-800 cursor-pointer transition-colors"
                  :class="i === activeIndex ? 'bg-indigo-500/10 border-indigo-500/40' : 'hover:bg-slate-800/30'"
                  @click="activeIndex = i">
                <td class="px-2 py-1.5">
                  <input type="checkbox" class="accent-indigo-500" :checked="!!c.decision" :disabled="true" />
                </td>
                <td class="px-2 py-1.5 font-mono text-slate-300">#{{ String(c.objectId).padStart(3,'0') }}</td>
                <td class="px-2 py-1.5 font-mono text-slate-400">{{ c.frameIndex }}</td>
                <!-- 缩略图 A（紫色虚线框覆盖） -->
                <td class="px-2 py-1.5">
                  <div v-if="mediaFrameCache[c.frameIndex]" class="relative w-12 h-10 rounded bg-black overflow-hidden">
                    <img :src="mediaFrameCache[c.frameIndex]" class="w-full h-full object-cover" />
                    <div class="absolute border border-dashed border-indigo-400"
                         :style="bboxStyleScaled(c.beforeBbox, '#818cf8')"></div>
                  </div>
                </td>
                <!-- 缩略图 B（青色实线） -->
                <td class="px-2 py-1.5">
                  <div v-if="mediaFrameCache[c.frameIndex]" class="relative w-12 h-10 rounded bg-black overflow-hidden">
                    <img :src="mediaFrameCache[c.frameIndex]" class="w-full h-full object-cover" />
                    <div class="absolute border border-emerald-400"
                         :style="bboxStyleScaled(c.afterBbox, '#34d399')"></div>
                  </div>
                </td>
                <td class="px-2 py-1.5 font-mono" :class="!c.iou || c.iou > 0.7 ? 'text-emerald-400' : c.iou > 0.4 ? 'text-amber-400' : 'text-red-400'">
                  {{ c.iou !== undefined && c.iou !== null ? (+c.iou).toFixed(2) : '—' }}
                </td>
                <td class="px-2 py-1.5 font-mono text-slate-400">({{ deltaDx(c) }}, {{ deltaDy(c) }})</td>
                <td class="px-2 py-1.5 font-mono text-slate-400">({{ deltaW(c) }}, {{ deltaH(c) }})</td>
                <td class="px-2 py-1.5">
                  <select :value="c.decision?.choice ?? 'pending'"
                          class="w-full rounded bg-slate-950 border border-slate-700 text-[10px] px-1 py-0.5 focus:border-indigo-500 focus:outline-none"
                          @change="(e: any) => { const v = e.target.value; if (v === 'A' || v === 'B') selectDraft(v); submitChoice(); }">
                    <option value="pending">待确认</option>
                    <option value="A" :disabled="true" class="text-slate-500">保留原标注 A</option>
                    <option value="A">保留 A</option>
                    <option value="B">采纳 B</option>
                  </select>
                </td>
              </tr>
            </tbody>
          </table>

          <!-- Finalize 区 -->
          <div v-if="activeCs.state !== 'confirmed'" class="p-4 border-t border-slate-800 flex items-center justify-between">
            <div class="text-[11px] text-slate-500">
              全部 {{ totalChanges }} 项：已确认 {{ decidedCount }} · 待确认 <span class="text-amber-400">{{ pendingCount }}</span>
            </div>
            <button :disabled="!allDecided"
                    class="btn-primary !py-1.5"
                    @click="doFinalize">
              {{ allDecided ? '🔒 完成全片确认' : `还剩 ${pendingCount} 项待选择` }}
            </button>
          </div>
          <div v-else class="p-4 border-t border-emerald-500/30 bg-emerald-500/5 text-center">
            <div class="text-[11px] text-emerald-400">✓ 全片确认已完成，最终版本已冻结</div>
          </div>
        </div>
      </div>

      <!-- 右栏：对比确认进度 + 统计 + 操作（原型风格） -->
      <div class="border-l border-slate-800 bg-slate-950/50 flex flex-col overflow-y-auto">

        <!-- 进度 -->
        <div class="p-3 border-b border-slate-800">
          <h3 class="text-[11px] font-medium text-slate-400 uppercase tracking-wider">对比确认进度</h3>
          <div class="mt-3 relative">
            <!-- 圆环 -->
            <div class="flex items-center gap-3">
              <div class="relative w-[80px] h-[80px] shrink-0">
                <svg class="w-full h-full -rotate-90" viewBox="0 0 80 80">
                  <circle cx="40" cy="40" r="32" fill="none" stroke="rgb(30,41,59)" stroke-width="8" />
                  <!-- 采纳 B 部分（青色） -->
                  <circle cx="40" cy="40" r="32" fill="none"
                          stroke="#10b981" stroke-width="8" stroke-linecap="round"
                          :stroke-dasharray="201"
                          :stroke-dashoffset="201 * (1 - (totalChanges ? adoptedBCount / totalChanges : 0))" />
                  <!-- 保留 A 部分（紫色）叠在上面 -->
                  <circle cx="40" cy="40" r="32" fill="none"
                          stroke="#6366f1" stroke-width="8" stroke-linecap="round"
                          :stroke-dasharray="201"
                          :stroke-dashoffset="201 * (1 - (totalChanges ? adoptedBCount / totalChanges + keptACount / totalChanges : 0))" />
                </svg>
                <div class="absolute inset-0 flex items-center justify-center text-sm font-mono font-semibold text-slate-200">{{ progressPct }}%</div>
              </div>
              <div class="flex-1 space-y-1.5">
                <div class="flex items-center justify-between text-[11px]">
                  <span class="text-slate-500">总修改项</span>
                  <span class="font-mono text-slate-200">{{ totalChanges }}</span>
                </div>
                <div class="flex items-center justify-between text-[11px]">
                  <span class="text-slate-500">已确认</span>
                  <span class="font-mono text-emerald-300">{{ decidedCount }}</span>
                </div>
                <div class="flex items-center justify-between text-[11px]">
                  <span class="text-slate-500">待确认</span>
                  <span class="font-mono text-amber-400">{{ pendingCount }}</span>
                </div>
              </div>
            </div>
            <!-- 采纳 B / 保留 A -->
            <div class="mt-3 grid grid-cols-2 gap-2">
              <div class="rounded-lg border border-emerald-500/40 bg-emerald-500/5 px-2 py-1.5 text-center">
                <div class="text-[9px] text-slate-500">采纳 B</div>
                <div class="text-base font-mono font-semibold text-emerald-300">{{ adoptedBCount }}</div>
              </div>
              <div class="rounded-lg border border-indigo-500/40 bg-indigo-500/5 px-2 py-1.5 text-center">
                <div class="text-[9px] text-slate-500">保留 A</div>
                <div class="text-base font-mono font-semibold text-indigo-300">{{ keptACount }}</div>
              </div>
            </div>
          </div>
        </div>

        <!-- 当前修改项信息 -->
        <div class="p-3 border-b border-slate-800">
          <h3 class="text-[11px] font-medium text-slate-400 uppercase tracking-wider">当前修改项信息</h3>
          <div class="mt-2 grid grid-cols-2 gap-2 text-[11px]">
            <div>
              <div class="text-slate-500">对象 ID</div>
              <div class="font-mono text-slate-200 text-sm">#{{ String(currentChange?.objectId ?? 0).padStart(3,'0') }}</div>
            </div>
            <div>
              <div class="text-slate-500">修改项</div>
              <div class="font-mono text-slate-200 text-sm">{{ currentChange ? (changes.indexOf(currentChange)+1) : 0 }}</div>
            </div>
          </div>
          <div class="mt-3 space-y-1.5 text-[11px]">
            <div class="flex items-center justify-between">
              <span class="text-slate-500">IoU (A vs B)</span>
              <span class="font-mono text-sm"
                    :class="!currentChange?.iou || currentChange.iou > 0.7 ? 'text-emerald-400' : currentChange.iou > 0.4 ? 'text-amber-400' : 'text-red-400'">
                {{ currentChange?.iou !== undefined && currentChange?.iou !== null ? (+currentChange.iou).toFixed(2) : '—' }}
              </span>
            </div>
            <div class="flex items-center justify-between">
              <span class="text-slate-500">位置变化</span>
              <span class="font-mono text-sm text-slate-300">{{ currentChange ? `${((deltaDx(currentChange)**2+deltaDy(currentChange)**2)**0.5).toFixed(1)} px` : '—' }}</span>
            </div>
            <div class="flex items-center justify-between">
              <span class="text-slate-500">尺寸变化</span>
              <span class="font-mono text-sm text-slate-300">{{ currentChange ? `+${deltaW(currentChange)} × +${deltaH(currentChange)}` : '—' }}</span>
            </div>
          </div>
        </div>

        <!-- 单选 + 备注 + 提交按钮 -->
        <div class="p-3 flex-1 flex flex-col gap-3">
          <h3 class="text-[11px] font-medium text-slate-400 uppercase tracking-wider">最终采用哪一个标注？</h3>

          <!-- A -->
          <button class="w-full flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors text-left"
                  :class="(draftChoice ?? currentChange?.decision?.choice) === 'A' ? 'border-indigo-500 bg-indigo-500/10' : 'border-slate-700 hover:border-indigo-500/60'"
                  :disabled="activeCs.state === 'confirmed'"
                  @click="selectDraft('A')">
            <span class="w-4 h-4 rounded-full border-2 border-indigo-400 flex items-center justify-center shrink-0">
              <span v-if="(draftChoice ?? currentChange?.decision?.choice) === 'A'" class="w-2 h-2 rounded-full bg-indigo-400"></span>
            </span>
            <span class="text-[11px] text-slate-200">保留原标注 <span class="text-indigo-400 font-mono">A</span></span>
          </button>

          <!-- B -->
          <button class="w-full flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors text-left"
                  :class="(draftChoice ?? currentChange?.decision?.choice) === 'B' ? 'border-emerald-500 bg-emerald-500/10' : 'border-slate-700 hover:border-emerald-500/60'"
                  :disabled="activeCs.state === 'confirmed'"
                  @click="selectDraft('B')">
            <span class="w-4 h-4 rounded-full border-2 border-emerald-400 flex items-center justify-center shrink-0">
              <span v-if="(draftChoice ?? currentChange?.decision?.choice) === 'B'" class="w-2 h-2 rounded-full bg-emerald-400"></span>
            </span>
            <span class="text-[11px] text-slate-200">采用审查标注 <span class="text-emerald-400 font-mono">B</span></span>
          </button>

          <!-- 备注 -->
          <div>
            <div class="flex items-center justify-between">
              <span class="text-[11px] text-slate-400">确认备注（可选）</span>
              <span class="text-[9px] text-slate-600">{{ note.length }}/200</span>
            </div>
            <textarea v-model="note" maxlength="200" rows="2"
                      class="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-1.5 text-[11px] text-slate-200 placeholder-slate-600 focus:border-indigo-500 focus:outline-none resize-none"
                      placeholder="输入备注，说明选择的原因..."
                      :disabled="activeCs.state === 'confirmed'"></textarea>
          </div>

          <!-- 提交本项确认 -->
          <button class="w-full btn-primary !py-2 !text-xs"
                  :disabled="activeCs.state === 'confirmed' || !draftChoice"
                  @click="submitChoice()">
            提交本项确认
          </button>

          <div class="text-[10px] text-slate-600 text-center pt-1 border-t border-slate-800">
            快捷键: <span class="text-indigo-400">A</span> = 保留原标注
            <span class="mx-1">·</span>
            <span class="text-emerald-400">B</span> = 采用审查标注
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
