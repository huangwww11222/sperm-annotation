<script setup lang="ts">
import { createRequestId } from '../utils/browserCompat'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { confirmationApi as api, type Change, type ConfirmationSession, type FrameContext, type Operation, type Action, type Choice, type Failure, type Result } from '../api/confirmationApi'
import WorkflowProgress from '../components/WorkflowProgress.vue'
import WorkbenchHeader from '../components/WorkbenchHeader.vue'
import WorkbenchLayout from '../components/WorkbenchLayout.vue'
import ConfirmationImage from '../confirmation/ConfirmationImage.vue'
import TrainingDatasetExport from '../components/TrainingDatasetExport.vue'
import { addLeaveGuard } from '../router'
import { useAuth } from '../stores/auth'
import { invalidateSourceFrame } from '../api/sourceFrameApi'

const sessions=ref<ConfirmationSession[]>([]), session=ref<ConfirmationSession|null>(null), items=ref<Change[]>([])
const selected=ref<string|null>(null), context=ref<FrameContext|null>(null), imageUrl=ref('')
const query=ref(''), filter=ref('all'), busy=ref(false), error=ref(''), errorCode=ref(''), notice=ref('请选择一个视频开始对比确认。')
const errorStatus=ref(0), ready=ref(false), exportOpen=ref(false), exportBusy=ref(false)
const pending=ref<Operation|null>(null), mode=ref('pair'), zoom=ref(5), autoNext=ref(true), jump=ref('1')
const modal=ref<'help'|'complete'|'reopen'|'conflict'|'resume'|'context'|'return'|null>(null), dialog=ref<HTMLElement|null>(null)
const userId=useAuth().user.value?.id||'unknown', journalKey=`confirmation-intent:${userId}`, prefKey=`confirmation-prefs:${userId}`
let alive=true, leavingForLogin=false, removeGuard=()=>{}, previousFocus:HTMLElement|null=null
let readRetry:(()=>Promise<void>)|null=null
const returnReason=ref('A、B 的标注均不合适，请重新检查并修正本帧。')
const current=computed(()=>items.value.find(x=>x.changeId===selected.value))
const index=computed(()=>items.value.findIndex(x=>x.changeId===selected.value))
const frameIndex=computed(()=>current.value?.frameIndex??0)
const frameItems=computed(()=>items.value.filter(x=>x.frameIndex===frameIndex.value))
const itemFilter=ref<'all'|'pending'|'decided'>('all'), queuePage=ref(0)
const changeList=ref<HTMLElement|null>(null), inspectorSecondary=ref<HTMLElement|null>(null)
const queuePageSize=40
const itemNumbers=computed(()=>new Map(items.value.map((item,i)=>[item.changeId,i+1])))
const filteredItems=computed(()=>items.value.filter(item=>itemFilter.value==='all'||(itemFilter.value==='pending'?!item.decision:!!item.decision)))
const queuePages=computed(()=>Math.max(1,Math.ceil(filteredItems.value.length/queuePageSize)))
const queueStart=computed(()=>queuePage.value*queuePageSize)
const queue=computed(()=>filteredItems.value.slice(queueStart.value,queueStart.value+queuePageSize))
const queueGroups=computed(()=>{
  const groups:Array<{frameIndex:number;items:Change[]}>=[]
  for(const item of queue.value){const last=groups[groups.length-1];if(last?.frameIndex===item.frameIndex)last.items.push(item);else groups.push({frameIndex:item.frameIndex,items:[item]})}
  return groups
})
const currentOutsideFilter=computed(()=>!!current.value&&!filteredItems.value.some(item=>item.changeId===current.value?.changeId))
function scrollCurrentIntoList() {
  const row=changeList.value?.querySelector<HTMLElement>('.c-change-item.selected')
  if(!row)return
  // Keep automatic selection inside its two owning panes; never scroll the document.
  for(const pane of [changeList.value,inspectorSecondary.value]){
    if(!pane)continue
    const bounds=pane.getBoundingClientRect(),target=row.getBoundingClientRect()
    if(target.top<bounds.top)pane.scrollTop+=target.top-bounds.top
    else if(target.bottom>bounds.bottom)pane.scrollTop+=target.bottom-bounds.bottom
  }
}
function locateCurrent() {
  itemFilter.value='all'
  queuePage.value=Math.floor(Math.max(0,index.value)/queuePageSize)
  void nextTick(scrollCurrentIntoList)
}
function changeItemFilter(value:'all'|'pending'|'decided') {itemFilter.value=value;queuePage.value=0}
watch(selected,()=>{
  const at=filteredItems.value.findIndex(item=>item.changeId===selected.value)
  if(at>=0)queuePage.value=Math.floor(at/queuePageSize)
  void nextTick(scrollCurrentIntoList)
})
watch(queuePages,total=>{queuePage.value=Math.min(queuePage.value,total-1)})
const matching=computed(()=>sessions.value.filter(s=>`${s.media.name} ${s.media.mediaId}`.toLowerCase().includes(query.value.toLowerCase())))
const visible=computed(()=>matching.value.filter(s=>filter.value==='all'||s.state===filter.value))
const blocked=computed(()=>busy.value||!!pending.value||!ready.value||exportBusy.value)
const canChoose=computed(()=>!!session.value?.permissions.canEdit&&!!current.value&&!!imageUrl.value&&!blocked.value)
const caption=computed(()=>session.value?.state==='returned'?'本帧已退回，等待审查员重审':session.value?.state==='confirmed'?'已完成确认 · 当前版本只读':pending.value?'操作尚未确认保存':busy.value?'正在处理…':'所有已选择结果均已保存')
const stateText=(s:string)=>({pending:'待确认',in_progress:'确认中',confirmed:'已确认',returned:'已退回重审'}[s]||'历史任务')
const count=(state:string)=>state==='all'?matching.value.length:matching.value.filter(s=>s.state===state).length
const label=(c:Change)=>c.decision?c.decision.choice==='A'?'保留 A':'采用 B':'待选择'
function fail(e:unknown,action:string) {
  const f=e as Failure
  error.value=`${f.message||'连接中断，请重试'}${f.requestId?`（记录号 ${f.requestId}）`:''}`;errorCode.value=f.code||'';errorStatus.value=f.status||0
  console.error('[confirmation.operation_failed]',{action,sessionId:session.value?.id,changeId:selected.value,...f})
}
function update(s:ConfirmationSession) { session.value=s;sessions.value=sessions.value.map(x=>x.id===s.id?s:x) }
async function applyResult(r:Result,action:Action) {
  const previous=session.value?.revision??r.session.revision
  const allowed=action==='cursor'?0:1
  if(r.itemsScope==='changed'&&(r.session.revision<previous||r.session.revision>previous+allowed)) {
    // Another window changed business state: refresh instead of leaving a
    // partial list stale or silently rebasing an unconfirmed operation.
    const [s,cs]=await Promise.all([api.session(r.session.id),api.changes(r.session.id)])
    update(s);items.value=cs.items
    r.session=s;r.nextPendingChangeId=s.resume.firstPendingChangeId
  } else {
    update(r.session)
    if(r.itemsScope==='changed') {
      const updates=new Map(r.items.map(item=>[item.changeId,item]))
      items.value=items.value.map(item=>updates.get(item.changeId)||item)
    } else items.value=r.items
  }
}
function prefs() {
  try {localStorage.setItem(prefKey,JSON.stringify({mode:mode.value,zoom:zoom.value,autoNext:autoNext.value,sid:session.value?.id}))}
  catch(e) {console.warn('[confirmation.preferences_unavailable]',e)}
}
watch([mode,zoom,autoNext],prefs)
function persistIntent() {
  try {if(pending.value)sessionStorage.setItem(journalKey,JSON.stringify(pending.value));else sessionStorage.removeItem(journalKey);return true}
  catch(e) {console.warn('[confirmation.intent_backup_unavailable]',e);return false}
}
watch(modal,async(value,old)=>{
  if(value){if(!old)previousFocus=document.activeElement as HTMLElement;await nextTick();dialog.value?.querySelector<HTMLElement>('button')?.focus()}
  else previousFocus?.focus()
})
let imageSessionId = ''
function clearImage() {if(imageUrl.value)URL.revokeObjectURL(imageUrl.value);imageUrl.value='';context.value=null;imageSessionId=''}
async function list() {
  try {sessions.value=(await api.list()).items;readRetry=null;error.value=''}
  catch(e){fail(e,'list');readRetry=list}
}
async function loadItem(id:string|null,savePosition=true) {
  if(!session.value)return
  const sid=session.value.id,item=items.value.find(x=>x.changeId===id)
  selected.value=item?.changeId??null;jump.value=String(Math.max(0,index.value)+1)
  // Fixed A/B geometry and native pixels are unchanged between objects in the
  // same frozen frame. Reuse them, while saving each decision/cursor normally.
  if(imageSessionId===sid&&context.value?.frameIndex===(item?.frameIndex??0)&&imageUrl.value) {
    if(savePosition&&!pending.value)await bookmark()
    return
  }
  clearImage()
  try {
    const [ctx,blob]=await Promise.all([api.frame(sid,item?.frameIndex??0),api.image(session.value.media.mediaId,item?.frameIndex??0)])
    const url=URL.createObjectURL(blob),img=new Image();img.src=url
    try {await img.decode();if(img.naturalWidth!==session.value.media.width||img.naturalHeight!==session.value.media.height)throw {message:'图像尺寸与固定版本不一致，已停止选择。'}}
    catch(e){invalidateSourceFrame(session.value.media.mediaId,item?.frameIndex??0);URL.revokeObjectURL(url);throw e}
    if(!alive){URL.revokeObjectURL(url);return}
    context.value=ctx;imageUrl.value=url;imageSessionId=sid;readRetry=null
    if(savePosition&&!pending.value)await bookmark()
  } catch(e) {fail(e,'load_frame');readRetry=()=>selectItem(id)}
}
async function openSession(id:string,resume=true) {
  if(busy.value||pending.value)return
  busy.value=true;error.value=''
  try {
    const [s,cs]=await Promise.all([api.session(id),api.changes(id)])
    update(s);items.value=cs.items;itemFilter.value='all';queuePage.value=0;notice.value='浏览与切换视图不会产生选择。';prefs()
    const history=s.resume.lastViewedChangeId,first=s.resume.firstPendingChangeId
    await loadItem(history||first||cs.items[0]?.changeId||null,false)
    if(resume&&history&&first&&history!==first&&!pending.value)modal.value='resume'
  }catch(e){fail(e,'open_session');readRetry=()=>openSession(id,resume)}
  finally{busy.value=false}
}
async function selectItem(id:string|null) {
  if(blocked.value)return
  busy.value=true;error.value=''
  try{await loadItem(id)}finally{busy.value=false}
}
async function bookmark() {
  if(!session.value)return
  if(session.value.resume.lastViewedChangeId===selected.value)return
  let op:Operation={action:'cursor',sid:session.value.id,changeId:selected.value||undefined,body:{changeId:selected.value,expectedCursorRevision:session.value.resume.cursorRevision},key:createRequestId()}
  try {
    let r
    try {r=await api.operate(op)}
    catch(e) {
      if((e as Failure).code!=='CURSOR_REVISION_CONFLICT')throw e
      // A browsing position may follow this window after rebasing; it never writes decisions.
      const fresh=await api.session(op.sid)
      op={...op,key:createRequestId(),body:{...op.body,expectedCursorRevision:fresh.resume.cursorRevision}}
      r=await api.operate(op)
    }
    await applyResult(r,'cursor')
  }catch(e){pending.value=op;persistIntent();fail(e,'bookmark')}
}
function operation(action:Action,body:Record<string,unknown>={},changeId?:string) {
  if(!session.value||blocked.value)return
  pending.value={action,sid:session.value.id,changeId,body,key:createRequestId()};persistIntent()
  void retry()
}
async function retry():Promise<boolean> {
  const op=pending.value
  if(!op||busy.value)return !op
  busy.value=true;error.value='';errorCode.value='';modal.value=null
  let success=false
  try {
    const r=await api.operate(op)
    await applyResult(r,op.action);pending.value=null;persistIntent();success=true
    notice.value=op.action==='finish'?'已生成完整视频的最终版本。':op.action==='reopen'?'已重新开放确认，保留已有选择和历史最终版本。':op.action==='return'?'已退回本帧重审。其他帧的选择保留，审查员重新提交并完成视频后可继续确认。':op.action==='undo'?'已撤销上一次选择，并返回该项。':'已保存到服务器。'
    let target=op.action==='undo'?r.selectedChangeId:selected.value
    if(op.action==='decide'&&autoNext.value&&r.nextPendingChangeId)target=r.nextPendingChangeId
    if(target!==selected.value||!imageUrl.value)await loadItem(target||items.value[0]?.changeId||null,false)
    if(op.action!=='cursor')await bookmark()
    if(op.action==='decide'&&r.session.permissions.canComplete&&!pending.value)modal.value='complete'
  }catch(e){fail(e,op.action)}finally{busy.value=false}
  return success&&!pending.value
}
function choose(choice:Choice) {
  if(!canChoose.value||!current.value||current.value.decision?.choice===choice)return
  operation('decide',{choice,expectedDecisionRevision:current.value.decisionRevision},current.value.changeId)
}
function undo() {if(!blocked.value&&session.value?.permissions.canUndo&&session.value.undo)operation('undo',{expectedSessionRevision:session.value.revision,actionId:session.value.undo.actionId})}
function complete() {if(session.value?.permissions.canComplete)operation('finish',{expectedSessionRevision:session.value.revision})}
function reopen() {if(session.value?.permissions.canReopen)operation('reopen',{expectedSessionRevision:session.value.revision})}
function returnFrame(){if(current.value&&session.value?.permissions.canReturn&&returnReason.value.trim())operation('return',{expectedSessionRevision:session.value.revision,changeId:current.value.changeId,reason:returnReason.value.trim()})}
function move(delta:number) {const row=items.value[index.value+delta];if(row)void selectItem(row.changeId)}
function jumpTo() {
  const n=Number(jump.value)
  if(Number.isInteger(n)&&n>=1&&n<=items.value.length)void selectItem(items.value[n-1]!.changeId)
  else notice.value=`请输入 1–${items.value.length} 的修改项序号。`
}
function contextSelect(objectId:number) {const row=frameItems.value.find(x=>x.objectId===objectId);if(row)void selectItem(row.changeId)}
function downloadBlob(blob:Blob,name:string) {
  const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
}
async function download() {
  if(!session.value?.finalVersionId||blocked.value)return
  busy.value=true
  try{downloadBlob(await api.download(session.value.finalVersionId),`${session.value.media.name}-最终标注-${session.value.finalVersionId}.json`);error.value='';notice.value='已导出完整视频 JSON，包含未修改对象、空帧、A/B 几何和最终选择。'}
  catch(e){fail(e,'export');readRetry=download}finally{busy.value=false}
}
function backup() {if(pending.value)downloadBlob(new Blob([JSON.stringify(pending.value,null,2)],{type:'application/json'}),'confirmation-pending-operation.json')}
async function reloadServer() {
  // Explicit user resolution of an uncertain/conflicting local intent; never silently discard.
  const sid=pending.value?.sid||session.value?.id
  if(!sid)return
  busy.value=true
  try {
    const [s,cs]=await Promise.all([api.session(sid),api.changes(sid)])
    pending.value=null;persistIntent();update(s);items.value=cs.items;error.value='';modal.value=null
    await loadItem(selected.value||s.resume.firstPendingChangeId||cs.items[0]?.changeId||null,false)
    notice.value='已采用服务器当前结果；可以重新选择。'
  }catch(e){fail(e,'reload_server')}finally{busy.value=false}
}
async function retryRead() {if(!readRetry||busy.value)return;const run=readRetry;error.value='';await run()}
function loginAgain() {if(!persistIntent()&&pending.value)backup();leavingForLogin=true;useAuth().logout()}
function beforeUnload(e:BeforeUnloadEvent) {if(pending.value||busy.value){e.preventDefault();e.returnValue=''}}
function keydown(e:KeyboardEvent) {
  if(document.querySelector('[data-user-guide][open]'))return

  if(exportOpen.value)return
  if(modal.value) {
    if(e.key==='Escape'&&!busy.value){e.preventDefault();modal.value=null}
    if(e.key==='Tab'&&dialog.value){const nodes=Array.from(dialog.value.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]:not([aria-disabled="true"])'));const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&(document.activeElement===first||!nodes.includes(document.activeElement as HTMLElement))){e.preventDefault();last?.focus()}else if(!e.shiftKey&&(document.activeElement===last||!nodes.includes(document.activeElement as HTMLElement))){e.preventDefault();first?.focus()}}
    return
  }
  const target=e.target as HTMLElement
  if(e.isComposing||e.repeat||target.closest('input,textarea,select,[contenteditable="true"]'))return
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'&&!e.shiftKey&&!e.altKey){e.preventDefault();undo();return}
  if(e.ctrlKey||e.metaKey||e.altKey||blocked.value)return
  if(e.key==='?'){e.preventDefault();modal.value='help'}
  if(e.shiftKey)return
  if(e.key.toLowerCase()==='a'){e.preventDefault();choose('A')}
  if(e.key.toLowerCase()==='b'){e.preventDefault();choose('B')}
  if(e.key==='ArrowLeft'){e.preventDefault();move(-1)}
  if(e.key==='ArrowRight'){e.preventDefault();move(1)}
}
onMounted(async()=>{
  window.addEventListener('keydown',keydown);window.addEventListener('beforeunload',beforeUnload)
  removeGuard=addLeaveGuard(async()=>{if(leavingForLogin)return true;if(busy.value||exportBusy.value)return false;if(pending.value)return retry();return true})
  let last:string|undefined, stored:Operation|null=null
  try{const p=JSON.parse(localStorage.getItem(prefKey)||'{}');mode.value=p.mode==='overlay'?'overlay':'pair';zoom.value=Math.max(3,Math.min(8,Number(p.zoom)||5));autoNext.value=p.autoNext!==false;last=p.sid;stored=JSON.parse(sessionStorage.getItem(journalKey)||'null')}catch(e){console.warn('[confirmation.restore_preferences_failed]',e)}
  await list()
  const id=stored?.sid||last||sessions.value[0]?.id
  if(id&&sessions.value.some(s=>s.id===id))await openSession(id,!stored)
  if(stored){pending.value=stored;notice.value='检测到上次未确认保存的操作，请重试原请求或读取服务器结果。';error.value='上次操作的保存结果尚未确认。'}
  ready.value=true
})
onUnmounted(()=>{alive=false;clearImage();removeGuard();window.removeEventListener('keydown',keydown);window.removeEventListener('beforeunload',beforeUnload)})
</script>

<template>
  <div class="confirmation-page workbench-page" :data-busy="busy||!ready">
    <WorkbenchHeader title="对比确认" :description="session?.media.name||'选择视频，对比原始标注与审查修改'"><button @click="modal='help'">快捷键 ?</button></WorkbenchHeader>
    <WorkflowProgress v-if="session" label="视频确认进度" :percent="session.progress.percent" :summary="`已选择 ${session.progress.decided} / ${session.progress.totalChanges} 项 · 待选择 ${session.progress.pending} 项`" :detail="`保留 A ${session.progress.keptA} · 采用 B ${session.progress.adoptedB}`" :completed="session.state==='confirmed'" :state="session.state==='returned'?'已退回本帧 · 等待重审':session.state==='confirmed'?'视频已确认 · 最终版本已生成':session.progress.pending===0?(session.progress.totalChanges?'全部已选择 · 待完成视频确认':'无修改项 · 仍需完成视频确认'):'仅统计修改项 · 未修改对象沿用 A'">
      <button v-if="session.state!=='confirmed'&&session.state!=='returned'" class="c-primary" data-testid="complete-video" :disabled="blocked||!session.permissions.canComplete" @click="modal='complete'">完成本视频确认</button>
      <span v-else class="c-state">{{ session.state==='returned'?'等待重审':'已完成确认' }}</span>
    </WorkflowProgress>
    <div v-if="session?.returnedReview" class="c-notice">第 {{ session.returnedReview.frameIndex+1 }} 帧已退回重审：{{ session.returnedReview.reason }} <button v-if="session.returnedReview.nextConfirmationId" :disabled="blocked" @click="openSession(session.returnedReview.nextConfirmationId)">继续新一轮确认</button><button v-else :disabled="blocked" @click="openSession(session.id)">刷新重审状态</button></div>
    <div v-if="error" class="confirmation-error" role="alert"><span>{{ error }}</span><button v-if="errorStatus===401" :disabled="busy" @click="loginAgain">保留操作并重新登录</button><template v-if="pending"><button :disabled="busy" @click="retry">重试原请求</button><button :disabled="busy" @click="modal='conflict'">读取服务器结果</button><button @click="backup">备份未决操作</button></template><button v-else :disabled="busy" @click="retryRead">重试加载</button></div>
    <WorkbenchLayout library-label="视频列表">
      <template #library>
        <aside class="c-panel confirmation-sidebar">
          <div class="c-heading"><h2>待确认视频</h2><button :disabled="blocked" @click="list">刷新</button></div>
          <div class="c-search"><input v-model="query" aria-label="搜索视频" placeholder="搜索视频名称"/><div class="c-filters" aria-label="视频状态筛选"><button v-for="f in [['all','全部'],['pending','待确认'],['in_progress','确认中'],['confirmed','已确认'],['returned','已退回']]" :key="f[0]" :class="{active:filter===f[0]}" :aria-pressed="filter===f[0]" @click="filter=f[0]!">{{ f[1] }} {{ count(f[0]!) }}</button></div></div>
          <div class="c-video-list"><button v-for="s in visible" :key="s.id" class="c-video" :class="{selected:session?.id===s.id}" :data-session-id="s.id" :disabled="blocked" @click="openSession(s.id)"><strong :title="s.media.name">{{ s.media.name }}</strong><small class="c-task-id" :title="s.media.mediaId + ' · ' + s.id">{{ s.media.mediaId }} · {{ s.id.slice(-6) }}</small><span>{{ stateText(s.state) }} <small>{{ s.media.frameCount }} 帧</small></span><progress max="100" :value="s.progress.percent"/><span>{{ s.progress.decided }} / {{ s.progress.totalChanges }} 项 <small>剩余 {{ s.progress.pending }}</small></span></button><p v-if="!visible.length" class="c-empty">没有符合条件的视频。<br>完成审查后，视频会进入这里。</p></div>
        </aside>
      </template>
      <template #canvas>
        <main v-if="session" class="confirmation-center">
          <section class="c-panel c-comparison">
            <header class="c-heading"><h2 data-testid="current-item">第 {{ frameIndex+1 }} / {{ session.media.frameCount }} 帧 <template v-if="current">· 对象 #{{ current.objectId }}</template><template v-else>· 无修改项</template></h2><span class="c-state">{{ current?label(current):stateText(session.state) }}</span></header>
            <div class="c-image-workspace" :class="{'context-only':!items.length}">
            <section class="c-context" aria-label="完整帧定位">
              <div class="c-context-title"><h3>完整帧定位</h3><small v-if="current">金色外框定位当前对象</small><button :disabled="!imageUrl||!context" @click="modal='context'">展开</button></div>
              <div class="c-context-canvas"><ConfirmationImage v-if="imageUrl&&context" :src="imageUrl" :width="session.media.width" :height="session.media.height" :item="current" :context="context" variant="full" :zoom="zoom" :selectable-object-ids="frameItems.map(item=>item.objectId)" :interactive="!blocked" @select="contextSelect"/><p v-else class="c-empty">{{ busy?'正在加载当前帧…':'当前帧图像不可用，请重试加载。' }}</p></div>
            </section>
            <section v-if="items.length" class="c-local-comparison" aria-label="当前对象局部对比">
              <div class="c-toolbar"><div class="c-segment"><button :class="{active:mode==='pair'}" :aria-pressed="mode==='pair'" @click="mode='pair'">A / B 对照</button><button :class="{active:mode==='overlay'}" :aria-pressed="mode==='overlay'" @click="mode='overlay'">叠加对比</button></div><label>局部缩放 <input v-model.number="zoom" aria-label="局部缩放" type="range" min="3" max="8" step=".5"/> {{ zoom }}×</label></div>
              <div v-if="imageUrl&&context&&current" class="c-crops" :class="{overlay:mode==='overlay'}">
                <figure v-for="variant in (mode==='pair'?['A','B']:['overlay']) as ('A'|'B'|'overlay')[]" :key="variant"><figcaption><span v-if="variant!=='B'" class="c-a">A 原始标注 · 虚线</span><span v-if="variant!=='A'" class="c-b">B 审查标注 · 实线</span></figcaption><ConfirmationImage :src="imageUrl" :width="session.media.width" :height="session.media.height" :item="current" :context="context" :variant="variant" :zoom="zoom"/></figure>
              </div>
              <p v-else class="c-empty c-crop-empty">{{ busy?'正在加载局部对比…':'图像恢复后显示当前对象的 A/B 对比。' }}</p>
            </section>
            </div>
            <p v-if="!items.length" class="c-zero-message">本视频没有净修改项，所有对象沿用 A，可直接完成确认。</p>
          </section>
          <section class="c-panel c-queue-nav" aria-label="修改项浏览"><div class="c-step-nav"><button :disabled="blocked||index<=0" @click="move(-1)">← 上一修改项</button><span>修改项 {{ items.length?index+1:0 }} / {{ items.length }}</span><button :disabled="blocked||index>=items.length-1" @click="move(1)">下一修改项 →</button></div><div class="c-jump-nav"><button :disabled="blocked||!session.resume.firstPendingChangeId" @click="selectItem(session.resume.firstPendingChangeId)">首个待确认</button><label>跳至第 <input v-model="jump" aria-label="修改项序号" inputmode="numeric" @keydown.enter="jumpTo"/> 项</label><button :disabled="blocked||!items.length" @click="jumpTo">前往</button></div></section>
        </main>
        <div v-else class="c-panel c-empty">选择左侧视频，比较原始标注与审查修改。</div>
      </template>
      <template #inspector>
        <aside v-if="session" class="confirmation-right">
          <section class="c-panel c-decision"><div class="c-heading"><h2>确认本项</h2><small>{{ current?`对象 #${current.objectId}`:'无修改项' }}</small></div><div class="c-pad"><p v-if="session.readOnlyReason" class="c-notice">{{ session.readOnlyReason }}</p><button v-if="session.permissions.canClaim" class="c-primary c-wide" :disabled="blocked" @click="operation('claim')">领取并开始确认</button><p v-else-if="!session.permissions.canEdit&&session.state!=='confirmed'&&session.state!=='returned'" class="c-notice">当前任务由已领取的确认员编辑，您可以查看。</p><button class="c-choice c-choice-a" data-testid="choose-a" :aria-pressed="current?.decision?.choice==='A'" :class="{chosen:current?.decision?.choice==='A'}" :disabled="!canChoose" @click="choose('A')"><b>保留原标注 A</b><kbd>A</kbd></button><button class="c-choice c-choice-b" data-testid="choose-b" :aria-pressed="current?.decision?.choice==='B'" :class="{chosen:current?.decision?.choice==='B'}" :disabled="!canChoose" @click="choose('B')"><b>采用审查标注 B</b><kbd>B</kbd></button><button v-if="session.permissions.canReturn" class="c-wide" data-testid="return-frame-review" :disabled="!current||blocked" @click="modal='return'">A / B 都不合适，退回本帧重审</button><small class="c-save-state" role="status">{{ caption }}</small><label class="c-auto"><input v-model="autoNext" type="checkbox"/>选择后自动跳到下一待确认项</label><button class="c-wide" :disabled="blocked||!session.permissions.canUndo" @click="undo">撤销上一次选择 <small>Ctrl / ⌘ Z</small></button></div></section>
          <div ref="inspectorSecondary" class="c-inspector-secondary">
          <section v-if="session.state==='confirmed'" class="c-panel c-pad c-final"><h2>导出训练数据集</h2><TrainingDatasetExport v-if="session.finalVersionId && session.permissions.canExport" :key="session.finalVersionId" :final-version-id="session.finalVersionId" :media-name="session.media.name" :disabled="blocked" @open="exportOpen=$event" @busy="exportBusy=$event"/><button class="c-wide" :disabled="blocked||!session.permissions.canExport" @click="download">导出完整视频 JSON</button><button v-if="session.permissions.canReopen" class="c-wide" :disabled="blocked" @click="modal='reopen'">重新确认</button><small v-if="session.finalVersionId" class="c-version" :title="session.finalVersionId">版本 {{ session.finalVersionId }}</small></section>
          <section class="c-panel c-change-navigation" aria-label="修改项列表"><div class="c-heading"><h2>修改项</h2><button :disabled="!current" @click="locateCurrent">定位当前项</button></div><div class="c-change-filters" aria-label="修改项状态筛选"><button :class="{active:itemFilter==='all'}" :aria-pressed="itemFilter==='all'" @click="changeItemFilter('all')">全部 {{ items.length }}</button><button :class="{active:itemFilter==='pending'}" :aria-pressed="itemFilter==='pending'" @click="changeItemFilter('pending')">待确认 {{ session.progress.pending }}</button><button :class="{active:itemFilter==='decided'}" :aria-pressed="itemFilter==='decided'" @click="changeItemFilter('decided')">已确认 {{ session.progress.decided }}</button></div><p v-if="currentOutsideFilter" class="c-filter-note">当前项不在此筛选中，画面仍保留当前项。</p><div ref="changeList" class="c-change-list"><section v-for="group in queueGroups" :key="group.frameIndex" class="c-frame-group"><h3>第 {{ group.frameIndex+1 }} 帧</h3><button v-for="c in group.items" :key="c.changeId" class="c-change-item" :class="{selected:c.changeId===selected,chosen:!!c.decision}" :aria-current="c.changeId===selected?'true':undefined" :aria-label="`修改项 ${itemNumbers.get(c.changeId)}，第 ${c.frameIndex+1} 帧，对象 ${c.objectId}，${label(c)}`" :disabled="blocked" @click="selectItem(c.changeId)"><span>对象 #{{ c.objectId }}</span><small :class="c.decision?.choice==='A'?'c-a':c.decision?.choice==='B'?'c-b':''">{{ label(c) }}</small></button></section><p v-if="!filteredItems.length" class="c-empty">{{ items.length?'没有符合筛选的修改项。':'没有修改项，仍需完成视频确认。' }}</p></div><div v-if="queuePages>1" class="c-list-pagination"><button :disabled="queuePage===0" aria-label="上一页修改项" @click="queuePage--">上一页</button><span>{{ queuePage+1 }} / {{ queuePages }} 页</span><button :disabled="queuePage>=queuePages-1" aria-label="下一页修改项" @click="queuePage++">下一页</button></div></section>
          </div>
        </aside>
      </template>
    </WorkbenchLayout>
    <footer class="confirmation-footer" aria-live="polite"><span>{{ notice }}</span></footer>
    <div v-if="modal" class="c-modal-mask" @click.self="!busy&&(modal=null)"><section ref="dialog" :class="['c-modal',{'c-context-modal':modal==='context'}]" role="dialog" aria-modal="true" aria-labelledby="confirmation-dialog-title">
      <template v-if="modal==='context'"><h2 id="confirmation-dialog-title">完整帧上下文</h2><ConfirmationImage v-if="session&&imageUrl&&context" :src="imageUrl" :width="session.media.width" :height="session.media.height" :item="current" :context="context" variant="full" :zoom="zoom" :selectable-object-ids="frameItems.map(item=>item.objectId)" :interactive="!blocked" @select="contextSelect"/><div v-if="error" class="confirmation-error c-context-error" role="alert"><span>{{ error }}</span><button v-if="errorStatus===401" :disabled="busy" @click="loginAgain">重新登录</button><button v-if="pending" :disabled="busy" @click="retry">重试原请求</button><button v-else :disabled="busy" @click="retryRead">重试加载</button></div><p v-else-if="busy" role="status">正在加载当前帧…</p><p>金色外框定位当前对象；点击有修改的框可切换旁边的 A/B 对比。</p><button @click="modal=null">关闭</button></template>
      <template v-else-if="modal==='help'"><h2 id="confirmation-dialog-title">对比确认快捷键</h2><dl><div><dt>A / B</dt><dd>保留 A / 采用 B，立即保存；自动下一项由勾选项控制。</dd></div><div><dt>← / →</dt><dd>浏览上一 / 下一修改项，不创建选择。</dd></div><div><dt>Ctrl / ⌘ Z</dt><dd>撤销本视频上一次选择，返回该项；可连续撤销，刷新后仍有效。</dd></div><div><dt>?</dt><dd>打开本说明。</dd></div><div><dt>Esc / Tab</dt><dd>关闭对话框 / 在对话框内切换焦点。</dd></div></dl><p>输入框、中文输入法组合输入、长按重复键和对话框内不触发业务快捷键。只读、保存中或保存失败待处理时不能选择。完成视频需点击按钮确认。</p><button @click="modal=null">关闭</button></template>
      <template v-else-if="modal==='complete'"><h2 id="confirmation-dialog-title">完成本视频确认？</h2><p>共 {{ session?.progress.totalChanges }} 个修改项，保留 A {{ session?.progress.keptA }} 项，采用 B {{ session?.progress.adoptedB }} 项。将生成包含全部 {{ session?.media.frameCount }} 帧的最终版本，未修改对象沿用 A，空帧保留。</p><p>完成后只读；可通过“重新确认”创建后续版本。</p><button :disabled="busy" @click="modal=null">继续检查</button><button class="c-primary" :disabled="blocked" @click="complete">确认完成</button></template>
      <template v-else-if="modal==='reopen'"><h2 id="confirmation-dialog-title">重新确认本视频？</h2><p>保留当前所有选择，允许继续修改。历史最终版本永久保留；重新确认期间暂停导出训练数据集，已有训练包也暂不可下载。再次完成后生成新的最终版本，只能导出该版本的训练数据集。</p><button @click="modal=null">取消</button><button class="c-primary" :disabled="blocked" @click="reopen">开始重新确认</button></template>
      <template v-else-if="modal==='return'"><h2 id="confirmation-dialog-title">退回第 {{ frameIndex+1 }} 帧重审？</h2><p>本帧所有对象交回原审查员检查，需重新提交本帧并完成视频审查。本帧的旧 A/B 选择不作为新一轮决定；其他帧已确认项将保留。重审及再次确认完成前不能导出训练数据集。</p><label>退回原因<textarea v-model="returnReason" class="input" maxlength="1000" rows="3" aria-label="退回原因" /></label><button @click="modal=null">取消</button><button class="c-primary" :disabled="blocked||!returnReason.trim()" @click="returnFrame">确认退回本帧</button></template>
      <template v-else-if="modal==='resume'"><h2 id="confirmation-dialog-title">继续上次的确认进度</h2><p>已恢复上次查看的位置。您也可以跳到首个尚未选择的修改项。</p><button @click="modal=null">留在上次位置</button><button class="c-primary" @click="modal=null;selectItem(session!.resume.firstPendingChangeId)">从首个待确认开始</button></template>
      <template v-else><h2 id="confirmation-dialog-title">读取服务器当前结果？</h2><p>本地操作可能已经保存，也可能尚未保存。读取成功后，将采用服务器结果并清除本地未决操作。需要时可重新作出选择。</p><p>建议先“重试原请求”；重复请求不会生成重复选择。{{ errorCode?'错误代码：'+errorCode:'' }}</p><button @click="modal=null">取消</button><button @click="backup">备份未决操作</button><button class="c-primary" :disabled="busy" @click="reloadServer">采用服务器结果</button></template>
    </section></div>
  </div>
</template>
<style src="../confirmation/confirmation.css"></style>
