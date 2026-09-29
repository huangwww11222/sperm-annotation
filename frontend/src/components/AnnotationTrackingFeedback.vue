<script setup lang="ts">
import AppIcon from './AppIcon.vue'

type PauseNotice = {
  objectId: number
  displayName: string
  title: string
  summary: string
  metrics: string[]
  baselineFrame?: number
  reviewRange?: string
  reviewNotice?: string
  suggestion: string
  canLearn: boolean
  resolved?: boolean
}
defineProps<{
  pausedFrame: number | null
  currentFrame: number
  notices: PauseNotice[]
  target: PauseNotice | null
  remaining: number
  learn: boolean
  disabled: boolean
  busy: boolean
  pending: boolean
  error: string
}>()
defineEmits<{
  (event: 'update:learn', value: boolean): void
  (event: 'select', objectId: number): void
  (event: 'return'): void
  (event: 'confirm', decision: 'normal' | 'corrected'): void
  (event: 'retry'): void
}>()
</script>

<template>
  <section class="tracking-anomaly" role="region" aria-label="追踪异常确认" data-testid="tracking-feedback">
    <div class="tracking-feedback-title">
      <strong>追踪暂停 · 第 {{ pausedFrame == null ? '—' : pausedFrame + 1 }} 帧</strong>
      <span>{{ remaining }} 个对象待确认</span>
      <button v-if="pausedFrame != null && currentFrame !== pausedFrame" class="quiet-button" :disabled="busy" @click="$emit('return')">返回第 {{ pausedFrame + 1 }} 帧</button>
    </div>
    <div v-if="notices.length > 1" class="pause-object-tabs" aria-label="选择待确认对象">
      <button v-for="item in notices" :key="item.objectId" :class="{active:target?.objectId===item.objectId,resolved:item.resolved}" :aria-pressed="target?.objectId===item.objectId" :disabled="disabled||pending||item.resolved" @click="$emit('select',item.objectId)">#{{ item.objectId }} <AppIcon v-if="item.resolved" name="check" :size="12" /><span v-else>{{ item.displayName }}</span></button>
    </div>
    <template v-if="target">
      <p class="tracking-target"><strong>确认 #{{ target.objectId }} · {{ target.title }}</strong><span>先核对框与目标身份，并查看前几帧是否已经偏移。</span></p>
      <div class="tracking-feedback-controls">
        <label v-if="target.canLearn" class="tracking-learn"><input type="checkbox" :checked="learn" :disabled="disabled||pending" @change="$emit('update:learn',($event.target as HTMLInputElement).checked)" /><span>用本次正常确认减少同类暂停<small>仅本视频 #{{ target.objectId }}；取消勾选只确认本次</small></span></label>
        <p v-else class="tracking-learning-limit">本次只处理此提示，不扩大正常运动范围。</p>
        <div v-if="!pending" class="tracking-feedback-actions">
          <button class="btn-primary" data-testid="confirm-tracking-normal" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('confirm','normal')"><AppIcon name="check" :size="14" />{{ remaining>1?'本对象无异常':'无异常，继续追踪' }}</button>
          <button class="btn-secondary" data-testid="confirm-tracking-corrected" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('confirm','corrected')">{{ remaining>1?'已修正本对象':'已修正框，继续追踪' }}</button>
        </div>
        <div v-else class="tracking-feedback-actions"><button class="btn-primary" data-testid="retry-tracking-feedback" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('retry')">{{ busy?'正在保存确认…':'重试本次确认' }}</button></div>
      </div>
      <p class="tracking-feedback-note">修正时先在画布移动或缩放框，再确认。{{ remaining>1?'所有对象确认后才继续追踪。':'确认保存成功后才继续追踪。' }}新的持续偏离、丢失、形状突变仍会检测。</p>
      <p v-if="pausedFrame!=null&&currentFrame!==pausedFrame" class="tracking-return-note">正在查看第 {{ currentFrame+1 }} 帧，请返回暂停帧再确认。</p>
    </template>
    <div v-if="error" class="tracking-feedback-error" role="alert">{{ error }}<button v-if="!target" class="quiet-button" :disabled="busy||disabled" @click="$emit('retry')">重试本次确认</button></div>
    <details class="tracking-feedback-details"><summary>{{ notices.length }} 个异常提示 · 查看检查依据</summary><div v-for="item in notices" :key="item.objectId"><b>#{{ item.objectId }} {{ item.displayName }} · {{ item.resolved?'已确认':item.title }}</b><p>{{ item.summary }} {{ item.metrics.join(' · ') }}</p><p v-if="item.baselineFrame!=null">最近人工基准：第 {{ item.baselineFrame+1 }} 帧</p><p v-if="item.reviewRange">建议检查范围：{{ item.reviewRange }}</p><p>{{ item.reviewNotice }} {{ item.suggestion }}</p></div></details>
  </section>
</template>
