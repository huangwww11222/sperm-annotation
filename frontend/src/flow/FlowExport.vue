<script setup lang="ts">
// Training set from finished videos. Only what people judged; what nobody could tell is masked.
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import { flowApi, type FlowExport, type FlowFailure } from '../api/flowApi'
import { useFlow } from '../stores/flow'
import { createRequestId } from '../utils/browserCompat'
import { missLines, type FlowMisses } from './flowLogic'

const emit = defineEmits<{ close: [] }>()
const flow = useFlow()
const ready = computed(() => flow.items.value.filter(item => item.state === 'done'))
const chosen = ref<string[]>(ready.value.map(item => item.mediaId)), step = ref(4), videos = ref(true)
const jobs = ref<FlowExport[]>([]), misses = ref<FlowMisses | null>(null), error = ref(''), busy = ref(false)
const all = computed(() => ready.value.length > 0 && ready.value.every(item => chosen.value.includes(item.mediaId)))
const toggleAll = () => { chosen.value = all.value ? [] : ready.value.map(item => item.mediaId) }
const frames = computed(() => ready.value.filter(item => chosen.value.includes(item.mediaId)).reduce((sum, item) => sum + Math.ceil(item.frameCount / step.value), 0))
const folders = computed(() => new Set(ready.value.filter(item => chosen.value.includes(item.mediaId)).map(item => item.sample || item.folderPath)).size)
const working = computed(() => jobs.value.some(job => job.state === 'queued' || job.state === 'running'))
const lines = computed(() => missLines(misses.value))
let timer: ReturnType<typeof setTimeout> | null = null, stopped = false
async function load() {
  try { jobs.value = (await flowApi.exports()).items } catch (e) { error.value = (e as FlowFailure).message }
  if (!stopped && working.value) timer = setTimeout(load, 1200)
}
onMounted(async () => { void load(); try { misses.value = await flowApi.misses() } catch { /* The dialog works without it. */ } })
onUnmounted(() => { stopped = true; if (timer) clearTimeout(timer) })

// The same click must never make two packages: the key changes only with the settings.
let key = '', keyFor = ''
async function create() {
  const body = { mediaIds: chosen.value, step: step.value, includeSourceVideos: videos.value }, wanted = JSON.stringify(body)
  if (keyFor !== wanted) { key = createRequestId(); keyFor = wanted }
  busy.value = true; error.value = ''
  try { await flowApi.createExport(body, key); keyFor = ''; await load() } catch (e) { error.value = (e as FlowFailure).message } finally { busy.value = false }
}
async function remove(job: FlowExport) { try { await flowApi.removeExport(job.exportId); await load() } catch (e) { error.value = (e as FlowFailure).message } }
const when = (job: FlowExport) => new Date(job.createdAt).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
</script>

<template>
  <Teleport to="body">
    <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" @keydown.esc.prevent="emit('close')">
      <section class="app-dialog flow-dialog" role="dialog" aria-modal="true" aria-label="导出微流控数据集" data-testid="flow-export">
        <h2>导出数据集</h2>
        <p v-if="error" role="alert" class="text-[var(--danger)]">{{ error }}</p>
        <label class="flow-pick flow-pick-all"><input type="checkbox" :checked="all" @change="toggleAll" />全选判完的（{{ ready.length }} 段）</label>
        <div class="flow-pick-list">
          <label v-for="item in ready" :key="item.mediaId" class="flow-pick"><input v-model="chosen" type="checkbox" :value="item.mediaId" /><span>{{ item.videoName }}</span><em>{{ item.progress?.yes }} 个精子</em></label>
          <p v-if="!ready.length" class="sidebar-empty">还没有判完的视频</p>
        </div>
        <label class="flow-step-field">每隔几帧取一张<select v-model.number="step" aria-label="每隔几帧取一张" data-testid="flow-export-step"><option v-for="n in [1, 2, 4, 8]" :key="n" :value="n">{{ n === 1 ? '每帧都要' : `每 ${n} 帧 1 张` }}</option></select><em>约 {{ frames }} 张</em></label>
        <label class="flow-step-field"><input v-model="videos" type="checkbox" data-testid="flow-export-videos" />包含原视频<em>可导回复查，体积较大</em></label>
        <p v-if="chosen.length && folders < 2" class="review-note">全部来自同一个文件夹，训练和验证只能按视频分。</p>
        <div v-if="lines.length" class="flow-miss"><b>漏标估计</b><p v-for="line in lines" :key="line">{{ line }}</p></div>
        <ul v-if="jobs.length" class="flow-jobs" data-testid="flow-export-jobs">
          <li v-for="job in jobs" :key="job.exportId"><span>{{ when(job) }} · {{ job.videoCount }} 段<template v-if="job.counts"> · {{ job.counts.train + job.counts.val }} 张 {{ job.counts.sperm }} 框</template><em v-if="job.state === 'failed'">{{ job.errorMessage }}</em></span>
            <a v-if="job.downloadUrl" class="btn-secondary" :href="job.downloadUrl" :download="`微流控训练数据集-${job.exportId}.zip`" data-testid="flow-export-download">下载</a>
            <small v-else-if="job.state !== 'failed'">{{ job.totalFrames ? `${Math.round(job.completedFrames / job.totalFrames * 100)}%` : '排队中' }}</small>
            <button v-if="job.state === 'ready' || job.state === 'failed'" class="icon-button" aria-label="删除这次导出" title="删除这次导出" @click="remove(job)"><AppIcon name="trash" :size="14" /></button>
          </li>
        </ul>
        <div class="mt-4 flex justify-end gap-3"><button class="btn-secondary" @click="emit('close')">关闭</button><button class="btn-primary" data-testid="flow-export-start" :disabled="busy || !chosen.length" @click="create">导出 {{ chosen.length }} 段</button></div>
      </section>
    </div>
  </Teleport>
</template>
