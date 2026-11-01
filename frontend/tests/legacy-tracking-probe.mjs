// Compatibility-only probe for the retired single-direction entry. New-user
// workflows must use the visible endpoint button, never this store-level probe.
export function legacyTrackingProbe(page){
  return {click:()=>page.evaluate(async()=>{const resource=performance.getEntriesByType('resource').find(r=>r.name.includes('/src/stores/workspace.ts'));const ws=(await import(resource.name)).useWorkspace();void ws.runAiTrack()}),
    isEnabled:()=>page.evaluate(async()=>{const resource=performance.getEntriesByType('resource').find(r=>r.name.includes('/src/stores/workspace.ts'));const ws=(await import(resource.name)).useWorkspace();return !ws.editingBlocked.value&&ws.trackingPausedFrame.value===null}),
    isDisabled:()=>page.evaluate(async()=>{const resource=performance.getEntriesByType('resource').find(r=>r.name.includes('/src/stores/workspace.ts'));const ws=(await import(resource.name)).useWorkspace();return ws.editingBlocked.value||ws.hasPausedTracking?.value||!!ws.trackingPausedFrame.value})}
}
export async function passReview(page,token,mid){
  // API shortcut for suites that need a reviewed video; the review UI itself is
  // exercised in training-export and critical-workflows.
  const headers={Authorization:'Bearer '+token}
  const call=async(path,method='GET',data)=>{const r=await page.request.fetch(new URL('/api/annotation-review/media/'+mid+path,page.url()).href,{method,data,headers});if(!r.ok())throw Error(await r.text());return r.json()}
  let state=await call('/start','POST')
  for(const item of state.review.items)state=await call('/items/'+item.id,'PUT',{reviewId:state.review.reviewId,result:item.kind==='unknown'?'empty':'ok'})
  return call('/pass','POST',{reviewId:state.review.reviewId})
}
