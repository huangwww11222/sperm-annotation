<script setup lang="ts">
// End of a re-check: first a way back, then the targets the two rounds disagree on, one at a time.
import { computed, ref, watch } from 'vue'
import { useFlow } from '../stores/flow'
import { ANSWERS, fitGrid, nextDifference, toggleDropped, type FlowAnswer } from './flowLogic'
import FlowStrip from './FlowStrip.vue'
import { useSize } from './useSize'

const flow = useFlow()
const { detail, view } = flow
const area = ref<HTMLElement | null>(null), { width, height } = useSize(area)
const list = computed(() => detail.value?.differences ?? null)
const left = computed(() => list.value?.filter(entry => !entry.settled).length ?? 0)
const at = ref(0), reviewing = ref(false)
watch(() => detail.value?.recheck?.id + String(!!list.value), () => { reviewing.value = false; at.value = list.value ? Math.max(0, nextDifference(list.value, -1)) : 0 }, { immediate: true })
const entry = computed(() => list.value?.[at.value] ?? null)
const finished = computed(() => !!list.value && !left.value && !reviewing.value)

const off = ref<number[]>([]), ready = ref(false), playingAt = ref(0)
watch(() => entry.value?.key, () => {
  const e = entry.value
  ready.value = false; playingAt.value = e?.best ?? 0
  off.value = e ? [...(e.settled ?? (e.second.answer === 'yes' ? e.second : e.first)).dropped] : []
}, { immediate: true })
const offIndexes = computed(() => entry.value ? entry.value.frames.map((frame, index) => off.value.includes(frame) ? index : -1).filter(index => index >= 0) : [])
const loop = computed(() => Math.floor(Math.max(120, Math.min(height.value - 16, (width.value - 32) * .42, 460))))
const film = computed(() => fitGrid(entry.value?.n ?? 1, width.value - 32 - loop.value - 20, height.value - 34, 8, 210, 6))
function choose(value: FlowAnswer) {
  const e = entry.value
  if (!e || !ready.value) return
  flow.settle(e.key, value, off.value)
  const next = nextDifference(list.value!, at.value)
  if (next >= 0) at.value = next
  else if (reviewing.value && at.value + 1 < list.value!.length) at.value++
  else reviewing.value = false
}
function previous() { if (entry.value && at.value > 0) { if (finished.value) reviewing.value = true; at.value-- } }
const busy = ref(false)
async function next() {
  if (finished.value) { await flow.leaveRecheck(); return }
  if (list.value || busy.value) return
  busy.value = true
  try { await flow.compare() } finally { busy.value = false }
}
defineExpose({ choose, previous, next })
const stats = computed(() => detail.value?.recheck?.stats)
</script>

<template>
  <div ref="area" class="flow-main">
    <div v-if="!list" class="flow-empty"><h3>复查判完了</h3><p>看了不一致的，复查的答案就不能再改</p></div>
    <div v-else-if="finished" class="flow-result flow-in" data-testid="flow-recheck-done">
      <div class="flow-hero"><span>复查完成</span><b>{{ list.length ? `${list.length} 个不一致，都定了` : '两次完全一致' }}</b><span v-if="stats">共 {{ stats.sperm }} 个精子 · 两次都判是 {{ stats.bothYes }} 个 · 第一次漏 {{ stats.firstMissed }} 个 · 第二次漏 {{ stats.secondMissed }} 个</span></div>
      <p v-if="detail?.recheck?.samePerson" class="flow-warn">两次有同一个人，漏标会被低估。</p>
    </div>
    <div v-else-if="entry && detail" :key="entry.key" class="flow-card flow-in" :style="{ '--loop': `${loop}px`, '--cols': film.cols, '--tile': `${film.size}px` }" data-testid="flow-difference">
      <div class="flow-tile flow-loop"><FlowStrip :media-id="detail.mediaId" :run="detail.progress.runId" :tile="entry" :view="view" playing :skip="offIndexes" @ready="ready = $event" @frame="playingAt = $event" /></div>
      <div v-if="entry.n > 1" class="flow-side">
        <div class="flow-film">
          <button v-for="(frame, index) in entry.frames" :key="frame" class="flow-tile" :class="{ off: off.includes(frame), now: playingAt === index }" :aria-pressed="off.includes(frame)" :aria-label="`第 ${index + 1} 帧`" @click="off = toggleDropped(entry.frames, off, frame)"><FlowStrip :media-id="detail.mediaId" :run="detail.progress.runId" :tile="entry" :view="view" :cell="index" /></button>
        </div>
        <p class="flow-note">{{ entry.first.answer === entry.second.answer ? '两次点掉的帧不同，' : '' }}哪一帧不是它，点掉</p>
      </div>
    </div>
  </div>
  <footer class="flow-foot">
    <template v-if="list && !finished && entry">
      <div class="flow-bar"><i :style="{ width: `${Math.round((list.length - left) / list.length * 100)}%` }" /></div>
      <div class="flow-answers">
        <button v-for="(option, index) in ANSWERS" :key="option.key" class="flow-answer" :class="[`tone-${option.tone}`, { on: entry.settled?.answer === option.key }]" :disabled="!ready" :data-testid="`flow-settle-${option.key}`" @click="choose(option.key)"><i aria-hidden="true" />{{ option.label }}<em v-if="entry.first.answer === option.key">第一次</em><em v-if="entry.second.answer === option.key">第二次</em><kbd>{{ index + 1 }}</kbd></button>
      </div>
      <div class="flow-row"><button class="flow-quiet" :disabled="at === 0" @click="previous">上一个 <kbd>←</kbd></button><span class="flow-count">还剩 {{ left }}</span></div>
    </template>
    <div v-else class="flow-row">
      <button v-if="!list" class="flow-quiet" @click="flow.goTo('cards')">回去改</button>
      <button class="flow-primary" data-testid="flow-compare" :disabled="busy" @click="next">{{ list ? '返回结果' : '看不一致的' }} <kbd>Enter</kbd></button>
    </div>
  </footer>
</template>
