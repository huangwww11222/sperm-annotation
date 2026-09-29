<script setup lang="ts">
import { createRequestId } from '../utils/browserCompat'
import WorkflowProgress from '../components/WorkflowProgress.vue'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { reviewWorkflowApi as api, type Frame, type Session, type Mutation, type Failure } from '../api/reviewWorkflowApi'
import { clone, equal, patches, metrics, dragBox, crop, type ReviewObject, type Box } from '../review/geometry'
import { useRouter, addLeaveGuard } from '../router'

const router=useRouter()
const sessions=ref<Session[]>([]), session=ref<Session|null>(null), frame=ref<Frame|null>(null)
const working=ref<ReviewObject[]>([]), selected=ref<number|null>(null), imageUrl=ref('')
const filter=ref('all'), query=ref(''), busy=ref(false), error=ref(''), saveLabel=ref('无待保存内容'), notice=ref('')
const modal=ref<'resume'|'draft'|'complete'|'help'|'discard'|'conflict'|null>(null)
const dialog=ref<HTMLElement|null>(null)
let priorFocus:HTMLElement|null=null
watch(modal,async(value,previous)=>{
  if(value){if(!previous)priorFocus=document.activeElement as HTMLElement;await nextTick();dialog.value?.querySelector<HTMLElement>('button')?.focus()}
  else priorFocus?.focus()
})
const historical=ref<number|null>(null), jump=ref('1'), overlay=ref(false), zoom=ref(1), playing=ref(false)
const viewport=ref<HTMLElement|null>(null), drawing=ref<SVGSVGElement|null>(null), jumpInput=ref<HTMLInputElement|null>(null)
const history=ref<ReviewObject[][]>([])
const handles=['tl','tr','bl','br']
let resizeObserver:ResizeObserver|null=null, alive=true, playbackTimer:ReturnType<typeof setTimeout>|null=null
let pointer:{id:number;index:number;startX:number;startY:number;box:Box;before:ReviewObject[];handle:string}|null=null
let requestPromise:Promise<Mutation|null>|null=null
let pending:{kind:'draft'|'submit'|'discard'|'finish';key:string;body:any;sessionId:string;fi:number}|null=null
let removeGuard=()=>{}
const filteredByText=computed(()=>sessions.value.filter(s=>`${s.media.name} ${s.mediaId}`.toLowerCase().includes(query.value.toLowerCase())))
const visible=computed(()=>filteredByText.value.filter(s=>filter.value==='all'||s.state===filter.value))
const rows=computed(()=>working.value.map((o,i)=>({...o,a:frame.value!.baselineObjects[i].bbox,m:metrics(frame.value!.baselineObjects[i].bbox,o.bbox)})))
const changed=computed(()=>rows.value.filter(r=>r.m))
const average=computed(()=>changed.value.length?changed.value.reduce((n,r)=>n+r.m!.iou,0)/changed.value.length:null)
const maximum=computed(()=>changed.value.length?Math.max(...changed.value.map(r=>r.m!.shift)):null)
const selectedRow=computed(()=>rows.value.find(r=>r.objectId===selected.value))
const patch=computed(()=>frame.value?patches(frame.value.baselineObjects,working.value):[])
const dirty=computed(()=>!!frame.value&&!equal(patch.value,frame.value.patch))
const canEdit=computed(()=>!!session.value?.permissions.canEdit&&!!imageUrl.value&&!busy.value&&!pending)
const canSubmit=computed(()=>canEdit.value&&!!frame.value&&(frame.value.state!=='submitted'||dirty.value))
const isReadonly=computed(()=>!!session.value&&!session.value.permissions.canEdit)
const total=computed(()=>session.value?.frameCount||0)
const fi=computed(()=>frame.value?.frameIndex??0)
const mac=/Mac|iPhone|iPad/.test(navigator.platform), primaryKey=mac?'⌘':'Ctrl+'
const count=(state:string)=>state==='all'?filteredByText.value.length:filteredByText.value.filter(s=>s.state===state).length
const status=(s:string)=>({pending:'待审查',in_progress:'审查中',reviewed:'已审查'}[s]||'历史只读任务')
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
  if(!viewport.value||!session.value||pointer) return
  zoom.value=Math.min((viewport.value.clientWidth-24)/session.value.media.width,(viewport.value.clientHeight-24)/session.value.media.height,1)
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
    const f=await api.frame(session.value.id,index)
    url=await api.image(session.value.mediaId,index)
    await new Promise<void>((resolve,reject)=>{const img=new Image();img.onload=()=>resolve();img.onerror=()=>reject(new Error('帧图像无法解码'));img.src=url})
    if(!alive){URL.revokeObjectURL(url);return false}
    if(imageUrl.value) URL.revokeObjectURL(imageUrl.value)
    imageUrl.value=url;applyFrame(f);history.value=[];selected.value=null
    saveLabel.value=f.hasDraft?'已有未提交草稿':f.state==='submitted'?'本帧已提交':'无待保存内容'
    if(showDraft&&f.hasDraft) modal.value='draft'
    await bookmark()
    return true
  }catch(e){if(url)URL.revokeObjectURL(url);showError(e,'load_frame');return false}
  finally{busy.value=false}
}
async function open(s:Session) {
  stopPlayback()
  if(!await flush()) return
  busy.value=true;error.value=''
  try {
    let detail=await api.session(s.id)
    if(detail.permissions.canClaim) detail=(await api.claim(s.id,createRequestId())).session
    updateSession(detail);historical.value=detail.resume.lastViewedFrameIndex
    const target=detail.state==='reviewed'?(historical.value??0):(detail.resume.firstUnsubmittedFrameIndex??historical.value??0)
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
    const result=op.kind==='finish'?await api.finish(op.sessionId,op.body.expectedSessionRevision,op.key):await api.mutate(op.sessionId,op.fi,op.kind,op.body,op.key)
    if(!alive)return result
    updateSession(result.session)
    if(result.frame)applyFrame(result.frame)
    pending=null
    saveLabel.value=op.kind==='submit'?`第 ${op.fi+1} 帧已提交`:op.kind==='discard'?'已放弃草稿':op.kind==='finish'?'视频审查已完成':frame.value?.hasDraft?'草稿已保存，尚未提交':'已保存，无待提交修改'
    console.info('[review.operation_succeeded]',{action:op.kind,sessionId:op.sessionId,frameIndex:op.fi,revision:result.session.revision})
    return result
  }catch(e){saveLabel.value='保存失败，编辑已保留';showError(e,op.kind);return null}
  finally{busy.value=false}
}
async function perform(kind:'draft'|'submit'|'discard'|'finish') {
  if(!session.value||!frame.value)return null
  if(requestPromise) return requestPromise
  if(pending&&pending.kind!==kind) {error.value='请先重试未完成的请求，或处理版本冲突。';return null}
  if(!pending)pending={kind,key:createRequestId(),sessionId:session.value.id,fi:fi.value,body:kind==='finish'?{expectedSessionRevision:session.value.revision}:
    {expectedFrameRevision:frame.value.frameRevision,...(kind==='draft'||kind==='submit'?{patch:clone(patch.value)}:{})}}
  requestPromise=runPending()
  const r=await requestPromise;requestPromise=null;return r
}
async function flush() {
  if(pointer)return false
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
async function complete() {modal.value=null;stopPlayback();if(!await flush())return;if(!session.value?.permissions.canComplete)return;await perform('finish')}
async function reloadConflict() {
  // User explicitly chooses to discard the local snapshot. Keep it until reload succeeds.
  modal.value=null;busy.value=true
  const oldPending=pending, oldWorking=clone(working.value)
  try {const s=await api.session(session.value!.id);updateSession(s);if(await loadFrame(fi.value)){pending=null;error.value=''}else{pending=oldPending;working.value=oldWorking}}
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
  nextTick(()=>{
    document.querySelector(`[data-row="${id}"]`)?.scrollIntoView({block:'nearest',inline:'nearest'})
    if(locate)document.querySelector(`[data-box="${id}"]`)?.scrollIntoView({block:'nearest',inline:'nearest'})
  })
}
function pointerDown(e:PointerEvent,index:number,handle='move') {
  if(e.button!==0||busy.value||pending||modal.value)return
  stopPlayback();selectObject(working.value[index].objectId)
  if(!canEdit.value)return
  e.preventDefault();drawing.value?.setPointerCapture(e.pointerId)
  pointer={id:e.pointerId,index,startX:e.clientX,startY:e.clientY,box:clone(working.value[index].bbox),before:clone(working.value),handle}
}
function pointerMove(e:PointerEvent) {
  if(!pointer||pointer.id!==e.pointerId||!session.value)return
  const p=pointer
  working.value[p.index].bbox=dragBox(p.box,(e.clientX-p.startX)/zoom.value,(e.clientY-p.startY)/zoom.value,p.handle,session.value.media.width,session.value.media.height)
}
function pointerEnd(e?:PointerEvent) {
  if(!pointer)return
  const before=pointer.before,id=pointer.id;pointer=null
  if(drawing.value?.hasPointerCapture(id))drawing.value.releasePointerCapture(id)
  if(!equal(patches(frame.value!.baselineObjects,before),patch.value)) {
    history.value.push(before);if(history.value.length>50)history.value.shift()
    void perform('draft')
  }
}
function cancelGesture() {
  if(!pointer)return
  working.value=pointer.before;const id=pointer.id;pointer=null
  if(drawing.value?.hasPointerCapture(id))drawing.value.releasePointerCapture(id)
}
async function undo() {if(!canEdit.value||!history.value.length)return;working.value=history.value.pop()!;await perform('draft')}
function stopPlayback(){playing.value=false;if(playbackTimer)clearTimeout(playbackTimer);playbackTimer=null}
async function playTick() {
  if(!playing.value)return
  if(fi.value>=total.value-1||modal.value||error.value){stopPlayback();return}
  await navigate(fi.value+1,true)
  if(playing.value)playbackTimer=setTimeout(playTick,Math.max(40,1000/(session.value?.media.fps||30)))
}
async function togglePlay(){if(playing.value){stopPlayback();return}if(!await flush())return;playing.value=true;void playTick()}
function doJump(){const n=Number(jump.value);if(!/^\d+$/.test(jump.value)||!Number.isSafeInteger(n)||n<1||n>total.value){error.value=`请输入 1～${total.value} 的整数帧号`;return}jumpInput.value?.blur();void navigate(n-1)}
function esc(){if(modal.value){modal.value=null;return}if(pointer){cancelGesture();return}if(document.activeElement===jumpInput.value){jump.value=String(fi.value+1);jumpInput.value?.blur();return}selected.value=null}
function keydown(e:KeyboardEvent) {
  if(document.querySelector('[data-user-guide][open]'))return

  if(e.isComposing||e.repeat)return
  if(e.key==='Escape'){esc();e.preventDefault();return}
  if(modal.value){
    if(e.key==='Tab'){
      const items=Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),[tabindex="0"]')||[])
      if(items.length){const i=items.indexOf(document.activeElement as HTMLElement);items[(i+(e.shiftKey?-1:1)+items.length)%items.length]?.focus();e.preventDefault()}
    }
    return
  }
  const target=e.target as HTMLElement
  if(target.closest('input,textarea,select,[contenteditable="true"]')) {if(target===jumpInput.value&&e.key==='Enter'&&!e.ctrlKey&&!e.metaKey&&!e.altKey){e.preventDefault();doJump()}return}
  if(busy.value||pointer||!session.value)return
  const mod=e.ctrlKey||e.metaKey
  if(mod&&!e.altKey&&!e.shiftKey&&(e.key.toLowerCase()==='z'||e.key==='Enter')){e.preventDefault();void(e.key==='Enter'?submit():undo());return}
  if(e.ctrlKey||e.metaKey||e.altKey)return
  if(e.key===' '&&target.closest('button'))return
  switch(e.key){
    case 'ArrowLeft':e.preventDefault();void navigate(fi.value-1);break
    case 'ArrowRight':e.preventDefault();void navigate(fi.value+1);break
    case '[':case ']':{e.preventDefault();const i=working.value.findIndex(o=>o.objectId===selected.value);const next=i<0?(e.key===']'?0:working.value.length-1):Math.max(0,Math.min(working.value.length-1,i+(e.key===']'?1:-1)));if(working.value[next])selectObject(working.value[next].objectId,true);break}
    case 'f':case 'F':e.preventDefault();jumpInput.value?.focus();jumpInput.value?.select();break
    case 'o':case 'O':e.preventDefault();overlay.value=!overlay.value;break
    case '?':e.preventDefault();modal.value='help';stopPlayback();break
    case ' ':e.preventDefault();void togglePlay();break
  }
}
function beforeUnload(e:BeforeUnloadEvent){if(dirty.value||pending||busy.value){e.preventDefault();e.returnValue=''}}
onMounted(async()=>{
  await list()
  resizeObserver=new ResizeObserver(()=>fit());if(viewport.value)resizeObserver.observe(viewport.value)
  window.addEventListener('keydown',keydown);window.addEventListener('beforeunload',beforeUnload)
  removeGuard=addLeaveGuard(async()=>{stopPlayback();return await flush()})
})
onUnmounted(()=>{alive=false;stopPlayback();cancelGesture();removeGuard();resizeObserver?.disconnect();window.removeEventListener('keydown',keydown);window.removeEventListener('beforeunload',beforeUnload);if(imageUrl.value)URL.revokeObjectURL(imageUrl.value)})
</script>

