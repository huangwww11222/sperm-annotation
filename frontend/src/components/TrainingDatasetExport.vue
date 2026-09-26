<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useAuth } from '../stores/auth'
import { trainingExportApi as api, type ExportJob, type ExportSettings, type ExportSummary } from '../api/trainingExportApi'

const props = defineProps<{ finalVersionId: string; mediaName: string; disabled?: boolean }>()
const emit = defineEmits<{ open: [value: boolean]; busy: [value: boolean] }>()
const opened = ref(false), busy = ref(false), error = ref('')
const summary = ref<ExportSummary | null>(null), job = ref<ExportJob | null>(null)
const format = ref<ExportSettings['format']>('yolo'), splitRatio = ref(.8)
const dialog = ref<HTMLElement | null>(null)
const storageKey = computed(() => `training-export:${useAuth().user.value?.id}:${props.finalVersionId}`)
const working = computed(() => job.value?.state === 'queued' || job.value?.state === 'running')
const trainCount = computed(() => summary.value ? Math.max(1, Math.min(summary.value.frameCount - 1, Math.floor(summary.value.frameCount * splitRatio.value))) : 0)
const intent = ref<{ settings: ExportSettings; key: string; jobId?: string } | null>(null)
type RetryPhase = 'preview' | 'create' | 'status' | 'download'
const retryPhase = ref<RetryPhase>('preview')
let timer: ReturnType<typeof setTimeout> | null = null
let alive = true, automaticDownload = false, polling = false
let priorFocus: HTMLElement | null = null

watch(busy, value => emit('busy', value))
watch(opened, async value => {
  emit('open', value)
  if (value) {
    priorFocus = document.activeElement as HTMLElement
    await nextTick()
    dialog.value?.querySelector<HTMLElement>('button')?.focus()
  } else priorFocus?.focus()
})
function stopPolling() { if (timer) clearTimeout(timer); timer = null }
function persist() {
  try { if (intent.value) localStorage.setItem(storageKey.value, JSON.stringify(intent.value)) }
  catch (e) { console.warn('[dataset.recovery_storage_unavailable]', e) }
}
function fail(e: unknown, phase: RetryPhase) {
  const f = e as { message?: string; requestId?: string }
  error.value = `${f.message || '连接中断，请重试'}${f.requestId ? `（记录号 ${f.requestId}）` : ''}`
  retryPhase.value = phase
  console.error('[dataset.operation_failed]', { phase, finalVersionId: props.finalVersionId, exportId: job.value?.exportId, error: e })
}
async function open() {
  if (props.disabled || busy.value) return
  opened.value = true; error.value = ''; busy.value = true
  try {
    summary.value = await api.preview([props.finalVersionId])
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey.value) || 'null')
      if (stored?.settings?.finalVersionIds?.length === 1 && stored.settings.finalVersionIds[0] === props.finalVersionId) intent.value = stored
    } catch (e) { console.warn('[dataset.restore_failed]', e) }
    if (intent.value) {
      format.value = intent.value.settings.format; splitRatio.value = intent.value.settings.splitRatio
      if (intent.value.jobId) {
        try { job.value = await api.status(intent.value.jobId) }
        catch (e) {
          if ((e as {status?: number}).status !== 404) throw e
          console.warn('[dataset.expired_job]', {jobId: intent.value.jobId})
          intent.value = null; job.value = null; localStorage.removeItem(storageKey.value)
        }
      }
      else { error.value = '上次创建导出的响应尚未确认，可使用原请求重试。'; retryPhase.value = 'create' }
    }
  } catch (e) { fail(e, 'preview') }
  finally { busy.value = false }
  if (working.value) schedule()
}
function close() {
  if (busy.value) return
  opened.value = false; automaticDownload = false
}
function schedule() {
  stopPolling()
  if (alive && working.value) timer = setTimeout(() => { void refreshStatus() }, 1000)
}
async function refreshStatus() {
  if (!job.value || polling) return
  const id = job.value.exportId
  polling = true
  try {
    const next = await api.status(id)
    if (!alive || job.value?.exportId !== id) return
    job.value = next; error.value = ''
    if (next.state === 'ready' && automaticDownload && opened.value) {
      automaticDownload = false
      await download()
    }
    schedule()
  } catch (e) { if (alive) fail(e, 'status') }
  finally { polling = false }
}
async function create(retry = false) {
  if (busy.value || !summary.value || working.value) return
  error.value = ''; busy.value = true
  if (!retry) {
    intent.value = { settings: { finalVersionIds: [props.finalVersionId], format: format.value, splitRatio: splitRatio.value }, key: crypto.randomUUID() }
    job.value = null; persist()
  }
  if (!intent.value) { busy.value = false; return }
  try {
    job.value = await api.create(intent.value.settings, intent.value.key)
    intent.value.jobId = job.value.exportId; persist()
    automaticDownload = true
  } catch (e) { fail(e, 'create') }
  finally { busy.value = false }
  if (job.value?.state === 'ready') { automaticDownload = false; await download() }
  else schedule()
}
async function download() {
  if (!job.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const blob = await api.download(job.value.exportId)
    const url = URL.createObjectURL(blob), link = document.createElement('a')
    link.href = url; link.download = `${props.mediaName}-已确认训练数据集-${job.value.exportId}.zip`
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 2000)
  } catch (e) { fail(e, 'download') }
  finally { busy.value = false }
}
async function retry() {
  error.value = ''
  if (retryPhase.value === 'create') await create(true)
  else if (retryPhase.value === 'status') await refreshStatus()
  else if (retryPhase.value === 'download') await download()
  else await open()
}
function keys(e: KeyboardEvent) {
  if (!opened.value) return
  if (e.key === 'Escape') { e.preventDefault(); close() }
  if (e.key === 'Tab' && dialog.value) {
    const nodes = Array.from(dialog.value.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled)'))
    const first = nodes[0], last = nodes[nodes.length - 1]
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
  }
}
window.addEventListener('keydown', keys)
onUnmounted(() => { alive = false; stopPolling(); emit('open', false); emit('busy', false); window.removeEventListener('keydown', keys) })
</script>

