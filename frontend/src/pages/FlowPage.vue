<script setup lang="ts">
// Microfluidic mode (decision 0008): every target of a short clip is judged once, not frame by frame.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import WorkbenchShell from '../components/WorkbenchShell.vue'
import FlowCards from '../flow/FlowCards.vue'
import FlowCompare from '../flow/FlowCompare.vue'
import FlowDone from '../flow/FlowDone.vue'
import FlowExport from '../flow/FlowExport.vue'
import FlowLibrary from '../flow/FlowLibrary.vue'
import FlowMove from '../flow/FlowMove.vue'
import FlowScan from '../flow/FlowScan.vue'
import FlowWall from '../flow/FlowWall.vue'
import { ANSWERS, donePart, nextTodo, stateText, type FlowItem, type FlowStep } from '../flow/flowLogic'
import { helpOpen } from '../help/guide'
import { addLeaveGuard } from '../router'
import { useFlow } from '../stores/flow'
import { importPlan } from '../stores/importQueue'
import { dismissToast, showToast, toast } from '../stores/workspace/state'
import { confirmRequest } from '../utils/confirmDialog'
import '../flow/flow.css'

const flow = useFlow()
const { items, current, detail, loading, loadError, step, mode, view, playing, unsaved, sending, saveError } = flow
const drawer = ref(true), moving = ref(false), exporting = ref(false)

const ask = computed(() => mode.value === 'recheck' && step.value === 'done' ? ['对比两次', detail.value?.differences ? '两次不一致的，定一个' : ''] : ({
  scan: ['点出像精子的头', '这一帧先不给候选，用来量漏标'],
  wall: ['点出像精子的', '白头、透明头也算，拿不准也点上'],
  cards: ['是精子吗？', '看得到尾巴才算；看不清有没有尾巴，选看不清'],
  done: ['这一段判完了', ''],
} as Record<FlowStep, string[]>)[step.value])

/** Steps of this video, in order; a finished one can be opened again. */
const steps = computed(() => {
  const p = detail.value?.progress
  if (!p) return []
  const recheck = mode.value === 'recheck' ? detail.value?.recheck : null, shown = !!recheck && recheck.state !== 'open'
  // With a re-check the first round is fixed; once compared, so is the second.
  const fixed = shown || (mode.value === 'judge' && !!(current.value?.recheck ?? detail.value?.recheck))
  const out: { key: FlowStep; name: string; done: boolean; open: boolean }[] = []
  if (p.scanNeeded) out.push({ key: 'scan', name: '查漏', done: p.scanDone, open: !fixed })
  const wallOpen = !p.scanNeeded || p.scanDone
  out.push({ key: 'wall', name: '挑', done: p.pagesDone >= p.pages, open: wallOpen && !fixed })
  out.push({ key: 'cards', name: '判', done: p.state === 'done', open: wallOpen && p.second !== null && !fixed })
  if (recheck) out.push({ key: 'done', name: '对比', done: recheck.state === 'done', open: p.state === 'done' })
  return out
})
const jump = (key: FlowStep) => { if (key !== step.value) flow.goTo(key) }

