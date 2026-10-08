import { withRequestTimeout } from '../utils/browserCompat'
import { tokenStore } from './http'
import { sourceFrameBlob } from './sourceFrameApi'
import type { ReviewObject, Patch } from '../review/geometry'
export interface CompletionPreview { sourceRevision:string;frameCount:number;objectFrames:number;emptyFrames:number;unknownFrames:number;unknownFrameRanges:{start:number;end:number}[] }
export interface Progress { submittedFrames:number;unsubmittedFrames:number;draftFrames:number;modifiedFrames:number;modifiedBoxes:number;percent:number }
export interface Session {
  id:string;baselineId:string;state:string;revision:number;reviewerId:number|null;frameCount:number;mediaId:string
  media:{name:string;mediaId:string;width:number;height:number;fps:number;frameCount:number}
  progress:Progress
  resume:{lastViewedFrameIndex:number|null;cursorRevision:number;firstUnsubmittedFrameIndex:number|null;draftFrameIndexes:number[]}
  permissions:{canClaim:boolean;canEdit:boolean;canComplete:boolean;canWithdraw:boolean}
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
export interface Failure { message:string;status:number;code?:string;requestId?:string }
async function request<T>(path:string,method='GET',body?:unknown,key?:string):Promise<T> {
  return withRequestTimeout(30000, async (signal) => {
    const response=await fetch('/api/review'+path,{method,signal,headers:{Authorization:`Bearer ${tokenStore.get()}`,'Content-Type':'application/json',
      ...(key?{'X-Review-Contract':'2','Idempotency-Key':key}:{})},body:body===undefined?undefined:JSON.stringify(body)})
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
  submissionStatus:(id:string)=>request<{items:Session[]}>(`/media/${encodeURIComponent(id)}/submission-status`),
  withdraw:(id:string,revision:number,key:string)=>request<Mutation>(`/sessions/${id}/withdraw`,'POST',{expectedSessionRevision:revision},key),
  resetAnnotations:(id:string,revision:number,key:string)=>request<{ok:boolean;mediaId:string;revision:number;generationId:string}>(`/media/${encodeURIComponent(id)}/reset-annotations`,'POST',{expectedRevision:revision,confirmDiscard:true},key),
  list:()=>request<{items:Session[]}>('/sessions'),
  session:(id:string)=>request<Session>(`/sessions/${id}`),
  frame:(id:string,fi:number)=>request<Frame>(`/sessions/${id}/frames/${fi}`),
  claim:(id:string,key:string)=>request<{session:Session}>(`/sessions/${id}/claim`,'POST',{},key),
  mutate:(id:string,fi:number,kind:'draft'|'submit'|'discard',body:unknown,key:string)=>request<Mutation>(`/sessions/${id}/frames/${fi}/${kind}`,kind==='draft'?'PUT':'POST',body,key),
  finish:(id:string,revision:number,key:string)=>request<Mutation>(`/sessions/${id}/freeze`,'POST',{expectedSessionRevision:revision},key),
  cursor:(id:string,fi:number,revision:number,key:string)=>request<{revision:number}>(`/cursors`,'PUT',{sessionId:id,frameIndex:fi,expectedCursorRevision:revision},key),
  async image(mediaId:string,fi:number) {
    return URL.createObjectURL(await sourceFrameBlob(mediaId,fi))
  },
}
