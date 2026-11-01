import { createRequestId } from '../utils/browserCompat'
import { trackApi, type TrackWorkspaceState, type TrackingFeedbackInput } from '../api/trackApi'

export type WorkspaceDraft = Omit<TrackWorkspaceState, 'exists' | 'mediaId'>
type PendingWrite = { scope?: string; key: string; kind: 'workspace' | 'feedback'; body: WorkspaceDraft | TrackingFeedbackInput }
type WriteResult = { revision: number; [key: string]: unknown }

type RecordValue = Record<string, any>
/** deleted: the other side deleted it; localDeleted: this page deleted it. */
export type MergeEntry = { key: string; field: string; label: string; base: unknown; local: unknown; remote: unknown; deleted?: boolean; localDeleted?: boolean; frameIndex?: number; objectId?: number }
export type MergeChoice = { value: 'local' | 'remote'; remote: unknown }
const fields = ['manualAnnotations','manualBaselines','deletedTrackingIds','deletedObjectIds','deletedFrameObjects'] as const
const stable = (value:any):any => value===undefined?null:Array.isArray(value)?value.map(stable):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(k=>[k,stable(value[k])])):value
const canonical = (value:unknown):string => JSON.stringify(stable(value))
export const workspaceEqual = (a: unknown,b: unknown) => canonical(a)===canonical(b)
const equal=workspaceEqual
const clone = <T>(value:T):T => JSON.parse(JSON.stringify(value))
export const sharedWorkspace = (state: WorkspaceDraft): WorkspaceDraft => Object.fromEntries([
  ['format','annotation-workspace-v1'],['generationId',state.generationId],...fields.map(k=>[k,state[k]??[]]),
].filter(([,v])=>v!==undefined)) as WorkspaceDraft
function remapIdentity(state:WorkspaceDraft,aliases:Map<number,number>,mark=false):WorkspaceDraft{
  const output=clone(state) as RecordValue
  for(const field of ['manualAnnotations','manualBaselines','deletedFrameObjects'])output[field]=(output[field]??[]).map((o:any)=>{
    const id=aliases.get(o.objectId)
    return id===undefined?o:{...o,objectId:id,...(mark&&field==='manualAnnotations'?{collaborationOriginalObjectId:o.objectId}:{}),...(typeof o.name==='string'?{name:o.name.replace(new RegExp(` ${o.objectId}$`),` ${id}`)}:{})}
  })
  output.deletedObjectIds=(output.deletedObjectIds??[]).map((id:number)=>aliases.get(id)??id)
  return output
}
export function alignWorkspaceIds(state:WorkspaceDraft,reference:WorkspaceDraft):WorkspaceDraft{
  // Only follow aliases produced by collaboration. Endpoint identity projection
  // has its own canonical mapping and must never be reverted to an old server ID.
  const byBirth=new Map((reference.manualAnnotations??[]).filter((o:any)=>o.creationId&&Number.isSafeInteger(o.collaborationOriginalObjectId)&&!o.endpointAnchorHash).map((o:any)=>[o.creationId,o as RecordValue]))
  if(!byBirth.size)return state
  const aliases=new Map<number,number>()
  for(const raw of state.manualAnnotations??[]){const obj=raw as RecordValue,ref=byBirth.get(obj.creationId);if(ref&&!obj.endpointAnchorHash&&ref.objectId!==obj.objectId)aliases.set(obj.objectId,ref.objectId)}
  const aligned=remapIdentity(state,aliases)
  aligned.manualAnnotations=(aligned.manualAnnotations??[]).map((raw:any)=>{const ref=byBirth.get(raw.creationId);return ref&&!raw.endpointAnchorHash?{...raw,collaborationOriginalObjectId:ref.collaborationOriginalObjectId}:raw})
  return aligned
}