<template>
  <section class="review-page" aria-label="审查模式">
    <div class="review-flow"><strong>审查模式</strong><span>逐帧检查 · 修改后提交本帧</span><small>A 原始标注永久保留</small></div>

    <div v-if="error" class="review-error" role="alert">{{ error }} <button @click="retry" :disabled="busy">重试</button><template v-if="pending"><button @click="downloadLocal">导出本地修改</button><button @click="modal='conflict'">处理冲突 / 重新读取</button></template></div>
    <div v-if="notice" class="review-notice">{{ notice }} <button @click="bookmark">重试位置同步</button></div>
    <div class="review-top">
      <aside class="review-sidebar panel-r">
        <div class="section-head"><h2>待审查视频列表 <small>已完成标注</small></h2><button aria-label="刷新视频列表" :disabled="busy" @click="list">↻</button></div>
        <input class="review-search" v-model="query" placeholder="搜索视频名称或 ID…" aria-label="搜索视频" />
        <div class="review-filters"><button v-for="(label,key) in {all:'全部',pending:'待审查',in_progress:'审查中',reviewed:'已审查'}" :key="key" :class="{active:filter===key}" @click="filter=key">{{ label }} <small>{{ count(key) }}</small></button></div>
        <div class="review-videos">
          <button v-for="s in visible" :key="s.id" :data-session-id="s.id" class="video-card" :class="{selected:session?.id===s.id}" :disabled="busy" @click="open(s)">
            <div class="card-heading"><b>{{ s.media.name }}</b><span class="badge" :class="s.state">{{ status(s.state) }}</span></div>
            <small>{{ time(s.frameCount,s.media.fps) }} · {{ s.frameCount }} 帧</small>
            <progress :value="s.progress.submittedFrames" :max="s.frameCount"></progress>
            <div class="card-progress"><span>{{ s.progress.submittedFrames }} / {{ s.frameCount }} · {{ s.progress.percent.toFixed(1) }}%</span></div>
            <small>上次停留：{{ s.resume.lastViewedFrameIndex===null?'尚未浏览':`第 ${s.resume.lastViewedFrameIndex+1} 帧` }} <em v-if="s.progress.draftFrames"> · {{ s.progress.draftFrames }} 帧草稿</em></small>
          </button>
          <p v-if="!visible.length" class="empty-state">暂无符合条件的已标注任务。<br>请先由标注端完成全视频标注并送审。</p>
        </div>
      </aside>
      <main class="review-center panel-r">
        <div class="section-head"><div><h2>{{ session?.media.name || '选择视频开始审查' }}</h2><small v-if="session">{{ session.media.width }} × {{ session.media.height }} · 第 {{ fi+1 }} / {{ total }} 帧 <span class="frame-status" data-testid="frame-state">{{ session.state==='reviewed'?'已完成 · 只读':frame?.state==='submitted'?'已提交':frame?.hasDraft?'未提交草稿':'未审查' }}</span></small></div><div class="toolbar"><label><input v-model="overlay" type="checkbox" /> 叠加原框</label><button @click="modal='help';stopPlayback()" title="快捷键（?）">快捷键 ⓘ</button></div></div>
        <div v-if="session?.readOnlyReason" class="review-notice">{{ session.readOnlyReason }}</div>
        <div v-else-if="isReadonly&&session?.state!=='reviewed'" class="review-notice">当前账号仅可查看。任务由已领取的审查员编辑；你也可以领取尚未分配的任务，检查自己的标注。</div>
        <div ref="viewport" class="review-viewport">
          <div v-if="session&&imageUrl&&frame" class="stage-holder">
            <svg ref="drawing" class="review-drawing" :width="session.media.width*zoom" :height="session.media.height*zoom" :viewBox="`0 0 ${session.media.width} ${session.media.height}`" @pointermove="pointerMove" @pointerup="pointerEnd" @pointercancel="cancelGesture" @lostpointercapture="cancelGesture" @pointerdown.self="selected=null">
              <image :href="imageUrl" :width="session.media.width" :height="session.media.height" @pointerdown="selected=null" />
              <template v-for="(o,i) in working" :key="o.annotationId">
                <rect v-if="overlay&&rows[i].m" class="original-box" :x="rows[i].a[0]" :y="rows[i].a[1]" :width="rows[i].a[2]-rows[i].a[0]" :height="rows[i].a[3]-rows[i].a[1]" />
                <g :data-box="o.objectId" :class="['review-object',{modified:!!rows[i].m,chosen:selected===o.objectId,readonly:isReadonly}]" @pointerdown.stop="pointerDown($event,i)">
                  <rect :x="o.bbox[0]" :y="o.bbox[1]" :width="o.bbox[2]-o.bbox[0]" :height="o.bbox[3]-o.bbox[1]" />
                  <text :x="o.bbox[0]" :y="Math.max(12/zoom,o.bbox[1]-4/zoom)" :font-size="12/zoom">#{{ String(o.objectId).padStart(3,'0') }}</text>
                </g>
                <template v-if="selected===o.objectId&&!isReadonly">
                  <rect v-for="h in handles" :key="h" class="resize-handle" :data-handle="h" :x="(h.includes('l')?o.bbox[0]:o.bbox[2])-4/zoom" :y="(h.includes('t')?o.bbox[1]:o.bbox[3])-4/zoom" :width="8/zoom" :height="8/zoom" @pointerdown.stop="pointerDown($event,i,h)" />
                </template>
              </template>
            </svg>
          </div>
          <p v-else class="empty-state">{{ busy?'正在加载视频帧…':'选择左侧视频，查看原始标注并逐帧审查' }}</p>
        </div>
        <div class="zoom-bar"><small>{{ isReadonly?'只读查看': '拖动框移动 · 拖动角点缩放' }}</small><button @click="zoom=Math.max(.1,zoom-.1)" aria-label="缩小">−</button><span>{{ Math.round(zoom*100) }}%</span><button @click="zoom=Math.min(5,zoom+.1)" aria-label="放大">+</button><button @click="fit" title="显示完整视频帧">适应窗口</button></div>
        <div class="review-controls"><button :disabled="!frame||busy||!!error" @click="togglePlay" :aria-label="playing?'暂停':'播放'">{{ playing?'Ⅱ':'▶' }}</button><span>{{ time(fi) }} / {{ time(total) }}</span><input class="timeline" type="range" aria-label="视频时间轴" min="0" :max="Math.max(0,total-1)" :value="fi" :disabled="busy||!frame" @change="navigate(Number(($event.target as HTMLInputElement).value))"/><button :disabled="busy||!frame||fi===0" @click="navigate(fi-1)" title="上一帧（←）">上一帧</button><button :disabled="busy||!frame||fi===total-1" @click="navigate(fi+1)" title="下一帧（→）">下一帧</button><label>跳转到 <input ref="jumpInput" v-model="jump" aria-label="跳转帧号" inputmode="numeric" :disabled="busy||!frame" /></label><button :disabled="busy||!frame" @click="doJump">确定</button></div>
      </main>
      <aside class="review-right">
        <WorkflowProgress v-if="session" label="视频审查进度" variant="card" :percent="session.progress.percent" :summary="`已提交 ${session.progress.submittedFrames} / ${total} 帧 · 剩余 ${session.progress.unsubmittedFrames} 帧`" :detail="`修改帧 ${session.progress.modifiedFrames} · 修改框 ${session.progress.modifiedBoxes} · 草稿 ${session.progress.draftFrames} 帧`" :completed="session.state==='reviewed'" :state="session.state==='reviewed'?'视频已审查 · B 版本已固定':session.progress.unsubmittedFrames===0?'全部帧已提交 · 待完成视频审查':'逐帧提交后，再完成视频审查'">
          <button class="primary" :disabled="busy||!session.permissions.canComplete||dirty||!!pending" @click="modal='complete'">完成视频审查</button>
        </WorkflowProgress>
        <section class="review-submit-panel panel-r"><h2>审查操作</h2><small class="muted">每帧检查后提交，空帧也不例外。</small><div class="review-submit-buttons"><button :disabled="!canEdit||!history.length" @click="undo" :title="`撤销（${primaryKey}Z）`">↶ 撤销</button><button class="primary" data-testid="submit-frame" :disabled="!canSubmit" @click="submit" :title="`提交本帧（${primaryKey}Enter）`">✓ {{ busy?'处理中…':frame?.state==='submitted'&&!dirty?'本帧已提交':'提交本帧' }}</button></div><p class="save-label" role="status">{{ saveLabel }}</p><button v-if="frame?.hasDraft||dirty" class="full" :disabled="busy" @click="modal='discard'">放弃本帧草稿</button></section>
        <div class="review-details-scroll panel-r">
        <section><h2>当前帧审查统计</h2><div class="stats-grid"><div><small>标注框数</small><b>{{ working.length }}</b></div><div><small>已修改框</small><b class="red">{{ changed.length }}</b></div><div><small>未修改框</small><b class="green">{{ working.length-changed.length }}</b></div><div class="wide"><small>平均 IoU（修改框）</small><b>{{ average===null?'—':average.toFixed(2) }}</b></div><div class="wide"><small>最大位置偏移</small><b>{{ maximum===null?'—':`${maximum.toFixed(1)} px` }}</b></div></div></section>
        <section v-if="session?.resume.draftFrameIndexes.length"><h2>待提交草稿</h2><p class="muted"><button v-for="i in session.resume.draftFrameIndexes.slice(0,12)" :key="i" class="link" :disabled="busy" @click="navigate(i)">第 {{ i+1 }} 帧</button></p></section>
        </div>
      </aside>
    </div>
    <div class="review-bottom">
      <section class="review-compare panel-r"><h2>当前帧对比 <small>原标注 vs 审查后</small></h2><div class="compare-pair"><div v-for="side in ['A','B']" :key="side"><h3 :class="side==='A'?'green':'red'">{{ side==='A'?'原始标注（A）':`审查后（${frame?.hasDraft||dirty?'草稿':'当前'}）` }}</h3><svg v-if="selectedRow&&session" :viewBox="crop(selectedRow.a,selectedRow.bbox,session.media.width,session.media.height)" class="crop"><image :href="imageUrl" :width="session.media.width" :height="session.media.height"/><rect v-for="b in [side==='A'?selectedRow.a:selectedRow.bbox]" :key="side" :x="b[0]" :y="b[1]" :width="b[2]-b[0]" :height="b[3]-b[1]" :stroke="side==='A'?'#22c55e':'#ef4444'" fill="none" vector-effect="non-scaling-stroke" stroke-width="2"/></svg><div v-else class="crop empty-state">请选择一个标注框</div><small>{{ selectedRow?coords(side==='A'?selectedRow.a:selectedRow.bbox):'—' }}</small></div></div></section>
      <section class="review-table panel-r"><h2>当前帧标注对比列表 <small>共 {{ rows.length }} 个框 · 坐标 x / y / w / h（px）</small></h2><div class="table-scroll"><table><thead><tr><th>ID</th><th>原始标注 A</th><th>审查后（当前）</th><th>状态</th><th>IoU</th><th>中心位移</th><th>尺寸变化 Δw / Δh</th><th>操作</th></tr></thead><tbody><tr v-for="r in rows" :key="r.annotationId" :data-row="r.objectId" :class="{selected:selected===r.objectId,modified:!!r.m}" @click="selectObject(r.objectId,true)"><td>#{{ String(r.objectId).padStart(3,'0') }}</td><td>{{ coords(r.a) }}</td><td>{{ coords(r.bbox) }}</td><td :title="r.m?.type">{{ r.m?'已修改':'未修改' }}</td><td>{{ r.m?r.m.iou.toFixed(2):'—' }}</td><td>{{ r.m?`${r.m.shift.toFixed(1)} px`:'—' }}</td><td>{{ r.m?`${signed(r.m.dw)} / ${signed(r.m.dh)}`:'—' }}</td><td><button class="link" @click.stop="selectObject(r.objectId,true)">查看</button></td></tr><tr v-if="!rows.length"><td colspan="8" class="empty-state">{{ frame?'当前帧没有标注框，检查后仍需提交本帧。':'选择视频后显示标注对象。' }}</td></tr></tbody></table></div></section>
    </div>
    <div v-if="modal" class="review-modal-backdrop" @click.self="modal=null"><div ref="dialog" class="review-modal" role="dialog" aria-modal="true" :aria-label="modal==='help'?'快捷键':'审查提示'">
      <button class="modal-close" aria-label="关闭" @click="modal=null">×</button>
      <template v-if="modal==='help'"><h2>快捷键</h2><table class="help-table"><tbody><tr v-for="[action,key] in [['上一帧 / 下一帧','← / →'],['上一个 / 下一个框','[ / ]'],['定位帧号输入框','F'],['跳转（帧号输入框中）','Enter'],['撤销',primaryKey+'Z'],['提交本帧',primaryKey+'Enter'],['原框叠加','O'],['播放 / 暂停','Space'],['取消拖动 / 选中 / 关闭弹窗','Esc'],['快捷键帮助','?']]" :key="action"><td>{{ action }}</td><td><kbd>{{ key }}</kbd></td></tr></tbody></table><p class="muted">方向键只浏览，不移动框。输入框内保留文字编辑；长按不重复提交。视频完成始终单独确认。</p></template>
      <template v-else-if="modal==='resume'"><h2>继续上次审查？</h2><p>上次停留：第 {{ (historical??0)+1 }} 帧</p><p>已提交 {{ session?.progress.submittedFrames }} / {{ total }} 帧</p><p>继续审查将进入第 {{ fi+1 }} 帧，优先处理最早未审查帧。</p><div class="modal-actions"><button @click="modal=null;navigate(0)">查看第1帧</button><button class="primary" @click="modal=null">继续审查</button></div></template>
      <template v-else-if="modal==='draft'||modal==='discard'"><h2>第 {{ fi+1 }} 帧有未提交草稿</h2><p>草稿已保留，尚不计入已提交进度。</p><p>放弃将恢复{{ frame?.lastSubmission?'上一份成功提交的 B 结果':'原始标注 A' }}。</p><div class="modal-actions"><button @click="discard">放弃修改</button><button class="primary" @click="modal=null">继续编辑</button></div></template>
      <template v-else-if="modal==='complete'"><h2>完成视频审查</h2><p>已提交 {{ session?.progress.submittedFrames }} / {{ total }} 帧</p><p>有修改帧：{{ session?.progress.modifiedFrames }} · 修改框：{{ session?.progress.modifiedBoxes }}</p><p>完成后固定 B 版本并进入只读状态，修改部分交给第三人对比确认。</p><div class="modal-actions"><button @click="modal=null">继续检查</button><button class="primary" :disabled="!session?.permissions.canComplete||busy" @click="complete">确认完成视频审查</button></div></template>
      <template v-else-if="modal==='conflict'"><h2>重新读取服务端结果？</h2><p>您可以先导出当前本地修改。重新读取成功后，将放弃当前未同步的本地编辑；不会覆盖服务端结果。</p><div class="modal-actions"><button @click="downloadLocal">导出本地修改</button><button class="primary" @click="reloadConflict">放弃本地编辑并重新读取</button></div></template>
    </div></div>
  </section>
</template>
<style scoped src="../review/review.css"></style>
