<script setup lang="ts">
import { computed } from 'vue'
import AppIcon from './AppIcon.vue'
import type { TrackingProgressView } from '../annotation/trackingProgress'

// Stepped tracking progress. Each segment is one real backend stage and fills
// only from frames that stage reported; there is no blended overall percent.
// full: AI card · compact: header / focus bar · mini: media list card.
const props = withDefaults(defineProps<{ view: TrackingProgressView | null; finished?: boolean; variant?: 'full' | 'compact' | 'mini' }>(), { finished: false, variant: 'full' })

const steps = computed(() => props.view?.steps ?? [
  { key: 'forward', label: '正向追踪', state: 'done', fraction: 1 },
  { key: 'backward', label: '反向核验', state: 'done', fraction: 1 },
  { key: 'combine', label: '综合标注', state: 'done', fraction: 1 },
] as TrackingProgressView['steps'])
const state = computed(() => props.view ? props.view.mode : 'done')
const title = computed(() => props.view ? props.view.title : '追踪完成')
const detail = computed(() => props.view ? props.view.detail : '结果已载入，可检查和微调')
const percent = (fraction: number | null) => fraction == null ? undefined : Math.round(fraction * 100)
</script>

<template>
  <div v-if="view || finished" class="tracking-progress" :class="[`is-${variant}`, `mode-${state}`]" :title="variant === 'compact' ? `${title} · ${detail}` : undefined" data-testid="tracking-progress" :data-mode="state">
    <!-- Only the stage name is announced, so screen readers are not flooded on every poll. -->
    <span class="tp-live" role="status" aria-live="polite">{{ title }}</span>
    <div v-if="variant === 'full'" class="tp-head">
      <span class="tp-icon" aria-hidden="true"><AppIcon :name="state === 'done' ? 'check' : 'spark'" :size="14" /></span>
      <strong>{{ title }}</strong>
      <span v-if="view?.elapsed" class="tp-elapsed" title="已用时间">{{ view.elapsed }}</span>
    </div>
    <div class="tp-track" role="group" :aria-label="`追踪进度：${title}`">
      <div v-for="step in steps" :key="step.key" class="tp-seg" :class="[`is-${step.state}`, { indeterminate: step.state === 'active' && step.fraction == null }]"
        role="progressbar" :aria-label="step.label" aria-valuemin="0" aria-valuemax="100" :aria-valuenow="percent(step.fraction)" :aria-valuetext="step.state === 'active' ? detail : step.state === 'done' ? '已完成' : '未开始'" :title="`${step.label}：${step.state === 'done' ? '已完成' : step.state === 'active' ? detail : '未开始'}`">
        <i :style="{ transform: `scaleX(${step.fraction ?? 0})` }" />
      </div>
    </div>
    <ol v-if="variant === 'full'" class="tp-steps">
      <li v-for="step in steps" :key="step.key" :class="`is-${step.state}`"><AppIcon v-if="step.state === 'done'" name="check" :size="11" /><span>{{ step.label }}</span></li>
    </ol>
    <p v-if="variant === 'full'" class="tp-detail">{{ detail }}</p>
    <span v-if="variant === 'compact'" class="tp-compact">{{ view ? view.compact : '追踪完成' }}</span>
  </div>
</template>