export class WorkspaceMergeConflict extends Error {
  code='WORKSPACE_MERGE_CONFLICT'
  constructor(public entries: MergeEntry[], public base: WorkspaceDraft, public local: WorkspaceDraft, public remote: WorkspaceDraft) {
    super('有标注被同时修改，请选择保留哪一份；双方修改均已保留')
  }
}
export function mergeWorkspace(base: WorkspaceDraft, local: WorkspaceDraft, remote: WorkspaceDraft, choices: Record<string,MergeChoice> = {}): WorkspaceDraft {
  if(base.generationId!==remote.generationId || local.generationId!==remote.generationId)throw new Error('视频已覆盖重新标注，不能合并旧草稿；请备份后重新读取')
  const original={base,local,remote}
  base=alignWorkspaceIds(base,remote);local=alignWorkspaceIds(local,remote)
  const reserved=new Set<number>()
  for(const state of [base,local,remote]){
    for(const obj of [...(state.manualAnnotations??[]),...(state.manualBaselines??[]),...(state.deletedFrameObjects??[])])reserved.add((obj as RecordValue).objectId)
    for(const id of state.deletedObjectIds??[])reserved.add(id)
  }
  let nextId=1
  for(const id of reserved)if(Number.isSafeInteger(id))nextId=Math.max(nextId,id+1)
  const aliases=new Map<number,number>(),existing=new Set((base.manualAnnotations??[]).map((o:any)=>o.objectId))
  const group=(state:WorkspaceDraft)=>{
    const groups=new Map<number,RecordValue[]>()
    for(const raw of state.manualAnnotations??[]){const obj=raw as RecordValue;if(!groups.has(obj.objectId))groups.set(obj.objectId,[]);groups.get(obj.objectId)!.push(obj)}
    return groups
  }
  const ourGroups=group(local),theirGroups=group(remote)
  for(const [oid,ours] of ourGroups){
    if(existing.has(oid)||!theirGroups.has(oid))continue
    const theirs=theirGroups.get(oid)!
    if(!ours.every(o=>o.creationId)||!theirs.every(o=>o.creationId)||ours.some(a=>theirs.some(b=>a.creationId===b.creationId)))continue
    const overlap=ours.some(a=>theirs.some(b=>a.frameIndex===b.frameIndex&&a.bbox&&b.bbox&&Math.min(a.bbox.x+a.bbox.width,b.bbox.x+b.bbox.width)>Math.max(a.bbox.x,b.bbox.x)&&Math.min(a.bbox.y+a.bbox.height,b.bbox.y+b.bbox.height)>Math.max(a.bbox.y,b.bbox.y)))
    if(!overlap)aliases.set(oid,nextId++)
  }
  if(aliases.size)local=remapIdentity(local,aliases,true)
  base=clone(base);local=clone(local);remote=clone(remote)
  const deletionConflicts:MergeEntry[]=[]
  const scopes=new Map<string,{oid:number;fi?:number}>()
  for(const state of [base,local,remote]){
    for(const oid of state.deletedObjectIds??[])scopes.set(`object-delete:${oid}`,{oid})
    for(const d of state.deletedFrameObjects??[])scopes.set(`frame-delete:${d.frameIndex}:${d.objectId}`,{oid:d.objectId,fi:d.frameIndex})
  }
  const snapshot=(state:WorkspaceDraft,oid:number,fi?:number)=>({
    manualAnnotations:(state.manualAnnotations??[]).filter((o:any)=>o.objectId===oid&&(fi===undefined||o.frameIndex===fi)),
    manualBaselines:(state.manualBaselines??[]).filter(o=>o.objectId===oid&&(fi===undefined||o.frameIndex===fi)),
    deletedObjectIds:(state.deletedObjectIds??[]).filter(id=>id===oid),
    deletedFrameObjects:(state.deletedFrameObjects??[]).filter(o=>o.objectId===oid&&(fi===undefined||o.frameIndex===fi)),
  })
  for(const [key,{oid,fi}] of scopes){
    if(fi!==undefined&&scopes.has(`object-delete:${oid}`))continue
    const snapshots=[base,local,remote].map(state=>snapshot(state,oid,fi))
    const deleted=snapshots.map(state=>state.deletedObjectIds.length>0||(fi!==undefined&&state.deletedFrameObjects.length>0))
    if(deleted[1]===deleted[2] || !(deleted[1]!==deleted[0]&&!equal(snapshots[2],snapshots[0]) || deleted[2]!==deleted[0]&&!equal(snapshots[1],snapshots[0])))continue
    const choice=choices[key]
    if(!choice||!equal(choice.remote,snapshots[2])){
      deletionConflicts.push({key,field:'deletion',label:fi===undefined?`对象 ${oid} · 全部帧`:`第 ${fi+1} 帧 · 对象 ${oid}`,base:snapshots[0],local:snapshots[1],remote:snapshots[2],deleted:deleted[2],localDeleted:deleted[1],objectId:oid,frameIndex:fi??(snapshots.flatMap(state=>state.manualAnnotations)[0] as RecordValue|undefined)?.frameIndex});continue
    }
    const target=choice.value==='local'?remote:local
    for(const field of ['manualAnnotations','manualBaselines','deletedFrameObjects'] as const){
      (target as RecordValue)[field]=[...(target[field]??[]).filter((o:any)=>!(o.objectId===oid&&(fi===undefined||o.frameIndex===fi))),...snapshots[0][field]]
    }
    if(fi===undefined)target.deletedObjectIds=[...(target.deletedObjectIds??[]).filter(id=>id!==oid),...snapshots[0].deletedObjectIds]
  }
  if(deletionConflicts.length)throw new WorkspaceMergeConflict(deletionConflicts,original.base,original.local,original.remote)
  const result=sharedWorkspace(remote) as RecordValue, conflicts:MergeEntry[]=[]
  for(const field of fields){
    if(field==='manualBaselines')continue // Rebuild derived references from the adopted manual geometry below.
    const keyOf=(o:any)=>field==='manualAnnotations'||field==='deletedFrameObjects'?`${o.frameIndex??0}:${o.objectId??o.id}`:String(o)
    const maps=[base,local,remote].map(state=>new Map(((state[field]??[]) as any[]).map(o=>[keyOf(o),o])))
    const merged=[]
    for(const id of new Set(maps.flatMap(map=>[...map.keys()]))){
      const [b,l,r]=maps.map(map=>map.get(id)),key=`${field}:${id}`
      let chosen=equal(l,b)?r:equal(r,b)||equal(l,r)?l:undefined
      const oid=(l??b)?.objectId,fi=(l??b)?.frameIndex??0
      if(!equal(l,b)&&!equal(r,b)&&!equal(l,r)){
        const choice=choices[key]
        if(choice && equal(choice.remote,r))chosen=choice.value==='local'?l:r
        else conflicts.push({key,field,label:typeof(l??r??b)==='object'?`第 ${fi+1} 帧 · 对象 ${oid??id}`:'删除范围',base:b,local:l,remote:r,deleted:r===undefined,localDeleted:l===undefined,...(typeof(l??r??b)==='object'?{objectId:oid,frameIndex:fi}:{})})
      }
      if(chosen!==undefined)merged.push(chosen)
    }
    result[field]=merged
  }
  if(conflicts.length)throw new WorkspaceMergeConflict(conflicts,original.base,original.local,original.remote)
  const references=new Map<number,NonNullable<WorkspaceDraft['manualBaselines']>[number]>()
  const keyOf=(o:any)=>`${o.frameIndex??0}:${o.objectId}`
  const mergedManual=new Map(((result.manualAnnotations??[]) as RecordValue[]).map(o=>[keyOf(o),o]))
  for(const state of [local,remote,base]){
    const originals=new Map((state.manualAnnotations??[]).map(o=>[keyOf(o),o]))
    for(const reference of state.manualBaselines??[]){
    const adopted=mergedManual.get(keyOf(reference))
    const corresponding=originals.get(keyOf(reference))
    if(!adopted||!equal(adopted,corresponding))continue
    const previous=references.get(reference.objectId)
    if(!previous||previous.frameIndex<reference.frameIndex)references.set(reference.objectId,reference)
  }
  }
  result.manualBaselines=[...references.values()]
  return result
}

