<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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

const mediaIdentity = (row: DbRow) =>
  `${row.media_type || 'video'}:${String(row.media_name || row.media_id).trim().toLocaleLowerCase()}`

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
    // 同一文件（兼容历史临时 media_id）、同一帧、同一标注人只显示一行。
    const key = `${mediaIdentity(row)}:${row.frame_index}:${row.user_id}`
    const existing = groups.get(key)
    if (existing) {
      if (row.object_name && !existing.objects.includes(row.object_name)) existing.objects.push(row.object_name)
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
  return Array.from(groups.values())
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
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="flex min-h-0 flex-1 flex-col gap-4">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 class="text-lg font-semibold">标注结果</h2>
        <p class="mt-1 text-xs text-slate-500">这里只显示数据库中的人工新增或人工修改记录，不显示 AI Tracking 结果。</p>
      </div>
      <div class="flex items-center gap-2">
        <label class="text-xs text-slate-400">文件</label>
        <select v-model="selectedMediaId" class="input min-w-[260px]">
          <option value="">全部文件</option>
          <option v-for="media in mediaOptions" :key="media.id" :value="media.id">
            {{ media.name }}（{{ media.type === 'video' ? '视频' : '图片' }}）
          </option>
        </select>
        <button class="btn-secondary" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="errorMessage" class="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-xs text-red-300">
      {{ errorMessage }}
    </div>

    <div class="panel min-h-0 flex-1 overflow-auto">
      <table class="w-full border-collapse text-left text-xs">
        <thead class="sticky top-0 bg-slate-900">
          <tr class="border-b border-slate-800 text-slate-400">
            <th class="px-4 py-3">文件</th>
            <th class="px-4 py-3">类型</th>
            <th class="px-4 py-3">第几帧</th>
            <th class="px-4 py-3">该帧人工标注对象</th>
            <th class="px-4 py-3">标注人</th>
            <th class="px-4 py-3">保存时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in groupedRows" :key="row.key" class="border-b border-slate-900 align-top hover:bg-slate-900/40">
            <td class="px-4 py-3 font-medium">{{ row.mediaName }}</td>
            <td class="px-4 py-3">{{ row.mediaType === 'video' ? '视频' : '图片' }}</td>
            <td class="px-4 py-3 font-mono">{{ row.frameIndex ?? '-' }}</td>
            <td class="px-4 py-3">
              <div class="flex flex-wrap gap-1.5">
                <span v-for="name in row.objects" :key="name" class="rounded bg-indigo-500/15 px-2 py-1 text-indigo-300">{{ name }}</span>
                <span v-if="!row.objects.length" class="text-slate-600">-</span>
              </div>
              <div v-if="row.timestampMs !== null" class="mt-1 text-[10px] text-slate-600">视频时间 {{ formatTimestamp(row.timestampMs) }}</div>
            </td>
            <td class="px-4 py-3">
              <span class="rounded bg-slate-800 px-2 py-1 text-slate-300">{{ row.username }}</span>
            </td>
            <td class="px-4 py-3 text-slate-500">{{ formatSavedAt(row.savedAt) }}</td>
          </tr>
          <tr v-if="loading">
            <td colspan="6" class="px-4 py-16 text-center text-slate-500">正在读取数据库记录…</td>
          </tr>
          <tr v-else-if="!groupedRows.length">
            <td colspan="6" class="px-4 py-16 text-center text-slate-600">当前筛选条件下暂无人工标注记录</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
