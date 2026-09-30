<script setup lang="ts">
import { createRequestId } from '../utils/browserCompat'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useWorkspace } from '../stores/workspace'
import { useAuth } from '../stores/auth'
import { addLeaveGuard } from '../router'
import { reviewWorkflowApi as api, type CompletionPreview, type Session } from '../api/reviewWorkflowApi'
const {selectedMedia,currentMediaId,isAiBusy,editingBlocked,persistWorkspaceState,submissionLocks}=useWorkspace()
const visible=ref(false),busy=ref(false),error=ref(''),success=ref(''),emptyRanges=ref(''),confirmed=ref(false),mode=ref<'send'|'withdraw'>('send')
const preview=ref<CompletionPreview|null>(null),submitted=ref<Session[]>([]),loading=ref(false),statusError=ref('')
const withdrawable=computed(()=>submitted.value.find(s=>s.permissions.canWithdraw))
const pending=ref<{kind:'send'|'withdraw';mediaId:string;sid?:string;revision?:number;body?:unknown;key:string}|null>(null)
const lockId=createRequestId(),dialog=ref<HTMLElement|null>(null)
watch([visible,busy,pending],()=>{if(visible.value||busy.value||pending.value)submissionLocks.value.add(lockId);else submissionLocks.value.delete(lockId)})
watch(visible,async value=>{if(value){await nextTick();dialog.value?.querySelector<HTMLElement>('button:not(:disabled)')?.focus()}})
function dialogKeys(e:KeyboardEvent){if(e.key==='Escape'&&!busy.value&&!pending.value)visible.value=false;if(e.key==='Tab'){const nodes=Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')||[]);if(nodes.length){e.preventDefault();const index=nodes.indexOf(document.activeElement as HTMLElement);nodes[(index+(e.shiftKey?-1:1)+nodes.length)%nodes.length]?.focus()}}}
function beforeUnload(e:BeforeUnloadEvent){if(busy.value||pending.value){e.preventDefault();e.returnValue=''}}
onMounted(()=>window.addEventListener('beforeunload',beforeUnload))
const journal=()=>`annotation-submission:${useAuth().user.value?.id}:${selectedMedia.value?.serverMediaId}`
let serial=0
function remember(){try{if(pending.value)sessionStorage.setItem(journal(),JSON.stringify(pending.value));else sessionStorage.removeItem(journal())}catch(e){console.warn('[review.submission_journal_failed]',e)}}
async function refresh(){
  const id=selectedMedia.value?.serverMediaId,n=++serial
  submitted.value=[];statusError.value=''
  if(!id)return
  loading.value=true
  try{const r=await api.submissionStatus(id);if(n===serial)submitted.value=r.items}
  catch(e:any){if(n===serial)statusError.value=e.message||'送审状态读取失败';console.error('[review.submission_status_failed]',e)}
  finally{if(n===serial)loading.value=false}
}
watch(()=>selectedMedia.value?.serverMediaId,async()=>{
  visible.value=false;pending.value=null
  try{pending.value=JSON.parse(sessionStorage.getItem(journal())||'null');if(pending.value){visible.value=true;mode.value=pending.value.kind==='withdraw'?'withdraw':'send';error.value='上次操作结果尚未确认，请重试原请求。'}}catch{}
  await refresh()
},{immediate:true})
async function open(){
  const id=selectedMedia.value?.serverMediaId
  if(!id)return
  mode.value='send';visible.value=true;busy.value=true;error.value='';success.value='';preview.value=null;confirmed.value=false;emptyRanges.value=''
  try{await persistWorkspaceState(currentMediaId.value,true);preview.value=await api.completionPreview(id)}
  catch(e:any){error.value=e.message||'送审预览失败';console.error('[review.completion_preview_failed]',e)}finally{busy.value=false}
}
function openWithdraw(){mode.value='withdraw';visible.value=true;success.value='';error.value=''}
function ranges(){
  if(!emptyRanges.value.trim())return []
  return emptyRanges.value.split(/[,，\s]+/).filter(Boolean).map(s=>{
    if(!/^\d+(-\d+)?$/.test(s))throw new Error('空帧格式应为 3, 8-10；界面帧号从 1 开始')
    const [a,b=a]=s.split('-').map(Number)
    if(a<1||b<a||b>preview.value!.frameCount)throw new Error('空帧范围无效')
    return {start:a-1,end:b-1}
  })
}
async function submit(){
  if(busy.value)return
  busy.value=true;error.value=''
  try{
    if(!pending.value){
      const mediaId=selectedMedia.value!.serverMediaId!
      if(mode.value==='withdraw'){
        if(!withdrawable.value)throw new Error('任务已开始审查或不属于当前送审者，不能撤回')
        pending.value={kind:'withdraw',mediaId,sid:withdrawable.value.id,revision:withdrawable.value.revision,key:createRequestId()}
      }else{
        pending.value={kind:'send',mediaId,key:createRequestId(),body:{expectedSourceRevision:preview.value!.sourceRevision,confirmComplete:confirmed.value,explicitEmptyFrameRanges:ranges()}}
      }
      remember()
    }
    const op=pending.value
    if(op.kind==='withdraw')await api.withdraw(op.sid!,op.revision!,op.key)
    else await api.completeAnnotation(op.mediaId,op.body,op.key)
    success.value=op.kind==='withdraw'?'已撤回送审。工作区标注保留，修改后可重新送审；旧 A 快照留作历史记录。':'已生成完整 A 快照和待审查任务。你可以在审查模式领取并检查，也可以由其他审查员领取。'
    pending.value=null;remember();await refresh()
  }catch(e:any){error.value=(e.message||'操作失败')+(e.requestId?`（记录号 ${e.requestId}）`:'');console.error('[review.submission_failed]',e);if(e.status&&e.status<500){pending.value=null;remember();await refresh()}}
  finally{busy.value=false}
}
const removeGuard=addLeaveGuard(async()=>!busy.value&&!pending.value)
onUnmounted(()=>{removeGuard();submissionLocks.value.delete(lockId);window.removeEventListener('beforeunload',beforeUnload)})
</script>
<template>
  <div class="submission-controls">
    <small v-if="statusError" role="alert">{{ statusError }} <button class="quiet-button" @click="refresh">重试</button></small>
    <small v-else-if="submitted.length">已送审 · {{ submitted.some(s=>s.reviewerId!==null||s.state!=='pending')?'审查已开始':'等待领取' }}</small>
    <button v-if="withdrawable" class="btn-secondary" data-testid="withdraw-submission" :disabled="busy||isAiBusy||editingBlocked||loading||!!pending" @click="openWithdraw">撤回送审</button>
    <button v-if="pending" class="btn-secondary" @click="visible=true">重试{{ pending.kind==='withdraw'?'撤回':'送审' }}</button>
    <button v-else class="btn-primary" :disabled="busy||loading||!!statusError||submitted.length>0||isAiBusy||editingBlocked||!selectedMedia?.serverMediaId||selectedMedia.type!=='video'" @click="open">完成标注并送审</button>
    <button v-if="submitted.length&&!withdrawable" class="quiet-button" :disabled="busy" aria-label="刷新送审状态" @click="refresh">↻</button>
  </div>
  <Teleport to="body"><div v-if="visible" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" data-completion-dialog @keydown.esc="!busy&&!pending&&(visible=false)">
    <section ref="dialog" role="dialog" aria-modal="true" @keydown="dialogKeys" :aria-label="mode==='withdraw'?'撤回送审':'完成标注并送审'" class="app-dialog w-[560px]">
      <h2 class="mb-4 text-lg font-semibold">{{ mode==='withdraw'?'撤回送审':'完成标注并送审' }}</h2>
      <p v-if="busy">正在保存并校验…</p><p v-if="error" class="my-3 text-[var(--danger)]" role="alert">{{ error }}</p><p v-if="success" class="my-3 text-[var(--success)]" role="status">{{ success }}</p>
      <p v-else-if="mode==='withdraw'">撤回「{{ selectedMedia?.name }}」的待审查任务，保留工作区标注。修改后请重新送审。领取即视为开始审查；若其他人已经领取，服务器会拒绝撤回。</p>
      <template v-else-if="preview">
        <p>全视频 {{ preview.frameCount }} 帧：有对象 {{ preview.objectFrames }} 帧，已明确空帧 {{ preview.emptyFrames }} 帧，未知 {{ preview.unknownFrames }} 帧。</p>
        <p class="my-3 muted">送审读取已保存的人工修正和 Tracking 结果，保留完整 A 快照；未知帧不会自动视为空帧。</p>
        <template v-if="preview.unknownFrames"><p class="my-3">未知帧（从 1 开始）：{{ preview.unknownFrameRanges.map(r=>r.start===r.end?`${r.start+1}`:`${r.start+1}-${r.end+1}`).join(', ') }}</p><label class="block">已逐帧确认没有对象的帧号 / 范围<input v-model="emptyRanges" :disabled="busy||!!pending" class="input my-2 w-full" placeholder="例如 3, 8-10；其余未知帧请先完成标注" /></label></template>
        <label class="my-4 flex gap-2"><input v-model="confirmed" :disabled="busy||!!pending" type="checkbox" />我已检查全视频，确认已有标注与上述空帧声明完整、准确。</label>
      </template>
      <div class="mt-5 flex justify-end gap-3"><button class="btn-secondary" :disabled="busy||!!pending" @click="visible=false">关闭</button><button v-if="!success&&mode==='send'" class="btn-secondary" :disabled="busy||!!pending" @click="open">重新预览</button><button v-if="!success" class="btn-primary" :disabled="busy||(!pending&&(mode==='send'?!preview||!confirmed:!withdrawable))" @click="submit">{{ pending?'重试原请求':mode==='withdraw'?'确认撤回':'确认送审' }}</button></div>
    </section>
  </div></Teleport>
</template>
<style scoped>.submission-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.submission-controls small{color:var(--text-muted);font-size:12px}</style>
