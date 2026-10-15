<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import StatisticsDataExport from '../components/StatisticsDataExport.vue'
import { listResults } from '../api/httpAnnotationApi'

interface DbRow {
  id: number
  batch_id?: string | null
  user_id: number
  media_id: string
  media_name?: string | null
  media_type?: string | null
  frame_index: number
  timestamp_ms?: number | null
  object_name?: string | null
  source?: string | null
  username?: string | null
  created_at: string
}

interface ResultGroup {
  key: string
  mediaId: string
  mediaName: string
  mediaType: string
  frameIndex: number | null
  timestampMs: number | null
  objects: string[]
  username: string
  savedAt: string
}

const rawRows = ref<DbRow[]>([])
const loading = ref(false)
const errorMessage = ref('')
const selectedMediaId = ref('')
const search = ref(''), page = ref(1)
const pageSize = 50

const mediaIdentity = (row: DbRow) =>
  `${row.media_type || 'video'}:${row.media_id}`

const mediaOptions = computed(() => {
  const map = new Map<string, { id: string; name: string; type: string }>()
  for (const row of rawRows.value) {
    const identity = mediaIdentity(row)
    if (!map.has(identity)) {
      map.set(identity, {
        id: identity,
        name: row.media_name || row.media_id,
        type: row.media_type || 'video',
      })
    }
  }
  return Array.from(map.values()).sort((a, b) => a.name.localeCompare(b.name))
})

const filteredRows = computed(() => {
  return selectedMediaId.value
    ? rawRows.value.filter((row) => mediaIdentity(row) === selectedMediaId.value)
    : rawRows.value
})

const groupedRows = computed<ResultGroup[]>(() => {
  const groups = new Map<string, ResultGroup>()
  for (const row of filteredRows.value) {
    // 标注结果展示当前逻辑状态，而不是重复展示每次 Tracking 产生的保存批次。
    // Use stable media identity: distinct videos may legitimately share a filename.
    const key = `${mediaIdentity(row)}:${row.frame_index}:${row.user_id}`
    const existing = groups.get(key)
    if (existing) {
      if (row.object_name && !existing.objects.includes(row.object_name)) existing.objects.push(row.object_name)
      if (row.created_at > existing.savedAt) existing.savedAt = row.created_at
      continue
    }
    groups.set(key, {
      key,
      mediaId: row.media_id,
      mediaName: row.media_name || row.media_id,
      mediaType: row.media_type || 'video',
      frameIndex: row.media_type === 'video' ? row.frame_index : null,
      timestampMs: row.media_type === 'video' ? (row.timestamp_ms ?? 0) : null,
      objects: row.object_name ? [row.object_name] : [],
      username: row.username || '-',
      savedAt: row.created_at,
    })
  }
  const term = search.value.toLowerCase()
  return Array.from(groups.values()).filter(row => `${row.mediaName} ${row.username} ${row.objects.join(' ')}`.toLowerCase().includes(term))
})

