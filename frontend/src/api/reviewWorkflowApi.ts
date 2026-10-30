import { withRequestTimeout } from '../utils/browserCompat'
import { tokenStore } from './http'
import { sourceFrameBlob } from './sourceFrameApi'
import type { ReviewObject, Patch } from '../review/geometry'
export interface CompletionPreview { sourceRevision:string;frameCount:number;objectFrames:number;emptyFrames:number;unknownFrames:number;unknownFrameRanges:{start:number;end:number}[] }
export interface Progress { submittedFrames:number;unsubmittedFrames:number;draftFrames:number;modifiedFrames:number;modifiedBoxes:number;percent:number }
export interface Session {
  workflow?:'concentrated'|'legacy';finalVersionId?:string|null;
  id:string;baselineId:string;state:string;revision:number;reviewerId:number|null;submittedBy:number;frameCount:number;mediaId:string
  media:{name:string;mediaId:string;width:number;height:number;fps:number;frameCount:number}
  progress:Progress
  resume:{lastViewedFrameIndex:number|null;cursorRevision:number;firstUnsubmittedFrameIndex:number|null;draftFrameIndexes:number[];submittedFrameIndexes?:number[]}
  permissions:{canClaim:boolean;canEdit:boolean;canComplete:boolean;canWithdraw:boolean;canReturnAnnotation:boolean}
  withdrawalBlockedReason?:string|null
  returnAnnotation?:{reason:string;actorId:number;createdAt:string}|null
  returnRequests?:{frameIndex:number;reason:string;confirmationId:string}[]
  readOnlyReason:string|null;completedReviewVersionId:string|null
}
export interface Frame {
  sessionId:string;frameIndex:number;frameRevision:number;state:string;hasDraft:boolean
  baselineObjects:ReviewObject[];effectiveObjects:ReviewObject[];patch:Patch[]
  lastSubmission:{id:string;patch:Patch[];submittedAt:string}|null
  permissions:{canEdit:boolean;canSubmit:boolean;canDiscard:boolean}
}
export interface Mutation { frame?:Frame;session:Session;nextUnsubmittedFrameIndex?:number|null }
export interface ReviewCatalogObject {objectId:number;classKey:string;origin:'baseline'|'review'}
export interface Failure { message:string;status:number;code?:string;requestId?:string }
async function request<T>(path:string,method='GET',body?:unknown,key?:string):Promise<T> {
  return withRequestTimeout(30000, async (signal) => {
    const response=await fetch('/api/review'+path,{method,signal,headers:{Authorization:`Bearer ${tokenStore.get()}`,'Content-Type':'application/json',
      ...(key?{'X-Review-Contract':'3','Idempotency-Key':key}:{})},body:body===undefined?undefined:JSON.stringify(body)})
    const data=await response.json()
    if(!response.ok) {
      const failure:Failure={status:response.status,message:data.message|| (typeof data.detail==='string'?data.detail:'请求未通过校验'),code:data.code,requestId:data.requestId||response.headers.get('X-Request-ID')}
      console.error('[review.request_failed]',{method,path,...failure})
      throw failure
    }
    return data
  })
}
export const reviewWorkflowApi={
  completionPreview:(id:string)=>request<CompletionPreview>(`/media/${encodeURIComponent(id)}/completion-preview`),
  completeAnnotation:(id:string,body:unknown,key:string)=>request<Mutation>(`/media/${encodeURIComponent(id)}/complete`,'POST',body,key),
  submissionStatus:(id:string)=>request<{items:Session[];history?:Session[]}>(`/media/${encodeURIComponent(id)}/submission-status`),
  withdraw:(id:string,revision:number,key:string)=>request<Mutation>(`/sessions/${id}/withdraw`,'POST',{expectedSessionRevision:revision},key),
  returnAnnotation:(id:string,revision:number,reason:string,key:string)=>request<Mutation>(`/sessions/${id}/return-annotation`,'POST',{expectedSessionRevision:revision,reason},key),
  objects:(id:string)=>request<{items:ReviewCatalogObject[]}>(`/sessions/${id}/objects`),
  reserveObject:(id:string,revision:number,key:string,classKey?:string)=>request<{session:Session;object:ReviewCatalogObject}>(`/sessions/${id}/objects`,'POST',{expectedSessionRevision:revision,...(classKey?{classKey}:{})},key),
  resetAnnotations:(id:string,revision:number,key:string)=>request<{ok:boolean;mediaId:string;revision:number;generationId:string}>(`/media/${encodeURIComponent(id)}/reset-annotations`,'POST',{expectedRevision:revision,confirmDiscard:true},key),
  list:()=>request<{items:Session[]}>('/sessions'),
  session:(id:string)=>request<Session>(`/sessions/${id}`),
  async evidence(id:string):Promise<ReviewEvidence> {
    const data=await request<ReviewEvidence>(`/sessions/${id}/evidence`)
    if(!data||!Array.isArray(data.groups)||!Array.isArray(data.checkedFrames)||!Array.isArray(data.failedFrames)
      ||data.groups.some(g=>!g||typeof g.id!=='string'||typeof g.message!=='string'||!Number.isInteger(g.startFrame)||g.startFrame<0||!Number.isInteger(g.endFrame)||g.endFrame<g.startFrame||!Array.isArray(g.samples)))throw new Error('疑点响应结构无效，不能视为没有疑点')
    const endpoint=data.endpointVerification
    if(endpoint && (!['checked','failed','insufficient'].includes(endpoint.state)||!endpoint.matches||typeof endpoint.matches!=='object'||Array.isArray(endpoint.matches)||!Object.values(endpoint.matches).every(v=>Number.isSafeInteger(v)&&v>0)||!Number.isSafeInteger(endpoint.checkedFrames)||endpoint.checkedFrames<0))throw new Error('首尾核验响应结构无效，请重新读取')
    if(data.jointTracking && (!['checked','failed'].includes(data.jointTracking.state)||!Number.isSafeInteger(data.jointTracking.checkedFrames)||data.jointTracking.checkedFrames<0))throw new Error('自动标注状态无效，请重新读取')
    return data
  },
  frame:(id:string,fi:number)=>request<Frame>(`/sessions/${id}/frames/${fi}`),
  claim:(id:string,key:string)=>request<{session:Session}>(`/sessions/${id}/claim`,'POST',{},key),
  mutate:(id:string,fi:number,kind:'draft'|'submit'|'discard',body:unknown,key:string)=>request<Mutation>(`/sessions/${id}/frames/${fi}/${kind}`,kind==='draft'?'PUT':'POST',body,key),
  reopen:(id:string,body:unknown,key:string)=>request<Mutation>(`/sessions/${id}/reopen-review`,'POST',body,key),
  finish:(id:string,revision:number,key:string)=>request<Mutation>(`/sessions/${id}/freeze`,'POST',{expectedSessionRevision:revision},key),
  cursor:(id:string,fi:number,revision:number,key:string)=>request<{revision:number}>(`/cursors`,'PUT',{sessionId:id,frameIndex:fi,expectedCursorRevision:revision},key),
  async image(mediaId:string,fi:number) {
    return URL.createObjectURL(await sourceFrameBlob(mediaId,fi))
  },
}

export interface ReviewDoubt {id:string;objectId:number|null;kind:string;message:string;startFrame:number;endFrame:number;reviewed:boolean;samples:{frameIndex:number;bbox?:number[];candidateBox?:number[];message:string}[]}
export interface ReviewEvidence {groups:ReviewDoubt[];checkedFrames:number[];failedFrames:number[];policy:string;backwardCheck:string;backwardChecked?:number[];backwardFailed?:number[];sourceHash?:string;endpointVerification?:{state:'checked'|'failed'|'insufficient';matches:Record<string,number>;checkedFrames:number;anchorFrame:number;error?:string};jointTracking?:{state:'checked'|'failed';checkedFrames:number}}