<style scoped>
.tracking-progress{--tp-fill:var(--accent);--tp-fill-2:var(--accent-hover);min-width:0}
.tp-live{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.tp-head{display:flex;align-items:center;gap:7px;font-size:13px;margin-bottom:9px}
.tp-head strong{font-weight:650;letter-spacing:.02em}
.tp-icon{display:grid;place-items:center;width:22px;height:22px;border-radius:50%;background:var(--accent-soft);color:var(--accent);flex-shrink:0}
.mode-running .tp-icon,.mode-preparing .tp-icon{animation:tp-breathe 1.6s var(--ease-standard) infinite}
.tp-elapsed{margin-left:auto;font-family:var(--font-mono);font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.tp-track{display:flex;gap:3px;min-width:0}
.tp-seg{position:relative;flex:1;height:8px;border-radius:999px;background:var(--line);overflow:hidden}
/* scaleX keeps every poll update on the compositor; 0.7 s matches the poll interval so the bar glides instead of stepping. */
.tp-seg>i{position:absolute;inset:0;transform-origin:left center;border-radius:inherit;background:linear-gradient(90deg,var(--tp-fill),var(--tp-fill-2));transition:transform .7s linear,background-color var(--dur-slow)}
.tp-seg.is-done>i{transition-duration:var(--dur-slow);transition-timing-function:var(--ease-out)}
/* Moving stripes over the active stage say "still working" even when a slow window holds the frame count. */
.tp-seg.is-active>i::after{content:'';position:absolute;inset:0;background:repeating-linear-gradient(-45deg,#ffffff38 0 6px,transparent 6px 12px);background-size:17px 17px;animation:tp-stripes .9s linear infinite}
.tp-seg.is-active{box-shadow:0 0 0 2px var(--accent-soft)}
/* No frame count yet (reading frames / loading the model): a light sweeps across instead of a guessed fill. */
.tp-seg.indeterminate::before{content:'';position:absolute;top:0;bottom:0;width:40%;border-radius:inherit;background:linear-gradient(90deg,transparent,var(--tp-fill),transparent);animation:tp-sweep 1.3s var(--ease-standard) infinite}
.mode-queued .tp-seg{background:transparent;box-shadow:inset 0 0 0 1px var(--control-line);animation:soft-pulse 2s ease-in-out infinite}
.mode-finishing,.mode-done{--tp-fill:var(--success);--tp-fill-2:var(--success)}
.mode-done .tp-icon{background:var(--success-soft);color:var(--success);animation:tp-pop .45s var(--ease-out)}
.mode-done .tp-seg>i{animation:tp-flash .9s var(--ease-out)}
.tp-steps{display:flex;gap:3px;margin:7px 0 0;padding:0;list-style:none}
.tp-steps li{flex:1;display:flex;align-items:center;gap:3px;min-width:0;font-size:11px;color:var(--muted);white-space:nowrap;transition:color var(--dur-base)}
.tp-steps li span{overflow:hidden;text-overflow:ellipsis}
.tp-steps li.is-active{color:var(--accent);font-weight:650}
.tp-steps li.is-done{color:var(--success)}
.tp-steps li svg{animation:tp-pop .35s var(--ease-out)}
.tp-detail{margin:8px 0 0;font-size:12px;line-height:1.6;color:var(--text);font-variant-numeric:tabular-nums}
/* Header / focus bar: a 72 px three-segment meter plus the live frame count. */
.is-compact{flex-shrink:0;display:inline-flex;align-items:center;gap:7px;height:24px;padding:0 9px 0 7px;border-radius:12px;background:var(--accent-soft);color:var(--accent);font-size:11px;font-weight:600;white-space:nowrap;animation:rise-in var(--dur-base) var(--ease-out)}
.is-compact .tp-track{width:72px;flex-shrink:0;gap:2px}
.is-compact .tp-seg{height:5px;background:var(--surface)}
.is-compact .tp-seg.is-active{box-shadow:none}
.is-compact.mode-done{background:var(--success-soft);color:var(--success)}
.tp-compact{font-variant-numeric:tabular-nums}
/* Media card: a hairline under the file name, so background runs are visible in the list. */
.is-mini{margin-top:7px}
.is-mini .tp-track{gap:2px}
.is-mini .tp-seg{height:3px}
.is-mini .tp-seg.is-active{box-shadow:none}
@keyframes tp-stripes{to{background-position:17px 0}}
@keyframes tp-sweep{from{left:-40%}to{left:100%}}
@keyframes tp-breathe{50%{box-shadow:0 0 0 5px var(--accent-soft)}}
@keyframes tp-pop{0%{transform:scale(.4);opacity:0}70%{transform:scale(1.15)}100%{transform:scale(1)}}
@keyframes tp-flash{0%{filter:brightness(1.6)}100%{filter:none}}
@media(max-width:1100px){.is-compact{padding:0 8px}.is-compact .tp-track{width:44px}.is-compact .tp-compact{display:none}}
@media(prefers-reduced-motion:reduce){.tp-seg.is-active>i::after,.tp-seg.indeterminate::before{display:none}.tp-seg.indeterminate{background:var(--accent-soft)}}
</style>
