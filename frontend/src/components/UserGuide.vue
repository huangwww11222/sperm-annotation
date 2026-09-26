<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import guide from '../help/user-guide.md?raw'

const props = defineProps<{ page: string }>()
const dialog = ref<HTMLDialogElement | null>(null), search = ref(''), selected = ref('开始使用')
const content = ref<HTMLElement | null>(null)
let priorOverflow = '', opened = false
const sections = guide.split(/^## /m).slice(1).map(part => {
  const [title, ...lines] = part.trim().split('\n')
  const blocks = lines.join('\n').trim().split(/\n\s*\n/).map(text => {
    const rows = text.split('\n')
    if (rows[0]?.startsWith('|')) return { type: 'table', rows: rows.filter((_, i) => i !== 1).map(row => row.split('|').slice(1, -1).map(cell => cell.trim())), text, items: [] }
    const type = /^\d+\. /.test(text) ? 'ol' : text.startsWith('- ') ? 'ul' : 'p'
    return { type, rows: [], text, items: rows.map(row => row.replace(/^(\d+\. |- )/, '')) }
  })
  return { title: title!, text: part, blocks }
})
const matches = computed(() => sections.filter(s => s.text.toLowerCase().includes(search.value.trim().toLowerCase())))
const current = computed(() => matches.value.find(s => s.title === selected.value) || matches.value[0])
async function open() {
  selected.value = ({ '/annotate': '人工标注', '/review': '审查模式', '/confirm': '对比确认' } as Record<string,string>)[props.page] || '开始使用'
  search.value = ''
  await nextTick()
  priorOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'; opened = true
  dialog.value?.showModal()
  content.value?.scrollTo(0, 0)
}
function restore() { if (opened) { document.body.style.overflow = priorOverflow; opened = false } }
function close() { dialog.value?.close(); restore() }
function keydown(event: KeyboardEvent) {
  if (event.key !== 'Tab') return
  const nodes = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),input,[tabindex="0"]') || [])
  const first = nodes[0], last = nodes[nodes.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
function select(title: string) { selected.value = title; content.value?.scrollTo(0, 0) }
function download() {
  const url = URL.createObjectURL(new Blob([guide], {type:'text/markdown;charset=utf-8'}))
  const link = document.createElement('a'); link.href = url; link.download = '微流控标注工作台-使用说明.md'; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
onUnmounted(restore)
</script>

<template>
  <button class="quiet-button user-guide-launcher" @click="open" aria-haspopup="dialog"><AppIcon name="help" :size="16" />使用说明</button>
  <Teleport to="body">
    <dialog ref="dialog" class="user-guide" data-user-guide aria-labelledby="user-guide-title" @cancel.prevent="close" @close="restore" @keydown.stop="keydown">
      <header class="user-guide-header"><div><h2 id="user-guide-title">使用说明</h2><p>从标注到训练集，按步骤完成</p></div><button class="icon-button" aria-label="关闭使用说明" @click="close"><AppIcon name="close" /></button></header>
      <div class="user-guide-layout">
        <aside class="user-guide-nav"><input v-model="search" class="input" type="search" aria-label="搜索使用说明" placeholder="搜索流程、保存、快捷键…" @input="content?.scrollTo(0,0)"/><nav aria-label="说明目录"><button v-for="section in matches" :key="section.title" :aria-current="current?.title===section.title?'page':undefined" @click="select(section.title)">{{ section.title }}</button></nav><p v-if="!matches.length" role="status">没有匹配内容，请换个关键词。</p></aside>
        <article :key="current?.title || 'empty'" ref="content" class="user-guide-content" tabindex="0" aria-label="说明正文">
          <template v-if="current"><h3>{{ current.title }}</h3><template v-for="(block,i) in current.blocks" :key="current.title+i">
            <table v-if="block.type==='table'"><thead><tr><th v-for="(cell,j) in block.rows[0]" :key="j">{{ cell }}</th></tr></thead><tbody><tr v-for="(row,j) in block.rows.slice(1)" :key="j"><td v-for="(cell,k) in row" :key="k">{{ cell }}</td></tr></tbody></table>
            <ol v-else-if="block.type==='ol'"><li v-for="(item,j) in block.items" :key="j">{{ item }}</li></ol>
            <ul v-else-if="block.type==='ul'"><li v-for="(item,j) in block.items" :key="j">{{ item }}</li></ul>
            <p v-else>{{ block.text }}</p>
          </template></template><p v-else>试试搜索“草稿”“只读”或“导出”。</p>
        </article>
      </div>
      <footer class="user-guide-footer"><span>可下载完整说明，离线阅读或发给同事。</span><button class="btn-secondary" @click="download">下载完整说明（Markdown）</button><button class="btn-primary" @click="close">返回工作台</button></footer>
    </dialog>
  </Teleport>
</template>

<style scoped>
.user-guide{width:min(940px,calc(100vw - 48px));max-width:none;height:min(720px,calc(100dvh - 64px));max-height:none;margin:auto;padding:0;border:1px solid var(--line);border-radius:16px;background:var(--surface);color:var(--text);box-shadow:0 24px 100px #0005;overflow:hidden}
.user-guide[open]{display:flex;flex-direction:column}.user-guide::backdrop{background:#0c203588;backdrop-filter:blur(3px)}
.user-guide-header{display:flex;justify-content:space-between;align-items:center;padding:20px 24px;border-bottom:1px solid var(--line);flex-shrink:0}.user-guide-header h2{font-size:20px;font-weight:700}.user-guide-header p{font-size:12px;color:var(--muted);margin-top:5px}
.user-guide-layout{display:grid;grid-template-columns:216px minmax(0,1fr);min-height:0;flex:1}.user-guide-nav{padding:18px 14px;background:var(--surface-subtle);border-right:1px solid var(--line);overflow:auto}.user-guide-nav input{width:100%;font-size:11px}.user-guide-nav nav{display:flex;flex-direction:column;gap:5px;margin-top:16px}.user-guide-nav button{text-align:left;padding:10px;border-radius:7px;font-size:13px}.user-guide-nav button:hover{background:var(--surface-hover)}.user-guide-nav button[aria-current]{background:var(--accent-soft);color:var(--accent);font-weight:600}.user-guide-nav p{font-size:12px;line-height:1.7;margin-top:16px;color:var(--muted)}
.user-guide-content{padding:24px 28px;overflow:auto;font-size:13px;line-height:1.85;overscroll-behavior:contain}.user-guide-content h3{font-size:21px;font-weight:650;margin-bottom:18px}.user-guide-content p,.user-guide-content ol,.user-guide-content ul,.user-guide-content table{margin-bottom:18px}.user-guide-content ol{list-style:decimal;padding-left:23px}.user-guide-content ul{list-style:disc;padding-left:20px}.user-guide-content li{margin:8px 0;padding-left:3px}.user-guide-content li::marker{color:var(--accent);font-weight:650}.user-guide-content table{border-collapse:collapse;width:100%;font-size:12px}.user-guide-content th,.user-guide-content td{padding:7px 9px;border:1px solid var(--line);text-align:left}.user-guide-content th{background:var(--surface-subtle)}
.user-guide-footer{display:flex;gap:12px;align-items:center;justify-content:flex-end;padding:15px 20px;border-top:1px solid var(--line);flex-shrink:0}.user-guide-footer span{margin-right:auto;font-size:11px;color:var(--muted)}
@media(max-width:950px){.user-guide-launcher{font-size:11px;padding:6px}.user-guide-launcher svg{display:none}.user-guide-layout{grid-template-columns:185px minmax(0,1fr)}.user-guide-content{padding:20px}.user-guide-footer span{display:none}}
</style>
