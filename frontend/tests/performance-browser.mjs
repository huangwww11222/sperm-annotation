// Real user-video copies, bounded client state, compact C writes and recovery.
import assert from 'node:assert/strict'
import fs from 'node:fs'
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright')
const fixture=JSON.parse(fs.readFileSync('work/real-4k-browser-fixture.json'))
const origin=process.env.CONFIRMATION_ORIGIN||'http://127.0.0.1:5387'
const browser=await chromium.launch({channel:'chrome',headless:true}),page=await browser.newPage({viewport:{width:1366,height:768}})
const errors=[],checks=[],reads=[],responses=[]
const check=(ok,msg)=>{assert(ok,msg);checks.push(msg);console.log('PASS',checks.length,msg)}
page.on('pageerror',e=>errors.push(String(e)))
page.on('dialog',d=>d.accept())
page.on('request',r=>{if(r.url().includes('/api/track/frame/'))reads.push(r.url())})
page.on('response',async r=>{if(r.url().includes('/api/confirmation/')&&r.request().method()!=='GET'){try{responses.push(await r.json())}catch{}}})
const button=name=>page.getByRole('button',{name,exact:true})
const idle=()=>page.waitForFunction(()=>window.ws&&!window.ws.workspaceRestoring.value&&!window.ws.exactFrameLoading.value)
const cIdle=()=>page.locator('.confirmation-page[data-busy="false"]').waitFor()
const api=async(path,method='GET',body)=>{const r=await page.request.fetch(origin+'/api'+path,{method,headers:{Authorization:'Bearer '+fixture.token,'Idempotency-Key':crypto.randomUUID(),'X-Review-Contract':'2','X-Confirmation-Response':'delta'},data:body});assert(r.ok(),await r.text());return r.json()}
const attach=()=>page.evaluate(async()=>{window.ws=(await import(performance.getEntriesByType('resource').find(r=>r.name.includes('/src/stores/workspace.ts')).name)).useWorkspace()})
fs.mkdirSync('output/playwright',{recursive:true})
try{
  await page.addInitScript(f=>{localStorage.setItem('rare-sperm-token',f.token);localStorage.setItem('rare-sperm-auth',JSON.stringify({id:'3',name:'Performance tester'}))},fixture)
  await page.route('**/api/track/media',async route=>{const r=await route.fetch(),d=await r.json();d.items=d.items.filter(m=>fixture.assets.some(x=>x.mid===m.mediaId));await route.fulfill({response:r,json:d})})
  await page.goto(origin+'/annotate');await attach();await idle()
  for(const asset of fixture.assets){
    await page.locator('.asset-card').filter({hasText:asset.name}).click();await idle()
    const state=await page.evaluate(()=>({w:window.ws.selectedMedia.value.width,h:window.ws.selectedMedia.value.height,fps:window.ws.videoFps.value}))
    check(state.w===asset.width&&state.h===asset.height&&state.fps===asset.fps,asset.name+': native dimensions and source FPS load')
    for(const fi of [Math.floor(asset.frameCount/2),asset.frameCount-1,0]){
      await page.evaluate(async fi=>{await window.ws.loadExactFrame(fi)},fi);await idle()
      check(await page.evaluate(fi=>window.ws.currentFrame.value===fi&&!!window.ws.exactFrameUrl.value,fi),asset.name+': exact frame '+fi+' displays')
    }
  }
  const long=fixture.assets.reduce((a,b)=>a.frameCount>b.frameCount?a:b)
  await page.locator('.asset-card').filter({hasText:long.name}).click();await idle()
  const telemetry=await page.evaluate(async()=>{
    const ws=window.ws,mid=ws.currentMediaId.value;window.originalAnnotations=ws.annotationsByMedia.value[mid]
    const rows=Array.from({length:50000},(_,i)=>({id:'stress-ai-'+i,objectId:100+i%80,frameIndex:Math.floor(i/80),name:'sperm '+(100+i%80),source:'ai',bbox:{x:10+i%20,y:20,width:1,height:1}}))
    const started=performance.now();ws.annotationsByMedia.value={...ws.annotationsByMedia.value,[mid]:[...window.originalAnnotations.filter(o=>o.source==='manual'),...rows]}
    const count=ws.currentObjects.value.length
    return {count,assignmentMs:performance.now()-started,deepProxy:!!ws.annotationsByMedia.value[mid][1].__v_isReactive}
  })
  check(telemetry.count===81&&!telemetry.deepProxy,'50,000 AI boxes use shallow immutable storage and a frame index')
  await page.waitForTimeout(1100)
  const cached=await page.evaluate(()=>{const mid=window.ws.currentMediaId.value,raw=localStorage.getItem('annotationsByMedia');return {bytes:raw.length,objects:JSON.parse(raw)[mid]}})
  check(cached.bytes<50000&&cached.objects.length===1&&cached.objects.every(o=>o.source==='manual'),'large server AI results are not repeatedly serialized into localStorage')
  await page.evaluate(()=>{window.ws.selectedObjectId.value='stress-ai-0';window.ws.nudgeSelected(1,0)})
  check(await page.evaluate(()=>window.ws.currentObjects.value.find(o=>o.id==='stress-ai-0').source==='manual'),'editing a dense frame still commits only the selected object')
  await page.evaluate(()=>window.ws.undo())
  check(await page.evaluate(()=>window.ws.currentObjects.value.find(o=>o.id==='stress-ai-0').source==='ai'),'dense-frame undo restores exact object source')
  await page.evaluate(async()=>{const ws=window.ws,mid=ws.currentMediaId.value;ws.annotationsByMedia.value={...ws.annotationsByMedia.value,[mid]:window.originalAnnotations};await ws.persistWorkspaceState(mid,true)})
  const badFrame=Math.floor(long.frameCount/2)+7,badUrl=`**/api/track/frame/${long.mid}/${badFrame}`
  await page.route(badUrl,route=>route.fulfill({status:200,contentType:'image/jpeg',body:'broken JPEG'}))
  const badResult=await page.evaluate(async fi=>({ok:await window.ws.loadExactFrame(fi),frame:window.ws.currentFrame.value,error:window.ws.frameError.value}),badFrame)
  check(!badResult.ok&&badResult.frame===0&&!!badResult.error,'undecodable HTTP 200 frame keeps the previous image/frame and locks editing')
  await page.unroute(badUrl);await page.evaluate(async()=>{await window.ws.retryExactFrame()});await idle()
  check(await page.evaluate(fi=>window.ws.currentFrame.value===fi&&!window.ws.frameError.value,badFrame),'bad JPEG cache invalidation allows a fresh successful retry')
  await page.evaluate(async()=>{await window.ws.loadExactFrame(0)});await idle()
  let failRead=true
  await page.route('**/api/track/result/**',async route=>{if(failRead)await route.fulfill({status:500,json:{message:'Injected initial result read failure'}});else await route.continue()})
  await page.reload();await attach();await idle()
  check(await page.evaluate(()=>window.ws.editingBlocked.value&&window.ws.saveError.value.includes('追踪结果读取失败')),'failed initial AI-result read locks editing instead of treating unknown frames as empty')
  failRead=false;await button('重新读取工作区').click();await idle()
  check(await page.evaluate(()=>!window.ws.editingBlocked.value&&window.ws.currentObjects.value.length===3),'explicit reload restores server AI results after compact local caching')
  await page.goto(origin+'/confirm');await cIdle()
  await page.locator(`[data-session-id="${fixture.confirmation}"]`).click();await cIdle()
  if(await button('留在上次位置').count())await button('留在上次位置').click()
  const mid=fixture.assets.reduce((a,b)=>a.frameCount<b.frameCount?a:b).mid
  const imageReads=()=>reads.filter(u=>u.includes('/track/frame/'+mid+'/')).length
  const initialReads=imageReads()
  await button('下一修改项 →').click();await cIdle();await button('← 上一修改项').click();await cIdle()
  check(imageReads()===initialReads,'C switches same-frame objects without repeated 4K JPEG downloads')
  const items=(await api('/confirmation/sessions/'+fixture.confirmation+'/changes')).items
  await page.getByTestId('choose-a').click();await cIdle()
  check(responses.some(r=>r.itemsScope==='changed'&&r.items.length===1),'a C decision returns only the changed item with updated session progress')
  check(imageReads()===initialReads,'automatic next object on the same frame keeps its decoded image')
  await api(`/confirmation/sessions/${fixture.confirmation}/changes/${items[2].changeId}/decision`,'PUT',{choice:'A',expectedDecisionRevision:0})
  const badConfirmationImage=`**/api/track/frame/${mid}/1`
  await page.route(badConfirmationImage,route=>route.fulfill({status:200,contentType:'image/jpeg',body:'broken JPEG'}))
  await page.getByTestId('choose-b').click();await cIdle()
  check((await api('/confirmation/sessions/'+fixture.confirmation)).progress.decided===3,'concurrent decisions keep all saved server choices')
  check(await page.locator('[data-testid=video-progress]').textContent().then(t=>t.includes('3 / 6')),'version-gap recovery refreshes the full client list and progress')
  check(await button('重试加载').isVisible()&&await page.getByTestId('choose-a').isDisabled(),'C image decode failure blocks new choices while keeping committed decisions')
  await page.unroute(badConfirmationImage);await button('重试加载').click();await cIdle()
  check(await page.locator('.c-context [data-variant=full]').count()===1&&await page.getByTestId('choose-a').isEnabled(),'C invalidates broken blobs before retrying the original target frame')
  const beforeUndo=imageReads();await page.getByRole('button',{name:/撤销上一次选择/}).click();await cIdle()
  check(imageReads()===beforeUndo,'undo back to an earlier frame reuses the bounded JPEG cache')
  check((await api('/confirmation/sessions/'+fixture.confirmation)).progress.decided===2,'delta undo preserves unrelated and concurrent decisions')
  check(responses.filter(r=>r.itemsScope==='changed').some(r=>r.items.length===0),'cursor receipts contain no full video change list')
  check(errors.length===0,'no uncaught browser errors across real-video and dense-annotation flows')
  await page.screenshot({path:'output/playwright/real-4k-performance.png'})
  fs.writeFileSync('output/playwright/performance-results.json',JSON.stringify({checks,telemetry,errors},null,2))
  console.log('Completed',checks.length,'real-video performance/recovery checks.')
}finally{await browser.close()}
