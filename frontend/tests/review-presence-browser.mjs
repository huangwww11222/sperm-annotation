// Real B/C state, real frame decoding and actual export; no model inference.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import {execFileSync} from 'node:child_process'
import {useLegacyBrowserAPIs} from './legacy-browser-profile.mjs'
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright')
// The task list is a drawer that stays closed while a task is open.
const openTask=async(target,selector)=>{const card=target.locator(selector),toggle=target.getByTestId('toggle-workbench-library');for(let i=0;i<40;i++){if(await card.isVisible()){try{await card.click({timeout:1500});return}catch{}}else if(await toggle.isVisible())await toggle.click();await target.waitForTimeout(250)}await card.click()}
// The media list is a drawer that stays closed while a video is open.
const openAsset=async(target,locator)=>{const toggle=target.getByTestId('toggle-workbench-library');for(let i=0;i<40;i++){if(await locator.isVisible()){try{await locator.click({timeout:1500});return}catch{}}else if(await toggle.isVisible())await toggle.click();await target.waitForTimeout(250)}await locator.click()}
const fixture=JSON.parse(fs.readFileSync(process.env.REVIEW_PRESENCE_FIXTURE||'work/review-presence-browser-fixture.json'))
const origin=process.env.REVIEW_ORIGIN||process.env.CONFIRMATION_ORIGIN||'http://127.0.0.1:5373'
const output=process.env.REVIEW_PRESENCE_OUTPUT||'output/playwright/review-presence'
fs.mkdirSync(output,{recursive:true})
const browser=await chromium.launch({channel:process.env.CHROME_CHANNEL||'chrome',headless:true})
const page=await browser.newPage({viewport:{width:1366,height:768}})
page.setDefaultTimeout(15000)
await useLegacyBrowserAPIs(page)
const checks=[],errors=[],logs=[]
page.on('pageerror',e=>errors.push(String(e)))
page.on('console',m=>logs.push({type:m.type(),text:m.text()}))
const check=(v,m)=>{assert(v,m);checks.push(m);console.log('PASS presence',checks.length,m)}
const button=name=>page.getByRole('button',{name,exact:true})
const api=async(path,method='GET',data)=>{const r=await page.request.fetch(origin+'/api'+path,{method,headers:{Authorization:'Bearer '+fixture.token,'X-Review-Contract':'3','Idempotency-Key':crypto.randomUUID()},data});assert(r.ok(),`${path}: ${r.status()} ${await r.text()}`);return r.json()}
const sid=fixture.items.main.sid
const frame=(fi=0)=>api(`/review/sessions/${sid}/frames/${fi}`)
const session=()=>api(`/review/sessions/${sid}`)
const poll=async(fn,message)=>{for(let i=0;i<100;i++){if(await fn())return;await page.waitForTimeout(75)}throw new Error(message)}
const saved=()=>page.waitForFunction(()=>!document.querySelector('[data-testid=submit-frame]')?.textContent.includes('处理中'))
const closeRecovery=async()=>{if(await page.getByRole('dialog').count()){if(await button('继续编辑').count())await button('继续编辑').click();else await button('关闭').click()}}
async function stripState(fi,kind,returned=false){
  const cell=page.getByTestId('frame-strip').locator('i').nth(fi)
  const matches=()=>cell.evaluate((e,{kind,returned})=>e.classList.contains(kind)&&e.classList.contains('returned')===returned&&!(['submitted','draft','empty'].some(k=>k!==kind&&e.classList.contains(k))),{kind,returned})
  await poll(matches,`frame ${fi+1} strip state ${kind}, returned=${returned}`)
  const title=await cell.getAttribute('title'),label=kind==='submitted'?'已提交':kind==='draft'?'草稿未提交':'未审查'
  check(await matches()&&title?.includes(`第 ${fi+1} 帧 · ${label}`)&&title.includes('被退回重审')===returned,`frame strip ${fi+1} shows ${kind} and returned=${returned}, with matching accessible hover text`)
}
async function stripSeek(fi){
  await page.getByTestId('frame-strip').locator('i').nth(fi).click()
  await poll(async()=>await page.getByRole('textbox',{name:'跳转帧号'}).inputValue()===String(fi+1)&&await page.getByTestId('frame-strip').locator('i').nth(fi).evaluate(e=>e.classList.contains('current')),'frame strip seeks to clicked source frame')
  await saved();await closeRecovery()
}
async function go(fi){await page.getByRole('textbox',{name:'跳转帧号'}).fill(String(fi+1));await page.getByRole('textbox',{name:'跳转帧号'}).press('Enter');await poll(async()=>await page.getByRole('textbox',{name:'跳转帧号'}).inputValue()===String(fi+1),'frame navigation');await saved();await closeRecovery()}
async function deleteBox(id){await page.locator(`[data-row="${id}"]`).click();await page.getByTestId('review-delete-box').click();await saved()}
async function addBox(id='new',rect=[570,290,650,350],loseReserve=false,options={}){
  const opSid=options.sid||sid
  await page.getByTestId('review-add-box').click()
  const bounds=await page.locator('.review-drawing').boundingBox();assert(bounds)
  await page.mouse.move(bounds.x+rect[0]/800*bounds.width,bounds.y+rect[1]/450*bounds.height);await page.mouse.down()
  await page.mouse.move(bounds.x+rect[2]/800*bounds.width,bounds.y+rect[3]/450*bounds.height,{steps:4});await page.mouse.up()
  const dialog=page.getByTestId('review-add-object-dialog');await dialog.waitFor()
  await dialog.getByRole('combobox',{name:'关联对象'}).selectOption(String(id))
  const keys=[]
  if(loseReserve)await page.route(`**/api/review/sessions/${opSid}/objects`,async route=>{
    if(route.request().method()!=='POST'){await route.continue();return}
    keys.push(route.request().headers()['idempotency-key'])
    if(keys.length===1){await route.fetch();await route.abort('failed')}else await route.continue()
  })
  await page.getByTestId('review-confirm-add').click()
  if(loseReserve){
    await dialog.getByRole('alert').waitFor()
    const count=(await api(`/review/sessions/${opSid}/objects`)).items.length
    if(options.afterReserveLost)await options.afterReserveLost()
    await page.reload();await page.getByTestId('review-add-object-dialog').waitFor()
    await page.getByTestId('review-confirm-add').click()
    await page.getByTestId('review-add-object-dialog').waitFor({state:'hidden'});await saved()
    check(keys.length===2&&keys[0]===keys[1],'new identity response loss retries the original reservation key')
    check((await api(`/review/sessions/${opSid}/objects`)).items.length===count,'reservation retry does not allocate another identity')
    await page.unroute(`**/api/review/sessions/${opSid}/objects`)
  }else{await dialog.waitFor({state:'hidden'});await saved()}
}
const inView=loc=>loc.evaluate(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth})
try{
  await page.addInitScript(({token})=>{localStorage.setItem('rare-sperm-token',token);localStorage.setItem('rare-sperm-auth',JSON.stringify({id:'1',name:'confirm-A'}));localStorage.setItem('workbench-library-collapsed','false')},fixture)
  await page.goto(origin+'/review')
  await page.locator(`[data-withdraw-session="${fixture.items.pending.sid}"]`).click()
  check((await api('/review/sessions/'+fixture.items.pending.sid)).reviewerId===null,'list withdrawal opens without claiming the task')
  await page.getByTestId('review-confirm-task-action').click();await page.getByRole('dialog').waitFor({state:'hidden'})
  check((await api('/review/sessions/'+fixture.items.pending.sid)).state==='withdrawn','review-list withdrawal preserves the original unclaimed boundary')
  await openTask(page,`[data-session-id="${sid}"]`);await page.locator('.review-drawing').waitFor();await saved();await closeRecovery()
  const baseline=JSON.stringify((await frame()).baselineObjects)
  for(let fi=0;fi<4;fi++)await stripState(fi,'empty')
  const seekBefore={frames:await Promise.all([0,1,2,3].map(fi=>frame(fi))),progress:(await session()).progress}
  await stripSeek(2);await stripSeek(0)
  assert.deepEqual({frames:await Promise.all([0,1,2,3].map(fi=>frame(fi))),progress:(await session()).progress},seekBefore)
  check(true,'clicking the review frame strip navigates actual frames without editing or submitting any frame')
  await deleteBox(2)
  check((await frame()).patch.some(p=>p.objectId===2&&p.op==='delete'),'delete records a frame-only tombstone')
  await stripState(0,'draft');await stripState(1,'empty')
  for(let i=0;i<20;i++)await page.getByRole('button',{name:'放大画面',exact:true}).click()
  const scale=await page.locator('.shell-zoom').innerText()
  await page.locator('.review-viewport').evaluate(e=>{e.scrollLeft=0;e.scrollTop=0})
  await page.locator('[data-row="2"]').click()
  const deletedBounds=await page.locator('.deleted-review-box[data-box="2"]').boundingBox(),viewportBounds=await page.locator('.review-viewport').boundingBox()
  check(deletedBounds.x>=viewportBounds.x&&deletedBounds.x+deletedBounds.width<=viewportBounds.x+viewportBounds.width&&deletedBounds.y>=viewportBounds.y&&deletedBounds.y+deletedBounds.height<=viewportBounds.y+viewportBounds.height,'deleted object can be located from its list row in a zoomed viewport')
  check(await page.locator('.shell-zoom').innerText()===scale,'locating a deleted object preserves the user zoom')
  await page.screenshot({animations:'disabled',path:`${output}/deleted-object-location.png`})
  await page.getByTestId('review-restore-box').click();await saved()
  check((await frame()).patch.length===0,'restore deletion returns to the original with no confirmation item')
  await button('适应画面').click()
  await button('全部').last().click()
  await deleteBox(1);await button('↶ 撤销').click();await saved()
  check((await frame()).effectiveObjects.length===2,'undo restores a deleted frame object')
  check(await page.locator('[data-row="1"]').isVisible(),'undo keeps the restored selected object visible')
  const writeKeys=[]
  await page.route(`**/api/review/sessions/${sid}/frames/0/draft`,async route=>{writeKeys.push(route.request().headers()['idempotency-key']);if(writeKeys.length===1){await route.fetch();await route.abort('failed')}else await route.continue()})
  await deleteBox(1);await page.getByRole('alert').waitFor()
  check((await frame()).effectiveObjects.length===1,'lost delete response actually committed before showing recovery')
  await page.getByRole('alert').getByRole('button',{name:'重试',exact:true}).click();await saved()
  check(writeKeys.length===2&&writeKeys[0]===writeKeys[1],'delete draft retries the original idempotency key')
  await page.unroute(`**/api/review/sessions/${sid}/frames/0/draft`)
  await addBox('new',[570,290,650,350],true)
  check((await frame()).effectiveObjects.some(o=>o.objectId===3),'first review-added object receives stable identity 3')
  check(JSON.stringify((await frame()).baselineObjects)===baseline,'review additions and deletions leave original A unchanged')
  const beforeRefresh=await frame();await page.reload();await openTask(page,`[data-session-id="${sid}"]`);await page.locator('.review-drawing').waitFor();await saved();await closeRecovery()
  assert.deepEqual((await frame()).patch,beforeRefresh.patch);check(true,'addition and deletion drafts survive browser refresh')
  for(const size of[{width:1366,height:768},{width:1180,height:760},{width:760,height:900},{width:390,height:844}]){
    await page.setViewportSize(size)
    for(const dark of[false,true]){
      const toggle=button(dark?'切换深色主题':'切换浅色主题');if(await toggle.count())await toggle.click()
      await page.getByTestId('review-add-box').scrollIntoViewIfNeeded()
      check(await inView(page.getByTestId('review-add-box')),`review add operation reachable at ${size.width}, dark=${dark}`)
      await page.getByTestId('submit-frame').scrollIntoViewIfNeeded()
      check(await inView(page.getByTestId('submit-frame')),`frame submit reachable at ${size.width}, dark=${dark}`)
      check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`no page horizontal overflow at ${size.width}, dark=${dark}`)
      await page.screenshot({animations:'disabled',path:`${output}/review-${size.width}-${dark?'dark':'light'}.png`,fullPage:true})
    }
  }
  await page.setViewportSize({width:1366,height:768})
  await page.getByTestId('submit-frame').click();await poll(async()=>await page.getByRole('textbox',{name:'跳转帧号'}).inputValue()==='2','submit f0 advances');await saved()
  await stripState(0,'submitted');await stripState(1,'empty')
  // Editing an already submitted frame must remove green immediately; undoing
  // back to the committed B proposal restores its original submitted state.
  await go(0);await deleteBox(2);await stripState(0,'draft')
  await button('↶ 撤销').click();await saved();await stripState(0,'submitted');await go(1)
  await deleteBox(1);await button('全部').last().click();await deleteBox(2)
  check((await frame(1)).effectiveObjects.length===0,'deleting all objects makes an explicit reviewed empty frame')
  await page.getByTestId('submit-frame').click();await poll(async()=>await page.getByRole('textbox',{name:'跳转帧号'}).inputValue()==='3','submit f1 advances');await saved()
  await addBox(1,[100,100,160,160]);await addBox('new',[570,290,650,350])
  check((await frame(2)).effectiveObjects.map(o=>o.objectId).sort().join(',')==='1,4','empty original frame accepts an existing identity and a new identity')
  await page.getByTestId('submit-frame').click();await poll(async()=>await page.getByRole('textbox',{name:'跳转帧号'}).inputValue()==='4','submit f2 advances');await saved()
  await addBox(3,[570,290,650,350]);await deleteBox(1)
  await page.getByTestId('submit-frame').click();await page.getByRole('dialog').waitFor()
  check((await session()).state==='in_progress','all changed frames still require explicit review completion')
  await button('确认完成视频审查').click();await saved()
  const confirmations=(await api('/confirmation/sessions')).items
  const reviewSession=await session();const cs=confirmations.find(c=>c.baselineId===reviewSession.baselineId&&c.state!=='returned');assert(cs)
  await page.goto(origin+'/confirm');await openTask(page,`[data-session-id="${cs.id}"]`);await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  if(await button('领取并开始确认').count())await button('领取并开始确认').click()
  await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  await page.getByRole('checkbox',{name:'选择后自动跳到下一待确认项'}).uncheck()
  const changes=(await api(`/confirmation/sessions/${cs.id}/changes`)).items
  check(changes.length===8,'only eight final presence changes enter confirmation')
  const choices={'0:1':'A','0:3':'B','1:1':'B','1:2':'B','2:1':'A','2:4':'B','3:1':'A','3:3':'A'}
  for(const c of changes){
    await page.getByRole('button',{name:new RegExp(`修改项 \\d+，第 ${c.frameIndex+1} 帧，对象 ${c.objectId}，`)}).click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
    check(await page.getByTestId('presence-a').isVisible()&&await page.getByTestId('presence-b').isVisible(),`confirmation shows truthful absence state for frame ${c.frameIndex+1} object ${c.objectId}`)
    if(c.frameIndex===0)for(const size of[{width:1366,height:768},{width:1180,height:760}]){
      await page.setViewportSize(size)
      for(const dark of[false,true]){
        const toggle=button(dark?'切换深色主题':'切换浅色主题');if(await toggle.count())await toggle.click()
        check(await inView(page.getByTestId('choose-a'))&&await inView(page.getByTestId('choose-b')),`presence ${c.changeKind} choices visible at ${size.width}, dark=${dark}`)
        await page.screenshot({animations:'disabled',path:`${output}/confirmation-${c.changeKind}-${size.width}-${dark?'dark':'light'}.png`,fullPage:true})
      }
    }
    await page.getByTestId('choose-'+choices[`${c.frameIndex}:${c.objectId}`].toLowerCase()).click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  }
  if(!await page.getByRole('dialog').count())await page.getByTestId('complete-video').click()
  await button('确认完成').click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  await page.getByTestId('export-training').click();await page.getByRole('heading',{name:'导出已确认训练数据集'}).waitFor()
  const [download]=await Promise.all([page.waitForEvent('download'),button('生成并下载 ZIP').click()]);const zip=`${output}/presence-yolo.zip`;await download.saveAs(zip)
  const inspected=JSON.parse(execFileSync(process.env.TEST_PYTHON||'work/review-venv/bin/python',['-c',`
import zipfile,json,cv2,numpy as np,sys
with zipfile.ZipFile(sys.argv[1]) as z:
 m=json.loads(z.read('manifest.json')); samples={s['frameIndex']:s for s in m['samples']}
 assert len(samples)==4
 counts={i:len(z.read(s['label']).decode().splitlines()) for i,s in samples.items()}
 assert counts=={0:3,1:0,2:1,3:1},counts
 for s in samples.values(): assert cv2.imdecode(np.frombuffer(z.read(s['image']),np.uint8),cv2.IMREAD_COLOR).shape[:2]==(450,800)
 assert z.read(samples[1]['label'])==b''
 print(json.dumps({'counts':counts,'frames':len(samples)}))
`,zip],{encoding:'utf8'}))
  check(inspected.frames===4,'real YOLO ZIP honors accepted/rejected additions/deletions and preserves empty-frame image')
  const firstFinal=(await api(`/confirmation/sessions/${cs.id}`)).finalVersionId,firstFinalData=await api('/final-versions/'+firstFinal)
  const firstExport=(await page.locator('.training-export-id').innerText()).split('：')[1]
  await button('关闭').click()
  // Re-review the whole frame, including its add/delete pair. Decisions from
  // this frame must be reconsidered; only other frames inherit old decisions.
  await button('重新确认').click();await button('开始重新确认').click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  await page.getByRole('button',{name:/修改项 \d+，第 1 帧，对象 3，/}).click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  await page.getByTestId('return-frame-review').click();await page.getByRole('textbox',{name:'退回原因'}).fill('请重新检查本帧补标和删除建议。');await button('确认退回本帧').click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  check((await api(`/confirmation/sessions/${cs.id}`)).state==='returned','presence confirmation can return its entire frame for re-review')
  check(await page.getByTestId('export-training').count()===0,'training export remains unavailable during presence re-review')
  const staleDownload=await page.request.get(`${origin}/api/datasets/exports/${firstExport}/download`,{headers:{Authorization:'Bearer '+fixture.token}})
  check(staleDownload.status()===409,'previous training ZIP is blocked while presence frame is re-reviewed')
  assert.deepEqual(await api('/final-versions/'+firstFinal),firstFinalData);check(true,'re-review keeps the historical final version immutable')
  await page.goto(origin+'/review');await openTask(page,`[data-session-id="${sid}"]`);await page.locator('.review-drawing').waitFor();await saved();await closeRecovery()
  const returnedFrame=await frame(0)
  check(returnedFrame.patch.some(p=>p.op==='add'&&p.objectId===3)&&returnedFrame.patch.some(p=>p.op==='delete'&&p.objectId===1),'B re-review restores the full add/delete proposal for the returned frame')
  await stripState(0,'empty',true);for(let fi=1;fi<4;fi++)await stripState(fi,'submitted')
  await page.getByTestId('submit-frame').click();await page.getByRole('dialog').waitFor()
  await stripState(0,'submitted',true)
  await button('确认完成视频审查').click();await saved();await stripState(0,'submitted')
  const next=(await api('/confirmation/sessions')).items.find(c=>c.baselineId===reviewSession.baselineId&&c.id!==cs.id&&c.state!=='returned');assert(next)
  check(next.progress.decided===6&&next.progress.pending===2,'next confirmation inherits six other-frame choices and reopens both returned-frame changes')
  const nextChanges=(await api(`/confirmation/sessions/${next.id}/changes`)).items
  check(nextChanges.filter(c=>c.frameIndex===0).every(c=>!c.decision),'neither added nor deleted change inherits its old returned-frame decision')
  await page.goto(origin+'/confirm');await openTask(page,`[data-session-id="${cs.id}"]`);await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  if(await button('留在上次位置').count())await button('留在上次位置').click()
  await button('继续新一轮确认').click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  if(await button('留在上次位置').count())await button('留在上次位置').click()
  check(await page.locator(`[data-session-id="${next.id}"]`).evaluate(e=>e.classList.contains('selected')),'old returned confirmation links to the new presence round')
  if(await button('领取并开始确认').count())await button('领取并开始确认').click()
  await page.getByRole('checkbox',{name:'选择后自动跳到下一待确认项'}).check()
  await page.getByTestId('choose-a').click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  await page.getByTestId('choose-b').click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  check((await api(`/confirmation/sessions/${next.id}`)).state==='in_progress','reselected presence changes still require explicit video completion')
  if(!await page.getByRole('dialog').count())await page.getByTestId('complete-video').click()
  await button('确认完成').click();await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  const nextFinal=await api(`/confirmation/sessions/${next.id}`)
  check(nextFinal.state==='confirmed'&&nextFinal.finalVersionId!==firstFinal,'explicit completion freezes a new final version after presence re-review')
  assert.deepEqual(await api('/final-versions/'+firstFinal),firstFinalData);check(true,'old final data remains unchanged after the new final is generated')
  // Two independent clients must never turn a recovered old intent into a new
  // write against the latest frame revision.
  const race=fixture.items['race-draft'],freshBox=[550,270,665,365],draftKeys=[],draftBodies=[]
  await page.goto(origin+'/review');await openTask(page,`[data-session-id="${race.sid}"]`);await page.locator('.review-drawing').waitFor();await saved();await closeRecovery()
  await page.route(`**/api/review/sessions/${race.sid}/frames/0/draft`,async route=>{draftKeys.push(route.request().headers()['idempotency-key']);draftBodies.push(route.request().postDataJSON());if(draftKeys.length===1){await route.fetch();await route.abort('failed')}else await route.continue()})
  await addBox('new',[570,290,650,350],false,{sid:race.sid});await page.getByRole('alert').waitFor()
  const committed=await api(`/review/sessions/${race.sid}/frames/0`),added=committed.effectiveObjects.find(o=>o.objectId===2);assert(added)
  await api(`/review/sessions/${race.sid}/frames/0/draft`,'PUT',{expectedFrameRevision:committed.frameRevision,patch:[{objectId:2,op:'add',classKey:added.classKey,bbox:freshBox}]})
  const newest=await api(`/review/sessions/${race.sid}/frames/0`)
  await page.reload();await page.getByRole('alert').filter({hasText:'上次保存结果尚未确认'}).waitFor()
  await page.getByRole('alert').getByRole('button',{name:'重试',exact:true}).click();await saved()
  check(draftKeys.length===2&&draftKeys[0]===draftKeys[1]&&JSON.stringify(draftBodies[0])===JSON.stringify(draftBodies[1]),'refresh after committed addition replays the original draft key and body')
  assert.deepEqual((await api(`/review/sessions/${race.sid}/frames/0`)).effectiveObjects.find(o=>o.objectId===2).bbox,freshBox)
  check(Number(await page.locator('[data-box="2"] rect').getAttribute('x'))===freshBox[0],'old draft receipt refreshes the page to the other client latest geometry')
  check((await api(`/review/sessions/${race.sid}/frames/0`)).frameRevision===newest.frameRevision,'old draft replay does not increment or overwrite the newer frame')
  await page.unroute(`**/api/review/sessions/${race.sid}/frames/0/draft`)
  const reserveRace=fixture.items['race-reserve']
  await openTask(page,`[data-session-id="${reserveRace.sid}"]`);await page.locator('.review-drawing').waitFor();await saved();await closeRecovery()
  await addBox('new',[570,290,650,350],true,{sid:reserveRace.sid,afterReserveLost:async()=>{
    const f=await api(`/review/sessions/${reserveRace.sid}/frames/0`),objects=(await api(`/review/sessions/${reserveRace.sid}/objects`)).items,object=objects.find(o=>o.origin==='review');assert(object)
    await api(`/review/sessions/${reserveRace.sid}/frames/0/draft`,'PUT',{expectedFrameRevision:f.frameRevision,patch:[{objectId:object.objectId,op:'add',classKey:object.classKey,bbox:freshBox}]})
  }})
  await page.getByRole('alert').waitFor()
  check((await page.getByRole('alert').innerText()).includes('其他窗口'),'reserve recovery retains the original frame revision and reports a real concurrent edit')
  assert.deepEqual((await api(`/review/sessions/${reserveRace.sid}/frames/0`)).effectiveObjects.find(o=>o.objectId===2).bbox,freshBox);check(true,'reserve recovery cannot overwrite another client later use of the same reserved identity')
  await button('处理冲突 / 重新读取').click();await button('放弃本地编辑并重新读取').click();await saved();await closeRecovery()
  check(Number(await page.locator('[data-box="2"] rect').getAttribute('x'))===freshBox[0],'explicit conflict reload displays the preserved current server geometry')
  // A separate source verifies return without disturbing the frozen main history.
  const returned=fixture.items.return
  const beforeWorkspace=await api('/track/workspace/'+returned.mid)
  await page.goto(origin+'/review');await openTask(page,`[data-session-id="${returned.sid}"]`);await page.locator('.review-drawing').waitFor();await saved();await closeRecovery()
  await page.locator('.review-task-actions summary').click();await page.getByTestId('review-return-annotation').click()
  await page.getByRole('dialog').getByRole('textbox').fill('整段需要重新标注，保留历史。')
  const returnKeys=[]
  await page.route(`**/api/review/sessions/${returned.sid}/return-annotation`,async route=>{returnKeys.push(route.request().headers()['idempotency-key']);if(returnKeys.length===1){await route.fetch();await route.abort('failed')}else await route.continue()})
  await page.getByTestId('review-confirm-task-action').click();await page.getByRole('dialog').getByRole('alert').waitFor()
  await page.reload();await page.getByRole('dialog',{name:'退回重新标注'}).waitFor()
  await page.getByTestId('review-confirm-task-action').click();await page.getByRole('dialog').waitFor({state:'hidden'})
  check(returnKeys.length===2&&returnKeys[0]===returnKeys[1],'return response loss replays one original operation')
  check((await api('/review/sessions/'+returned.sid)).state==='returned_to_annotation','old returned review becomes read-only')
  assert.deepEqual((await api('/track/workspace/'+returned.mid)).manualAnnotations,beforeWorkspace.manualAnnotations);check(true,'return retains original annotation workspace')
  await page.goto(origin+'/annotate');await openAsset(page,page.locator('.asset-card').filter({hasText:returned.name}));await page.getByTestId('annotation-return-status').waitFor()
  check((await page.getByTestId('annotation-return-status').innerText()).includes('整段需要重新标注'),'annotator sees the return reason beside resubmission')
  const completion=await api(`/review/media/${returned.mid}/completion-preview`);await api(`/review/media/${returned.mid}/complete`,'POST',{expectedSourceRevision:completion.sourceRevision,confirmComplete:true,concentratedReview:true,explicitEmptyFrameRanges:[]})
  const status=await api(`/review/media/${returned.mid}/submission-status`)
  check(status.items.length===1&&status.items[0].id!==returned.sid,'resubmission creates a new active review and keeps old history')
  check(status.history.some(s=>s.id===returned.sid&&s.returnAnnotation?.reason.includes('整段')),'return reason and old immutable task remain queryable')
  // Whole-video return may follow C's frame return. The historical C must
  // explain the new terminal state rather than wait forever for the old B.
  let handoff=await api(`/confirmation/sessions/${next.id}`)
  await api(`/confirmation/sessions/${next.id}/reopen`,'POST',{expectedSessionRevision:handoff.revision})
  handoff=await api(`/confirmation/sessions/${next.id}`)
  const handoffChanges=(await api(`/confirmation/sessions/${next.id}/changes`)).items
  await api(`/confirmation/sessions/${next.id}/return`,'POST',{expectedSessionRevision:handoff.revision,changeId:handoffChanges[0].changeId,reason:'请先重审本帧'})
  const reviewForReturn=await session()
  await api(`/review/sessions/${sid}/return-annotation`,'POST',{expectedSessionRevision:reviewForReturn.revision,reason:'本帧重审后决定整段重新标注'})
  await page.goto(origin+'/confirm');await openTask(page,`[data-session-id="${next.id}"]`);await page.locator('.confirmation-page[data-busy="false"]').waitFor()
  if(await button('留在上次位置').count())await button('留在上次位置').click()
  check((await page.locator('.confirmation-page').innerText()).includes('本帧重审后决定整段重新标注'),'historical C explains whole-video return after frame re-review')
  check(await page.getByTestId('choose-b').isDisabled()&&await page.getByTestId('export-training').count()===0,'whole-video return leaves historical confirmation read-only and blocks export')
  check(errors.length===0,'new review workflows produce no uncaught browser errors')
  console.log(`Completed ${checks.length} review-presence checks.`)
}finally{
  fs.writeFileSync(`${output}/results.json`,JSON.stringify({checks,errors,logs},null,2))
  await page.screenshot({animations:'disabled',path:`${output}/last.png`,fullPage:true}).catch(()=>{})
  await browser.close()
}