const wall = ref<InstanceType<typeof FlowWall> | null>(null), cards = ref<InstanceType<typeof FlowCards> | null>(null), scan = ref<InstanceType<typeof FlowScan> | null>(null), compare = ref<InstanceType<typeof FlowCompare> | null>(null)
function keydown(event: KeyboardEvent) {
  if (event.ctrlKey || event.metaKey || event.altKey || helpOpen.value || moving.value || exporting.value || importPlan.value || confirmRequest.value) return
  const target = event.target as HTMLElement
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
  const key = event.key.toLowerCase(), act = (run: () => unknown) => { event.preventDefault(); if (!event.repeat) run() }
  const onButton = target.tagName === 'BUTTON' || target.getAttribute('role') === 'button'
  if (key === 'm') return act(() => { drawer.value = !drawer.value })
  if (key === 'enter' && !current.value && todo.value && !onButton) return act(() => flow.openNext())
  if (!detail.value || loading.value) return
  if (key === 'e' && step.value !== 'done') return act(() => { view.value = view.value === 'e' ? 'r' : 'e' })
  if (key === 'p' && step.value === 'wall') return act(() => { playing.value = !playing.value })
  // Enter on something reached with Tab acts on that thing only.
  if (step.value === 'wall') {
    if ((key === 'enter' && !onButton) || key === 'arrowright') return act(() => wall.value?.next())
    if (key === 'arrowleft' || key === 'backspace') return act(() => wall.value?.previous())
  } else if (step.value === 'cards') {
    const answer = ANSWERS[Number(key) - 1]
    if (answer) return act(() => cards.value?.choose(answer.key))
    if (key === 'arrowleft' || key === 'backspace') return act(() => cards.value?.previous())
  } else if (step.value === 'scan') {
    if (key === 'enter' && !onButton) return act(() => scan.value?.next())
    if (key === 'arrowleft' || key === 'backspace') return act(() => scan.value?.previous())
  } else if (mode.value === 'recheck') {
    const answer = ANSWERS[Number(key) - 1]
    if (answer) return act(() => compare.value?.choose(answer.key))
    if (key === 'arrowleft' || key === 'backspace') return act(() => compare.value?.previous())
    if (key === 'enter' && !onButton) return act(() => compare.value?.next())
  } else if (key === 'enter' && !onButton) return act(() => flow.openNext())
}

async function select(item: FlowItem) {
  ;(document.activeElement as HTMLElement | null)?.blur()
  if (item.mediaId === current.value?.mediaId && detail.value) return
  if (!(await flow.open(item.mediaId))) showToast('上一段还有判定没存上，先重试保存', 'warning')
}
// A video opens by itself once its targets are found.
watch(() => [current.value?.mediaId, current.value?.state, current.value?.progress?.runId], () => {
  const item = current.value, ready = item && !['new', 'finding', 'failed'].includes(item.state)
  if (mode.value === 'judge' && ready && !loading.value && detail.value?.progress.runId !== item.progress?.runId) void flow.reload()
})

const guard = addLeaveGuard(async () => { const saved = await flow.flush(); if (!saved) showToast('还有判定没存上，先重试保存', 'warning'); return saved })
const beforeUnload = (event: BeforeUnloadEvent) => { if (unsaved.value) { event.preventDefault(); event.returnValue = '' } }
let stops: (() => void)[] = []
onMounted(() => {
  stops = [flow.startPolling(), flow.startClock()]
  window.addEventListener('keydown', keydown)
  window.addEventListener('beforeunload', beforeUnload)
})
onUnmounted(() => {
  stops.forEach(stop => stop()); guard()
  window.removeEventListener('keydown', keydown)
  window.removeEventListener('beforeunload', beforeUnload)
})
const tools = computed(() => !!detail.value && step.value !== 'done')
const todo = computed(() => nextTodo(items.value, null))
const idle = computed(() => {
  if (!flow.listReady.value) return ''
  if (todo.value) return `还有 ${items.value.filter(item => ['scan', 'wall', 'cards'].includes(item.state)).length} 段要判`
  return flow.finding.value ? '正在找目标，找好就能判' : items.value.length ? '都判完了' : '导入微流控视频开始'
})
/** A mouse click leaves no focus behind: Enter then always means the one big button, never the tile clicked last. */
const letGo = (event: MouseEvent) => { if (event.detail > 0) (document.activeElement as HTMLElement | null)?.blur() }
</script>