<template>
  <button class="c-primary c-wide" data-testid="export-training" :disabled="disabled || busy" @click="open">
    {{ working ? '查看数据集导出进度' : '导出训练数据集' }}
  </button>
  <Teleport to="body">
    <div v-if="opened" class="confirmation-page training-export-host">
      <div class="c-modal-mask" @click.self="close">
        <section ref="dialog" class="c-modal training-export-dialog" role="dialog" aria-modal="true" aria-labelledby="training-export-title">
          <h2 id="training-export-title">导出已确认训练数据集</h2>
          <p>{{ mediaName }} · 仅使用当前已完成审查与对比确认的最终版本。</p>
          <div v-if="summary" class="training-export-summary">
            <span><b>{{ summary.frameCount }}</b>全部帧</span>
            <span><b>{{ summary.objectCount }}</b>最终标注框</span>
            <span><b>{{ summary.emptyFrames }}</b>空帧</span>
            <span><b>{{ summary.classNames.length }}</b>类别</span>
          </div>
          <p v-if="summary" class="training-export-classes">类别：{{ summary.classNames.join(' · ') }}</p>
          <div v-if="error" role="alert" class="confirmation-error">
            <span>{{ error }}</span><button :disabled="busy" @click="retry">重试导出请求</button>
          </div>
          <template v-if="summary">
            <label class="training-export-field">输出格式
              <select v-model="format" aria-label="数据集格式" :disabled="busy || working || !!error">
                <option value="yolo">YOLO（默认）</option>
                <option value="coco">COCO</option>
                <option value="both">YOLO + COCO</option>
              </select>
            </label>
            <label class="training-export-field">训练 / 验证集划分
              <select v-model.number="splitRatio" aria-label="训练验证比例" :disabled="busy || working || !!error">
                <option :value=".7">70% / 30%</option>
                <option :value=".8">80% / 20%</option>
                <option :value=".9">90% / 10%</option>
              </select>
            </label>
            <p>预计训练 {{ trainCount }} 帧、验证 {{ summary.frameCount - trainCount }} 帧。保留空帧与未修改对象；真实图像与标签逐帧对应。按帧序划分，必要时交换样本，让训练与验证尽量都含有目标。</p>
            <p v-if="format !== 'coco'">ZIP 包含 images/、labels/、data.yaml 和版本追溯信息。解压后使用 data.yaml 训练。</p>
          </template>
          <div v-if="job" class="training-export-job" aria-live="polite">
            <template v-if="working">
              <strong>{{ job.state === 'queued' ? '等待导出' : '正在提取图像和生成标签' }}</strong>
              <progress :max="job.totalFrames" :value="job.completedFrames" />
              <span>{{ job.completedFrames }} / {{ job.totalFrames }} 帧 · 关闭弹窗后后台继续</span>
            </template>
            <template v-else-if="job.state === 'ready'">
              <strong>数据集已生成（{{ intent?.settings.format.toUpperCase() }}）</strong>
              <p>训练 {{ job.manifest?.counts.train }} 帧 · 验证 {{ job.manifest?.counts.val }} 帧</p>
            </template>
            <template v-else><strong>导出未完成</strong><p role="alert">{{ job.errorMessage }}</p></template>
            <small class="training-export-id">任务号：{{ job.exportId }}</small>
          </div>
          <p v-if="!summary && !error">正在校验最终版本…</p>
          <div class="training-export-actions">
            <button :disabled="busy" @click="close">{{ working ? '后台继续' : '关闭' }}</button>
            <button v-if="summary && !working && job?.state !== 'invalidated'" class="c-primary" :disabled="busy || (!!error && (retryPhase === 'create' || retryPhase === 'preview'))" @click="create(false)">
              {{ busy ? '正在处理…' : job ? '重新生成 ZIP' : '生成并下载 ZIP' }}
            </button>
            <button v-if="job?.state === 'ready'" class="c-primary" :disabled="busy" @click="download">下载 ZIP</button>
          </div>
        </section>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.training-export-host { min-height: 0; padding: 0; }
.training-export-dialog { width: 570px; }
.training-export-summary { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; padding: 12px; margin-bottom: 12px; background: var(--surface-subtle); border-radius: 5px; text-align: center; color: var(--muted); }
.training-export-summary b { display: block; color: var(--accent); font-size: 20px; }
.training-export-classes { overflow-wrap: anywhere; }
.training-export-field { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin: 14px 0; color: var(--text); }
.training-export-field select { background: var(--surface-subtle); border: 1px solid var(--line); border-radius: 5px; padding: 7px 10px; color: var(--text); }
.training-export-job { display: flex; flex-direction: column; gap: 10px; background: var(--surface-subtle); border: 1px solid var(--line); border-radius: 5px; padding: 12px; margin-top: 14px; }
.training-export-job p { margin-bottom: 0; }
.training-export-id { overflow-wrap: anywhere; }
.training-export-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; margin-top: 14px; }
</style>
