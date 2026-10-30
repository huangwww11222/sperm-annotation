<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { objectNumberText, presentPauseEvidence } from '../annotation/pauseEvidence'
import '../annotation/pause-evidence.css'

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
const props = defineProps<{
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
  compact?: boolean
  detailsOpen: boolean
  detailsTarget: HTMLElement | null
}>()
const emit = defineEmits<{
  (event: 'update:learn', value: boolean): void
  (event: 'update:detailsOpen', value: boolean): void
  (event: 'select', objectId: number): void
  (event: 'return'): void
  (event: 'confirm', decision: 'normal' | 'corrected', scope: 'object' | 'frame'): void
  (event: 'retry'): void
  (event: 'restart'): void
  (event: 'restoreMissing'): void
  (event: 'locatePrevious'): void
}>()
const details = ref<HTMLDetailsElement | null>(null), detailContent = ref<HTMLElement | null>(null)
const otherNotices = computed(() => props.notices.filter(item => item.objectId !== props.target?.objectId))
const otherEvidence = computed(() => otherNotices.value.map(notice => ({ notice, evidence: presentPauseEvidence(notice.triggerSummary, notice.metrics) })))
const evidence = computed(() => presentPauseEvidence(props.target?.triggerSummary, props.target?.metrics || []))
// Confirmed mask area can come from a different sample than the current box
// reference. Its source frame is not recorded in the notice; do not invent it.
const referenceFrame = computed(() => props.target?.maskTrigger ? undefined : props.target?.geometryReferenceFrame ?? props.target?.baselineFrame)
const referenceLabel = computed(() => props.target?.geometryReferenceFrame != null && props.target.geometryReferenceSource === 'confirmed-normal'
  ? '人工确认正常' : '人工标注')
const frameText = (frame: number | null | undefined) => frame == null ? '未记录' : `第 ${frame + 1} 帧`
function pairCaption(objectId: number, pair: NonNullable<PauseNotice['overlapPairs']>[number]) {
  const prefix = `#${objectId} ↔ #${pair.otherObjectId} `
  return objectNumberText(pair.label.startsWith(prefix) ? pair.label.slice(prefix.length).trim() : pair.label)
}
watch(() => [props.detailsOpen, props.target?.objectId], async () => {
  if (!props.detailsOpen) return
  await nextTick()
  detailContent.value?.scrollTo(0, 0)
})
async function closeDetails() {
  emit('update:detailsOpen', false)
  await nextTick()
  details.value?.querySelector('summary')?.focus({ preventScroll: true })
}
function toggleDetails(event: Event) {
  const open = (event.target as HTMLDetailsElement).open
  if (open !== props.detailsOpen) emit('update:detailsOpen', open)
}
</script>

