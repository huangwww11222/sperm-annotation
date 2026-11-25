<script setup lang="ts">
// Videos of the wet-mount list to judge as microfluidic ones instead.
import { computed, ref } from 'vue'
import type { FlowFailure } from '../api/flowApi'
import { flowApi } from '../api/flowApi'
import { useFlow } from '../stores/flow'
import { useWorkspace } from '../stores/workspace'

const emit = defineEmits<{ close: [] }>()
const flow = useFlow()
const { mediaAssets, refreshLibrary } = useWorkspace()
const chosen = ref<string[]>([]), search = ref(''), busy = ref(false), error = ref('')
const videos = computed(() => {
  const term = search.value.trim().toLowerCase()
  return mediaAssets.value.filter(media => media.serverMediaId && media.type === 'video' && (!term || `${media.folderPath ?? ''}/${media.name}`.toLowerCase().includes(term)))
})
const all = computed(() => videos.value.length > 0 && videos.value.every(media => chosen.value.includes(media.serverMediaId!)))
const toggleAll = () => { const ids = videos.value.map(media => media.serverMediaId!); chosen.value = all.value ? chosen.value.filter(id => !ids.includes(id)) : [...new Set([...chosen.value, ...ids])] }
async function move() {
  busy.value = true; error.value = ''
  try {
    // One request per folder: the folder is the sample the video belongs to.
    const groups = new Map<string, string[]>()
    for (const id of chosen.value) {
      const folder = mediaAssets.value.find(media => media.serverMediaId === id)?.folderPath ?? ''
      groups.set(folder, [...(groups.get(folder) ?? []), id])
    }
    for (const [folder, ids] of groups) await flowApi.add(ids, folder)
    emit('close')
  } catch (e) { error.value = (e as FlowFailure).message || '操作失败，请重试' } finally { busy.value = false; await flow.load(); await refreshLibrary() }
}
</script>

<template>
  <Teleport to="body">
    <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" @keydown.esc.prevent="!busy && emit('close')">
      <section class="app-dialog flow-dialog" role="dialog" aria-modal="true" aria-label="从标注列表移入" data-testid="flow-move">
        <h2>从标注列表移入</h2>
        <p v-if="error" role="alert" class="text-[var(--danger)]">{{ error }}</p>
        <input v-model="search" class="input" placeholder="查找视频…" aria-label="查找视频" />
        <label class="flow-pick flow-pick-all"><input type="checkbox" :checked="all" @change="toggleAll" />全选（{{ videos.length }} 段）</label>
        <div class="flow-pick-list">
          <label v-for="media in videos" :key="media.id" class="flow-pick"><input v-model="chosen" type="checkbox" :value="media.serverMediaId" /><span>{{ media.name }}</span><em>{{ media.folderPath || '未分类' }}</em></label>
          <p v-if="!videos.length" class="sidebar-empty">没有可移入的视频</p>
        </div>
        <p class="review-note">已经人工标注或审查过的视频不能移。</p>
        <div class="mt-4 flex justify-end gap-3"><button class="btn-secondary" :disabled="busy" @click="emit('close')">取消</button><button class="btn-primary" data-testid="flow-move-confirm" :disabled="busy || !chosen.length" @click="move">移入 {{ chosen.length }} 段</button></div>
      </section>
    </div>
  </Teleport>
</template>