<template>
  <WorkbenchShell v-model:drawer-open="drawer" name="flow" drawer-label="微流控视频" :drawer-count="items.length" :drawer-busy="flow.finding.value" no-inspector>
    <template #context>
      <strong v-if="current" :title="current.videoName">{{ current.videoName }}</strong><strong v-else>微流控</strong>
      <span v-if="detail" class="shell-meta">{{ current?.frameCount }} 帧 · {{ detail.progress.targets }} 个目标</span>
      <span v-if="unsaved && !saveError" class="save-indicator">保存中…</span>
      <span v-else-if="detail && !saveError" class="save-indicator">已保存</span>
      <button class="btn-primary flow-export-open" data-testid="flow-export-open" @click="exporting = true"><AppIcon name="upload" :size="15" />导出数据集</button>
    </template>
    <template #drawer><FlowLibrary @open="select" @move="moving = true" /></template>
    <template #canvas>
      <div class="flow-stage" data-testid="flow-stage" @click.capture="letGo">
        <div v-if="saveError" class="flow-save-error" role="alert"><AppIcon name="alert" :size="16" />{{ unsaved }} 项判定没存上：{{ saveError }}<button class="flow-quiet" :disabled="sending" @click="flow.flush">重试</button></div>
        <template v-if="detail">
          <header class="flow-head">
            <ol class="flow-steps"><li v-if="mode === 'recheck'"><b class="flow-badge">复查</b></li><li v-for="(entry, index) in steps" :key="entry.key"><button :class="{ on: entry.key === step, done: entry.done }" :disabled="!entry.open" :aria-current="entry.key === step ? 'step' : undefined" :data-testid="`flow-step-${entry.key}`" @click="jump(entry.key)"><i>{{ entry.done ? '✓' : index + 1 }}</i>{{ entry.name }}</button></li></ol>
            <div class="flow-ask"><h1>{{ ask?.[0] }}</h1><p>{{ ask?.[1] }}</p></div>
            <div class="flow-tools">
              <template v-if="mode === 'recheck'"><button class="flow-quiet" title="先退出，以后接着复查" data-testid="flow-recheck-leave" @click="flow.leaveRecheck">退出复查</button><button v-if="detail.recheck?.state !== 'done'" class="flow-quiet" @click="flow.cancelRecheck">作废</button></template>
              <template v-if="tools || mode === 'recheck'">
                <button class="flow-tool" :class="{ on: view === 'e' }" :aria-pressed="view === 'e'" title="增强对比 (E)" aria-label="增强对比" @click="view = view === 'e' ? 'r' : 'e'"><AppIcon name="contrast" :size="17" /></button>
                <button v-if="step === 'wall'" class="flow-tool" :class="{ on: playing }" :aria-pressed="playing" :title="playing ? '停住小图 (P)' : '播放小图 (P)'" :aria-label="playing ? '停住小图' : '播放小图'" @click="playing = !playing"><AppIcon :name="playing ? 'pause' : 'play'" :size="17" /></button>
              </template>
            </div>
          </header>
          <FlowScan v-if="step === 'scan'" ref="scan" />
          <FlowWall v-else-if="step === 'wall'" ref="wall" />
          <FlowCards v-else-if="step === 'cards'" ref="cards" />
          <FlowCompare v-else-if="mode === 'recheck'" ref="compare" />
          <FlowDone v-else @export="exporting = true" />
        </template>
        <div v-else class="flow-empty">
          <template v-if="!current"><AppIcon name="film" :size="36" /><h3>{{ idle }}</h3><button v-if="todo" class="flow-primary" data-testid="flow-start" @click="flow.openNext">开始判 <kbd>Enter</kbd></button></template>
          <template v-else-if="current.state === 'finding'"><h3>{{ stateText(current).text }}</h3><div class="flow-bar"><i :style="{ width: `${Math.round(donePart(current) * 100)}%` }" /></div><p>可以先判别的视频</p></template>
          <template v-else-if="current.state === 'failed' || current.state === 'new'"><h3>{{ stateText(current).text }}</h3><p v-if="current.task?.message && current.state === 'failed'">{{ current.task.message }}</p><button class="flow-primary" data-testid="flow-find" @click="flow.find(current)">{{ current.state === 'failed' ? '重试' : '找目标' }}</button></template>
          <template v-else-if="loadError"><h3>没打开</h3><p>{{ loadError }}</p><button class="flow-primary" @click="flow.reload()">重试</button></template>
          <p v-else>正在打开…</p>
        </div>
        <Transition name="toast"><div v-if="toast" :key="toast.id" class="status-toast" :class="`toast-${toast.tone}`" role="status" data-testid="status-toast"><span class="notice-icon"><AppIcon :name="toast.tone === 'success' ? 'check' : toast.tone === 'info' ? 'info' : 'alert'" :size="14" /></span><span>{{ toast.message }}</span><button class="notice-close" aria-label="关闭提示" @click="dismissToast"><AppIcon name="close" :size="13" /></button></div></Transition>
      </div>
    </template>
  </WorkbenchShell>
  <FlowMove v-if="moving" @close="moving = false" />
  <FlowExport v-if="exporting" @close="exporting = false" />
</template>
