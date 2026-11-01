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
    // Indented lines continue the previous list item instead of becoming items.
    const items: string[] = []
    for (const row of rows) {
      if (type !== 'p' && items.length && /^\s+\S/.test(row) && !/^\s*(\d+\. |- )/.test(row)) items[items.length - 1] += '\n' + row.trim()
      else items.push(row.replace(/^(\d+\. |- )/, ''))
    }
    return { type, rows: [], text, items }
  })
  return { title: title!.trim(), text: part, blocks }
})
// Inline Markdown (**bold**, `code`) as text segments, so content is never injected as HTML.
const inline = (text: string) => text.split(/(\*\*[^*]+\*\*|`[^`]+`)/).filter(Boolean).map(part =>
  part.startsWith('**') && part.endsWith('**') && part.length > 4 ? { kind: 'b', text: part.slice(2, -2) }
    : part.startsWith('`') && part.endsWith('`') && part.length > 2 ? { kind: 'code', text: part.slice(1, -1) }
    : { kind: 'text', text: part })
const matches = computed(() => sections.filter(s => s.text.toLowerCase().includes(search.value.trim().toLowerCase())))
const current = computed(() => matches.value.find(s => s.title === selected.value) || matches.value[0])
async function open() {
  selected.value = ({ '/annotate': '人工标注', '/review': '审查', '/results': '标注记录与统计导出' } as Record<string,string>)[props.page] || '开始使用'
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
  <button class="quiet-button user-guide-launcher" aria-label="使用说明" title="使用说明" @click="open" aria-haspopup="dialog"><AppIcon name="help" :size="16" /><span class="user-guide-label">使用说明</span></button>
  <Teleport to="body">
    <dialog ref="dialog" class="user-guide" data-user-guide aria-labelledby="user-guide-title" @cancel.prevent="close" @close="restore" @keydown.stop="keydown">
      <header class="user-guide-header"><div><h2 id="user-guide-title">使用说明</h2><p>从标注到训练集，按步骤完成</p></div><button class="icon-button" aria-label="关闭使用说明" @click="close"><AppIcon name="close" /></button></header>
      <div class="user-guide-layout">
        <aside class="user-guide-nav"><input v-model="search" class="input" type="search" aria-label="搜索使用说明" placeholder="搜索流程、保存、快捷键…" @input="content?.scrollTo(0,0)"/><nav aria-label="说明目录"><button v-for="section in matches" :key="section.title" :aria-current="current?.title===section.title?'page':undefined" @click="select(section.title)">{{ section.title }}</button></nav><p v-if="!matches.length" role="status">没有匹配内容，请换个关键词。</p></aside>
        <article :key="current?.title || 'empty'" ref="content" class="user-guide-content" tabindex="0" aria-label="说明正文">
          <template v-if="current"><h3>{{ current.title }}</h3><template v-for="(block,i) in current.blocks" :key="current.title+i">
            <table v-if="block.type==='table'"><thead><tr><th v-for="(cell,j) in block.rows[0]" :key="j"><template v-for="(seg,n) in inline(cell)" :key="n"><strong v-if="seg.kind==='b'">{{ seg.text }}</strong><code v-else-if="seg.kind==='code'">{{ seg.text }}</code><template v-else>{{ seg.text }}</template></template></th></tr></thead><tbody><tr v-for="(row,j) in block.rows.slice(1)" :key="j"><td v-for="(cell,k) in row" :key="k"><template v-for="(seg,n) in inline(cell)" :key="n"><strong v-if="seg.kind==='b'">{{ seg.text }}</strong><code v-else-if="seg.kind==='code'">{{ seg.text }}</code><template v-else>{{ seg.text }}</template></template></td></tr></tbody></table>
            <ol v-else-if="block.type==='ol'"><li v-for="(item,j) in block.items" :key="j"><template v-for="(seg,n) in inline(item)" :key="n"><strong v-if="seg.kind==='b'">{{ seg.text }}</strong><code v-else-if="seg.kind==='code'">{{ seg.text }}</code><template v-else>{{ seg.text }}</template></template></li></ol>
            <ul v-else-if="block.type==='ul'"><li v-for="(item,j) in block.items" :key="j"><template v-for="(seg,n) in inline(item)" :key="n"><strong v-if="seg.kind==='b'">{{ seg.text }}</strong><code v-else-if="seg.kind==='code'">{{ seg.text }}</code><template v-else>{{ seg.text }}</template></template></li></ul>
            <p v-else><template v-for="(seg,n) in inline(block.text)" :key="n"><strong v-if="seg.kind==='b'">{{ seg.text }}</strong><code v-else-if="seg.kind==='code'">{{ seg.text }}</code><template v-else>{{ seg.text }}</template></template></p>
          </template></template><p v-else>试试搜索“草稿”“只读”或“导出”。</p>
        </article>
      </div>
      <footer class="user-guide-footer"><span>可下载完整说明，离线阅读或发给同事。</span><button class="btn-secondary" @click="download">下载完整说明（Markdown）</button><button class="btn-primary" @click="close">返回工作台</button></footer>
    </dialog>
  </Teleport>
</template>

<style scoped>
.user-guide{width:min(940px,calc(100vw - 48px));max-width:none;height:min(720px,calc(100vh - 64px));height:min(720px,calc(100dvh - 64px));max-height:none;margin:auto;padding:0;border:1px solid var(--line);border-radius:var(--radius-lg);background:var(--surface);color:var(--text);box-shadow:var(--shadow-lg);overflow:hidden}
.user-guide[open]{display:flex;flex-direction:column}
.user-guide-header{display:flex;justify-content:space-between;align-items:center;padding:20px 24px;border-bottom:1px solid var(--line);flex-shrink:0}.user-guide-header h2{font-size:20px;font-weight:700;letter-spacing:.02em}.user-guide-header p{font-size:12px;color:var(--muted);margin-top:5px}
.user-guide-layout{display:grid;grid-template-columns:216px minmax(0,1fr);min-height:0;flex:1}.user-guide-nav{padding:18px 14px;background:var(--surface-subtle);border-right:1px solid var(--line);overflow:auto}.user-guide-nav input{width:100%;font-size:12px}.user-guide-nav nav{display:flex;flex-direction:column;gap:5px;margin-top:16px}.user-guide-nav button{position:relative;text-align:left;padding:9px 10px 9px 12px;border-radius:7px;font-size:13px;color:var(--muted)}.user-guide-nav button:hover{background:var(--surface-hover);color:var(--text)}.user-guide-nav button[aria-current]{background:var(--surface);color:var(--accent);font-weight:600;box-shadow:var(--shadow-sm)}.user-guide-nav button[aria-current]::before{content:'';position:absolute;left:0;top:9px;bottom:9px;width:3px;border-radius:2px;background:var(--accent)}.user-guide-nav p{font-size:12px;line-height:1.7;margin-top:16px;color:var(--muted)}
.user-guide-content{padding:26px 32px;overflow:auto;font-size:13.5px;line-height:1.9;overscroll-behavior:contain;animation:fade-in var(--dur-base) var(--ease-standard)}.user-guide-content>*{max-width:72ch}.user-guide-content li{white-space:pre-line}.user-guide-content strong{font-weight:650;color:var(--text)}.user-guide-content code{font-family:var(--font-mono);font-size:.9em;padding:1px 5px;border-radius:4px;background:var(--surface-subtle);border:1px solid var(--line)}.user-guide-content h3{font-size:21px;font-weight:700;letter-spacing:.02em;margin-bottom:18px;padding-bottom:12px;border-bottom:1px solid var(--line)}.user-guide-content p,.user-guide-content ol,.user-guide-content ul,.user-guide-content table{margin-bottom:18px}.user-guide-content ol{list-style:decimal;padding-left:23px}.user-guide-content ul{list-style:disc;padding-left:20px}.user-guide-content li{margin:8px 0;padding-left:3px}.user-guide-content li::marker{color:var(--accent);font-weight:650}.user-guide-content table{border-collapse:collapse;width:100%;font-size:12px}.user-guide-content th,.user-guide-content td{padding:7px 9px;border:1px solid var(--line);text-align:left}.user-guide-content th{background:var(--surface-subtle);font-weight:600}.user-guide-content tbody tr:hover{background:var(--surface-subtle)}
.user-guide-footer{display:flex;gap:12px;align-items:center;justify-content:flex-end;padding:15px 20px;border-top:1px solid var(--line);flex-shrink:0}.user-guide-footer span{margin-right:auto;font-size:11px;color:var(--muted)}
@media(max-width:1100px){.user-guide-launcher{width:32px;padding:7px}.user-guide-label{display:none}}
@media(max-width:950px){.user-guide-layout{grid-template-columns:185px minmax(0,1fr)}.user-guide-content{padding:20px}.user-guide-footer span{display:none}}
@media(max-width:600px){
  .user-guide{width:calc(100vw - 24px);height:calc(100vh - 24px);height:calc(100dvh - 24px)}
  .user-guide-header{padding:12px 14px}
  .user-guide-header h2{font-size:18px}
  .user-guide-layout{display:flex;flex-direction:column}
  .user-guide-nav{padding:10px 12px;flex-shrink:0;max-height:160px;border-right:0;border-bottom:1px solid var(--line)}
  .user-guide-nav nav{flex-direction:row;flex-wrap:wrap;gap:4px;margin-top:8px}
  .user-guide-nav button{font-size:12px;padding:7px 8px}
  .user-guide-content{padding:16px;min-height:0;flex:1}
  .user-guide-content h3{font-size:18px}
  .user-guide-content table{font-size:11px;table-layout:fixed;overflow-wrap:anywhere}
  .user-guide-content th,.user-guide-content td{padding:6px 5px}
  .user-guide-footer{padding:10px 12px;gap:8px;flex-wrap:wrap}
  .user-guide-footer button{font-size:12px;padding:7px 9px;flex:1}
}
</style>
