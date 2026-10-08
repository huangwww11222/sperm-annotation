import { withRequestTimeout } from '../utils/browserCompat'
import { tokenStore } from './http'
import { sourceFrameBlob } from './sourceFrameApi'
import type { Box, ReviewObject } from '../review/geometry'
export type Choice = 'A' | 'B'
export interface Change {
  changeId:string;frameIndex:number;objectId:number;annotationId:string;beforeBbox:Box;afterBbox:Box
  metrics:{iou:number;centerShiftPx:number;deltaCenterX:number;deltaCenterY:number;deltaWidth:number;deltaHeight:number;changeType:string}
  decisionRevision:number;decision:{choice:Choice;eventId:string}|null
}
export interface ConfirmationSession {
  id:string;baselineId:string;reviewVersionId:string;state:string;revision:number;confirmerId:number|null
  media:{mediaId:string;name:string;width:number;height:number;fps:number;frameCount:number}
  progress:{totalChanges:number;decided:number;pending:number;keptA:number;adoptedB:number;percent:number}
  permissions:{canClaim:boolean;canEdit:boolean;canUndo:boolean;canComplete:boolean;canReopen:boolean;canExport:boolean;canReturn:boolean}
  returnedReview?:{frameIndex:number;reason:string;reviewSessionId:string;nextConfirmationId:string|null}|null
  resume:{lastViewedChangeId:string|null;cursorRevision:number;firstPendingChangeId:string|null}
  undo:{actionId:string;changeId:string}|null;finalVersionId:string|null;readOnlyReason:string|null
}
export interface FrameContext { frameIndex:number;baselineObjects:ReviewObject[];reviewObjects:ReviewObject[] }
export type Action = 'claim'|'decide'|'undo'|'finish'|'reopen'|'return'|'cursor'
export interface Operation { action:Action;sid:string;changeId?:string;body:Record<string,unknown>;key:string }
export interface Result { session:ConfirmationSession;items:Change[];itemsScope?:'changed';selectedChangeId:string|null;nextPendingChangeId:string|null;savedAt:string }
export interface Failure {message:string;status?:number;code?:string;requestId?:string}
async function request<T>(path:string,method='GET',body?:unknown,key?:string,blob=false):Promise<T> {
  try {
    return await withRequestTimeout(30000, async (signal) => {
      const r=await fetch('/api'+path,{method,signal,headers:{Authorization:`Bearer ${tokenStore.get()}`,'Content-Type':'application/json',...(key?{'X-Review-Contract':'2','Idempotency-Key':key,'X-Confirmation-Response':'delta'}:{})},body:body===undefined?undefined:JSON.stringify(body)})
      if(!r.ok) {
        const d=await r.json().catch(()=>({}))
        throw {message:d.message||(typeof d.detail==='string'?d.detail:`请求失败（${r.status}）`),status:r.status,code:d.code,requestId:d.requestId||r.headers.get('X-Request-ID')}
      }
      return (blob ? await r.blob() : await r.json()) as T
    })
  } catch(e) {
    const f=e as Failure
    const failure:Failure={...f,message:f.message||'连接中断或超时，请重试'}
    console.error('[confirmation.request_failed]',{path,method,...failure})
    throw failure
  }
}
const base='/confirmation/sessions'
export const confirmationApi={
  list:()=>request<{items:ConfirmationSession[]}>(base),
  session:(id:string)=>request<ConfirmationSession>(`${base}/${id}`),
  changes:(id:string)=>request<{items:Change[]}>(`${base}/${id}/changes`),
  frame:(id:string,fi:number)=>request<FrameContext>(`${base}/${id}/frames/${fi}`),
  operate:(op:Operation)=>request<Result>(`${base}/${op.sid}/`+(op.action==='decide'?`changes/${op.changeId}/decision`:op.action==='finish'?'finalize':op.action),op.action==='decide'||op.action==='cursor'?'PUT':'POST',op.body,op.key),
  image:(media:string,fi:number)=>sourceFrameBlob(media,fi),
  download:(id:string)=>request<Blob>(`/final-versions/${id}/download`,'GET',undefined,undefined,true),
}
