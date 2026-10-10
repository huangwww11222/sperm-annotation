<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useAuth } from '../stores/auth'
import { createRequestId } from '../utils/browserCompat'
import { statisticsExportApi as api, type StatisticsExportError, type StatisticsExportJob } from '../api/statisticsExportApi'

type Intent = { key: string; exportId?: string }
type RetryPhase = 'create' | 'status' | 'download'
const auth = useAuth()
const actorId = computed(() => auth.user.value?.id || '')
const dialog = ref<HTMLDialogElement | null>(null)
const opened = ref(false), busy = ref(false), polling = ref(false)
const job = ref<StatisticsExportJob | null>(null), intent = ref<Intent | null>(null)
const error = ref(''), recoveryWarning = ref(''), expired = ref(false)
const retryPhase = ref<RetryPhase | null>(null)
const working = computed(() => job.value?.state === 'queued' || job.value?.state === 'running')
const unresolvedCreate = computed(() => !!intent.value && !intent.value.exportId)
const canGenerate = computed(() => !busy.value && !polling.value && !working.value && !unresolvedCreate.value
  && retryPhase.value !== 'status'
  && (!intent.value || job.value?.state === 'ready' || job.value?.state === 'failed'))
const stateTitle = computed(() => {
  if (job.value?.state === 'ready') return '统计数据已生成'
  if (job.value?.state === 'failed') return '统计数据生成失败'
  if (job.value?.state === 'queued') return '等待生成统计数据'
  if (job.value?.stage === 'compressing') return '正在打包 ZIP'
  if (job.value?.state === 'running') return '正在读取统计数据'
  return ''
})
const downloadSize = computed(() => {
  const bytes = job.value?.sizeBytes
  if (bytes == null) return ''
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
})
let alive = true, epoch = 0
let controller: AbortController | null = null
let timer: ReturnType<typeof setTimeout> | null = null
let priorFocus: HTMLElement | null = null, priorOverflow = ''
let scrollLocked = false
const storageKey = (actor: string) => `statistics-export:v1:${actor}`
function stop() {
  if (timer) clearTimeout(timer)
  timer = null
  controller?.abort(); controller = null
  epoch += 1
  busy.value = false; polling.value = false
}
function persist() {
  try {
    if (intent.value) localStorage.setItem(storageKey(actorId.value), JSON.stringify(intent.value))
    else localStorage.removeItem(storageKey(actorId.value))
  } catch (failure) {
    recoveryWarning.value = '浏览器无法保存恢复记录。本次页面内可重试原请求，刷新或离开页面后将无法恢复此次任务。'
    console.error('[statistics_export.recovery_storage_failed]', { actorId: actorId.value, exportId: intent.value?.exportId, error: failure })
  }
}
function restoreIntent() {
  if (intent.value) return
  try {
    const raw = localStorage.getItem(storageKey(actorId.value))
    if (!raw) return
    const stored = JSON.parse(raw) as Intent
    if (!stored || typeof stored.key !== 'string' || !stored.key || stored.key.length > 128
      || !(stored.exportId === undefined || typeof stored.exportId === 'string' && !!stored.exportId)) throw new Error('统计导出恢复记录损坏')
    intent.value = { key: stored.key, ...(stored.exportId ? { exportId: stored.exportId } : {}) }
  } catch (failure) {
    recoveryWarning.value = '浏览器中的恢复记录无法读取。重新生成前，请确认上次任务已结束；服务器会拒绝同时运行的重复任务。'
    console.error('[statistics_export.recovery_read_failed]', { actorId: actorId.value, error: failure })
  }
}
function begin() {
  controller?.abort()
  controller = new AbortController()
  return { epoch, actor: actorId.value, controller }
}
function current(request: ReturnType<typeof begin>) {
  return alive && opened.value && epoch === request.epoch && actorId.value === request.actor && !request.controller.signal.aborted
}
function fail(failure: unknown, phase: RetryPhase) {
  const detail = failure as StatisticsExportError
  if (detail?.status === 410 && (detail.code === 'STATISTICS_EXPORT_EXPIRED' || detail.code === 'STATISTICS_EXPORT_FILE_MISSING')) {
    job.value = null; intent.value = null; expired.value = true; retryPhase.value = null
    error.value = detail.code === 'STATISTICS_EXPORT_FILE_MISSING'
      ? '上次导出的 ZIP 文件已失效，请重新生成统计数据。原有标注数据不受影响。'
      : '上次导出任务已过期或服务器已重启，请重新生成统计数据。原有标注数据不受影响。'
    persist()
  } else {
    retryPhase.value = phase
    error.value = `${detail?.message || '连接中断，请重试'}${detail?.requestId ? `（记录号 ${detail.requestId}）` : ''}`
  }
  console.error('[statistics_export.operation_failed]', { phase, actorId: actorId.value, exportId: intent.value?.exportId, error: failure })
}
function accept(next: StatisticsExportJob) {
  const stateChanged = job.value?.state !== next.state || job.value?.stage !== next.stage || job.value?.exportId !== next.exportId
  job.value = next
  if (intent.value) { intent.value.exportId = next.exportId; persist() }
  error.value = ''; expired.value = false; retryPhase.value = null
  if (stateChanged) console.info('[statistics_export.task_received]', { actorId: actorId.value, exportId: next.exportId, state: next.state, stage: next.stage })
}
function schedule() {
  if (timer) clearTimeout(timer)
  timer = null
  if (alive && opened.value && working.value && !error.value) timer = setTimeout(() => { void refreshStatus() }, 1000)
}
async function open() {
  if (!actorId.value || opened.value) return
  priorFocus = document.activeElement as HTMLElement
  opened.value = true
  await nextTick()
  if (!alive || !opened.value) return
  priorOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'; scrollLocked = true
  dialog.value?.showModal()
  restoreIntent()
  if (intent.value?.exportId) await refreshStatus()
  else if (intent.value) {
    error.value = '上次生成请求的结果尚未确认。请用原请求重试，避免在有效期内重复创建；服务器重启或任务到期后可能需要重新生成。'
    retryPhase.value = 'create'
  }
}
function restorePage() {
  if (scrollLocked) { document.body.style.overflow = priorOverflow; scrollLocked = false }
  if (priorFocus?.isConnected) priorFocus.focus()
}
function close() {
  opened.value = false; stop()
  dialog.value?.close()
  restorePage()
}
async function create(retry = false) {
  if (busy.value || polling.value || working.value || !actorId.value) return
  if (!retry) {
    if (!canGenerate.value) return
    intent.value = { key: createRequestId() }; job.value = null; expired.value = false; persist()
  }
  if (!intent.value) return
  const key = intent.value.key, request = begin()
  busy.value = true; error.value = ''; retryPhase.value = null
  try {
    const next = await api.create(key, request.controller.signal)
    if (current(request) && intent.value?.key === key) accept(next)
  } catch (failure) { if (current(request)) fail(failure, 'create') }
  finally { if (current(request)) { busy.value = false; schedule() } }
}
async function refreshStatus() {
  if (busy.value || polling.value || !intent.value?.exportId) return
  const id = intent.value.exportId, request = begin()
  polling.value = true; error.value = ''; retryPhase.value = null
  try {
    const next = await api.status(id, request.controller.signal)
    if (current(request) && intent.value?.exportId === id) {
      if (next.exportId !== id) throw new Error('统计任务身份不匹配，请重试查询')
      accept(next)
    }
  } catch (failure) { if (current(request)) fail(failure, 'status') }
  finally { if (current(request)) { polling.value = false; schedule() } }
}
async function download() {
  if (busy.value || polling.value || !job.value?.downloadAllowed || job.value.state !== 'ready') return
  const id = job.value.exportId, fileName = job.value.fileName || `标注统计数据-${id}.zip`, request = begin()
  busy.value = true; error.value = ''; retryPhase.value = null
  try {
    const blob = await api.download(id, request.controller.signal)
    if (!current(request) || job.value?.exportId !== id) return
    const url = URL.createObjectURL(blob), link = document.createElement('a')
    link.href = url; link.download = fileName.replace(/[\\/]/g, '_'); link.click()
    setTimeout(() => URL.revokeObjectURL(url), 2000)
    console.info('[statistics_export.download_started]', { actorId: actorId.value, exportId: id, sizeBytes: blob.size })
  } catch (failure) { if (current(request)) fail(failure, 'download') }
  finally { if (current(request)) busy.value = false }
}
async function retry() {
  if (retryPhase.value === 'create') await create(true)
  else if (retryPhase.value === 'status') await refreshStatus()
  else if (retryPhase.value === 'download') await download()
}
function keydown(event: KeyboardEvent) {
  if (event.key !== 'Tab') return
  const nodes = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),[tabindex="0"]') || [])
  const first = nodes[0], last = nodes[nodes.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
watch(actorId, () => {
  close(); job.value = null; intent.value = null; error.value = ''; recoveryWarning.value = ''; expired.value = false; retryPhase.value = null
}, { flush: 'sync' })
onUnmounted(() => { alive = false; close() })
</script>

<template>
  <button class="btn-primary statistics-export-launcher" data-testid="statistics-export-open" aria-haspopup="dialog" :disabled="!actorId" @click="open"><AppIcon name="folder" :size="16" />导出统计数据</button>
  <Teleport to="body">
    <dialog ref="dialog" class="statistics-export-dialog" aria-labelledby="statistics-export-title" aria-describedby="statistics-export-purpose" @cancel.prevent="close" @close="close" @keydown.stop="keydown">
      <header class="statistics-export-header"><div><h2 id="statistics-export-title">导出统计数据</h2><p>整个系统的统计数据 · 不受本页筛选条件影响</p></div><button class="icon-button" data-testid="statistics-export-close" aria-label="关闭统计数据导出" @click="close"><AppIcon name="close" /></button></header>
      <div class="statistics-export-content">
        <p id="statistics-export-purpose">导出整个系统已送审的原始标注 A 及其审查 B、确认 C、最终 F、人员及操作历史，包含未完成审查或确认的任务和历史版本。未送审的工作区草稿不在包内。</p>
        <div class="statistics-export-scope"><strong>统计数据 ZIP</strong><p>不含原视频、密码或登录凭据。它用于统计分析；训练数据集仍需在对比确认完成后导出。</p><p>包含人员名称和标注记录，请仅交给负责数据统计的人员。</p></div>
        <p v-if="recoveryWarning" class="statistics-export-warning" role="status">{{ recoveryWarning }}</p>
        <div v-if="error" class="statistics-export-error" data-testid="statistics-export-error" role="alert"><p>{{ error }}</p><button v-if="retryPhase" class="btn-secondary" data-testid="statistics-export-retry" :disabled="busy || polling" @click="retry">{{ retryPhase === 'create' ? '重试原生成请求' : retryPhase === 'status' ? '重试查询任务' : '重试下载 ZIP' }}</button></div>
        <div v-if="job" class="statistics-export-job" data-testid="statistics-export-progress" aria-live="polite">
          <strong>{{ stateTitle }}</strong>
          <template v-if="working"><progress :max="Math.max(1, job.progress.totalTables)" :value="job.progress.completedTables" /><p>已读取 {{ job.progress.completedTables }} / {{ job.progress.totalTables }} 个数据表 · {{ job.progress.rowsWritten }} 条记录</p><p>关闭弹窗后，后台仍会继续生成。再次打开可查看进度。</p></template>
          <template v-else-if="job.state === 'ready'"><p>{{ job.fileName }}<span v-if="downloadSize"> · {{ downloadSize }}</span></p><p>点击“下载 ZIP”，然后在独立统计系统中导入。导出文件临时保留，过期或服务器重启后需要重新生成。</p></template>
          <p v-else role="alert">{{ job.error?.message || '生成未完成，请重新生成。' }}</p>
          <small data-testid="statistics-export-job-id">任务号：{{ job.exportId }}</small>
        </div>
        <p v-else-if="busy" data-testid="statistics-export-progress" role="status">正在确认生成请求，请稍候…</p>
        <p v-else-if="polling" role="status">正在恢复上次任务…</p>
      </div>
      <footer class="statistics-export-actions"><button class="btn-secondary" @click="close">{{ working ? '后台继续' : '关闭' }}</button><button v-if="canGenerate" class="btn-primary" :data-testid="job || expired ? 'statistics-export-regenerate' : 'statistics-export-confirm'" @click="create(false)">{{ job || expired ? '重新生成 ZIP' : '确认并生成 ZIP' }}</button><button v-if="job?.state === 'ready' && job.downloadAllowed" class="btn-primary" data-testid="statistics-export-download" :disabled="busy || polling" @click="download">{{ busy ? '正在下载…' : '下载 ZIP' }}</button></footer>
    </dialog>
  </Teleport>
</template>

<style scoped>
.statistics-export-launcher{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}.statistics-export-dialog{width:min(600px,calc(100vw - 32px));max-width:none;max-height:calc(100vh - 48px);padding:0;margin:auto;border:1px solid var(--line);border-radius:14px;background:var(--surface);color:var(--text);box-shadow:0 24px 90px #0005;overflow:hidden}.statistics-export-dialog[open]{display:flex;flex-direction:column}.statistics-export-dialog::backdrop{background:#0c203588}.statistics-export-header{display:flex;align-items:center;justify-content:space-between;gap:15px;padding:20px 24px;border-bottom:1px solid var(--line);flex-shrink:0}.statistics-export-header h2{font-size:20px;font-weight:650}.statistics-export-header p{margin-top:6px;color:var(--muted);font-size:12px}.statistics-export-content{padding:22px 24px;overflow:auto;min-height:0;font-size:13px;line-height:1.8;overscroll-behavior:contain}.statistics-export-content>p{margin-bottom:14px}.statistics-export-scope,.statistics-export-job{padding:14px 16px;border:1px solid var(--line);border-radius:8px;background:var(--surface-subtle)}.statistics-export-scope p{margin-top:6px;color:var(--muted)}.statistics-export-warning{padding:12px;margin-top:14px;border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px;color:var(--muted)}.statistics-export-error{margin-top:14px;padding:12px 14px;border:1px solid #dc526955;border-radius:7px;background:#dc52690b;color:var(--danger,#c53651)}.statistics-export-error p{overflow-wrap:anywhere;margin-bottom:8px}.statistics-export-job{display:flex;flex-direction:column;gap:8px;margin-top:16px}.statistics-export-job p,.statistics-export-job small{overflow-wrap:anywhere}.statistics-export-job small{color:var(--muted);font-size:11px}.statistics-export-job progress{width:100%;height:9px;accent-color:var(--accent)}.statistics-export-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap;padding:16px 24px;border-top:1px solid var(--line);flex-shrink:0}@media(max-width:600px){.statistics-export-header,.statistics-export-content,.statistics-export-actions{padding:16px}.statistics-export-actions button{flex:1}.statistics-export-dialog{max-height:calc(100vh - 24px)}}
</style>
