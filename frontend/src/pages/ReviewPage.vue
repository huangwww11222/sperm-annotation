<script setup lang="ts">
import { createRequestId } from '../utils/browserCompat'
import WorkbenchShell from '../components/WorkbenchShell.vue'
import WorkbenchFrameStrip from '../components/WorkbenchFrameStrip.vue'
import AppIcon from '../components/AppIcon.vue'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { reviewWorkflowApi as api, type Frame, type Session, type Mutation, type Failure, type ReviewCatalogObject } from '../api/reviewWorkflowApi'
import TrainingDatasetExport from '../components/TrainingDatasetExport.vue'
import type { ReviewEvidence, ReviewDoubt } from '../api/reviewWorkflowApi'
import { sourceFrameBlob, invalidateSourceFrame } from '../api/sourceFrameApi'
import { clone, equal, patches, metrics, dragBox, crop, type ReviewObject, type Box } from '../review/geometry'
import { useRouter, addLeaveGuard } from '../router'
import { useAuth } from '../stores/auth'
import { useViewportNavigation } from '../workbench/useViewportNavigation'
import { useWorkbenchFocus } from '../workbench/useWorkbenchFocus'

const evidence=ref<ReviewEvidence|null>(null), evidenceError=ref(''), doubtsOnlyPending=ref(true)
const exportOpen=ref(false), exportBusy=ref(false)
const evidenceReady=computed(()=>session.value?.workflow!=='concentrated'||!!evidence.value)
const selectedDoubt=ref<ReviewDoubt|null>(null), doubtLimit=ref(30), doubtCategory=ref('all')
const doubtBox=computed(()=>selectedDoubt.value?.samples.find(s=>s.frameIndex===fi.value)?.bbox)
async function loadEvidence(sid:string) {
  evidence.value=null;evidenceError.value='';selectedDoubt.value=null
  try {const data=await api.evidence(sid);if(session.value?.id===sid){if(data.groups.some(g=>g.endFrame>=session.value!.frameCount))throw new Error('疑点范围超出视频');evidence.value=data}}
  catch(e){if(session.value?.id===sid)evidenceError.value='疑点读取失败，请重试；不能视为没有疑点';console.error('[review.evidence_load_failed]',{sid,error:e})}
}
const doubtReviewed=(g:ReviewDoubt)=>Array.from({length:g.endFrame-g.startFrame+1},(_,i)=>g.startFrame+i).every(i=>submittedSet.value.has(i))
const doubtPriority=(g:ReviewDoubt)=>['subject_identity','tracking_lost','backward_disagreement','check_failed'].includes(g.kind)?0:g.kind==='detector_unavailable_for_object'?2:1
const visibleDoubts=computed(()=>(evidence.value?.groups||[]).filter(g=>(!doubtsOnlyPending.value||!doubtReviewed(g))&&(doubtCategory.value==='all'||(doubtCategory.value==='candidate'?g.kind==='unlabelled_candidate':doubtCategory.value==='uncertain'?['detector_unavailable_for_object','check_failed'].includes(g.kind):!['unlabelled_candidate','detector_unavailable_for_object','check_failed'].includes(g.kind)))).sort((a,b)=>doubtPriority(a)-doubtPriority(b)||a.startFrame-b.startFrame))
watch(doubtCategory,()=>{doubtLimit.value=30})
async function openDoubt(g:ReviewDoubt) {
  await navigate(g.startFrame)
  if(fi.value!==g.startFrame)return
  selectedDoubt.value=g
  if(g.objectId!==null&&working.value.some(o=>o.objectId===g.objectId))selectObject(g.objectId,true)
  else {await nextTick();const pane=viewport.value,box=pane?.querySelector('.review-doubt-box');if(pane&&box){const a=pane.getBoundingClientRect(),b=box.getBoundingClientRect();if(b.left<a.left||b.right>a.right)pane.scrollLeft+=(b.left+b.right-a.left-a.right)/2;if(b.top<a.top||b.bottom>a.bottom)pane.scrollTop+=(b.top+b.bottom-a.top-a.bottom)/2}}

}
async function reopenReview() {
  if(!session.value||busy.value)return
  modal.value=null
  pending={kind:'reopen',key:createRequestId(),sessionId:session.value.id,fi:fi.value,body:{expectedSessionRevision:session.value.revision,frameIndex:fi.value}}
  await perform('reopen')
}
const router=useRouter()
const sessions=ref<Session[]>([]), session=ref<Session|null>(null), frame=ref<Frame|null>(null)
const working=ref<ReviewObject[]>([]), selected=ref<number|null>(null), imageUrl=ref('')
const filter=ref('all'), query=ref(''), busy=ref(false), error=ref(''), saveLabel=ref('无待保存内容'), notice=ref('')
const modal=ref<'resume'|'draft'|'complete'|'help'|'discard'|'conflict'|'add'|'withdraw'|'return'|'reopen'|null>(null)
const dialog=ref<HTMLElement|null>(null)
let priorFocus:HTMLElement|null=null
watch(modal,async(value,previous)=>{
  if(value){navigation.blur();if(!previous)priorFocus=document.activeElement as HTMLElement;await nextTick();dialog.value?.querySelector<HTMLElement>('button')?.focus()}
  else priorFocus?.focus()
})
const historical=ref<number|null>(null), jump=ref('1'), overlay=ref(false), hiddenBoxes=ref(false), zoom=ref(1), playing=ref(false)
const { focused, toggleFocus: changeFocus }=useWorkbenchFocus()
const focusButton=ref<HTMLButtonElement|null>(null)
const autoFit=ref(true)
const viewport=ref<HTMLElement|null>(null), drawing=ref<SVGSVGElement|null>(null), jumpInput=ref<HTMLInputElement|null>(null)
const history=ref<ReviewObject[][]>([])
const objectFilter=ref<'all'|'changed'|'deleted'>('all'), objectQuery=ref('')
const drawMode=ref(false),newBox=ref<Box|null>(null),catalog=ref<ReviewCatalogObject[]>([]),catalogError=ref(''),objectChoice=ref('new')
const addClass=ref('')
const taskTarget=ref<Session|null>(null),returnReason=ref(''),taskError=ref('')
const pendingTask=ref<{kind:'withdraw'|'return';sid:string;revision:number;reason:string;key:string}|null>(null)
const pendingObject=ref<{sid:string;revision:number;key:string;bbox:Box;classKey?:string;frameRevision:number;working:ReviewObject[]}|null>(null)
const objectList=ref<HTMLElement|null>(null)
const handles=['tl','tr','bl','br']
const operationJournal=()=>`review-operation:${useAuth().user.value?.id}`
function rememberOperation(){
  try{
    if(pendingTask.value||pendingObject.value||pending)sessionStorage.setItem(operationJournal(),JSON.stringify({task:pendingTask.value,object:pendingObject.value,frame:pending,working:pending?working.value:undefined,frameIndex:fi.value}))
    else sessionStorage.removeItem(operationJournal())
    return true
  }catch(e){console.error('[review.operation_journal_failed]',e);return false}
}
let resizeObserver:ResizeObserver|null=null, alive=true, playbackTimer:ReturnType<typeof setTimeout>|null=null
let pointer:{id:number;index:number;startX:number;startY:number;box:Box;before:ReviewObject[];handle:string}|null=null
let drawPointer:{id:number;x:number;y:number}|null=null
const gestureActive=ref(false)
const viewBlocked=computed(()=>exportOpen.value||!session.value||!frame.value||!imageUrl.value||!!modal.value||gestureActive.value)
const navigation=useViewportNavigation({viewport,stage:drawing,scale:zoom,minScale:.1,maxScale:5,
  blocked:()=>viewBlocked.value||!!pointer||!!drawPointer,
  beforeZoom:()=>{autoFit.value=false}})
