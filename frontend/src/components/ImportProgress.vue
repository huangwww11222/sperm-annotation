<script setup lang="ts">
// Progress and the confirmation dialog of the batch import; both libraries show the same one queue.
import { ref } from 'vue'
import { clearImportReport, importCounts, importCurrent, importItems, importPlan, importRunning, retryFailedImports, startImport } from '../stores/importQueue'

const reportOpen = ref(false)
const stateLabel: Record<string, string> = { waiting: '等待', uploading: '上传中', done: '成功', exists: '已存在', failed: '失败' }
</script>

<template>
  <div v-if="importItems.length" class="library-import" data-testid="import-progress">
    <div class="library-import-head">
      <strong>{{ importRunning ? `导入中 ${importCounts.total - importCounts.left} / ${importCounts.total}` : '导入完成' }}</strong>
      <span>成功 {{ importCounts.done }} · 已存在 {{ importCounts.exists }} · 失败 {{ importCounts.failed }}</span>
    </div>
    <template v-if="importCurrent"><small class="library-import-file" :title="importCurrent.name">{{ importCurrent.name }}</small><progress :max="1" :value="importCurrent.progress" /></template>
    <div class="library-import-actions">
      <button class="quiet-button" data-testid="import-report-toggle" @click="reportOpen = !reportOpen">{{ reportOpen ? '收起明细' : '查看明细' }}</button>
      <button v-if="importCounts.failed && !importRunning" class="quiet-button" data-testid="import-retry" @click="retryFailedImports">重试失败项</button>
      <button v-if="!importRunning" class="quiet-button" @click="clearImportReport(); reportOpen = false">清除</button>
    </div>
    <ul v-if="reportOpen" class="library-import-report" data-testid="import-report">
      <li v-for="item in importItems" :key="item.id" :class="item.state"><b>{{ stateLabel[item.state] }}</b> <span :title="item.folderPath">{{ item.name }}</span><small v-if="item.message"> {{ item.message }}</small></li>
    </ul>
  </div>
  <Teleport to="body">
    <div v-if="importPlan" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" @keydown.esc.prevent="importPlan = null">
      <section class="app-dialog w-[520px]" role="dialog" aria-modal="true" aria-label="确认导入" data-testid="import-confirm">
        <h2>{{ importPlan.flow ? '导入到微流控' : '确认导入' }}</h2>
        <p data-testid="import-summary">视频 {{ importPlan.items.filter(item => item.kind === 'video').length }} 段<template v-if="!importPlan.flow || importPlan.items.some(item => item.kind === 'package')"> · 标注包 {{ importPlan.items.filter(item => item.kind === 'package').length }} 个</template> · 跳过其他文件 {{ importPlan.skipped.length }} 个</p>
        <p v-if="importPlan.tooLarge.length" role="alert" class="text-[var(--danger)]">{{ importPlan.tooLarge.length }} 个文件超过大小上限，不会上传。</p>
        <label class="block my-3">放到文件夹（留空为未分类）<input v-model="importPlan.folder" class="input w-full" data-testid="import-folder-name" /></label>
        <label v-if="importPlan.hasSubfolders" class="flex gap-2 my-2"><input v-model="importPlan.keepSubfolders" type="checkbox" />保留子文件夹结构</label>
        <label v-if="importPlan.existing.length" class="flex gap-2 my-2"><input v-model="importPlan.uploadExisting" type="checkbox" data-testid="import-upload-existing" />{{ importPlan.existing.length }} 段服务器已有同名同大小的视频，默认跳过；仍然上传并比对内容</label>
        <p class="review-note">{{ importPlan.flow && importPlan.items.every(item => item.kind === 'package') ? '别人的判定一起导入，可以直接导出，也可以复查。不会覆盖已有视频。' : importPlan.flow ? '同一份样本放同一个文件夹，导出时不会被拆到训练和验证两边。导入后自动找目标。' : '批量导入不会覆盖已有视频。上传时可以继续标注；关闭页面会中断，重新选择同一文件夹只传缺的。' }}</p>
        <div class="mt-5 flex justify-end gap-3"><button class="btn-secondary" @click="importPlan = null">取消</button><button class="btn-primary" data-testid="import-start" :disabled="!importPlan.items.length" @click="startImport">开始导入</button></div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.library-import { animation: rise-in var(--dur-base) var(--ease-out); margin: 0 12px 8px; padding: 8px; display: flex; flex-direction: column; gap: 4px; background: var(--surface-subtle); border: 1px solid var(--line); border-radius: var(--radius-sm, 5px); font-size: 12px; }
.library-import-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 2px 8px; }
.library-import-head span, .library-import-file { color: var(--muted); }
.library-import-file { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.library-import progress { width: 100%; }
.library-import-actions { display: flex; flex-wrap: wrap; gap: 4px; }
.library-import-report { max-height: 180px; overflow-y: auto; margin: 0; padding: 0; list-style: none; overflow-wrap: anywhere; }
.library-import-report li { padding: 3px 0; border-top: 1px solid var(--line); }
.library-import-report li.failed b { color: var(--danger); }
.library-import-report li.done b { color: var(--success); }
.library-import-report small { color: var(--muted); }
</style>
