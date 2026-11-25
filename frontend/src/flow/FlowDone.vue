<script setup lang="ts">
// One judged video: what came out, and what to do next.
import { computed, onMounted, ref } from 'vue'
import { flowApi } from '../api/flowApi'
import { useFlow } from '../stores/flow'
import { missLines, nextTodo, type FlowMisses } from './flowLogic'

const emit = defineEmits<{ export: [] }>()
const flow = useFlow()
const { detail, items, current, currentId, answers, cards, dropped } = flow
const recheck = computed(() => current.value?.recheck ?? detail.value?.recheck ?? null)
/** After a finished re-check the settled answers count, and only the server has them all. */
const settled = computed(() => recheck.value?.state === 'done' ? current.value?.progress ?? null : null)
const count = (value: 'yes' | 'unsure') => settled.value ? settled.value[value] : cards.value.filter(tile => answers.value[tile.key] === value).length
const fixed = computed(() => cards.value.filter(tile => answers.value[tile.key] === 'yes' && dropped.value[tile.key]?.length).length)
const next = computed(() => nextTodo(items.value, currentId.value))
const misses = ref<FlowMisses | null>(null)
onMounted(async () => { try { misses.value = await flowApi.misses() } catch { /* Only a summary; the export dialog shows it again. */ } })
const lines = computed(() => missLines(misses.value))
</script>

<template>
  <div class="flow-main flow-main-top">
    <div v-if="detail" class="flow-result flow-in" data-testid="flow-done">
      <div class="flow-hero"><span>{{ detail.progress.targets }} 个目标判完</span><b>{{ count('yes') }} 个精子</b><span>不是 {{ detail.progress.targets - count('yes') - count('unsure') }} 个<template v-if="count('unsure')"> · 看不清 {{ count('unsure') }} 个</template><template v-if="fixed && !settled"> · 点掉过错帧的 {{ fixed }} 个</template></span></div>
      <p v-if="detail.progress.unsteady" class="flow-warn">这段有些目标没连起来，同一个精子可能出现多次。</p>
      <p v-if="recheck?.state === 'done' && recheck.stats" class="flow-note">复查过：两次都判是 {{ recheck.stats.bothYes }} 个，第一次漏 {{ recheck.stats.firstMissed }} 个，第二次漏 {{ recheck.stats.secondMissed }} 个 · <button class="flow-link" @click="flow.openRecheck">看不一致的</button> · <button class="flow-link" @click="flow.cancelRecheck">作废复查</button></p>
      <div v-if="lines.length" class="flow-panel"><h2>漏标估计（全部视频）</h2><p v-for="line in lines" :key="line">{{ line }}</p></div>
    </div>
  </div>
  <footer class="flow-foot">
    <div class="flow-row">
      <button v-if="next" class="flow-primary" data-testid="flow-next-video" @click="flow.openNext">下一段 <kbd>Enter</kbd></button>
      <button v-if="recheck?.state !== 'done'" class="flow-quiet" data-testid="flow-recheck" title="不看原来的答案再判一遍，判完对比。可做可不做" @click="flow.openRecheck">{{ recheck ? '继续复查' : '复查（可选）' }}</button>
      <button :class="next ? 'flow-quiet' : 'flow-primary'" @click="emit('export')">导出数据集</button>
    </div>
  </footer>
</template>
