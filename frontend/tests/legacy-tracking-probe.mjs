// Compatibility-only probe for the retired single-direction entry. New-user
// workflows must use the visible endpoint button, never this store-level probe.
export function legacyTrackingProbe(page){
  return {click:()=>page.evaluate(async()=>{const resource=performance.getEntriesByType('resource').find(r=>r.name.includes('/src/stores/workspace.ts'));const ws=(await import(resource.name)).useWorkspace();void ws.runAiTrack()}),
    isEnabled:()=>page.evaluate(async()=>{const resource=performance.getEntriesByType('resource').find(r=>r.name.includes('/src/stores/workspace.ts'));const ws=(await import(resource.name)).useWorkspace();return !ws.editingBlocked.value&&ws.trackingPausedFrame.value===null}),
    isDisabled:()=>page.evaluate(async()=>{const resource=performance.getEntriesByType('resource').find(r=>r.name.includes('/src/stores/workspace.ts'));const ws=(await import(resource.name)).useWorkspace();return ws.editingBlocked.value||ws.hasPausedTracking?.value||!!ws.trackingPausedFrame.value})}
}
export async function legacySubmit(page,token,mid){
  // Historical workflow ingress remains an authenticated API, not a new UI action.
  const headers={Authorization:'Bearer '+token,'X-Review-Contract':'2','Idempotency-Key':crypto.randomUUID()}
  const p=await page.request.get(new URL('/api/review/media/'+mid+'/completion-preview',page.url()).href,{headers});if(!p.ok())throw Error(await p.text());const preview=await p.json()
  const response=await page.request.post(new URL('/api/review/media/'+mid+'/complete',page.url()).href,{headers,data:{expectedSourceRevision:preview.sourceRevision,confirmComplete:true,concentratedReview:true,explicitEmptyFrameRanges:[]}})
  if(!response.ok())throw Error(await response.text());return response.json()
}
