<script setup lang="ts">
import AppIcon from './AppIcon.vue'

type PauseNotice = {
  objectId: number
  displayName: string
  title: string
  summary: string
  metrics: string[]
  baselineFrame?: number
  geometryReferenceFrame?: number
  geometryReferenceSource?: 'manual' | 'confirmed-normal'
  triggerSummary?: string
  maskTrigger?: boolean
  overlapPairs?: Array<{ otherObjectId: number; label: string; coverage?: number; iou?: number; referenceFrame?: number; referenceSource?: string }>
  acceptsOverlap?: boolean
  pauseStartFrame?: number
  decisionFrame?: number
  acceptsGeometry: boolean
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
  canRestartEarlier?: boolean
  missingTarget?: boolean
  canCopyMissingTarget?: boolean
  previousTargetFrame?: number | null
}>()
defineEmits<{
  (event: 'update:learn', value: boolean): void
  (event: 'select', objectId: number): void
  (event: 'return'): void
  (event: 'confirm', decision: 'normal' | 'corrected', scope: 'object' | 'frame'): void
  (event: 'retry'): void
  (event: 'restart'): void
  (event: 'restoreMissing'): void
  (event: 'locatePrevious'): void
}>()
</script>

<template>
  <section class="tracking-anomaly" role="region" aria-label="追踪异常确认" data-testid="tracking-feedback" :data-pause-frame="pausedFrame" :data-decision-frame="target?.decisionFrame">
    <div class="tracking-feedback-title">
      <strong>追踪暂停 · 第 {{ pausedFrame == null ? '—' : pausedFrame + 1 }} 帧</strong>
      <span>{{ remaining }} 个对象待确认</span>
      <button v-if="pausedFrame != null && currentFrame !== pausedFrame" class="quiet-button" :disabled="busy||disabled||pending" @click="$emit('return')">返回第 {{ pausedFrame + 1 }} 帧</button>
    </div>
    <div v-if="notices.length" class="pause-object-tabs" aria-label="选择待确认对象">
      <button v-for="item in notices" :key="item.objectId" :class="{active:target?.objectId===item.objectId,resolved:item.resolved}" :aria-label="`选择暂停对象 #${item.objectId} ${item.displayName}`" :aria-pressed="target?.objectId===item.objectId" :disabled="disabled||pending||item.resolved" @click="$emit('select',item.objectId)">#{{ item.objectId }} <AppIcon v-if="item.resolved" name="check" :size="12" /><span v-else>{{ item.displayName }}</span></button>
    </div>
    <template v-if="target">
      <p class="tracking-target"><strong>{{ target.displayName }} · {{ target.title }}</strong></p>
      <p v-if="target.triggerSummary" class="tracking-feedback-note" data-testid="tracking-trigger-evidence">{{ target.triggerSummary.split('；')[0] }}<template v-if="target.geometryReferenceFrame!=null&&!target.maskTrigger">；参考第 {{ target.geometryReferenceFrame+1 }} 帧{{ target.geometryReferenceSource==='confirmed-normal'?'人工确认正常的框':'人工框' }}。</template></p>
      <div v-if="missingTarget" class="tracking-missing-target" data-testid="tracking-missing-object">
        <span>对象 #{{ target.objectId }} 本帧缺少可编辑框。</span>
        <button v-if="canCopyMissingTarget" class="btn-secondary" data-testid="restore-missing-tracking-object" :disabled="disabled||pending" @click="$emit('restoreMissing')">复制上一帧补回缺失对象</button>
        <button v-else-if="previousTargetFrame!=null" class="btn-secondary" data-testid="locate-missing-tracking-object" :disabled="disabled||pending" @click="$emit('locatePrevious')">返回第 {{ previousTargetFrame+1 }} 帧已有框</button>
        <small v-else>历史中没有此对象可补回的框，请先重新读取工作区核对来源；新编号不能替代此对象。</small>
      </div>
      <div class="tracking-feedback-controls">
        <div v-if="!pending" class="tracking-feedback-actions">
          <button class="btn-primary" data-testid="confirm-tracking-object-normal" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('confirm','normal','object')">{{ remaining>1?'本对象无异常':'本对象无异常，继续追踪' }}</button>
          <button class="btn-secondary" data-testid="confirm-tracking-corrected" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('confirm','corrected','object')">{{ remaining>1?'已修正本对象':'已修正框，继续追踪' }}</button>
          <button class="quiet-button tracking-frame-confirm" data-testid="confirm-tracking-normal" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('confirm','normal','frame')">整帧全部无异常</button>
        </div>
        <div v-else class="tracking-feedback-actions"><button class="btn-primary" data-testid="retry-tracking-feedback" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('retry')">{{ busy?'正在保存确认…':'重试本次确认' }}</button></div>
        <button v-if="canRestartEarlier" class="btn-primary" data-testid="tracking-restart-open" :disabled="disabled||pending" @click="$emit('restart')">从本帧修正并重新追踪</button>
      </div>
      <p v-if="pausedFrame!=null&&currentFrame!==pausedFrame" class="tracking-return-note">正在浏览第 {{ currentFrame+1 }} 帧，点击对象按钮可返回暂停帧并定位；确认只作用于暂停帧。</p>
    </template>
    <div v-if="error" class="tracking-feedback-error" role="alert">{{ error }}<button v-if="!target" class="quiet-button" :disabled="busy||disabled" @click="$emit('retry')">重试本次确认</button></div>
    <details class="tracking-feedback-details" data-testid="tracking-diagnostics"><summary>检查依据与确认说明</summary>
      <p v-if="target?.overlapPairs?.length" class="tracking-feedback-note tracking-overlap-targets" data-testid="tracking-overlap-pairs">本次核对：{{ target.overlapPairs.map(pair=>pair.label).join('；') }}</p>
      <label v-if="notices.some(item=>!item.resolved&&item.canLearn)" class="tracking-learn"><input type="checkbox" :checked="learn" :disabled="disabled||pending" @change="$emit('update:learn',($event.target as HTMLInputElement).checked)" /><span>用本次正常运动确认减少同类暂停<small>仅学习确认范围内有运动依据且未经修框的对象；取消不调整运动范围</small></span></label>
      <p v-if="target?.decisionFrame!=null&&pausedFrame!=null&&target.decisionFrame>pausedFrame" class="tracking-feedback-note" data-testid="tracking-persistence-evidence">观察至第 {{ target.decisionFrame+1 }} 帧后判定暂停，返回首次风险的第 {{ pausedFrame+1 }} 帧；只确认暂停帧。</p>
      <p class="tracking-feedback-note" data-testid="tracking-geometry-acceptance">“本对象无异常”只认可此对象；“整帧全部无异常”表示已经检查全部有效框（含未报异常对象），保存整帧人工检查点。两者均更新确认范围内的尺寸参考，无需重画，不改写原标注来源。</p>
      <p v-if="target?.acceptsOverlap" class="tracking-feedback-note" data-testid="tracking-overlap-acceptance">整帧确认包含本帧所有重叠组合；单项正常不会顺带认可另一项，双方分别确认正常后才认可共同状态。新伙伴或明显恶化仍检查。</p>
      <p>核对本对象的框与目标身份，并查看前几帧是否已经偏移。修正时先在画布移动或缩放框，再确认。确认保存成功且全部待处理对象均已处理后才继续追踪；读取或保存失败时返回或重试，不会忽略其他对象。</p>
      <div v-for="item in notices" :key="item.objectId"><b>#{{ item.objectId }} {{ item.displayName }} · {{ item.resolved?'已确认':item.title }}</b><p>{{ item.summary }} {{ item.metrics.join(' · ') }}</p><p v-if="item.triggerSummary">{{ item.triggerSummary }}</p><p v-for="pair in item.overlapPairs" :key="pair.otherObjectId">{{ pair.label }}<template v-if="pair.coverage!=null"> · 较小框覆盖 {{ (pair.coverage*100).toFixed(1) }}%</template></p><p v-if="item.geometryReferenceFrame!=null">尺寸 / 形状判断参考：第 {{ item.geometryReferenceFrame+1 }} 帧（{{ item.geometryReferenceSource==='confirmed-normal'?'人工确认无异常':'人工标注' }}）</p><p v-if="item.baselineFrame!=null">最近人工标注：第 {{ item.baselineFrame+1 }} 帧</p><p v-if="item.reviewRange">建议检查范围：{{ item.reviewRange }}</p><p>{{ item.reviewNotice }} {{ item.suggestion }}</p></div>
    </details>
  </section>
</template>