let requestPromise:Promise<Mutation|null>|null=null
let pending:{kind:'draft'|'submit'|'discard'|'finish'|'reopen'|'reopen';key:string;body:any;sessionId:string;fi:number}|null=null
let removeGuard=()=>{}
const drawerOpen=ref(false)
// Per-frame state for the frame strip: submitted / draft / not yet reviewed,
// plus frames the confirmer sent back. The current frame reflects local edits.
const submittedSet=computed(()=>new Set(session.value?.resume.submittedFrameIndexes||[]))
const draftSet=computed(()=>new Set(session.value?.resume.draftFrameIndexes||[]))
const returnedSet=computed(()=>new Set((session.value?.returnRequests||[]).map(r=>r.frameIndex)))
function reviewFrameKind(f:number){if(f===fi.value&&frame.value){if(dirty.value||frame.value.hasDraft)return 'draft';if(frame.value.state==='submitted')return 'submitted'}return submittedSet.value.has(f)?'submitted':draftSet.value.has(f)?'draft':'empty'}
const reviewFrameFlags=(f:number)=>returnedSet.value.has(f)?['returned']:[]
function reviewFrameTitle(f:number){const k=reviewFrameKind(f);return `第 ${f+1} 帧 · ${k==='submitted'?'已提交':k==='draft'?'草稿未提交':'未审查'}${returnedSet.value.has(f)?' · 被退回重审':''}`}
const frameStateText=computed(()=>session.value?.state==='reviewed'?'已完成 · 只读':frame.value?.state==='submitted'&&!dirty.value?'本帧已提交':frame.value?.hasDraft||dirty.value?'未提交草稿':'本帧未审查')
const frameStateClass=computed(()=>session.value?.state==='reviewed'||(frame.value?.state==='submitted'&&!dirty.value)?'ok':frame.value?.hasDraft||dirty.value?'warn':'')
const filteredByText=computed(()=>sessions.value.filter(s=>`${s.media.name} ${s.mediaId}`.toLowerCase().includes(query.value.toLowerCase())))
const visible=computed(()=>filteredByText.value.filter(s=>filter.value==='all'||s.state===filter.value))
const rows=computed(()=>{
  if(!frame.value)return []
  const original=new Map(frame.value.baselineObjects.map(o=>[o.objectId,o]))
  const effective=working.value.map(o=>{const a=original.get(o.objectId);return {...o,a:a?.bbox??null,m:a?metrics(a.bbox,o.bbox):null,kind:a?'update':'add'}})
  const deleted=frame.value.baselineObjects.filter(o=>!working.value.some(x=>x.objectId===o.objectId)).map(o=>({...o,a:o.bbox,m:null,kind:'delete'}))
  return [...effective,...deleted]
})
const changed=computed(()=>rows.value.filter(r=>r.m||r.kind!=='update'))
const visibleObjects=computed(()=>rows.value.filter(r=>(objectFilter.value==='deleted'?r.kind==='delete':objectFilter.value==='changed'?(!!r.m||r.kind!=='update'):r.kind!=='delete')&&String(r.objectId).padStart(3,'0').includes(objectQuery.value.trim().replace(/^#/,''))))
const deletedCount=computed(()=>rows.value.filter(r=>r.kind==='delete').length)
const rowById=computed(()=>new Map(rows.value.map(r=>[r.objectId,r])))
const addCandidates=computed(()=>catalog.value.filter(o=>!working.value.some(x=>x.objectId===o.objectId)))
const addClasses=computed(()=>{const values=[...new Set(catalog.value.map(o=>o.classKey))];return values.length?values:['rare sperm']})
const selectedRow=computed(()=>rows.value.find(r=>r.objectId===selected.value))
const patch=computed(()=>frame.value?patches(frame.value.baselineObjects,working.value):[])
const dirty=computed(()=>!!frame.value&&!equal(patch.value,frame.value.patch))
const canEdit=computed(()=>!!session.value?.permissions.canEdit&&!!imageUrl.value&&!busy.value&&!pending&&!pendingTask.value&&!pendingObject.value)
const canSubmit=computed(()=>canEdit.value&&!!frame.value&&(frame.value.state!=='submitted'||dirty.value))
const isReadonly=computed(()=>!!session.value&&!session.value.permissions.canEdit)
const total=computed(()=>session.value?.frameCount||0)
const fi=computed(()=>frame.value?.frameIndex??0)
const mac=/Mac|iPhone|iPad/.test(navigator.platform), primaryKey=mac?'⌘':'Ctrl+'
const count=(state:string)=>state==='all'?filteredByText.value.length:filteredByText.value.filter(s=>s.state===state).length
const status=(s:string)=>({pending:'待审查',in_progress:'审查中',reviewed:'已审查',withdrawn:'已撤回送审',returned_to_annotation:'已退回标注'}[s]||'历史只读任务')
const rowState=(r:typeof rows.value[number])=>r.kind==='add'?'补标':r.kind==='delete'?'已删除':r.m?'已调整':'未调整'
const number=(n:number,d=1)=>Number(n.toFixed(d)).toString()
const signed=(n:number)=>(n>0?'+':'')+number(n,3)
const coords=(b:Box)=>`${number(b[0],3)}, ${number(b[1],3)}, ${number(b[2]-b[0],3)} × ${number(b[3]-b[1],3)}`
function time(index:number,fps= session.value?.media.fps||30) { const ms=Math.round(index/fps*1000);return `${String(Math.floor(ms/60000)).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}` }
function showError(e:any,action:string) {
  const f=e as Failure
  error.value=`${f.message||'网络连接失败，请重试'}${f.requestId?`（记录号 ${f.requestId}）`:''}`
  console.error('[review.operation_failed]',{action,sessionId:session.value?.id,frameIndex:fi.value,code:f.code,requestId:f.requestId,message:f.message})
  stopPlayback()
}
function updateSession(s:Session) { session.value=s; sessions.value=sessions.value.map(x=>x.id===s.id?s:x) }
async function list() { try { sessions.value=(await api.list()).items } catch(e) { showError(e,'list') } }
function applyFrame(f:Frame) { frame.value=f;working.value=clone(f.effectiveObjects);jump.value=String(f.frameIndex+1) }
function fit() {
  if(!viewport.value||!session.value||!imageUrl.value||!frame.value||pointer||drawPointer||navigation.panning.value) return
  autoFit.value=true
  // Like annotation, a fitted view fills the canvas; small sources are scaled up too.
  zoom.value=Math.max(.01,Math.min((viewport.value.clientWidth-24)/session.value.media.width,(viewport.value.clientHeight-24)/session.value.media.height,5))
}
function changeZoom(delta:number) {
  void navigation.zoomBy(delta)
}
function wheel(e:WheelEvent){if(e.ctrlKey||e.metaKey){e.preventDefault();if(!e.deltaY)return;void navigation.zoomBy(e.deltaY<0?.15:-.15,{x:e.clientX,y:e.clientY})}}
function panStart(e:PointerEvent){if(navigation.startPan(e))stopPlayback()}
function panMove(e:PointerEvent){navigation.movePan(e)}
function panEnd(e:PointerEvent){navigation.endPan(e)}
function panCancel(e:PointerEvent){if(!navigation.panning.value)return;e.stopPropagation();console.info('[review.pan_cancelled]',{sessionId:session.value?.id,frameIndex:fi.value,reason:e.type});navigation.cancelPan()}
function panLost(e:PointerEvent){if(navigation.panning.value)panCancel(e)}
function panAuxClick(e:MouseEvent){if(e.button===1)e.preventDefault()}
function blur(){navigation.blur();cancelGesture('window-blur')}
async function toggleFocus(){
  if(viewBlocked.value||navigation.panning.value)return
  stopPlayback();navigation.blur()
  const manual=!autoFit.value,anchor=manual?navigation.captureAnchor():null
  // A manual rendering scale survives layout changes. A fitted view uses the
  // larger focus viewport instead, without changing selection or frame data.
  await changeFocus()
  if(manual)await navigation.restoreAnchor(anchor);else fit()
  focusButton.value?.focus({preventScroll:true})
}
let pendingBookmark:{sid:string;index:number;revision:number;key:string}|null=null
async function bookmark() {
  if(!session.value||!frame.value) return
  const sid=session.value.id,index=fi.value
  if(!pendingBookmark||pendingBookmark.sid!==sid||pendingBookmark.index!==index)
    pendingBookmark={sid,index,revision:session.value.resume.cursorRevision,key:createRequestId()}
  try {
    let op=pendingBookmark
    let result
    try {result=await api.cursor(sid,index,op.revision,op.key)}
    catch(e:any) {
      if(e.code!=='CURSOR_REVISION_CONFLICT')throw e
      // A different window (or a response lost before navigation) advanced the cursor.
      const fresh=await api.session(sid)
      if(session.value?.id!==sid||fi.value!==index)return
      op={sid,index,revision:fresh.resume.cursorRevision,key:createRequestId()};pendingBookmark=op
      result=await api.cursor(sid,index,op.revision,op.key)
    }
    if(session.value?.id===sid&&fi.value===index){
      session.value.resume.cursorRevision=result.revision;session.value.resume.lastViewedFrameIndex=index
      pendingBookmark=null;notice.value=''
    }
  } catch(e) { notice.value='浏览位置尚未同步；已提交结果不受影响。';console.warn('[review.cursor_failed]',{sessionId:sid,frameIndex:index,error:e}) }
}
async function loadFrame(index:number,showDraft=true) {
  if(!session.value||index<0||index>=total.value) return false
  busy.value=true;error.value=''
  let url=''
  try {
    const [f,blob]=await Promise.all([api.frame(session.value.id,index),sourceFrameBlob(session.value.mediaId,index)])
    url=URL.createObjectURL(blob)
    await new Promise<void>((resolve,reject)=>{const img=new Image();img.onload=()=>resolve();img.onerror=()=>reject(new Error('帧图像无法解码'));img.src=url})
      .catch(e=>{invalidateSourceFrame(session.value!.mediaId,index);throw e})
    if(!alive){URL.revokeObjectURL(url);return false}
    if(imageUrl.value) URL.revokeObjectURL(imageUrl.value)
    imageUrl.value=url;applyFrame(f);history.value=[];selected.value=null;drawMode.value=false;newBox.value=null
    saveLabel.value=f.hasDraft?'已有未提交草稿':f.state==='submitted'?'本帧已提交':'无待保存内容'
    if(showDraft&&f.hasDraft) modal.value='draft'
    await bookmark()
    return true
  }catch(e){if(url)URL.revokeObjectURL(url);showError(e,'load_frame');return false}
  finally{busy.value=false}
}
async function open(s:Session,claim=true) {
  stopPlayback();navigation.blur()
  if(!await flush()) return
  busy.value=true;error.value=''
  try {
    let detail=await api.session(s.id)
    if(claim&&detail.permissions.canClaim) detail=(await api.claim(s.id,createRequestId())).session
    updateSession(detail);historical.value=detail.resume.lastViewedFrameIndex
    const target=detail.state==='reviewed'?(historical.value??0):(detail.resume.firstUnsubmittedFrameIndex??historical.value??0)
    await loadEvidence(detail.id)
    frame.value=null;working.value=[];pending=null
    if(imageUrl.value)URL.revokeObjectURL(imageUrl.value)
    imageUrl.value=''
    if(await loadFrame(target)) {
      await nextTick();fit()
      if(!(frame.value as Frame|null)?.hasDraft&&historical.value!==null&&detail.state!=='reviewed') modal.value='resume'
    }
  }catch(e){showError(e,'open_session')}
  finally{busy.value=false}
}
async function runPending():Promise<Mutation|null> {
  if(!pending||!session.value) return null
  const op=pending
  busy.value=true;error.value='';saveLabel.value=op.kind==='submit'?'提交中…':op.kind==='finish'?'正在完成视频…':'保存中…'
  try {
    if(!rememberOperation())throw new Error('浏览器无法保留保存请求，请检查浏览器存储后重试')
    let result=op.kind==='reopen'?await api.reopen(op.sessionId,op.body,op.key):op.kind==='finish'?await api.finish(op.sessionId,op.body.expectedSessionRevision,op.key):await api.mutate(op.sessionId,op.fi,op.kind,op.body,op.key)
    if(!alive)return result
    // A recovered idempotency receipt can precede another client's later edit.
    // Resolve the original write first, then read current state without rebasing
    // the old local patch onto a newer revision.
    if(result.session.revision<session.value.revision||(result.frame&&frame.value&&result.frame.frameRevision<frame.value.frameRevision)){
      const [latest,latestFrame]=await Promise.all([api.session(op.sessionId),api.frame(op.sessionId,op.fi)])
      result={...result,session:latest,frame:latestFrame}
      console.info('[review.receipt_refreshed]',{sessionId:op.sessionId,frameIndex:op.fi,revision:latest.revision})
    }
    updateSession(result.session)
    if(result.frame)applyFrame(result.frame)
    pending=null;rememberOperation()
    saveLabel.value=op.kind==='submit'?`第 ${op.fi+1} 帧已提交`:op.kind==='discard'?'已放弃草稿':op.kind==='finish'?'视频审查已完成':frame.value?.hasDraft?'草稿已保存，尚未提交':'已保存，无待提交修改'
    console.info('[review.operation_succeeded]',{action:op.kind,sessionId:op.sessionId,frameIndex:op.fi,revision:result.session.revision})
    return result
  }catch(e){saveLabel.value='保存失败，编辑已保留';showError(e,op.kind);return null}
  finally{busy.value=false}
}
async function perform(kind:'draft'|'submit'|'discard'|'finish'|'reopen') {
  if(!session.value||!frame.value)return null
  if(requestPromise) return requestPromise
  if(pending&&pending.kind!==kind) {error.value='请先重试未完成的请求，或处理版本冲突。';return null}
  if(!pending)pending={kind,key:createRequestId(),sessionId:session.value.id,fi:fi.value,body:kind==='finish'?{expectedSessionRevision:session.value.revision}:
    {expectedFrameRevision:frame.value.frameRevision,...(kind==='draft'||kind==='submit'?{patch:clone(patch.value)}:{})}}
  requestPromise=runPending()
  const r=await requestPromise;requestPromise=null;return r
}
async function flush() {
  if(pointer||drawPointer||newBox.value||pendingTask.value||pendingObject.value)return false
  if(requestPromise&&!await requestPromise)return false
  if(pending) {if(!await perform(pending.kind))return false}
  if(dirty.value&&!await perform('draft'))return false
  return true
}
async function navigate(index:number,fromPlayback=false) {
  if(!fromPlayback)stopPlayback()
  if(busy.value||modal.value||!session.value||index===fi.value)return
  if(!await flush())return
  await loadFrame(index)
}
async function submit() {
  if(!canSubmit.value)return
  stopPlayback()
  const r=await perform('submit')
  if(!r)return
  history.value=[]
  if(r.nextUnsubmittedFrameIndex!=null) {const submitted=fi.value+1;await loadFrame(r.nextUnsubmittedFrameIndex);saveLabel.value=`第 ${submitted} 帧已提交`}
  else modal.value='complete'
}
async function discard() {
  modal.value=null
  if(pending) {error.value='存在结果未确认的请求，请先重试或处理冲突。';return}
  // Unsaved local edits are removed only after the server has confirmed discard.
  const r=await perform('discard');if(r)history.value=[]
}
async function complete() {modal.value=null;stopPlayback();if(!await flush())return;if(!session.value?.permissions.canComplete||!evidenceReady.value||evidenceError.value)return;await perform('finish')}
async function reloadConflict() {
  // User explicitly chooses to discard the local snapshot. Keep it until reload succeeds.
  modal.value=null;busy.value=true
  const oldPending=pending, oldWorking=clone(working.value)
  try {const s=await api.session(session.value!.id);updateSession(s);if(await loadFrame(fi.value)){pending=null;rememberOperation();error.value=''}else{pending=oldPending;working.value=oldWorking}}
  catch(e){pending=oldPending;working.value=oldWorking;showError(e,'reload_conflict')}
  finally{busy.value=false}
}
async function retry() { if(pending){const kind=pending.kind;const r=await perform(kind);if(r&&kind==='submit'&&r.session.permissions.canComplete)modal.value='complete'}else if(session.value)await loadFrame(fi.value);else await list() }
function downloadLocal() {
  const blob=new Blob([JSON.stringify({sessionId:session.value?.id,frameIndex:fi.value,expectedFrameRevision:frame.value?.frameRevision,patch:patch.value},null,2)],{type:'application/json'})
  const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`review-local-frame-${fi.value+1}.json`;a.click();URL.revokeObjectURL(url)
}
function selectObject(id:number,locate=false) {
  selected.value=id
  if(!visibleObjects.value.some(r=>r.objectId===id)){objectFilter.value=rowById.value.get(id)?.kind==='delete'?'deleted':'all';objectQuery.value=''}
  nextTick(()=>{
    // Scroll only the owning workbench panes. scrollIntoView also moves the page.
    const list=objectList.value,row=list?.querySelector<HTMLElement>(`[data-row="${id}"]`)
    if(list&&row){
      const pane=list.getBoundingClientRect(),item=row.getBoundingClientRect()
      if(item.top<pane.top)list.scrollTop+=item.top-pane.top
      else if(item.bottom>pane.bottom)list.scrollTop+=item.bottom-pane.bottom
    }
    const pane=viewport.value,box=pane?.querySelector<SVGElement>(`[data-box="${id}"]`)
    if(locate&&pane&&box){
      const bounds=pane.getBoundingClientRect(),target=box.getBoundingClientRect()
      if(target.left<bounds.left||target.right>bounds.right)pane.scrollLeft+=(target.left+target.right-bounds.left-bounds.right)/2
      if(target.top<bounds.top||target.bottom>bounds.bottom)pane.scrollTop+=(target.top+target.bottom-bounds.top-bounds.bottom)/2
    }
  })
}
function pointerDown(e:PointerEvent,index:number,handle='move') {
  if(drawMode.value){startDraw(e);return}
  if(e.button!==0||busy.value||pending||modal.value)return
  stopPlayback();selectObject(working.value[index].objectId)
  if(!canEdit.value)return
  e.preventDefault();drawing.value?.setPointerCapture(e.pointerId)
  gestureActive.value=true
  pointer={id:e.pointerId,index,startX:e.clientX,startY:e.clientY,box:clone(working.value[index].bbox),before:clone(working.value),handle}
}
function pointerMove(e:PointerEvent) {
  if(drawPointer&&drawPointer.id===e.pointerId){const p=imagePoint(e);newBox.value=[Math.min(drawPointer.x,p[0]),Math.min(drawPointer.y,p[1]),Math.max(drawPointer.x,p[0]),Math.max(drawPointer.y,p[1])];return}
  if(!pointer||pointer.id!==e.pointerId||!session.value)return
  const p=pointer
  working.value[p.index].bbox=dragBox(p.box,(e.clientX-p.startX)/zoom.value,(e.clientY-p.startY)/zoom.value,p.handle,session.value.media.width,session.value.media.height)
}
function pointerEnd(e?:PointerEvent) {
  if(drawPointer){const id=drawPointer.id;drawPointer=null;gestureActive.value=false;if(drawing.value?.hasPointerCapture(id))drawing.value.releasePointerCapture(id);if(newBox.value&&newBox.value[2]-newBox.value[0]>=1&&newBox.value[3]-newBox.value[1]>=1){drawMode.value=false;objectChoice.value='new';modal.value='add';void loadCatalog()}else newBox.value=null;return}
  if(!pointer)return
  const before=pointer.before,id=pointer.id;pointer=null;gestureActive.value=false
  if(drawing.value?.hasPointerCapture(id))drawing.value.releasePointerCapture(id)
  if(!equal(patches(frame.value!.baselineObjects,before),patch.value)) {
    history.value.push(before);if(history.value.length>50)history.value.shift()
    void perform('draft')
  }
}
function cancelGesture(reason:string|Event='pointer-cancel') {
  if(pointer||drawPointer)console.info('[review.gesture_cancelled]',{sessionId:session.value?.id,frameIndex:fi.value,kind:drawPointer?'draw':'box',reason:typeof reason==='string'?reason:reason.type})
  gestureActive.value=false
  if(drawPointer){const id=drawPointer.id;drawPointer=null;newBox.value=null;if(drawing.value?.hasPointerCapture(id))drawing.value.releasePointerCapture(id);return}
  if(!pointer)return
  working.value=pointer.before;const id=pointer.id;pointer=null
  if(drawing.value?.hasPointerCapture(id))drawing.value.releasePointerCapture(id)
}
async function undo() {if(!canEdit.value||!history.value.length)return;working.value=history.value.pop()!;if(selected.value!==null){if(!rows.value.some(r=>r.objectId===selected.value))selected.value=null;else selectObject(selected.value)}await perform('draft')}
function imagePoint(e:PointerEvent):[number,number] {
  const bounds=drawing.value!.getBoundingClientRect(),media=session.value!.media
  return [Math.max(0,Math.min(media.width,(e.clientX-bounds.left)/zoom.value)),Math.max(0,Math.min(media.height,(e.clientY-bounds.top)/zoom.value))]
}
function startDraw(e:PointerEvent){
  if(!drawMode.value){selected.value=null;return}
  if(e.button!==0||!canEdit.value||modal.value)return
  e.preventDefault();stopPlayback();const [x,y]=imagePoint(e);drawPointer={id:e.pointerId,x,y};gestureActive.value=true;newBox.value=[x,y,x,y];drawing.value?.setPointerCapture(e.pointerId)
}
async function loadCatalog(){
  if(!session.value)return
  catalogError.value='';busy.value=true
  try{catalog.value=(await api.objects(session.value.id)).items;if(!addClasses.value.includes(addClass.value))addClass.value=addClasses.value.length===1?addClasses.value[0]:''}
  catch(e:any){catalogError.value=e.message||'对象目录读取失败，请重试';console.error('[review.catalog_failed]',{sessionId:session.value.id,error:e})}
  finally{busy.value=false}
}
async function commitAddition(){
  if(!session.value||!newBox.value||busy.value||(catalogError.value&&!pendingObject.value))return
  busy.value=true;catalogError.value=''
  let reservationFrameRevision:number|undefined
  try{
    let object:ReviewCatalogObject
    if(pendingObject.value||objectChoice.value==='new'){
      if(!pendingObject.value&&!addClass.value)throw new Error('请选择补标类别')
      if(!pendingObject.value)pendingObject.value={sid:session.value.id,revision:session.value.revision,key:createRequestId(),bbox:clone(newBox.value),classKey:addClass.value,frameRevision:frame.value!.frameRevision,working:clone(working.value)}
      if(!rememberOperation())throw new Error('浏览器无法保留补标请求，请检查浏览器存储后重试')
      const op=pendingObject.value,result=await api.reserveObject(op.sid,op.revision,op.key,op.classKey)
      if(result.session.revision>=session.value.revision)updateSession(result.session);object=result.object;newBox.value=op.bbox
      reservationFrameRevision=op.frameRevision;working.value=clone(op.working)
      if(!catalog.value.some(o=>o.objectId===object.objectId))catalog.value.push(object)
      pendingObject.value=null
    }else{
      const candidate=addCandidates.value.find(o=>o.objectId===Number(objectChoice.value))
      if(!candidate)throw new Error('该对象在本帧已存在，请重新选择')
      object=candidate
    }
    const before=clone(working.value)
    const existing=working.value.find(o=>o.objectId===object.objectId)
    if(existing)existing.bbox=clone(newBox.value!)
    else working.value.push({objectId:object.objectId,annotationId:`review-${object.objectId}`,classKey:object.classKey,bbox:clone(newBox.value!)})
    history.value.push(before);if(history.value.length>50)history.value.shift()
    selected.value=object.objectId;objectFilter.value='all';objectQuery.value='';newBox.value=null;modal.value=null
    if(reservationFrameRevision!==undefined)pending={kind:'draft',key:createRequestId(),sessionId:session.value.id,fi:fi.value,body:{expectedFrameRevision:reservationFrameRevision,patch:clone(patch.value)}}
    console.info('[review.box_added]',{sessionId:session.value.id,frameIndex:fi.value,objectId:object.objectId})
  }catch(e:any){catalogError.value=e.message||'补标失败，请重试原请求';console.error('[review.add_failed]',{sessionId:session.value.id,error:e});if(e.status&&e.status<500){pendingObject.value=null;rememberOperation();try{updateSession(await api.session(session.value.id))}catch{}}}
  finally{busy.value=false}
  if(!modal.value)await perform('draft')
}
function closeModal(){if(busy.value||pendingObject.value||pendingTask.value)return;if(modal.value==='add')newBox.value=null;modal.value=null}
async function deleteSelected(){
  if(!canEdit.value||selected.value===null)return
  const index=working.value.findIndex(o=>o.objectId===selected.value);if(index<0)return
  history.value.push(clone(working.value));if(history.value.length>50)history.value.shift()
  const [removed]=working.value.splice(index,1)
  if(frame.value?.baselineObjects.some(o=>o.objectId===removed.objectId)){objectFilter.value='deleted'}else selected.value=null
  console.info('[review.box_deleted]',{sessionId:session.value?.id,frameIndex:fi.value,objectId:removed.objectId,scope:'frame'})
  await perform('draft')
}
async function restoreSelected(){
  if(!canEdit.value||selected.value===null||working.value.some(o=>o.objectId===selected.value))return
  const original=frame.value?.baselineObjects.find(o=>o.objectId===selected.value);if(!original)return
  history.value.push(clone(working.value));if(history.value.length>50)history.value.shift()
  working.value.push(clone(original));objectFilter.value='all'
  await perform('draft')
}
function openTaskAction(target:Session,kind:'withdraw'|'return'){
  if(busy.value||pendingTask.value||pendingObject.value)return
  stopPlayback();taskTarget.value=target;taskError.value='';returnReason.value='';modal.value=kind
}
async function submitTaskAction(){
  if(busy.value||!taskTarget.value)return
  const kind=modal.value==='withdraw'?'withdraw':'return'
  if(kind==='return'&&!returnReason.value.trim()&&!pendingTask.value){taskError.value='请填写退回原因，便于标注员修改';return}
  if(!pendingTask.value&&!await flush()){taskError.value='本帧修改尚未保存，请先关闭此窗口并处理保存失败，再执行任务操作。';return}
  busy.value=true;taskError.value=''
  if(!pendingTask.value)pendingTask.value={kind,sid:taskTarget.value.id,revision:session.value?.id===taskTarget.value.id?session.value.revision:taskTarget.value.revision,reason:returnReason.value.trim(),key:createRequestId()}
  const op=pendingTask.value
  try{
    if(!rememberOperation())throw new Error('浏览器无法保留任务操作，请检查浏览器存储后重试')
    const result=op.kind==='withdraw'?await api.withdraw(op.sid,op.revision,op.key):await api.returnAnnotation(op.sid,op.revision,op.reason,op.key)
    if(session.value?.id===result.session.id)updateSession(result.session)
    pendingTask.value=null;rememberOperation();modal.value=null
    await list();notice.value=op.kind==='withdraw'?'已撤回送审，原标注保留。请回人工标注修改后重新送审。':'已退回重新标注。原标注与本次审查记录保留，旧任务停止流转。'
    console.info('[review.task_action_succeeded]',{action:op.kind,sessionId:op.sid,revision:result.session.revision})
  }catch(e:any){taskError.value=(e.message||'操作失败，请重试原请求')+(e.requestId?`（记录号 ${e.requestId}）`:'');console.error('[review.task_action_failed]',{action:op.kind,sessionId:op.sid,error:e});if(e.status&&e.status<500){pendingTask.value=null;rememberOperation();await list();try{const latest=await api.session(op.sid);taskTarget.value=latest;if(session.value?.id===latest.id)updateSession(latest)}catch{}}}
  finally{busy.value=false}
}
function stopPlayback(){playing.value=false;if(playbackTimer)clearTimeout(playbackTimer);playbackTimer=null}
async function playTick() {
  if(!playing.value)return
  if(fi.value>=total.value-1||modal.value||error.value){stopPlayback();return}
  await navigate(fi.value+1,true)
  if(playing.value)playbackTimer=setTimeout(playTick,Math.max(40,1000/(session.value?.media.fps||30)))
}
async function togglePlay(){if(playing.value){stopPlayback();return}if(!await flush())return;playing.value=true;void playTick()}
function doJump(){const n=Number(jump.value);if(!/^\d+$/.test(jump.value)||!Number.isSafeInteger(n)||n<1||n>total.value){error.value=`请输入 1～${total.value} 的整数帧号`;return}jumpInput.value?.blur();void navigate(n-1)}
function esc(){if(modal.value){closeModal();return}if(pointer||drawPointer){cancelGesture('escape');return}if(navigation.panning.value){navigation.cancelPan();return}if(focused.value){void toggleFocus();return}if(drawMode.value){drawMode.value=false;return}if(document.activeElement===jumpInput.value){jump.value=String(fi.value+1);jumpInput.value?.blur();return}hiddenBoxes.value=false;selected.value=null}
function keydown(e:KeyboardEvent) {
  if(exportOpen.value||document.querySelector('[data-user-guide][open]'))return

  if(e.isComposing||e.keyCode===229||e.repeat)return
  if(e.key==='Escape'&&modal.value){esc();e.preventDefault();return}
  if(modal.value){
    if(e.key==='Tab'){
      const items=Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')||[])
      if(items.length){const i=items.indexOf(document.activeElement as HTMLElement);items[(i+(e.shiftKey?-1:1)+items.length)%items.length]?.focus();e.preventDefault()}
    }
    return
  }
  const target=e.target as HTMLElement
  if(target.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) {if(target===jumpInput.value){if(e.key==='Enter'&&!e.ctrlKey&&!e.metaKey&&!e.altKey){e.preventDefault();doJump()}else if(e.key==='Escape'){jump.value=String(fi.value+1);jumpInput.value?.blur();e.preventDefault()}}return}
  if(e.key==='Escape'){esc();e.preventDefault();return}
  const key=e.key.toLowerCase()
  // Viewing stays available while a write is pending or this account is read
  // only. These actions never save a patch, submit a frame or choose an object.
  if(!e.ctrlKey&&!e.metaKey&&!e.altKey&&session.value&&frame.value&&imageUrl.value&&!pointer&&!drawPointer&&!navigation.panning.value){
    if(e.key===' '){if(target.closest('button,summary,[role="button"]'))return;e.preventDefault();navigation.spaceHeld.value=true;return}
    if(key==='+'||key==='='){e.preventDefault();changeZoom(.25);return}
    if(key==='-'){e.preventDefault();changeZoom(-.25);return}
    if(key==='0'){e.preventDefault();fit();return}
    if(key==='h'){e.preventDefault();hiddenBoxes.value=!hiddenBoxes.value;return}
    if(key==='f'){e.preventDefault();void toggleFocus();return}
  }
  if(key==='m'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!focused.value){e.preventDefault();drawerOpen.value=!drawerOpen.value;return}
  if(busy.value||pointer||drawPointer||navigation.panning.value||!session.value)return
  const mod=e.ctrlKey||e.metaKey
  if(mod&&!e.altKey&&!e.shiftKey&&(e.key.toLowerCase()==='z'||e.key==='Enter')){e.preventDefault();void(e.key==='Enter'?submit():undo());return}
  if(e.ctrlKey||e.metaKey||e.altKey)return
  if(e.key===' '&&target.closest('button,summary'))return
  switch(e.key){
    case 'ArrowLeft':e.preventDefault();void navigate(Math.max(0,fi.value-(e.shiftKey?10:1)));break
    case 'ArrowRight':e.preventDefault();void navigate(Math.min(total.value-1,fi.value+(e.shiftKey?10:1)));break
    case '[':case ']':{e.preventDefault();const i=working.value.findIndex(o=>o.objectId===selected.value);const next=i<0?(e.key===']'?0:working.value.length-1):Math.max(0,Math.min(working.value.length-1,i+(e.key===']'?1:-1)));if(working.value[next])selectObject(working.value[next].objectId,true);break}
    case 'g':case 'G':e.preventDefault();jumpInput.value?.focus();jumpInput.value?.select();break
    case 'o':case 'O':e.preventDefault();overlay.value=!overlay.value;break
    case 'b':case 'B':if(canEdit.value){e.preventDefault();drawMode.value=true;stopPlayback()}break
    case 'v':case 'V':e.preventDefault();drawMode.value=false;break
    case 'Delete':case 'Backspace':if(canEdit.value){e.preventDefault();void deleteSelected()}break
    case '?':e.preventDefault();modal.value='help';stopPlayback();break
    case 'k':case 'K':e.preventDefault();void togglePlay();break
  }
}
function beforeUnload(e:BeforeUnloadEvent){if(dirty.value||pending||busy.value||newBox.value||pendingTask.value||pendingObject.value){e.preventDefault();e.returnValue=''}}
async function restoreOperation(){
  let saved:any
  try{saved=JSON.parse(sessionStorage.getItem(operationJournal())||'null')}catch(e){console.error('[review.operation_journal_failed]',e);error.value='上次任务操作无法恢复，请检查浏览器存储。';return false}
  if(!saved?.task&&!saved?.object&&!saved?.frame)return false
  const op=saved.task||saved.object||{sid:saved.frame.sessionId}
  try{
    const detail=await api.session(op.sid)
    await open(detail,false)
    if((saved.object||saved.frame)&&Number.isInteger(saved.frameIndex)&&fi.value!==saved.frameIndex)await loadFrame(saved.frameIndex,false)
    if(saved.task){pendingTask.value=saved.task;taskTarget.value=detail;returnReason.value=saved.task.reason;taskError.value='上次操作结果尚未确认，请重试原请求。';modal.value=saved.task.kind}
    else if(saved.frame){pending=saved.frame;if(Array.isArray(saved.working))working.value=clone(saved.working);modal.value=null;saveLabel.value='上次保存结果尚未确认';error.value='上次保存结果尚未确认，请重试原请求。'}
    else{pendingObject.value=saved.object;newBox.value=clone(saved.object.bbox);addClass.value=saved.object.classKey||'';objectChoice.value='new';modal.value='add';await loadCatalog();catalogError.value='上次对象分配结果尚未确认，请重试原请求。'}
    console.info('[review.operation_restored]',{sessionId:op.sid,action:saved.task?.kind||(saved.frame?'draft':'reserve'),frameIndex:saved.frameIndex})
  }catch(e){if(saved.task)pendingTask.value=saved.task;else if(saved.frame)pending=saved.frame;else pendingObject.value=saved.object;showError(e,'restore_operation');error.value+='；上次操作已保留，请恢复网络后刷新重试。'}
  return true
}
onMounted(async()=>{
  await list()
  if(!await restoreOperation())try{const target=sessionStorage.getItem('review-open-session');if(target){sessionStorage.removeItem('review-open-session');const linked=sessions.value.find(s=>s.id===target);if(linked)await open(linked)}}catch(e){console.warn('[review.navigation_target_failed]',e)}
  resizeObserver=new ResizeObserver(()=>{if(autoFit.value)fit()});if(viewport.value)resizeObserver.observe(viewport.value)
  window.addEventListener('keydown',keydown);window.addEventListener('keyup',navigation.keyUp);window.addEventListener('blur',blur);window.addEventListener('beforeunload',beforeUnload)
  removeGuard=addLeaveGuard(async()=>{stopPlayback();if(exportBusy.value)return false;return await flush()})
})
onUnmounted(()=>{alive=false;stopPlayback();cancelGesture('unmount');removeGuard();resizeObserver?.disconnect();window.removeEventListener('keydown',keydown);window.removeEventListener('keyup',navigation.keyUp);window.removeEventListener('blur',blur);window.removeEventListener('beforeunload',beforeUnload);if(imageUrl.value)URL.revokeObjectURL(imageUrl.value)})
</script>

<template>
  <section class="review-page" :data-focused="focused" aria-label="审查模式">
    <WorkbenchShell name="review" :focused="focused" focus-keeps-inspector drawer-label="审查视频" :drawer-count="sessions.length" :drawer-forced="!session" v-model:drawer-open="drawerOpen" :inspector-badge="working.length">
      <template #context>
        <strong :title="session?.media.name">{{ session?.media.name || '选择视频开始审查' }}</strong>
        <template v-if="session">
          <span class="shell-meta">第 {{ fi+1 }} / {{ total }} 帧</span>
          <span class="shell-pill" :class="frameStateClass" data-testid="frame-state">{{ frameStateText }}</span>
          <span class="shell-progress" data-testid="video-progress" :title="session.progress.draftFrames ? `${session.progress.draftFrames} 帧草稿待提交` : (session.state==='reviewed'?'视频已审查 · B 版本已固定':'逐帧提交后，再完成视频审查')"><progress :value="session.progress.percent" max="100" aria-label="视频审查进度" />已提交 {{ session.progress.submittedFrames }} / {{ total }}</span>
          <button v-if="session.state!=='reviewed'" class="btn-primary" :disabled="busy||!session.permissions.canComplete||!evidenceReady||dirty||!!pending||!!evidenceError" @click="modal='complete'">完成视频审查</button>
          <span v-else class="shell-pill ok">已完成审查</span>
        </template>
        <button class="icon-button" title="快捷键（?）" aria-label="快捷键" @click="modal='help';stopPlayback()"><AppIcon name="help" :size="16" /></button>
      </template>
      <template #drawer><div class="review-sidebar">
        <div class="review-drawer-tools"><small>已完成标注、等待或正在审查的视频</small><button class="icon-button" aria-label="刷新视频列表" title="刷新" :disabled="busy" @click="list">↻</button></div>
        <input class="review-search" v-model="query" placeholder="搜索视频名称或 ID…" aria-label="搜索视频" />
        <div class="review-filters"><button v-for="(label,key) in {all:'全部',pending:'待审查',in_progress:'审查中',reviewed:'已审查'}" :key="key" :class="{active:filter===key}" @click="filter=key">{{ label }} <small>{{ count(key) }}</small></button></div>
        <div class="review-videos">
          <div v-for="s in visible" :key="s.id" class="video-card-wrap">
          <button :data-session-id="s.id" class="video-card" :class="{selected:session?.id===s.id}" :disabled="busy" @click="open(s);drawerOpen=false">
            <div class="card-heading"><b>{{ s.media.name }}</b><span class="badge" :class="s.state">{{ status(s.state) }}</span></div>
            <small>{{ time(s.frameCount,s.media.fps) }} · {{ s.frameCount }} 帧</small>
            <progress :value="s.progress.submittedFrames" :max="s.frameCount"></progress>
            <div class="card-progress"><span>{{ s.progress.submittedFrames }} / {{ s.frameCount }} · {{ s.progress.percent.toFixed(1) }}%</span></div>
            <small>上次停留：{{ s.resume.lastViewedFrameIndex===null?'尚未浏览':`第 ${s.resume.lastViewedFrameIndex+1} 帧` }} <em v-if="s.progress.draftFrames"> · {{ s.progress.draftFrames }} 帧草稿</em></small>
          </button>
          <button v-if="s.permissions.canWithdraw" class="library-withdraw link" :disabled="busy" :data-withdraw-session="s.id" @click="openTaskAction(s,'withdraw')">撤回送审</button>
          </div>
          <p v-if="!visible.length" class="empty-state">暂无符合条件的已标注任务。<br>请先由标注端完成全视频标注并送审。</p>
        </div>
      </div></template>
      <template #alerts>
        <div v-if="notice" class="review-notice">{{ notice }} <button v-if="pendingBookmark" @click="bookmark">重试位置同步</button><button v-else class="link" @click="notice=''">知道了</button></div>
        <div v-if="session?.readOnlyReason" class="review-notice">{{ session.readOnlyReason }}</div>
        <div v-if="session?.returnRequests?.length" class="review-notice" data-testid="review-return-reason"><p v-for="request in session.returnRequests" :key="request.frameIndex">第 {{ request.frameIndex+1 }} 帧需重审：{{ request.reason }}。重新检查后请提交本帧，再完成视频审查。</p></div>
        <div v-if="session?.state==='withdrawn'" class="review-notice">送审者已撤回此任务。原始快照保留为历史记录，当前任务不能继续审查。</div>
        <div v-else-if="session?.state==='returned_to_annotation'" class="review-notice">已退回重新标注：{{ session.returnAnnotation?.reason }}。本次原始标注和审查记录保留，请回人工标注修改后重新送审。<button class="link" @click="router.push('/annotate')">前往人工标注</button></div>
        <div v-else-if="isReadonly&&session?.state!=='reviewed'" class="review-notice">当前账号仅可查看。任务由已领取的审查员编辑；你也可以领取尚未分配的任务，检查自己的标注。</div>
      </template>
      <template #canvas>
        <div ref="viewport" class="review-viewport" aria-label="完整帧画面" :class="{'annotations-hidden':hiddenBoxes,panning:navigation.panning.value,'pan-ready':navigation.spaceHeld.value}" @wheel="wheel" @pointerdown.capture="panStart" @pointermove.capture="panMove" @pointerup.capture="panEnd" @pointercancel.capture="panCancel" @lostpointercapture.capture="panLost" @click.capture="navigation.consumeClick" @auxclick="panAuxClick">
          <div v-if="session&&imageUrl&&frame" class="stage-holder">
            <svg ref="drawing" class="review-drawing" :class="{drawing:drawMode}" :width="session.media.width*zoom" :height="session.media.height*zoom" :viewBox="`0 0 ${session.media.width} ${session.media.height}`" @pointermove="pointerMove" @pointerup="pointerEnd" @pointercancel="cancelGesture" @lostpointercapture="cancelGesture" @pointerdown.self="startDraw">
              <image :href="imageUrl" :width="session.media.width" :height="session.media.height" @pointerdown="startDraw" />
              <template v-for="(o,i) in working" :key="o.annotationId">
                <rect v-if="overlay&&rowById.get(o.objectId)?.a&&rowById.get(o.objectId)?.m" v-show="!hiddenBoxes" class="original-box" :x="rowById.get(o.objectId)!.a![0]" :y="rowById.get(o.objectId)!.a![1]" :width="rowById.get(o.objectId)!.a![2]-rowById.get(o.objectId)!.a![0]" :height="rowById.get(o.objectId)!.a![3]-rowById.get(o.objectId)!.a![1]" />
                <g v-show="!hiddenBoxes" :data-box="o.objectId" :class="['review-object',{modified:!!rowById.get(o.objectId)?.m||rowById.get(o.objectId)?.kind==='add',chosen:selected===o.objectId,readonly:isReadonly}]" @pointerdown.stop="pointerDown($event,i)">
                  <rect :x="o.bbox[0]" :y="o.bbox[1]" :width="o.bbox[2]-o.bbox[0]" :height="o.bbox[3]-o.bbox[1]" />
                  <text :x="o.bbox[0]" :y="Math.max(12/zoom,o.bbox[1]-4/zoom)" :font-size="12/zoom">{{ o.objectId }}</text>
                </g>
                <template v-if="selected===o.objectId&&!isReadonly&&!hiddenBoxes">
                  <rect v-for="h in handles" :key="h" class="resize-handle" :data-handle="h" :x="(h.includes('l')?o.bbox[0]:o.bbox[2])-4/zoom" :y="(h.includes('t')?o.bbox[1]:o.bbox[3])-4/zoom" :width="8/zoom" :height="8/zoom" @pointerdown.stop="pointerDown($event,i,h)" />
                </template>
              </template>
              <rect v-if="doubtBox&&!hiddenBoxes" class="review-doubt-box" pointer-events="none" :x="doubtBox[0]" :y="doubtBox[1]" :width="doubtBox[2]-doubtBox[0]" :height="doubtBox[3]-doubtBox[1]" fill="none" stroke="var(--warning)" stroke-dasharray="5 4" stroke-width="2" vector-effect="non-scaling-stroke" />
              <rect v-if="newBox" class="new-review-box" :x="newBox[0]" :y="newBox[1]" :width="newBox[2]-newBox[0]" :height="newBox[3]-newBox[1]" />
              <rect v-if="selectedRow?.kind==='delete'" v-show="!hiddenBoxes" :data-box="selectedRow.objectId" class="deleted-review-box" :x="selectedRow.bbox[0]" :y="selectedRow.bbox[1]" :width="selectedRow.bbox[2]-selectedRow.bbox[0]" :height="selectedRow.bbox[3]-selectedRow.bbox[1]" />
            </svg>
          </div>
          <p v-else class="empty-state">{{ busy?'正在加载视频帧…':'选择左侧视频，查看原始标注并逐帧审查' }}</p>
        </div>
        <div class="shell-tools" aria-label="审查工具">
          <template v-if="session?.permissions.canEdit"><button class="shell-tool" :class="{active:!drawMode}" :aria-pressed="!drawMode" :disabled="!canEdit" title="选择 (V)" @click="drawMode=false"><AppIcon name="cursor" :size="16" />选择</button><button data-testid="review-add-box" class="shell-tool" :class="{active:drawMode}" :aria-pressed="drawMode" :disabled="!canEdit" title="补标本帧 (B)" @click="drawMode=true;stopPlayback()"><AppIcon name="box" :size="16" />补标本帧</button><i class="shell-tools-sep" /></template>
          <button class="shell-tool" :class="{active:overlay}" :aria-pressed="overlay" title="叠加原框 (O)" aria-label="叠加原框" @click="overlay=!overlay"><AppIcon name="layers" :size="16" />原框</button>
          <button class="shell-tool" :class="{active:hiddenBoxes}" :disabled="viewBlocked" :aria-pressed="hiddenBoxes" :aria-label="hiddenBoxes?'显示标注':'隐藏标注'" title="显示 / 隐藏标注 (H)" @click="hiddenBoxes=!hiddenBoxes"><AppIcon name="eye" :size="16" /></button>
          <button ref="focusButton" class="shell-tool" :class="{active:focused}" :disabled="viewBlocked" :aria-pressed="focused" :aria-label="focused?'退出专注':'进入专注'" title="专注 (F)" @click="toggleFocus"><AppIcon name="fit" :size="16" /></button>
        </div>
        <div class="shell-zoom" role="group" aria-label="画面缩放"><button :disabled="viewBlocked" @click="changeZoom(-.1)" aria-label="缩小画面" title="缩小画面（−）">−</button><button :disabled="viewBlocked" @click="fit" aria-label="适应画面" title="适应完整视频帧（0）">{{ Math.round(zoom*100) }}%</button><button :disabled="viewBlocked" @click="changeZoom(.1)" aria-label="放大画面" title="放大画面（+ / =）">+</button></div>
      </template>
      <template v-if="session" #bottom>
        <button class="icon-button" :disabled="busy||!frame||fi===0" aria-label="上一帧" title="上一帧（←）" @click="navigate(fi-1)"><AppIcon name="prev" :size="16" /></button>
        <button class="icon-button shell-play" :disabled="!frame||busy||!!error" @click="togglePlay" :aria-label="playing?'暂停':'播放'" :title="playing?'暂停 (K)':'播放 (K)'"><AppIcon :name="playing?'pause':'play'" :size="15" /></button>
        <button class="icon-button" :disabled="busy||!frame||fi===total-1" aria-label="下一帧" title="下一帧（→）" @click="navigate(fi+1)"><AppIcon name="next" :size="16" /></button>
        <label class="shell-jump"><input ref="jumpInput" v-model="jump" aria-label="跳转帧号" title="跳转帧号 (G)，Enter 跳转" inputmode="numeric" :disabled="busy||!frame" /> / {{ total }}</label>
        <WorkbenchFrameStrip :frame-count="total" :current="fi" :disabled="busy||!frame" :priority="['draft','empty','submitted']" :kind-of="reviewFrameKind" :flags-of="reviewFrameFlags" :title-of="reviewFrameTitle" @seek="navigate" />
        <span class="review-time">{{ time(fi) }} / {{ time(total) }}</span>
      </template>
      <template #inspector>
        <section class="shell-card review-submit-panel">
          <div class="shell-card-head"><strong>提交本帧</strong></div>
          <p class="frame-summary" data-testid="review-frame-summary">本帧 {{ working.length }} 个框 · {{ changed.length }} 项变化<span v-if="deletedCount">（含删除 {{ deletedCount }} 个）</span></p>
          <div class="review-submit-buttons">
            <button :disabled="!canEdit||!history.length" @click="undo" :title="`撤销（${primaryKey}Z）`">↶ 撤销</button>
            <button class="primary" data-testid="submit-frame" :disabled="!canSubmit" @click="submit" :title="`提交本帧（${primaryKey}Enter）`">✓ {{ busy?'处理中…':frame?.state==='submitted'&&!dirty?'本帧已提交':'提交本帧' }}</button>
          </div>
          <p class="save-label" role="status">{{ saveLabel }}</p>
          <div v-if="error" class="review-error" role="alert">{{ error }} <div class="review-error-actions"><button @click="retry" :disabled="busy">重试</button><template v-if="pending"><button @click="downloadLocal">导出本地修改</button><button @click="modal='conflict'">处理冲突 / 重新读取</button></template></div></div>
          <button v-if="frame?.hasDraft||dirty" class="link discard-draft" :disabled="busy" @click="modal='discard'">放弃本帧草稿</button>
          <TrainingDatasetExport v-if="session?.finalVersionId" :final-version-id="session.finalVersionId" :media-name="session.media.name" :disabled="busy||!!pending" @open="exportOpen=$event" @busy="exportBusy=$event" />
          <button v-if="session?.workflow==='concentrated'&&session.state==='reviewed'&&session.reviewerId===Number(useAuth().user.value?.id)" :disabled="busy||exportBusy" @click="modal='reopen'">重新审查本帧</button>
          <button v-if="session?.workflow==='legacy'&&session.state==='reviewed'" @click="router.push('/confirm')">继续旧任务的对比确认</button>
          <details v-if="session" class="review-task-actions"><summary>任务操作 <span>{{ status(session.state) }}</span></summary><div><button data-testid="review-withdraw" :disabled="busy||!session.permissions.canWithdraw" @click="openTaskAction(session,'withdraw')">撤回送审</button><small v-if="!session.permissions.canWithdraw">{{ session.withdrawalBlockedReason||'任务已领取后不能直接撤回，可由当前审查员退回重新标注。' }}</small><button v-if="session.permissions.canReturnAnnotation" data-testid="review-return-annotation" :disabled="!canEdit" @click="openTaskAction(session,'return')">退回重新标注</button></div></details>
        </section>
        <section v-if="session?.workflow==='concentrated'" class="shell-card review-doubts" aria-label="集中审查疑点">
          <div class="shell-card-head"><strong>疑点片段</strong><small>{{ visibleDoubts.length }}</small></div>
          <p class="review-evidence-hint">提示来自追踪初稿，人工框可能已修改。未提示不代表准确，请检查整幅画面。</p>
          <p v-if="evidenceError" class="review-error" role="alert">{{ evidenceError }} <button @click="loadEvidence(session.id)">重试读取疑点</button></p>
          <template v-else-if="evidence">
            <p v-if="evidence.jointTracking" class="review-evidence-hint" role="status">{{ evidence.jointTracking.state==='checked'?'自动标注已完成，请检查画面并处理疑点。':'自动标注未完成，请检查画面或重新生成。' }}</p>
            <p v-else-if="evidence.endpointVerification" class="review-evidence-hint" role="status">人工尾帧反向核验：{{ evidence.endpointVerification.state==='checked'?'已完成':evidence.endpointVerification.state==='failed'?'失败':'证据不足' }} · {{ Object.keys(evidence.endpointVerification.matches).length }} 个对象建立对应，{{ evidence.endpointVerification.checkedFrames }} 帧获得反向输出。编号不同不会直接判错；核验一致不代表准确。</p>
            <details><summary>自动检查覆盖</summary><p>头部检测抽查 {{ evidence.checkedFrames.length }} / {{ total }} 帧；失败 {{ evidence.failedFrames.length }} 帧。未抽查帧不作自动通过结论。短片段反向核验 {{ evidence.backwardChecked?.length||0 }} 处，失败 {{ evidence.backwardFailed?.length||0 }} 处。核验一致不代表准确。</p></details>
            <label class="review-doubt-filter">疑点类型<select v-model="doubtCategory" aria-label="疑点类型"><option value="all">全部</option><option value="position">位置与身份</option><option value="candidate">漏标候选</option><option value="uncertain">证据不足 / 检查失败</option></select></label>
            <label><input type="checkbox" v-model="doubtsOnlyPending" />只看尚未逐帧提交的片段</label>
            <div class="review-doubt-list"><button v-for="g in visibleDoubts.slice(0,doubtLimit)" :key="g.id" class="review-doubt" :class="{active:selectedDoubt?.id===g.id}" @click="openDoubt(g)" :disabled="busy||!!pending"><strong>{{ g.objectId===null?'全画面候选':`对象 ${g.objectId}` }} · 第 {{ g.startFrame+1 }}{{ g.endFrame>g.startFrame?`–${g.endFrame+1}`:'' }} 帧</strong><span>{{ g.message }}</span><small>{{ doubtReviewed(g)?'涉及帧已提交':'待人工核对' }}</small></button></div>
            <button v-if="visibleDoubts.length>doubtLimit" @click="doubtLimit+=30">显示更多疑点</button>
            <p v-if="!visibleDoubts.length" class="review-evidence-hint">当前筛选无疑点。仍需检查全帧，尤其是漏标、头部边界与身份。</p>
          </template>
          <div v-if="selectedDoubt" class="review-doubt-navigation"><button :disabled="busy||fi<=0" @click="navigate(Math.max(0,selectedDoubt.startFrame-3))">查看前几帧</button><button :disabled="busy" @click="navigate(selectedDoubt.endFrame)">片段末帧</button></div>
        </section>
        <section class="shell-card review-objects" aria-label="当前帧对象">
          <div class="shell-card-head"><strong>本帧对象</strong><small>{{ visibleObjects.length }} / {{ rows.length }}</small></div>
          <div class="object-filters">
            <input v-model="objectQuery" placeholder="搜索对象 ID" aria-label="搜索对象 ID" />
            <div class="object-filter-options" aria-label="对象筛选"><button :class="{active:objectFilter==='all'}" :aria-pressed="objectFilter==='all'" @click="objectFilter='all'">全部</button><button :class="{active:objectFilter==='changed'}" :aria-pressed="objectFilter==='changed'" @click="objectFilter='changed'">仅已调整</button><button :class="{active:objectFilter==='deleted'}" :aria-pressed="objectFilter==='deleted'" @click="objectFilter='deleted'">已删除 {{ deletedCount }}</button></div>
          </div>
          <div ref="objectList" class="review-object-list" data-testid="review-object-list">
            <button v-for="r in visibleObjects" :key="r.objectId" :data-row="r.objectId" class="object-row" :class="{selected:selected===r.objectId,modified:!!r.m||r.kind==='add',deleted:r.kind==='delete'}" :aria-pressed="selected===r.objectId" @click="selectObject(r.objectId,true)"><b><span class="review-object-number">{{ r.objectId }}</span> {{ r.classKey==='sperm'?'精子':r.classKey||'对象' }}</b><span>{{ rowState(r) }}</span><span class="object-locate" aria-hidden="true">⌖</span></button>
            <p v-if="!visibleObjects.length" class="empty-state">{{ !frame?'选择视频后显示标注对象。':!rows.length?'当前帧没有标注框，检查后仍需提交本帧。':'没有符合筛选条件的对象。' }}</p>
          </div>
          <div v-if="selectedRow&&canEdit" class="review-object-actions"><button v-if="selectedRow.kind==='delete'" data-testid="review-restore-box" :data-object-id="selectedRow.objectId" @click="restoreSelected">恢复本帧框</button><button v-else class="delete-frame-box" data-testid="review-delete-box" @click="deleteSelected">删除本帧框</button><small>只影响第 {{ fi+1 }} 帧 · 可撤销</small></div>
          <details v-if="selectedRow&&session" :key="selectedRow.annotationId" class="review-object-details" data-testid="review-object-details">
            <summary>对象对比与详情 <span>{{ selectedRow.objectId }} · {{ rowState(selectedRow) }}</span></summary>
            <div class="compare-pair"><div v-for="side in ['A','B']" :key="side"><h3 :class="side==='A'?'version-a':'version-b'">{{ side==='A'?'原始 A':`审查 B${frame?.hasDraft||dirty?' · 草稿':''}` }}</h3><svg :viewBox="crop(selectedRow.a||selectedRow.bbox,selectedRow.bbox,session.media.width,session.media.height)" class="crop" :aria-label="side==='A'?'原始标注 A 局部':'审查标注 B 局部'"><image :href="imageUrl" :width="session.media.width" :height="session.media.height"/><template v-if="side==='A'?!!selectedRow.a:selectedRow.kind!=='delete'"><rect v-for="b in [side==='A'?selectedRow.a!:selectedRow.bbox]" :key="side" :x="b[0]" :y="b[1]" :width="b[2]-b[0]" :height="b[3]-b[1]" :stroke="side==='A'?'var(--a)':'var(--b)'" fill="none" vector-effect="non-scaling-stroke" stroke-width="2"/></template></svg><small v-if="side==='A'&&!selectedRow.a">原标注中无此框</small><small v-else-if="side==='B'&&selectedRow.kind==='delete'">审查建议删除</small></div></div>
            <dl class="object-metrics">
              <div><dt>原始 A · x / y / w / h</dt><dd>{{ selectedRow.a?coords(selectedRow.a)+' px':'无此框' }}</dd></div>
              <div><dt>审查 B · x / y / w / h</dt><dd>{{ selectedRow.kind==='delete'?'建议删除':coords(selectedRow.bbox)+' px' }}</dd></div>
              <div><dt>IoU</dt><dd>{{ selectedRow.m?selectedRow.m.iou.toFixed(2):'—' }}</dd></div>
              <div><dt>中心位移</dt><dd>{{ selectedRow.m?`${selectedRow.m.shift.toFixed(1)} px`:'—' }}</dd></div>
              <div><dt>尺寸变化 Δw / Δh</dt><dd>{{ selectedRow.m?`${signed(selectedRow.m.dw)} / ${signed(selectedRow.m.dh)} px`:'—' }}</dd></div>
            </dl>
            <p class="metric-note">数值仅表示调整幅度，不代表标注质量。</p>
          </details>
          <details v-if="session?.resume.draftFrameIndexes.length" class="review-drafts"><summary>待提交草稿 <span>{{ session.progress.draftFrames }} 帧</span></summary><div><button v-for="i in session.resume.draftFrameIndexes" :key="i" class="link" :disabled="busy" @click="navigate(i)">第 {{ i+1 }} 帧</button></div></details>
        </section>
      </template>
    </WorkbenchShell>
    <div v-if="modal" class="review-modal-backdrop" @click.self="closeModal"><div ref="dialog" class="review-modal" role="dialog" aria-modal="true"  :data-testid="modal==='add'?'review-add-object-dialog':undefined" :aria-label="modal==='help'?'快捷键':modal==='add'?'补标本帧':modal==='return'?'退回重新标注':modal==='withdraw'?'撤回送审':'审查提示'">
      <button class="modal-close" aria-label="关闭" :disabled="busy||!!pendingObject||!!pendingTask" @click="closeModal">×</button>
      <template v-if="modal==='add'"><h2>补标本帧</h2><p>为新画的框选择对象身份。同一精子在不同帧使用相同编号。</p><label class="review-add-choice">对象身份<select aria-label="关联对象" v-model="objectChoice" :disabled="busy||!!pendingObject"><option value="new">新增对象（分配新编号）</option><option v-for="o in addCandidates" :key="o.objectId" :value="String(o.objectId)">对象 {{ o.objectId }} · {{ o.origin==='review'?'审查补标':'原始标注' }}</option></select></label><label v-if="objectChoice==='new'" class="review-add-choice">补标类别<select v-model="addClass" aria-label="补标类别" :disabled="busy||!!pendingObject"><option disabled value="">请选择类别</option><option v-for="key in addClasses" :key="key" :value="key">{{ key }}</option></select></label><small>仅列出本视频已有、当前帧尚未出现的对象；当前帧不会重复同一对象。</small><p v-if="catalogError" class="review-error" role="alert">{{ catalogError }} <button v-if="!pendingObject" @click="loadCatalog" :disabled="busy">重新读取对象</button></p><div class="modal-actions"><button :disabled="busy||!!pendingObject" @click="closeModal">取消补标</button><button class="primary" data-testid="review-confirm-add" :disabled="busy||(!!catalogError&&!pendingObject)||(objectChoice==='new'&&!addClass&&!pendingObject)" @click="commitAddition">{{ pendingObject?'重试原请求':'确认补标' }}</button></div></template>
      <template v-else-if="modal==='withdraw'||modal==='return'"><h2>{{ modal==='withdraw'?'撤回送审':'退回重新标注' }}</h2><p>视频：{{ taskTarget?.media.name }}</p><p v-if="modal==='withdraw'">仅在尚未领取时撤回。原标注与历史快照保留，修改后可重新送审。</p><template v-else><p>适用于整段追踪错误、大量漏标等需要回人工标注返工的情况。停止本次任务流转，保留原标注和审查历史。</p><label class="review-add-choice">退回原因<textarea v-model="returnReason" maxlength="2000" rows="4" :disabled="busy||!!pendingTask" placeholder="说明需要重新处理的内容与帧范围" /></label></template><p v-if="taskError" class="review-error" role="alert">{{ taskError }}</p><div class="modal-actions"><button :disabled="busy||!!pendingTask" @click="closeModal">取消</button><button class="primary" data-testid="review-confirm-task-action" :disabled="busy||(!pendingTask&&(modal==='withdraw'?!taskTarget?.permissions.canWithdraw:!taskTarget?.permissions.canReturnAnnotation))" @click="submitTaskAction">{{ pendingTask?'重试原请求':modal==='withdraw'?'确认撤回':'确认退回重新标注' }}</button></div></template>
      <template v-else-if="modal==='help'"><h2>快捷键</h2><table class="help-table"><tbody><tr v-for="[action,key] in [['上一帧 / 下一帧','← / →'],['前进 / 后退 10 帧','Shift + ← / →'],['放大 / 缩小画面','+ 或 = / −'],['适应完整画面','0'],['围绕鼠标缩放',primaryKey+'滚轮'],['拖动画面','按住 Space 拖动 / 中键拖动'],['显示 / 隐藏标注','H'],['专注 / 退出专注','F'],['上一个 / 下一个框','[ / ]'],['选择 / 补标本帧','V / B'],['删除选中对象的本帧框','Delete / Backspace'],['定位帧号输入框','G'],['跳转（帧号输入框中）','Enter'],['撤销',primaryKey+'Z'],['提交本帧',primaryKey+'Enter'],['原框叠加','O'],['播放 / 暂停','K'],['取消拖动 / 退出专注 / 选中 / 关闭弹窗','Esc'],['快捷键帮助','?']]" :key="action"><td>{{ action }}</td><td><kbd>{{ key }}</kbd></td></tr></tbody></table><p class="muted">方向键只浏览，不移动框。缩放、平移、专注和隐藏标注不改变草稿或提交状态。增删仅作用于当前帧，草稿不等于提交。输入框内保留文字编辑；长按不重复提交。</p></template>
      <template v-else-if="modal==='resume'"><h2>继续上次审查？</h2><p>上次停留：第 {{ (historical??0)+1 }} 帧</p><p>已提交 {{ session?.progress.submittedFrames }} / {{ total }} 帧</p><p>继续审查将进入第 {{ fi+1 }} 帧，优先处理最早未审查帧。</p><div class="modal-actions"><button @click="modal=null;navigate(0)">查看第1帧</button><button class="primary" @click="modal=null">继续审查</button></div></template>
      <template v-else-if="modal==='draft'||modal==='discard'"><h2>第 {{ fi+1 }} 帧有未提交草稿</h2><p>草稿已保留，尚不计入已提交进度。</p><p>放弃将恢复{{ frame?.lastSubmission?'上一份成功提交的 B 结果':'原始标注 A' }}。</p><div class="modal-actions"><button @click="discard">放弃修改</button><button class="primary" @click="modal=null">继续编辑</button></div></template>
      <template v-else-if="modal==='reopen'"><h2>重新审查本帧</h2><p>保留历史最终版本，立即停止旧训练包下载。本帧重新提交并再次完成视频审查后，才能导出新版本。</p><div class="modal-actions"><button @click="modal=null">取消</button><button class="primary" @click="reopenReview">确认重新审查</button></div></template>
      <template v-else-if="modal==='complete'"><h2>完成视频审查</h2><p>已提交 {{ session?.progress.submittedFrames }} / {{ total }} 帧</p><p>有修改帧：{{ session?.progress.modifiedFrames }} · 修改框：{{ session?.progress.modifiedBoxes }}</p><p>{{ session?.workflow==='concentrated'?'请确认已检查全部画面（包括未提示的区域）。完成后以本次审查结果生成最终版本，可直接导出训练集。':'此为旧流程任务：固定 B 后继续历史对比确认。' }}</p><div class="modal-actions"><button @click="modal=null">继续检查</button><button class="primary" :disabled="!session?.permissions.canComplete||busy" @click="complete">确认完成视频审查</button></div></template>
      <template v-else-if="modal==='conflict'"><h2>重新读取服务端结果？</h2><p>您可以先导出当前本地修改。重新读取成功后，将放弃当前未同步的本地编辑；不会覆盖服务端结果。</p><div class="modal-actions"><button @click="downloadLocal">导出本地修改</button><button class="primary" @click="reloadConflict">放弃本地编辑并重新读取</button></div></template>
    </div></div>
  </section>
</template>
<style scoped src="../review/review.css"></style>