// IndexedDB keeps large video drafts and exact retry receipts outside the small
// Web Storage quota. The fallback supports older stored journals and unit hosts.
async function recoveryRecord(key:string, action:'get'|'put'|'delete', value?:unknown):Promise<any>{
  if(typeof indexedDB==='undefined'){
    if(action==='get')return JSON.parse(sessionStorage.getItem(key)||'null')
    if(action==='put')sessionStorage.setItem(key,JSON.stringify(value));else sessionStorage.removeItem(key)
    return
  }
  // window.name survives a reload but is independent of another tab, including
  // tabs whose sessionStorage was cloned by window.open.
  let client='test-host'
  if(typeof window!=='undefined'){
    if(!window.name.startsWith('annotation-recovery-client:'))window.name='annotation-recovery-client:'+createRequestId()
    client=window.name
  }
  key=client+':'+key
  return new Promise((resolve,reject)=>{
    const open=indexedDB.open('annotation-workspace-recovery',1)
    open.onupgradeneeded=()=>{if(!open.result.objectStoreNames.contains('records'))open.result.createObjectStore('records')}
    open.onerror=()=>reject(new Error('无法打开本机草稿缓存，请保持页面打开并导出备份'))
    open.onblocked=()=>reject(new Error('本机草稿缓存被其他页面占用，请关闭旧页面后重试'))
    open.onsuccess=()=>{
      const db=open.result,tx=db.transaction('records',action==='get'?'readonly':'readwrite'),store=tx.objectStore('records')
      const request=action==='get'?store.get(key):action==='put'?store.put(clone(value),key):store.delete(key)
      tx.oncomplete=()=>{const result=request.result;db.close();resolve(result)}
      tx.onerror=()=>{db.close();reject(new Error('本机草稿保存失败，请保持页面打开并导出备份'))}
      tx.onabort=tx.onerror
    }
  })
}

