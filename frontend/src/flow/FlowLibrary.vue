<script setup lang="ts">
// Microfluidic videos: what each one needs next. Nothing here changes an answer.
import { computed, onMounted, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import ImportProgress from '../components/ImportProgress.vue'
import { prepareImport } from '../stores/importQueue'
import { useFlow } from '../stores/flow'
import { byFolder, donePart, stateText, type FlowItem } from './flowLogic'

const emit = defineEmits<{ open: [item: FlowItem]; move: [] }>()
const flow = useFlow()
const { items, listReady, listError, currentId } = flow
const search = ref(''), todoOnly = ref(false)
const visible = computed(() => {
  const term = search.value.trim().toLowerCase()
  return items.value.filter(item => (!term || `${item.folderPath}/${item.videoName}`.toLowerCase().includes(term)) && (!todoOnly.value || item.state !== 'done'))
})
const groups = computed(() => byFolder(visible.value))
const done = computed(() => items.value.filter(item => item.state === 'done').length)
const videoInput = ref<HTMLInputElement | null>(null), folderInput = ref<HTMLInputElement | null>(null), packageInput = ref<HTMLInputElement | null>(null)
onMounted(() => { if (folderInput.value) folderInput.value.webkitdirectory = true })
function pickedFiles(event: Event) { const input = event.target as HTMLInputElement; prepareImport(input.files, true); input.value = '' }
const judged = (item: FlowItem) => !!item.progress && (item.progress.pagesDone > 0 || item.progress.secondDone > 0 || item.progress.scanDone)
</script>

<template>
  <div class="asset-panel annot-assets flow-library">
    <div class="asset-actions flow-actions">
      <button class="btn-primary" data-testid="flow-import" @click="videoInput?.click()"><AppIcon name="upload" :size="15" />导入视频</button>
      <button class="btn-secondary" title="选择一个文件夹，导入里面的全部视频" @click="folderInput?.click()">文件夹</button>
      <button class="btn-secondary" title="导入别人从微流控页导出的 ZIP（导出时要勾选“包含原视频”）" data-testid="flow-import-package" @click="packageInput?.click()">标注包</button>
      <button class="btn-secondary" data-testid="flow-move-in" title="把标注列表里的视频改成按微流控判" @click="emit('move')">从标注列表移入</button>
      <input ref="videoInput" type="file" accept="video/*" multiple hidden data-testid="flow-video-input" @change="pickedFiles" />
      <input ref="packageInput" type="file" accept=".zip" multiple hidden data-testid="flow-package-input" @change="pickedFiles" />
      <input ref="folderInput" type="file" hidden data-testid="flow-folder-input" @change="pickedFiles" />
    </div>
    <ImportProgress />
    <div class="asset-search"><input v-model="search" class="input" placeholder="查找视频…" aria-label="查找视频" /></div>
    <div class="flow-filter"><span>判完 {{ done }} / {{ items.length }}</span><label><input v-model="todoOnly" type="checkbox" />只看没判完的</label></div>
    <p v-if="listError" class="flow-list-error" role="alert">{{ listError }} <button class="quiet-button" @click="flow.load">重试</button></p>
    <div class="asset-list" data-testid="flow-list">
      <template v-for="group in groups" :key="group.folder">
        <p v-if="group.folder || groups.length > 1" class="flow-folder" :title="group.folder">{{ group.folder || '未分类' }}</p>
        <div v-for="item in group.items" :key="item.mediaId" class="flow-item" :class="{ selected: item.mediaId === currentId }" role="button" tabindex="0" :data-media-id="item.mediaId" data-testid="flow-item" @click="emit('open', item)" @keydown.enter="emit('open', item)" @keydown.space.prevent.stop="emit('open', item)">
          <strong :title="item.videoName">{{ item.videoName }}</strong>
          <small><b :class="`tone-${stateText(item).tone}`" data-testid="flow-state">{{ stateText(item).text }}</b><template v-if="item.imported"> · 导入的</template><template v-if="item.progress?.lastActorName"> · {{ item.progress.lastActorName }}</template></small>
          <i v-if="item.state !== 'done' && item.state !== 'new' && item.state !== 'failed'" class="flow-item-bar" :class="{ busy: item.state === 'finding' }" aria-hidden="true"><em :style="{ width: `${Math.round(donePart(item) * 100)}%` }" /></i>
          <span class="flow-item-tools">
            <button v-if="!judged(item) && item.state !== 'finding'" class="icon-button" title="移回标注列表" :aria-label="`把 ${item.videoName} 移回标注列表`" @click.stop="flow.moveBack(item)" @keydown.enter.stop @keydown.space.stop><AppIcon name="undo" :size="14" /></button>
            <button v-if="item.state !== 'finding'" class="icon-button" title="删除视频" :aria-label="`删除 ${item.videoName}`" @click.stop="flow.removeVideo(item)" @keydown.enter.stop @keydown.space.stop><AppIcon name="trash" :size="14" /></button>
          </span>
        </div>
      </template>
      <p v-if="listReady && !visible.length" class="sidebar-empty">{{ items.length ? '没有匹配的视频' : '导入微流控视频开始' }}</p>
    </div>
  </div>
</template>