const formatTimestamp = (ms: number | null) => {
  if (ms == null) return '-'
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  const millis = ms % 1000
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`
}

const formatSavedAt = (value: string) => {
  const normalized = value?.includes('T') ? value : String(value || '').replace(' ', 'T')
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const load = async () => {
  loading.value = true
  errorMessage.value = ''
  try {
    const res = await listResults('default')
    rawRows.value = (res.items ?? [])
      .map((row) => row as DbRow)
      .filter((row) => row.source === 'manual')
  } catch (error: any) {
    errorMessage.value = error?.message || '读取数据库标注记录失败'
    console.error('[records.load_failed]', error)
  } finally {
    loading.value = false
  }
}

const pageCount = computed(() => Math.max(1, Math.ceil(groupedRows.value.length / pageSize)))
const visibleRows = computed(() => groupedRows.value.slice((page.value-1)*pageSize,page.value*pageSize))
watch([search,selectedMediaId],()=>{page.value=1})
watch(pageCount,n=>{page.value=Math.min(page.value,n)})
onMounted(load)
</script>

<template>
  <section class="records-page">
    <header class="records-heading"><div><h2>标注记录</h2><p>回查已入库的人工对象与标注人；最终数据集在对比确认完成后导出。</p></div><div class="records-heading-actions"><button class="btn-secondary" :disabled="loading" @click="load">{{ loading?'读取中…':'刷新记录' }}</button><StatisticsDataExport /></div></header>
    <div class="panel records-panel"><div class="records-filters"><label class="records-search"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><circle cx="10" cy="10" r="6.5"/><path d="m15 15 5 5"/></svg><input v-model="search" class="input" aria-label="搜索标注记录" placeholder="搜索素材、对象或标注人…"/></label><select v-model="selectedMediaId" class="input" aria-label="按素材筛选"><option value="">全部素材</option><option v-for="media in mediaOptions" :key="media.id" :value="media.id">{{ media.name }} · {{ media.id.slice(-8) }}</option></select><span>按素材 / 帧 / 标注人汇总</span></div>
    <div class="records-stats"><div><AppIcon name="folder" :size="15"/><span>素材 <strong>{{ mediaOptions.length }}</strong></span></div><div><AppIcon name="list" :size="15"/><span>当前筛选 <strong>{{ groupedRows.length }}</strong> 条帧记录</span></div><p>独立查询入口，无需经过此页即可送审。</p></div>
    <div v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</div>
    <div class="records-table"><table><thead><tr><th>素材</th><th>帧号</th><th>人工标注对象</th><th>标注人</th><th>最近入库时间</th></tr></thead><tbody><tr v-for="row in visibleRows" :key="row.key"><td><strong>{{ row.mediaName }}</strong><small>{{ row.mediaType==='video'?'视频':'图片' }} · {{ row.mediaId.slice(-10) }}</small></td><td>{{ row.frameIndex===null?'—':row.frameIndex+1 }}<small v-if="row.timestampMs!==null">{{ formatTimestamp(row.timestampMs) }}</small></td><td><div class="record-tags"><span v-for="name in row.objects" :key="name">{{ name }}</span><span v-if="!row.objects.length">—</span></div></td><td>{{ row.username }}</td><td>{{ formatSavedAt(row.savedAt) }}</td></tr><tr v-if="!visibleRows.length"><td colspan="5" class="records-empty">{{ loading?'正在读取记录…':search||selectedMediaId?'没有匹配的记录':'暂无已入库的人工标注记录' }}</td></tr></tbody></table></div>
    <footer class="records-pagination"><small>第 {{ page }} / {{ pageCount }} 页 · 每页 {{ pageSize }} 条</small><button class="btn-secondary" :disabled="page===1" @click="page--">上一页</button><button class="btn-secondary" :disabled="page===pageCount" @click="page++">下一页</button></footer></div>
  </section>
</template>
<style scoped>
.records-page{max-width:1600px;min-width:0;min-height:420px;height:calc(100vh - var(--app-header-height) - var(--page-gutter) - var(--page-gutter) - 16px);margin:8px auto;display:flex;flex-direction:column;gap:18px}
.records-heading{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;flex-shrink:0}.records-heading>div:first-child{min-width:0}
.records-heading-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.records-heading h2{font-size:22px;font-weight:650;margin:0;line-height:1.4;letter-spacing:-.02em}
.records-heading p{font-size:13px;line-height:1.7;color:var(--muted);margin:7px 0 0;max-width:70ch}
.records-panel{overflow:hidden;min-width:0;min-height:0;flex:1;display:flex;flex-direction:column;border-radius:10px;box-shadow:none}
.records-filters{display:flex;flex-wrap:wrap;gap:12px;padding:16px;align-items:center;border-bottom:1px solid var(--line);flex-shrink:0}
.records-search{position:relative;display:flex;align-items:center;flex:1;min-width:220px;max-width:430px}.records-search>svg{position:absolute;left:11px;color:var(--muted);pointer-events:none}
.records-filters .records-search input{width:100%;padding-left:36px;min-height:38px;font-size:13px}.records-filters select{width:260px;max-width:100%;min-height:38px;font-size:13px}
.records-filters>span{margin-left:auto;font-size:12px;color:var(--muted);line-height:1.6}
.records-stats{display:flex;flex-wrap:wrap;gap:8px 22px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--surface-subtle);flex-shrink:0}
.records-stats>div{display:flex;gap:7px;align-items:center;color:var(--muted)}.records-stats span{font-size:12px}.records-stats strong{color:var(--text);font-size:13px;font-weight:650;font-variant-numeric:tabular-nums;margin-left:3px}
.records-stats p{margin:0 0 0 auto;color:var(--muted);font-size:12px;line-height:1.6}
.records-table{overflow:auto;flex:1;min-height:0;overscroll-behavior:contain}
table{width:100%;min-width:760px;font-size:13px;border-collapse:collapse}th{position:sticky;top:0;z-index:1;background:var(--surface-subtle);color:var(--muted);text-align:left;font-weight:600;white-space:nowrap}
th,td{padding:14px 18px;border-bottom:1px solid var(--line)}td{vertical-align:top;line-height:1.65}td:first-child{min-width:190px;max-width:330px}td strong{font-weight:600;overflow-wrap:anywhere}td small{display:block;color:var(--muted);font-size:11px;margin-top:4px}td:nth-child(2),td:last-child{white-space:nowrap}tbody tr:hover{background:var(--surface-subtle)}
.record-tags{display:flex;gap:5px;flex-wrap:wrap;min-width:170px;max-width:400px}.record-tags span{background:var(--accent-soft);color:var(--accent);border-radius:4px;padding:2px 7px;font-size:12px;overflow-wrap:anywhere}
.records-empty{text-align:center;padding:64px 20px;color:var(--muted)}.records-pagination{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px 16px;flex-shrink:0}.records-pagination small{margin-right:auto;color:var(--muted);font-size:12px;line-height:1.6}
@media(max-width:1100px){.records-filters>span{display:none}.records-stats p{flex-basis:100%;margin-left:0}}
@media(max-width:680px){.records-page{height:auto;min-height:0;margin:2px 0;gap:14px}.records-heading{gap:12px}.records-heading h2{font-size:20px}.records-heading p{font-size:12px}.records-heading-actions{width:100%}.records-panel{display:block}.records-filters{padding:12px;gap:10px}.records-search{flex-basis:100%;min-width:0;max-width:none}.records-filters select{width:100%}.records-stats{padding:10px 12px;gap:6px 16px}.records-table{max-height:62vh;min-height:220px}.records-empty{text-align:left;padding-left:14px}.records-pagination{padding:12px;gap:7px}.records-pagination small{width:100%;margin-right:0}th,td{padding:12px 14px}}
@media(max-height:600px) and (min-width:681px){.records-page{height:auto;min-height:0}.records-table{flex:none;max-height:45vh;min-height:220px}}
</style>