/** Serialize writes and retain the exact request on an ambiguous network failure.
 * The server receipt is checked before the revision, so replay never applies twice.
 * Recovery journals are scoped to browser tab, signed-in actor and video; no auth token is stored.
 */
export class WorkspaceWrites {
  /** The browser recovery cache is a convenience. Its failure never blocks a server save. */
  onStorageWarning:(message:string)=>void=()=>{}
  private cacheFailed(action:string,error:unknown){
    console.warn('[annotation.recovery_cache_failed]',{action,error})
    this.onStorageWarning('本机草稿缓存不可用：修改仍直接保存到服务器，但刷新前未保存的修改无法恢复，请勿关闭页面')
  }
  private revisions = new Map<string, number>()
  private baselines = new Map<string, WorkspaceDraft>()
  private mergeErrors = new Map<string, WorkspaceMergeConflict>()
  setBaseline(mediaId: string, state: TrackWorkspaceState) {
    this.setRevision(mediaId,state.revision||0)
    this.baselines.set(this.storageKey(mediaId),clone(sharedWorkspace(state)))
  }
  getBaseline(mediaId:string){return this.baselines.get(this.storageKey(mediaId))}
  async retainConflict(mediaId:string,error:WorkspaceMergeConflict){
    this.mergeErrors.set(this.storageKey(mediaId),error)
    await this.saveDraft(mediaId,error.base,error.local).catch(e=>this.cacheFailed('conflict',e))
  }
  getConflict(mediaId:string){return this.mergeErrors.get(this.storageKey(mediaId))}
  private draftKey(mediaId:string){return this.storageKey(mediaId)+':draft'}
  private draftQueues=new Map<string,Promise<any>>()
  private queueDraft<T>(key:string,action:()=>Promise<T>){
    const task=(this.draftQueues.get(key)??Promise.resolve()).catch(()=>{}).then(action)
    this.draftQueues.set(key,task)
    void task.finally(()=>{if(this.draftQueues.get(key)===task)this.draftQueues.delete(key)}).catch(()=>{})
    return task
  }
  async getDraft(mediaId:string):Promise<{base:WorkspaceDraft;local:WorkspaceDraft}|null>{
    const key=this.draftKey(mediaId)
    await this.draftQueues.get(key)?.catch(()=>{})
    try{return await recoveryRecord(key,'get')??JSON.parse(sessionStorage.getItem(key)||'null')}
    catch(error){this.cacheFailed('draft-read',error);return null}
  }
  saveDraft(mediaId:string,base:WorkspaceDraft,local:WorkspaceDraft){
    const key=this.draftKey(mediaId),record=clone({base,local})
    return this.queueDraft(key,()=>recoveryRecord(key,'put',record))
  }
  clearDraft(mediaId:string,scope=this.storageKey(mediaId),expectedLocal?:WorkspaceDraft){
    const key=scope+':draft'
    return this.queueDraft(key,async()=>{
      this.mergeErrors.delete(scope)
      try{
        if(expectedLocal){const current=await recoveryRecord(key,'get');if(current&&!equal(current.local,expectedLocal))return}
        await recoveryRecord(key,'delete')
        sessionStorage.removeItem(key)
      }catch(error){this.cacheFailed('draft-clear',error)}
    })
  }
  async restoreDraft(mediaId:string,remote:TrackWorkspaceState){
    const scope=this.storageKey(mediaId),saved=await this.getDraft(mediaId)
    if(scope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
    this.setBaseline(mediaId,remote)
    if(!saved)return null
    if(saved.base.generationId!==remote.generationId || saved.local.generationId!==remote.generationId){
      await recoveryRecord(this.draftKey(mediaId)+':archive:'+(saved.base.generationId??'legacy'),'put',saved).catch(e=>this.cacheFailed('archive',e))
      await this.clearDraft(mediaId,scope)
      console.warn('[annotation.draft_generation_archived]',{mediaId,generation:remote.generationId})
      return null
    }
    try{const result=await this.write(mediaId,'workspace',mergeWorkspace(saved.base,saved.local,remote));await this.clearDraft(mediaId,scope);return result}
    catch(error){if(error instanceof WorkspaceMergeConflict)this.mergeErrors.set(this.storageKey(mediaId),error);throw error}
  }
  async resolve(mediaId:string,choices:Record<string,MergeChoice>){
    const conflict=this.getConflict(mediaId)
    if(!conflict)throw new Error('没有待处理的标注冲突')
    const scope=this.storageKey(mediaId),pending=await this.read(mediaId)
    if(scope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
    if(pending){
      await this.send(mediaId,pending)
      const state=await trackApi.getWorkspaceState(mediaId)
      if(scope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
      this.setBaseline(mediaId,state);await this.clearDraft(mediaId,scope)
      return {revision:state.revision||0,workspace:sharedWorkspace(state)}
    }
    const remote=await trackApi.getWorkspaceState(mediaId)
    if(scope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
    try {
      const merged=mergeWorkspace(conflict.base,conflict.local,remote,choices)
      this.setBaseline(mediaId,remote)
      const result=await this.write(mediaId,'workspace',merged)
      await this.clearDraft(mediaId,scope);return result
    }catch(error){if(error instanceof WorkspaceMergeConflict)this.mergeErrors.set(scope,error);throw error}
  }
  private queues = new Map<string, Promise<unknown>>()
  private pending = new Map<string, PendingWrite>()
  private conflicts = new Map<string, Error>()
  private storageKey(mediaId: string) {
    let actor = 'unknown'
    try { actor = String(JSON.parse(localStorage.getItem('rare-sperm-auth') || '{}').id || actor) } catch {}
    return `annotation-write:${actor}:${mediaId}`
  }
  private async read(mediaId: string) {
    const scope = this.storageKey(mediaId)
    if (this.pending.has(scope)) return this.pending.get(scope)
    try {
      let stored:unknown
      try{stored=await recoveryRecord(scope,'get')}catch(error){this.cacheFailed('pending-read',error)}
      const value = (stored??JSON.parse(sessionStorage.getItem(scope)||'null')) as PendingWrite | null
      if (value?.key && ['workspace', 'feedback'].includes(value.kind)) {
        if (value.scope && value.scope !== scope) throw new Error('待重试请求的账号不匹配')
        value.scope = scope
        this.pending.set(scope, value)
        return value
      }
    } catch (error) { console.warn('[annotation.pending_read_failed]', { mediaId, error });throw new Error('待重试的保存记录无效，请保持页面打开并重新读取工作区') }
    return undefined
  }
  private async remember(mediaId: string, value?: PendingWrite, scope = this.storageKey(mediaId)) {
    if (value) this.pending.set(scope, value); else this.pending.delete(scope)
    await recoveryRecord(scope,value?'put':'delete',value).catch(error=>this.cacheFailed('pending',error))
    try {
      if (value && JSON.stringify(value).length<500000) sessionStorage.setItem(scope, JSON.stringify(value))
      else sessionStorage.removeItem(scope)
    } catch (error) { console.warn('[annotation.pending_cache_failed]', { mediaId, error }) }
  }
  setRevision(mediaId: string, revision: number) { this.revisions.set(this.storageKey(mediaId), revision); this.conflicts.delete(this.storageKey(mediaId)) }
  getRevision(mediaId: string) { return this.revisions.get(this.storageKey(mediaId)) }
  discardForReset(mediaId: string, revision: number) {
    // Only a confirmed server generation change authorizes dropping an old
    // ambiguous request. Ordinary conflicts must retain the reload requirement.
    return this.enqueue(mediaId, async () => {
      await this.remember(mediaId)
      this.setRevision(mediaId, revision)
    })
  }
  private async send(mediaId: string, request: PendingWrite): Promise<WriteResult> {
    const scope = request.scope || this.storageKey(mediaId)
    request.scope = scope
    if (scope !== this.storageKey(mediaId)) throw new Error('账号已切换，请重新加载当前账号的工作区')
    await this.remember(mediaId, request, scope)
    if(scope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
    try {
      const result = request.kind === 'workspace'
        ? await trackApi.saveWorkspaceState(mediaId, request.body as WorkspaceDraft, request.key)
        : await trackApi.submitFeedback(mediaId, request.body as TrackingFeedbackInput, request.key)
      if (!Number.isInteger(result.revision) || result.revision < 0) throw new Error('服务器未返回有效保存版本，请使用原请求重试')
      if (request.scope && request.scope !== this.storageKey(mediaId)) throw new Error('账号已切换，请重新加载当前账号的工作区')
      this.revisions.set(scope, result.revision)
      await this.remember(mediaId, undefined, scope)
      if(scope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
      if(request.kind==='workspace'&&this.baselines.has(scope)){
        const workspace=sharedWorkspace(request.body as WorkspaceDraft)
        this.baselines.set(scope,clone(workspace))
        return {...result,workspace} as unknown as WriteResult
      }
      return result as unknown as WriteResult
    } catch (error) {
      // A definite rejection is not a lost response. The workspace-only caller
      // may three-way merge a revision conflict against a fresh server read.
      const status = Number((error as { status?: number }).status)
      if (status >= 400 && status < 500 && status !== 408 && status !== 429) {
        await this.remember(mediaId, undefined, scope)
        if (status === 409 && error instanceof Error && /工作区版本|重试标识/.test(error.message)) this.conflicts.set(scope, error)
      }
      console.error('[annotation.write_failed]', { mediaId, kind: request.kind, key: request.key, error })
      throw error
    }
  }
  private enqueue<T>(mediaId: string, operation: () => Promise<T>) {
    const scope = this.storageKey(mediaId)
    const task = (this.queues.get(scope) || Promise.resolve()).catch(() => {}).then(() => {
      if (scope !== this.storageKey(mediaId)) throw new Error('账号已切换，请重新加载当前账号的工作区')
      return operation()
    })
    this.queues.set(this.storageKey(mediaId), task)
    void task.finally(() => { if (this.queues.get(scope) === task) this.queues.delete(scope) }).catch(() => {})
    return task
  }
  replay(mediaId: string) {
    return this.enqueue(mediaId, async () => {
      const pending = await this.read(mediaId)
      if (pending) await this.send(mediaId, pending)
    })
  }
  write(mediaId: string, kind: PendingWrite['kind'], body: PendingWrite['body'], key = createRequestId()) {
    const intentScope=this.storageKey(mediaId),intentBase=this.baselines.get(intentScope)
    const initialDraft=kind==='workspace'&&intentBase?sharedWorkspace(body as WorkspaceDraft):null
    const cached=(initialDraft&&!equal(initialDraft,intentBase)?this.saveDraft(mediaId,intentBase!,initialDraft):Promise.resolve()).catch(error=>this.cacheFailed('draft',error))
    // Attach a rejection handler immediately; the serialized operation below still
    // awaits and reports it before making any network request.
    void cached.catch(()=>{})
    return this.enqueue(mediaId, async () => {
      const initiatingScope=this.storageKey(mediaId)
      await cached
      if(intentScope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
      if (this.conflicts.has(this.storageKey(mediaId))) throw this.conflicts.get(this.storageKey(mediaId))
      const pending = await this.read(mediaId)
      if(initiatingScope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
      if (pending) {
        const result = await this.send(mediaId, pending)
        if (pending.key === key) return result
      }
      if (!this.revisions.has(this.storageKey(mediaId))) {
        const scope = this.storageKey(mediaId)
        const state = await trackApi.getWorkspaceState(mediaId)
        if (scope !== this.storageKey(mediaId)) throw new Error('账号已切换，请重新加载当前账号的工作区')
        this.setRevision(mediaId, state.revision || 0)
      }
      const scope=this.storageKey(mediaId),base=intentBase??this.baselines.get(scope)
      if(kind==='workspace'&&base){
        const draft=sharedWorkspace(body as WorkspaceDraft)
        if(equal(base,draft))return {revision:this.getRevision(mediaId)!,workspace:this.baselines.get(scope)??base}
        if(scope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
        let intended=mergeWorkspace(base,draft,this.baselines.get(scope)??base)
        if(equal(intended,this.baselines.get(scope))){await this.clearDraft(mediaId,scope,draft);return {revision:this.getRevision(mediaId)!,workspace:intended}}
        for(let attempt=0;attempt<3;attempt++){
          const request={scope,kind,key:attempt===0?key:createRequestId(),body:{...intended,expectedRevision:this.getRevision(mediaId)!}}
          try {
            const result=await this.send(mediaId,request)
            this.baselines.set(scope,clone(intended));await this.clearDraft(mediaId,scope,draft)
            return {...result,workspace:intended}
          }catch(error){
            if(Number((error as {status?:number}).status)!==409 || !/工作区版本/.test((error as Error).message))throw error
            const remote=await trackApi.getWorkspaceState(mediaId)
            if(scope!==this.storageKey(mediaId))throw new Error('账号已切换，请重新读取')
            try{intended=mergeWorkspace(base,draft,remote)}
            catch(conflict){if(conflict instanceof WorkspaceMergeConflict){this.mergeErrors.set(scope,conflict);this.conflicts.delete(scope)}throw conflict}
            this.setRevision(mediaId,remote.revision||0)
            console.warn('[annotation.workspace_rebased]',{mediaId,revision:remote.revision,attempt})
          }
        }
        throw new Error('此视频正在被连续修改，草稿已保留，请稍后重试')
      }
      const request = { scope, kind, key, body: { ...body, expectedRevision: this.revisions.get(scope)! } }
      return this.send(mediaId, request)
    })
  }
}
