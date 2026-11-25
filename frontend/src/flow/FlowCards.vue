<script setup lang="ts">
// Second pass: one target at a time, three answers; frames that are not it can be clicked off.
import { computed, ref, watch } from 'vue'
import { flowApi } from '../api/flowApi'
import { useFlow } from '../stores/flow'
import { ANSWERS, fitGrid, type FlowAnswer } from './flowLogic'
import FlowStrip from './FlowStrip.vue'
import { useSize } from './useSize'

const flow = useFlow()
const { detail, card, cards, answers, dropped, view } = flow
const area = ref<HTMLElement | null>(null), { width, height } = useSize(area)
const tile = computed(() => cards.value[card.value] ?? null)
const off = computed(() => tile.value ? dropped.value[tile.value.key] ?? [] : [])
const offIndexes = computed(() => tile.value ? tile.value.frames.map((frame, index) => off.value.includes(frame) ? index : -1).filter(index => index >= 0) : [])
const left = computed(() => cards.value.filter(entry => !(entry.key in answers.value)).length)

const usable = computed(() => ({ w: Math.max(0, width.value - 32), h: Math.max(0, height.value - 16) }))
const loop = computed(() => Math.floor(Math.max(120, Math.min(usable.value.h, usable.value.w * .42, 460))))
const film = computed(() => fitGrid(tile.value?.n ?? 1, usable.value.w - loop.value - 20, usable.value.h - 34, 8, 210, 6))

const ready = ref(false), playingAt = ref(0)
watch(() => tile.value?.key, () => { ready.value = false; playingAt.value = tile.value?.best ?? 0 }, { immediate: true })
async function choose(value: FlowAnswer) { if (ready.value && tile.value) await flow.answer(value) }
defineExpose({ choose, previous: flow.previousCard })

watch([card, view, ready], () => {
  const d = detail.value
  if (!d || !ready.value) return
  for (const next of cards.value.slice(card.value + 1, card.value + 4)) void flowApi.strip(d.mediaId, d.progress.runId, next.key, view.value).catch(() => { /* Shown when its card opens. */ })
}, { immediate: true })
</script>

<template>
  <div ref="area" class="flow-main">
    <div v-if="detail && tile" :key="tile.key" class="flow-card flow-in" :style="{ '--loop': `${loop}px`, '--cols': film.cols, '--tile': `${film.size}px` }" data-testid="flow-card">
      <div class="flow-tile flow-loop"><FlowStrip :media-id="detail.mediaId" :run="detail.progress.runId" :tile="tile" :view="view" playing :skip="offIndexes" @ready="ready = $event" @frame="playingAt = $event" /></div>
      <div v-if="tile.n > 1" class="flow-side">
        <div class="flow-film">
          <button v-for="(frame, index) in tile.frames" :key="frame" class="flow-tile" :class="{ off: off.includes(frame), now: playingAt === index }" :aria-pressed="off.includes(frame)" :aria-label="`第 ${index + 1} 帧`" data-testid="flow-frame" @click="flow.dropFrame(frame)">
            <FlowStrip :media-id="detail.mediaId" :run="detail.progress.runId" :tile="tile" :view="view" :cell="index" />
          </button>
        </div>
        <p class="flow-note">哪一帧不是它，点掉</p>
      </div>
    </div>
    <p v-else class="flow-note">没有要判的目标</p>
  </div>
  <footer v-if="detail" class="flow-foot">
    <div class="flow-bar"><i :style="{ width: `${cards.length ? Math.round((cards.length - left) / cards.length * 100) : 100}%` }" /></div>
    <div v-if="tile" class="flow-answers">
      <button v-for="(option, index) in ANSWERS" :key="option.key" class="flow-answer" :class="[`tone-${option.tone}`, { on: answers[tile.key] === option.key }]" :disabled="!ready" :data-testid="`flow-answer-${option.key}`" @click="choose(option.key)"><i aria-hidden="true" />{{ option.label }}<kbd>{{ index + 1 }}</kbd></button>
    </div>
    <div class="flow-row">
      <button class="flow-quiet" :disabled="card === 0" @click="flow.previousCard">上一个 <kbd>←</kbd></button>
      <span class="flow-count">{{ left ? `还剩 ${left}` : '都判完了' }}</span>
      <button v-if="!left" class="flow-quiet" data-testid="flow-cards-finish" @click="flow.goTo('done')">看结果</button>
    </div>
  </footer>
</template>