<template>
  <section class="tracking-anomaly" :class="{compact}" :data-presentation="compact?'compact':'inspector'" role="region" aria-label="追踪异常确认" data-testid="tracking-feedback" :data-pause-frame="pausedFrame" :data-decision-frame="target?.decisionFrame">
    <div class="tracking-feedback-title">
      <strong>追踪暂停 · 第 {{ pausedFrame == null ? '—' : pausedFrame + 1 }} 帧</strong>
      <span>{{ remaining }} 个对象待确认</span>
      <button v-if="pausedFrame != null && currentFrame !== pausedFrame" class="quiet-button" :disabled="busy||disabled||pending" @click="$emit('return')">返回第 {{ pausedFrame + 1 }} 帧</button>
    </div>
    <div class="tracking-feedback-subject"><div v-if="notices.length" class="pause-object-tabs" aria-label="选择待确认对象">
      <button v-for="item in notices" :key="item.objectId" :class="{active:target?.objectId===item.objectId,resolved:item.resolved}" :aria-label="`选择暂停对象 ${item.objectId} ${item.displayName}`" :aria-pressed="target?.objectId===item.objectId" :disabled="disabled||pending||item.resolved" @click="$emit('select',item.objectId)"><span class="pause-object-number">{{ item.objectId }}</span><AppIcon v-if="item.resolved" name="check" :size="12" /><span v-else>{{ item.displayName }}</span></button>
    </div>
    <template v-if="target">
      <p class="tracking-target" :title="target.displayName+' · '+target.title"><strong>{{ target.title }}</strong></p>
      <p v-if="target.triggerSummary" class="tracking-feedback-note" data-testid="tracking-trigger-evidence">{{ objectNumberText(target.triggerSummary.split('；')[0] || '') }}<template v-if="target.geometryReferenceFrame!=null&&!target.maskTrigger">；参考第 {{ target.geometryReferenceFrame+1 }} 帧{{ target.geometryReferenceSource==='confirmed-normal'?'人工确认正常的框':'人工框' }}。</template></p>
      <div v-if="missingTarget" class="tracking-missing-target" data-testid="tracking-missing-object">
        <span>对象 {{ target.objectId }} 本帧缺少可编辑框。</span>
        <button v-if="canCopyMissingTarget" class="btn-secondary" data-testid="restore-missing-tracking-object" :disabled="disabled||pending" @click="$emit('restoreMissing')">复制上一帧补回缺失对象</button>
        <button v-else-if="previousTargetFrame!=null" class="btn-secondary" data-testid="locate-missing-tracking-object" :disabled="disabled||pending" @click="$emit('locatePrevious')">返回第 {{ previousTargetFrame+1 }} 帧已有框</button>
        <small v-else>历史中没有此对象可补回的框，请先重新读取工作区核对来源；新编号不能替代此对象。</small>
      </div>
      <p v-if="pausedFrame!=null&&currentFrame!==pausedFrame" class="tracking-return-note">正在浏览第 {{ currentFrame+1 }} 帧，点击对象按钮可返回暂停帧并定位；确认只作用于暂停帧。</p>
    </template>
    </div>
    <div v-if="error" class="tracking-feedback-error" role="alert">{{ error }}<button v-if="!target" class="quiet-button" :disabled="busy||disabled" @click="$emit('retry')">重试本次确认</button></div>
      <div class="tracking-feedback-controls">
        <div v-if="target && !pending" class="tracking-feedback-actions">
          <button class="btn-primary" data-testid="confirm-tracking-object-normal" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('confirm','normal','object')">本对象无异常</button>
          <button class="btn-secondary" data-testid="confirm-tracking-corrected" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('confirm','corrected','object')">已修正本对象</button>
          <button class="quiet-button tracking-frame-confirm" data-testid="confirm-tracking-normal" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('confirm','normal','frame')">整帧全部无异常</button>
        </div>
        <div v-else-if="target" class="tracking-feedback-actions"><button class="btn-primary" data-testid="retry-tracking-feedback" :disabled="disabled||pausedFrame==null||currentFrame!==pausedFrame" @click="$emit('retry')">{{ busy?'正在保存确认…':'重试本次确认' }}</button></div>
        <button v-if="target && canRestartEarlier" class="btn-primary" data-testid="tracking-restart-open" :disabled="disabled||pending" @click="$emit('restart')">从本帧修正并重新追踪</button>
    <details ref="details" class="tracking-feedback-details" data-testid="tracking-diagnostics" :open="detailsOpen" @toggle="toggleDetails"><summary data-testid="pause-details-toggle" aria-label="检查依据与确认说明" :aria-expanded="detailsOpen">检查依据<span class="tracking-details-label">与确认说明</span></summary>
      <Teleport :to="detailsTarget || 'body'" :disabled="!detailsTarget">
      <section v-show="detailsOpen" class="pause-details-panel" data-testid="pause-details-panel" aria-label="检查依据与确认说明" @keydown.esc.stop.prevent="closeDetails">
      <header><strong>检查依据与确认说明</strong><button class="icon-button" aria-label="收起检查依据" @click="closeDetails"><AppIcon name="close" :size="16" /></button></header>
      <div ref="detailContent" class="pause-details-content pause-evidence">
        <article v-if="target" class="pause-evidence-current" data-testid="pause-current-evidence">
          <div class="pause-evidence-object"><span class="pause-evidence-id" :aria-label="`对象 ${target.objectId}`">{{ target.objectId }}</span><strong>{{ target.displayName }}</strong><span>当前对象</span></div>
          <h4>{{ target.title }}</h4>
          <section v-for="(measurement,index) in evidence.triggerMetrics" :key="index" class="pause-evidence-metric" data-testid="pause-trigger-metric">
            <div class="pause-evidence-metric-label"><strong>{{ objectNumberText(measurement.label) }}</strong><span v-if="measurement.immediate">立即暂停</span></div>
            <dl class="pause-evidence-comparison"><div><dt>当前</dt><dd>{{ measurement.current }}</dd></div><div><dt>{{ measurement.reference.startsWith('观察') ? measurement.reference.split(' ')[0] : '比较界限' }}</dt><dd>{{ measurement.bound == null ? '未记录' : measurement.reference.split(' ')[1] }}</dd></div></dl>
            <div v-if="measurement.scale" class="pause-evidence-scale" aria-hidden="true"><i class="pause-evidence-scale-fill" :style="{width:measurement.scale.current+'%'}"></i><i class="pause-evidence-scale-bound" :style="{left:measurement.scale.bound+'%'}"></i><i class="pause-evidence-scale-current" :style="{left:measurement.scale.current+'%'}"></i></div>
          </section>
          <ul v-if="evidence.triggerNotes.length" class="pause-evidence-reasons"><li v-for="note in evidence.triggerNotes" :key="note"><AppIcon name="eye" :size="15"/><span>{{ objectNumberText(note) }}</span></li></ul>
          <p v-if="!target.triggerSummary" class="pause-evidence-fallback">{{ objectNumberText(target.summary || '当前记录缺少详细指标，请结合原图核对。') }}</p>
          <details v-if="evidence.additionalMetrics.length" class="pause-evidence-more" :key="'metrics-'+target.objectId">
            <summary>辅助指标 <span>{{ evidence.additionalMetrics.length }} 项 · 不代表暂停原因</span></summary>
            <dl class="pause-evidence-measurements"><div v-for="(measurement,index) in evidence.additionalMetrics" :key="index"><dt>{{ measurement.label }}</dt><dd>{{ measurement.value }}</dd></div></dl>
          </details>
          <section v-if="target.overlapPairs?.length" class="pause-evidence-related" data-testid="tracking-overlap-pairs">
            <h5><AppIcon name="layers" :size="15"/>相关重叠 <span>{{ target.overlapPairs.length }} 组</span></h5>
            <div v-for="pair in target.overlapPairs" :key="pair.otherObjectId" class="pause-evidence-pair" :aria-label="`对象 ${target.objectId} 与对象 ${pair.otherObjectId}`"><div><span class="pause-evidence-pair-ids"><span class="pause-evidence-id">{{ target.objectId }}</span><span aria-hidden="true">↔</span><span class="pause-evidence-id">{{ pair.otherObjectId }}</span></span><span v-if="pair.coverage!=null">覆盖 {{ (pair.coverage*100).toFixed(1) }}%</span></div><small v-if="pairCaption(target.objectId,pair)||pair.referenceFrame!=null">{{ pairCaption(target.objectId,pair) }}<template v-if="pair.referenceFrame!=null"> · 参考第 {{ pair.referenceFrame+1 }} 帧</template></small></div>
            <small class="pause-evidence-caption">覆盖率描述框的相交程度，需结合原图判断是否自然重叠。</small>
          </section>
          <section class="pause-evidence-history">
            <h5><AppIcon name="list" :size="15"/>核对哪些帧</h5>
            <ol class="pause-evidence-frames"><li><span>{{ target.maskTrigger?'掩膜样本':target.geometryReferenceFrame!=null?'尺寸参考':'人工标注' }}</span><strong>{{ frameText(referenceFrame) }}</strong><small>{{ target.maskTrigger?'已确认样本，帧号未记录':referenceFrame==null?'参考来源缺失':referenceLabel }}</small></li><li class="pause-evidence-frame-current"><span>返回检查</span><strong>{{ frameText(pausedFrame) }}</strong><small>本次确认帧</small></li><li><span>观察结束</span><strong>{{ frameText(target.decisionFrame) }}</strong><small>{{ target.decisionFrame==null?'未记录检查终点':pausedFrame!=null&&target.decisionFrame>pausedFrame?'后续帧仅用于观察':'本帧判定' }}</small></li></ol>
            <p v-if="target.decisionFrame!=null&&pausedFrame!=null&&target.decisionFrame>pausedFrame" class="pause-evidence-caption" data-testid="tracking-persistence-evidence">观察至第 {{ target.decisionFrame+1 }} 帧后判定暂停，返回首次风险的第 {{ pausedFrame+1 }} 帧；只确认暂停帧。</p>
            <p v-if="target.baselineFrame!=null&&target.baselineFrame!==referenceFrame" class="pause-evidence-caption">最近人工标注：第 {{ target.baselineFrame+1 }} 帧</p>
            <p v-if="target.maskTrigger&&target.geometryReferenceFrame!=null" class="pause-evidence-caption">框尺寸参考：第 {{ target.geometryReferenceFrame+1 }} 帧（{{ referenceLabel }}）</p>
            <p v-if="target.reviewRange" class="pause-evidence-range"><AppIcon name="eye" :size="14"/>建议检查 {{ target.reviewRange }}</p>
          </section>
          <details class="pause-evidence-more pause-evidence-review" :key="'review-'+target.objectId"><summary>核对要点</summary><p>{{ objectNumberText(target.summary) }}</p><p v-if="target.reviewNotice">{{ objectNumberText(target.reviewNotice) }}</p><p>{{ objectNumberText(target.suggestion) }}</p></details>
        </article>
        <details v-if="otherEvidence.length" class="pause-evidence-more pause-evidence-others"><summary>其他暂停对象 <span>{{ otherEvidence.length }} 个</span></summary><div v-for="{notice:item,evidence:itemEvidence} in otherEvidence" :key="item.objectId" class="pause-evidence-other">
          <strong><span class="pause-evidence-id" :aria-label="`对象 ${item.objectId}`">{{ item.objectId }}</span>{{ item.displayName }}<AppIcon v-if="item.resolved" name="check" :size="13"/></strong><span>{{ item.resolved?'已确认':item.title }}</span><small>{{ objectNumberText(item.triggerSummary || item.summary) }}</small>
          <details class="pause-evidence-more pause-evidence-other-detail" :data-evidence-object-id="item.objectId"><summary>查看依据 <span>只读</span></summary>
            <p>{{ objectNumberText(item.summary) }}</p>
            <dl v-if="itemEvidence.additionalMetrics.length" class="pause-evidence-measurements"><div v-for="(measurement,index) in itemEvidence.additionalMetrics" :key="index"><dt>{{ measurement.label }}</dt><dd>{{ measurement.value }}</dd></div></dl>
            <dl class="pause-evidence-facts"><div v-if="item.geometryReferenceFrame!=null"><dt>框尺寸参考</dt><dd>{{ frameText(item.geometryReferenceFrame) }}<small>{{ item.geometryReferenceSource==='confirmed-normal'?'人工确认正常':'人工标注' }}</small></dd></div><div v-if="item.baselineFrame!=null"><dt>最近人工标注</dt><dd>{{ frameText(item.baselineFrame) }}</dd></div><div><dt>暂停检查</dt><dd>{{ frameText(pausedFrame) }}</dd></div><div><dt>观察结束</dt><dd>{{ frameText(item.decisionFrame) }}</dd></div><div v-if="item.reviewRange"><dt>建议检查范围</dt><dd>{{ item.reviewRange }}</dd></div></dl>
            <p v-if="item.maskTrigger" class="pause-evidence-caption">掩膜按已确认正常样本比较，样本帧号未记录。</p>
            <div v-if="item.overlapPairs?.length" class="pause-evidence-other-pairs"><strong>相关重叠</strong><p v-for="pair in item.overlapPairs" :key="pair.otherObjectId">{{ objectNumberText(pair.label) }}<template v-if="pair.coverage!=null"> · 覆盖 {{ (pair.coverage*100).toFixed(1) }}%</template><template v-if="pair.referenceFrame!=null"> · 参考第 {{ pair.referenceFrame+1 }} 帧</template></p></div>
            <div class="pause-evidence-other-advice"><strong>核对要点</strong><p v-if="item.reviewNotice">{{ objectNumberText(item.reviewNotice) }}</p><p>{{ objectNumberText(item.suggestion) }}</p></div>
          </details>
        </div></details>
        <section class="pause-evidence-confirmation" data-testid="tracking-geometry-acceptance">
          <h5><AppIcon name="check" :size="15"/>选择确认范围</h5>
          <dl class="pause-evidence-scope"><div><dt>本对象无异常</dt><dd>仅当前对象<small>保留其他待处理项</small></dd></div><div><dt>整帧全部无异常</dt><dd>本帧全部有效框<small>包括未报异常的对象</small></dd></div></dl>
          <p class="pause-evidence-caption">确认范围内保存尺寸参考，无需重画；保留原标注来源。</p>
          <div v-if="target?.acceptsOverlap" class="pause-evidence-pair-policy" data-testid="tracking-overlap-acceptance"><strong>重叠组合的确认</strong><ul><li>整帧确认包含本帧所有重叠组合。</li><li>单项正常不认可另一项；双方分别正常后才认可共同状态。</li><li>新伙伴或明显恶化仍检查。</li></ul></div>
        </section>
        <label v-if="notices.some(item=>!item.resolved&&item.canLearn)" class="tracking-learn pause-evidence-learn"><input type="checkbox" :checked="learn" :disabled="disabled||pending" @change="$emit('update:learn',($event.target as HTMLInputElement).checked)"/><span>用本次正常运动确认减少同类暂停<small>仅学习确认范围内有运动依据且未经修框的对象；取消不调整运动范围</small></span></label>
        <section class="pause-evidence-workflow"><h5><AppIcon name="box" :size="15"/>有误时这样修框</h5><ol><li><span>核对</span>确认主体身份，并查看前几帧</li><li><span>修框</span>在画布移动或缩放已有框</li><li><span>确认</span>点击“已修正本对象”</li></ol><p class="pause-evidence-resume"><AppIcon name="play" :size="14"/>全部处理并保存成功后才继续追踪</p><small>读取或保存失败时返回或重试，其他对象不会被忽略。</small></section>
      </div>
      </section>
      </Teleport>
    </details>
      </div>
  </section>
</template>
