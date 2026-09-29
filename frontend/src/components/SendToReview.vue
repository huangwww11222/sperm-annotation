<script setup lang="ts">
import { createRequestId } from '../utils/browserCompat'
import { ref } from 'vue'
import { useWorkspace } from '../stores/workspace'
import { reviewWorkflowApi as api, type CompletionPreview } from '../api/reviewWorkflowApi'
const {selectedMedia,currentMediaId,isAiBusy,editingBlocked,persistWorkspaceState}=useWorkspace()
const visible=ref(false),busy=ref(false),error=ref(''),success=ref(''),emptyRanges=ref(''),confirmed=ref(false)
const preview=ref<CompletionPreview|null>(null)
let mediaId='',pending:{body:unknown;key:string}|null=null
async function open(){
  if(!selectedMedia.value?.serverMediaId)return
  mediaId=selectedMedia.value.serverMediaId;visible.value=true;busy.value=true;error.value='';success.value='';preview.value=null;pending=null;confirmed.value=false;emptyRanges.value=''
  try {await persistWorkspaceState(currentMediaId.value,true);preview.value=await api.completionPreview(mediaId)}
  catch(e:any){error.value=e.message||'送审预览失败';console.error('[review.completion_preview_failed]',e)}
  finally{busy.value=false}
}
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
  if(!preview.value||busy.value)return
  busy.value=true;error.value=''
  try {
    if(!pending)pending={key:createRequestId(),body:{expectedSourceRevision:preview.value.sourceRevision,confirmComplete:confirmed.value,explicitEmptyFrameRanges:ranges()}}
    await api.completeAnnotation(mediaId,pending.body,pending.key);pending=null;success.value='已生成完整原始标注快照和待审查任务。你可以直接进入“审查模式”领取并检查自己的标注，也可以由其他审查员领取。'
  }catch(e:any){error.value=(e.message||'送审失败')+(e.requestId?`（记录号 ${e.requestId}）`:'');console.error('[review.annotation_completion_failed]',e)
    if(e.status&&e.status<500)pending=null
  }finally{busy.value=false}
}
</script>
<template>
  <button class="btn-primary" :disabled="isAiBusy||editingBlocked||!selectedMedia?.serverMediaId||selectedMedia.type!=='video'" @click="open">完成标注并送审</button>
  <Teleport to="body"><div v-if="visible" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" data-completion-dialog @keydown.esc="!busy&&(visible=false)">
    <section role="dialog" aria-modal="true" aria-label="完成标注并送审" class="app-dialog w-[560px]">
      <h2 class="mb-4 text-lg font-semibold">完成标注并送审</h2>
      <p v-if="busy">正在保存并校验…</p>
      <p v-if="error" class="my-3 text-[var(--danger)]" role="alert">{{ error }}</p>
      <p v-if="success" class="my-3 text-[var(--success)]" role="status">{{ success }}</p>
      <template v-else-if="preview">
        <p>全视频 {{ preview.frameCount }} 帧：有对象 {{ preview.objectFrames }} 帧，已明确空帧 {{ preview.emptyFrames }} 帧，未知 {{ preview.unknownFrames }} 帧。</p>
        <p class="my-3 muted">送审读取已保存的人工修正和 Tracking 结果，保留完整 A 快照；未知帧不会自动视为空帧。</p>
        <template v-if="preview.unknownFrames"><p class="my-3">未知帧（从 1 开始）：{{ preview.unknownFrameRanges.map(r=>r.start===r.end?`${r.start+1}`:`${r.start+1}-${r.end+1}`).join(', ') }}</p>
          <label class="block">已逐帧确认没有对象的帧号 / 范围<input v-model="emptyRanges" :disabled="busy||!!pending" class="input my-2 w-full" placeholder="例如 3, 8-10；其余未知帧请先完成标注" /></label>
        </template>
        <label class="my-4 flex gap-2"><input v-model="confirmed" :disabled="busy||!!pending" type="checkbox" />我已检查全视频，确认已有标注与上述空帧声明完整、准确。</label>
      </template>
      <div class="mt-5 flex justify-end gap-3"><button class="btn-secondary" :disabled="busy" @click="visible=false">关闭</button><button v-if="!success" class="btn-secondary" :disabled="busy||!!pending" @click="open">重新预览</button><button v-if="preview&&!success" class="btn-primary" :disabled="busy||!confirmed" @click="submit">{{ pending?'重试送审':'确认送审' }}</button></div>
    </section>
  </div></Teleport>
</template>
