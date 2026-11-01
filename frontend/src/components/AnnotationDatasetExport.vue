<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { addLeaveGuard } from '../router'
import { useWorkspace } from '../stores/workspace'
import { useAuth } from '../stores/auth'
import { createRequestId } from '../utils/browserCompat'
import { annotationReviewApi, type CompletionPreview } from '../api/annotationReviewApi'
import { trainingExportApi } from '../api/trainingExportApi'
import { useAnnotationReview } from '../stores/annotationReview'
import TrainingDatasetExport from './TrainingDatasetExport.vue'
const {selectedMedia,currentMediaId,persistWorkspaceState,isAiBusy,editingBlocked,submissionLocks}=useWorkspace()
const review=useAnnotationReview()
// Only reviewed content can become training data; the server enforces the same rule.
const reviewPassed=computed(()=>review.status.value==='passed'&&review.state.value?.mediaId===selectedMedia.value?.serverMediaId)
const dialog=ref<HTMLElement|null>(null)
let priorFocus:HTMLElement|null=null

const exportOpen=ref(false),exportBusy=ref(false)
const visible=ref(false),busy=ref(false),error=ref(''),confirmed=ref(false),emptyRanges=ref(''),preview=ref<CompletionPreview|null>(null),snapshot=ref('')
watch(()=>visible.value,async value=>{if(value){priorFocus=document.activeElement as HTMLElement;await nextTick();dialog.value?.querySelector<HTMLElement>("button:not(:disabled),input:not(:disabled)")?.focus()}else priorFocus?.focus()})
const exporter=ref<InstanceType<typeof TrainingDatasetExport>|null>(null)
const pending=ref<{mediaId:string;key:string;body:unknown}|null>(null)
const lock=createRequestId()
const journal=()=>`annotation-export-snapshot:${useAuth().user.value?.id}:${selectedMedia.value?.serverMediaId}`
function remember(){try{if(pending.value)sessionStorage.setItem(journal(),JSON.stringify(pending.value));else sessionStorage.removeItem(journal())}catch(e){console.warn('[dataset.snapshot_journal_failed]',e)}}
watch([visible,busy,pending,exportOpen,exportBusy],()=>{if(visible.value||busy.value||pending.value||exportOpen.value||exportBusy.value)submissionLocks.value.add(lock);else submissionLocks.value.delete(lock)})
watch(()=>selectedMedia.value?.serverMediaId,()=>{visible.value=false;snapshot.value='';pending.value=null;try{pending.value=JSON.parse(sessionStorage.getItem(journal())||'null')}catch{}},{immediate:true})
const removeGuard=addLeaveGuard(async()=>!busy.value&&!pending.value&&!exportBusy.value)
function beforeUnload(e:BeforeUnloadEvent){if(busy.value||pending.value||exportBusy.value){e.preventDefault();e.returnValue=''}}
window.addEventListener('beforeunload',beforeUnload)
onUnmounted(()=>{removeGuard();submissionLocks.value.delete(lock);window.removeEventListener('beforeunload',beforeUnload)})
async function open(){
  if(pending.value){await freeze();return}
  visible.value=false;busy.value=true;error.value='';confirmed.value=false;emptyRanges.value='';preview.value=null
  try{await persistWorkspaceState(currentMediaId.value,true)
    await review.load(selectedMedia.value!.serverMediaId!)
    if(!reviewPassed.value)throw new Error(review.status.value==='stale'?'审查通过后标注又有改动，请重新审查后再导出。':'该视频还没有审查通过，请先完成审查再导出。')
    preview.value=await annotationReviewApi.exportPreview(selectedMedia.value!.serverMediaId!)
    let prior=null;try{prior=JSON.parse(sessionStorage.getItem(journal()+':snapshot')||'null')}catch{}
    if(prior?.revision===preview.value.sourceRevision){snapshot.value=prior.id;visible.value=false;await nextTick();await exporter.value?.open()}
    else if(preview.value.unknownFrames===0){confirmed.value=true;await freeze()}
    else visible.value=true
  }
  catch(e:any){visible.value=true;error.value=e.message||'标注读取失败';console.error('[dataset.annotation_preview_failed]',e)}finally{busy.value=false}
}
function ranges(){return emptyRanges.value.split(/[,，\s]+/).filter(Boolean).map(s=>{if(!/^\d+(-\d+)?$/.test(s))throw Error('空帧格式例如 3, 8-10');const [a,b=a]=s.split('-').map(Number);if(a<1||b<a||b>preview.value!.frameCount)throw Error('空帧范围无效');return {start:a-1,end:b-1}})}
async function freeze(){
  busy.value=true;error.value=''
  try{
    if(!pending.value){pending.value={mediaId:selectedMedia.value!.serverMediaId!,key:createRequestId(),body:{expectedSourceRevision:preview.value!.sourceRevision,confirmComplete:confirmed.value,explicitEmptyFrameRanges:ranges()}};remember()}
    const op=pending.value,r=await trainingExportApi.annotationSnapshot(op.mediaId,op.body,op.key)
    snapshot.value=r.finalVersionId
    try{sessionStorage.setItem(journal()+':snapshot',JSON.stringify({id:r.finalVersionId,revision:(op.body as {expectedSourceRevision:string}).expectedSourceRevision}))}catch{}
    pending.value=null;remember();visible.value=false
    await nextTick();await exporter.value?.open()
  }catch(e:any){visible.value=true;error.value=e.message||'导出准备失败';console.error('[dataset.annotation_snapshot_failed]',e);if(e.status&&e.status<500&&e.code!=='SOURCE_BUSY'){pending.value=null;remember()}}
  finally{busy.value=false}
}
function keys(e:KeyboardEvent){if(e.key==='Escape'&&!busy.value&&!pending.value)visible.value=false;if(e.key==='Tab'){const nodes=Array.from((e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)'));if(nodes.length){e.preventDefault();const i=nodes.indexOf(document.activeElement as HTMLElement);nodes[(i+(e.shiftKey?-1:1)+nodes.length)%nodes.length]?.focus()}}}
</script>
<template>
  <button class="btn-primary" data-testid="annotation-export" :title="reviewPassed||pending?'':'审查通过后才能导出训练数据集'" :disabled="busy||isAiBusy||(!pending&&(editingBlocked||!reviewPassed))||!selectedMedia?.serverMediaId||selectedMedia.type!=='video'" @click="open">{{ pending?'恢复数据集导出':'导出训练数据集' }}</button>
  <TrainingDatasetExport v-if="snapshot" hide-trigger annotation-only ref="exporter" @open="exportOpen=$event" @busy="exportBusy=$event" :final-version-id="snapshot" :media-name="selectedMedia?.name||'视频'" />
  <Teleport to="body"><div v-if="visible" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" @keydown="keys">
    <section ref="dialog" role="dialog" aria-modal="true" aria-label="导出需要处理" class="app-dialog w-[560px]">
      <h2>导出需要处理</h2>
      <p v-if="busy">正在保存并检查…</p><p v-if="error" role="alert" class="text-[var(--danger)]">{{ error }}</p>
      <template v-if="preview && preview.unknownFrames"><p>共 {{ preview.frameCount }} 帧 · 有对象 {{ preview.objectFrames }} 帧 · 明确空帧 {{ preview.emptyFrames }} 帧 · 未知 {{ preview.unknownFrames }} 帧</p>
        <label v-if="preview.unknownFrames" class="block my-3">已人工确认无对象的帧（其余未知帧须补标）<input v-model="emptyRanges" class="input w-full" placeholder="例如 3, 8-10" :disabled="busy||!!pending" /></label>
        <label class="flex gap-2 my-3"><input v-model="confirmed" type="checkbox" :disabled="busy||!!pending" />以上帧确实没有目标。</label>
      </template>
      <div class="flex justify-end gap-3 mt-4"><button class="btn-secondary" :disabled="busy||!!pending" @click="visible=false">关闭</button><button v-if="!pending" class="btn-secondary" :disabled="busy" @click="open">重新读取</button><button class="btn-primary" :disabled="busy||(!pending&&(!preview||!confirmed))" @click="freeze">{{ pending?'重试原请求':'继续导出' }}</button></div>
    </section>
  </div></Teleport>
</template>
