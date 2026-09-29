import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'
import assert from 'node:assert/strict'
import fs from 'node:fs'
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright')
const fixture=JSON.parse(fs.readFileSync('work/confirmation-browser-fixture.json'))
const origin=process.env.CONFIRMATION_ORIGIN||'http://127.0.0.1:5373',sid=fixture.failure.sid
const browser=await chromium.launch({channel:'chrome',headless:true}),page=await browser.newPage({viewport:{width:1440,height:900}})
if (process.env.LEGACY_BROWSER === '1') await useLegacyBrowserAPIs(page)
const errors=[],logs=[];let checks=0
page.on('pageerror',e=>errors.push(String(e)));page.on('console',m=>logs.push({type:m.type(),text:m.text()}))
const check=(v,msg)=>{assert(v,msg);console.log('PASS',++checks,msg)}
const idle=()=>page.waitForSelector('.confirmation-page[data-busy="false"]')
const button=name=>page.getByRole('button',{name,exact:true})
const api=async(path,method='GET',body)=>{
 const r=await page.request.fetch(origin+'/api'+path,{method,headers:{Authorization:`Bearer ${fixture.token}`,'X-Review-Contract':'2','Idempotency-Key':crypto.randomUUID()},data:body});assert(r.ok(),await r.text());return r.json()
}
const session=()=>api('/confirmation/sessions/'+sid),changes=async()=> (await api('/confirmation/sessions/'+sid+'/changes')).items
const choose=async(c)=>{await page.getByTestId('choose-'+c.toLowerCase()).click();await idle()}
const go=async(n)=>{await page.getByRole('textbox',{name:'修改项序号'}).fill(String(n));await button('前往').click();await idle()}
const decisionPattern='**/api/confirmation/sessions/*/changes/*/decision'
try{
 await page.addInitScript(({token})=>{localStorage.setItem('rare-sperm-token',token);localStorage.setItem('rare-sperm-auth',JSON.stringify({id:'3',name:'confirm-C',role:'annotator'}))},fixture)
 await page.goto(origin+'/confirm');await idle();await page.locator(`[data-session-id="${sid}"]`).click();await idle();await button('领取并开始确认').click();await idle()
 // A failed image must not allow choosing against a stale previous frame.
 await page.route('**/api/track/frame/*/1',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'Injected image failure'})}))
 await go(3);await page.getByRole('alert').waitFor()
 check(await page.getByTestId('choose-a').isDisabled()&&(await session()).progress.decided===0,'failed frame disables choice and retains progress')
 check(await page.locator('.c-crops svg').count()===0,'stale previous image is removed')
 await page.unroute('**/api/track/frame/*/1');await button('重试加载').click();await idle()
 check(await page.getByTestId('choose-a').isEnabled(),'frame retry loads actual target and re-enables choice')
 await go(1)
 // Server failed before commit: cannot optimistically advance or leave.
 let keys=[]
 await page.route(decisionPattern,route=>{keys.push(route.request().headers()['idempotency-key']);return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'Injected storage unavailable',code:'STORAGE_UNAVAILABLE',requestId:'test-injected-503'})})})
 await choose('B');await page.getByRole('alert').waitFor()
 check((await session()).progress.decided===0,'failed decision does not mark item saved')
 check((await page.getByTestId('current-item').innerText()).includes('修改项 1 / 3'),'failed decision never auto-advances')
 check((await page.getByRole('alert').innerText()).includes('test-injected-503'),'error exposes request ID for log investigation')
 await button('审查模式').click();await idle()
 check(new URL(page.url()).pathname==='/confirm','route leave guard blocks unresolved failed decision')
 check(keys.length===2&&keys[0]===keys[1],'leave retry preserves original idempotency key')
 await page.reload();await idle();check((await page.getByRole('alert').innerText()).includes('保存结果尚未确认'),'refresh restores unresolved request journal')
 await page.unroute(decisionPattern);await button('重试原请求').click();await idle()
 check((await session()).progress.decided===1,'retry commits restored choice')
 check((await page.getByTestId('current-item').innerText()).includes('修改项 2 / 3'),'successful retry may auto-advance')
 // Server committed but response vanished; replay must not generate a second event.
 let lost=true;keys=[]
 await page.route(decisionPattern,async route=>{
  keys.push(route.request().headers()['idempotency-key'])
  if(lost){lost=false;await route.fetch();await route.abort('failed')}else await route.continue()
 })
 await choose('A');await page.getByRole('alert').waitFor()
 const before=(await changes())[1]
 check(before.decision.choice==='A','lost response may still have a committed server choice')
 check((await page.getByTestId('current-item').innerText()).includes('修改项 2 / 3'),'uncertain response stays on affected item')
 await button('重试原请求').click();await idle()
 const after=(await changes())[1]
 check(keys[0]===keys[1]&&before.decision.eventId===after.decision.eventId&&before.decisionRevision===after.decisionRevision,'lost-response replay returns identical event and revision')
 await page.unroute(decisionPattern)
 // Cursor write failure also preserves an intent; decision state must stay independent.
 await page.route('**/api/confirmation/sessions/*/cursor',route=>route.fulfill({status:503,contentType:'application/json',body:'{"message":"Injected cursor failure"}'}))
 await go(1);await page.getByRole('alert').waitFor()
 check((await session()).progress.decided===2,'bookmark failure does not alter decisions')
 check(await page.getByTestId('choose-a').isDisabled(),'bookmark recovery is serialized before next edit')
 await page.unroute('**/api/confirmation/sessions/*/cursor');await button('重试原请求').click();await idle()
 check((await session()).resume.lastViewedChangeId===(await changes())[0].changeId,'bookmark retry stores viewed location')
 // Same account in another tab changed this item: stale CAS must not overwrite it.
 const c=(await changes())[0]
 await api(`/confirmation/sessions/${sid}/changes/${c.changeId}/decision`,'PUT',{choice:'A',expectedDecisionRevision:c.decisionRevision})
 await choose('A');await page.getByRole('alert').waitFor()
 check((await page.getByRole('alert').innerText()).includes('其他窗口'),'concurrent choice produces explicit version conflict')
 check((await changes())[0].decision.choice==='A','conflict leaves remote result intact')
 await button('读取服务器结果').click();await button('采用服务器结果').click();await idle()
 check(await page.getByRole('alert').count()===0,'explicit resolution reloads authoritative choices')
 // Conflicting bookmarks rebase without changing any choice.
 const latest=await session(),last=(await changes())[2]
 await api(`/confirmation/sessions/${sid}/cursor`,'PUT',{changeId:last.changeId,expectedCursorRevision:latest.resume.cursorRevision})
 await go(2)
 check(await page.getByRole('alert').count()===0&&(await session()).progress.decided===2,'bookmark CAS rebases independently from decision history')
 // Failing undo cannot change visible or persisted decision.
 let undoKeys=[]
 await page.route('**/api/confirmation/sessions/*/undo',route=>{undoKeys.push(route.request().headers()['idempotency-key']);return route.fulfill({status:503,contentType:'application/json',body:'{"message":"Injected undo failure"}'})})
 await page.getByRole('button',{name:/撤销上一次选择/}).click();await idle()
 check((await changes())[0].decision.choice==='A','failed undo preserves prior choice')
 await page.unroute('**/api/confirmation/sessions/*/undo');await button('重试原请求').click();await idle()
 check((await changes())[0].decision.choice==='B','retry undo restores correct preceding choice')
 await go(3);await choose('B');await button('继续检查').click()
 // Finalization failure must stay in progress until original request succeeds.
 await page.route('**/api/confirmation/sessions/*/finalize',route=>route.fulfill({status:503,contentType:'application/json',body:'{"message":"Injected finalization failure"}'}))
 await page.getByTestId('complete-video').click();await button('确认完成').click();await idle()
 check((await session()).state==='in_progress'&&!(await session()).finalVersionId,'failed finish never exposes a final version')
 await page.unroute('**/api/confirmation/sessions/*/finalize');await button('重试原请求').click();await idle()
 const oldVersion=(await session()).finalVersionId;check(!!oldVersion,'retry finish generates final version')
 await page.route('**/api/confirmation/sessions/*/reopen',route=>route.fulfill({status:503,contentType:'application/json',body:'{"message":"Injected reopen failure"}'}))
 await button('重新确认').click();await button('开始重新确认').click();await idle()
 check((await session()).state==='confirmed','failed reopen keeps completed status')
 await page.unroute('**/api/confirmation/sessions/*/reopen');await button('重试原请求').click();await idle()
 check((await session()).state==='in_progress'&&(await session()).finalVersionId===oldVersion,'reopen retry keeps historical final version')
 // Reauthentication keeps the same pending intent instead of trapping the user behind the guard.
 await page.route(decisionPattern,route=>route.fulfill({status:401,contentType:'application/json',body:'{"message":"Injected expired session"}'}))
 await go(1);await choose('A');await button('保留操作并重新登录').click()
 await page.getByPlaceholder('请输入账号').fill('confirm-C');await page.getByPlaceholder('请输入密码').fill('confirmation-test');await button('登录').click()
 await button('对比确认').click();await idle();await page.getByRole('alert').waitFor()
 check(await page.getByRole('alert').count()===1,'reauthentication restores pending operation for the same account')
 await page.unroute(decisionPattern);await button('重试原请求').click();await idle()
 check((await changes())[0].decision.choice==='A','reauthenticated retry saves intended choice')
 check(errors.length===0,'all failure paths avoid uncaught browser exceptions')
 console.log(`SUCCESS ${checks} checks`)
}finally{
 fs.writeFileSync('output/playwright/confirmation-failure-console.json',JSON.stringify({errors,logs},null,2));await page.screenshot({path:'output/playwright/confirmation-failure-last.png',fullPage:true});await browser.close()
}
