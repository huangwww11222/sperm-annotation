import {legacySubmit} from './legacy-tracking-probe.mjs'
// Works against production bundles too. No store imports or prepared annotation state.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
// The task list is a drawer that stays closed while a task is open.
const openTask=async(target,selector)=>{const card=target.locator(selector),toggle=target.getByTestId('toggle-workbench-library');for(let i=0;i<40;i++){if(await card.isVisible()){try{await card.click({timeout:1500});return}catch{}}else if(await toggle.isVisible())await toggle.click();await target.waitForTimeout(250)}await card.click()}
const origin = process.env.CRITICAL_ORIGIN || 'http://127.0.0.1:5373'
assert(['localhost', '127.0.0.1'].includes(new URL(origin).hostname), 'Local isolated server required')
const video = path.resolve(process.env.CRITICAL_VIDEO || 'work/critical-upload.avi')
assert(video.startsWith(path.resolve('work')+path.sep), 'Disposable input required')
const modelMode = process.env.CRITICAL_AI_MODE || 'disabled'
const browser = await chromium.launch({ channel: process.env.CHROME_CHANNEL || 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1366, height: 768 }, acceptDownloads: true })
page.setDefaultTimeout(15000)
if (process.env.LEGACY_BROWSER === '1') await useLegacyBrowserAPIs(page)
const checks = [], errors = [], requests = []
const check = (value, message) => { assert(value, message); checks.push(message); console.log('PASS', checks.length, message) }
page.on('pageerror', e => errors.push(String(e)))
page.on('response', r => { if (r.status() >= 400) requests.push({ method:r.request().method(), path:new URL(r.url()).pathname, status:r.status() }) })
page.on('dialog', d => d.accept())
const button = name => page.getByRole('button', { name, exact:true })
let token, mid, sid
const api = async (url, method='GET', body) => {
  const response = await page.request.fetch(origin+'/api'+url, { method, data:body, headers:{ Authorization:'Bearer '+token, 'X-Review-Contract':'2', 'Idempotency-Key':crypto.randomUUID() } })
  assert(response.ok(), `${method} ${url}: ${response.status()} ${await response.text()}`)
  return response.json()
}
const poll = async (fn, description) => {
  for (let attempt=0; attempt<100; attempt++) { if (await fn()) return; await page.waitForTimeout(100) }
  throw new Error('Timed out: '+description)
}
const workspace = () => api('/track/workspace/'+mid)
const ready = async () => {
  await poll(async () => await page.getByTestId('annotation-hit').isVisible() && !(await page.getByTestId('annotation-hit').getAttribute('class')).includes('locked'), 'annotation canvas is editable')
  assert.equal(await page.getByTestId('workspace-save-error').count(), 0, 'Unexpected workspace error')
}
const draw = async (expectedCount = 1) => {
  // Move focus off any control (blank rail area) so B reaches the canvas shortcut.
  await ready(); await page.locator('.shell-rail').click({position:{x:22,y:300}}); await page.keyboard.press('b')
  const box = await page.getByTestId('annotation-hit').boundingBox(); assert(box)
  // Draw new objects away from existing boxes and their resize handles.
  const [x,y]=expectedCount===2?[.65,.25]:expectedCount===3?[.15,.6]:[.35,.35]
  await page.mouse.move(box.x+box.width*x, box.y+box.height*y); await page.mouse.down()
  await page.mouse.move(box.x+box.width*(x+.1), box.y+box.height*(y+.11), {steps:6}); await page.mouse.up()
  await poll(async () => (await workspace()).manualAnnotations?.length === expectedCount, 'manual box durably saved')
}
// Import lives in the media drawer, which stays closed while a video is open.
const openDrawer = async () => { if(!await page.locator('.shell-drawer').isVisible()) await page.getByTestId('toggle-workbench-library').click() }
const upload = async () => {
  await ready();await openDrawer();await poll(async()=>await button('导入视频').isEnabled(),'import control is ready')
  const response = page.waitForResponse(r => r.url().endsWith('/api/track/upload') && r.request().method()==='POST')
  await page.locator('input[type=file][accept="video/*"]').setInputFiles(video)
  const r = await response; assert.equal(r.status(), 201, await r.text()); mid = (await r.json()).mediaId
  await ready()
}
const reload = async () => { await page.reload(); await ready() }
const jump = async n => {
  const input = page.getByRole('spinbutton', {name:'跳转帧号'})
  await input.fill(String(n)); await input.press('Enter'); await ready()
  assert.equal(await input.inputValue(), String(n))
}
try {
  const username = 'critical-'+crypto.randomUUID(), password = crypto.randomUUID()
  const registered = await page.request.post(origin+'/api/auth/register', {data:{username,password}})
  assert.equal(registered.status(),201); token=(await registered.json()).token
  await page.goto(origin+'/login')
  await page.getByPlaceholder('请输入账号').fill(username); await page.getByPlaceholder('请输入密码').fill(password)
  await button('登录').click(); await page.getByTestId('annotation-page').waitFor(); await ready()
  await openDrawer()
  check(await button('导入视频').isEnabled(), 'real login with a local sample permits the first upload')
  await upload()
  check((await api('/track/result/'+mid)).state==='not_generated', 'fresh upload has explicit untracked state and editable canvas')
  const entry=(await api('/track/media')).items.find(item=>item.mediaId===mid)
  check(entry.trackingResultState==='not_generated' && entry.hasTrackingResult===false, 'media list identifies a healthy untracked video explicitly')
  await draw(); const manual=(await workspace()).manualAnnotations
  await reload()
  check(JSON.stringify((await workspace()).manualAnnotations)===JSON.stringify(manual), 'manual-only work survives refresh before first AI run')

  // Pair every blocking fault with a successful retry and another real edit.
  for (const fault of ['server', 'offline', 'invalid-json', 'missing-media', 'lost-results']) {
    const matcher='**/api/track/result/'+mid+'*'
    await page.route(matcher, route => fault==='offline' ? route.abort('failed') : route.fulfill(fault==='invalid-json' ?
      {status:200,contentType:'application/json',body:'{broken'} :
      {status:fault==='missing-media'?404:fault==='lost-results'?409:500,json:{detail:fault==='lost-results'?{code:'TRACKING_RESULTS_MISSING',message:'已生成的追踪结果文件缺失'}:'Injected '+fault}}))
    await page.reload(); await page.getByTestId('workspace-save-error').waitFor()
    check((await page.getByTestId('annotation-hit').getAttribute('class')).includes('locked'), fault+': read failure blocks editing')
    check(JSON.stringify((await workspace()).manualAnnotations)===JSON.stringify(manual), fault+': read failure preserves saved annotations')
    await page.unroute(matcher); await button('重新读取工作区').click(); await ready()
    check(await page.locator('.object-select').count()===1, fault+': explicit retry restores existing manual box')
  }

  // Lose only the HTTP reply after a real commit, then refresh and continue editing.
  const saveMatcher='**/api/track/workspace/'+mid
  const attempts=[]
  let loseReply=true
  await page.route(saveMatcher,async route=>{
    if(route.request().method()!=='PUT')return route.continue()
    const attempt={key:route.request().headers()['idempotency-key'],body:route.request().postDataJSON()}
    if(loseReply && attempt.body.manualAnnotations?.length!==2)return route.continue()
    const response=await route.fetch();assert(response.ok(),await response.text())
    attempt.receipt=await response.json();attempts.push(attempt)
    if(loseReply){loseReply=false;await route.abort('failed')}else await route.fulfill({response})
  })
  await draw(2);await page.getByTestId('workspace-save-error').waitFor()
  check((await workspace()).manualAnnotations.length===2, 'lost save reply follows a real server commit and leaves a visible pending operation')
  const original=attempts[0]
  await reload()
  const replay=attempts.slice(1).find(item=>item.key===original.key)
  check(!!replay && JSON.stringify(replay.body)===JSON.stringify(original.body) && JSON.stringify(replay.receipt)===JSON.stringify(original.receipt), 'refresh replays the original workspace key/body and identical committed receipt')
  check((await workspace()).manualAnnotations.length===2 && await page.locator('.object-select').count()===2, 'refresh recovers the committed boxes without duplicates')
  await page.unroute(saveMatcher);await draw(3);await reload()
  check((await workspace()).manualAnnotations.length===3, 'a new real edit saves and reloads after lost-response recovery')

  await openDrawer()
  const asset=()=>page.locator('.asset-card').filter({hasText:path.basename(video)})
  await asset().getByRole('button',{name:/删除/}).click()
  await poll(async () => !(await api('/track/media')).items.some(x=>x.mediaId===mid), 'server deletion')
  await upload(); await draw(); await reload()
  check((await workspace()).manualAnnotations.length===1, 'delete and reupload can draw, save and reload')
  await page.locator('input[type=file][accept="video/*"]').setInputFiles(video)
  await button('取消导入').click(); await ready()
  check((await workspace()).manualAnnotations.length===1, 'cancel duplicate import preserves annotation')
  await page.locator('input[type=file][accept="video/*"]').setInputFiles(video)
  await button('覆盖并重新标注').click(); await page.locator('[data-reimport-dialog]').waitFor({state:'hidden'}); await ready(); await reload()
  check((await workspace()).manualAnnotations.length===0 && (await api('/track/result/'+mid)).state==='not_generated', 'overwrite and refresh return to editable untracked state')
  await draw()

  const start = page.waitForResponse(r => new URL(r.url()).pathname==='/api/track' || new URL(r.url()).pathname==='/api/track/rewind')
  await button('首尾双向追踪').click(); await start
  if (modelMode==='simulated') {
    await poll(async () => (await api('/track/result/'+mid)).frames.length===3, 'real tracking job publishes simulated model output')
    await ready(); await reload()
    check((await api('/track/result/'+mid+'?required=true')).state==='available', 'first tracking uses real start/status/publication/read pipeline with simulated model')
    const raw=await page.request.get(origin+'/api/track/result-file/'+mid+'?required=true',{headers:{Authorization:'Bearer '+token}})
    check(raw.ok() && (await raw.text()).trim().split('\n').map(JSON.parse).length===3, 'raw download reads the same three published tracking frames')
  } else {
    await page.getByTestId('tracking-error').waitFor(); await ready()
    check((await page.getByTestId('tracking-error').innerText()).includes('未启用'), 'CPU deployment reports disabled AI and keeps manual annotation usable')
    for (const n of [2,3]) {await jump(n);await button('复制上一帧 C').click();await poll(async () => (await workspace()).manualAnnotations.length===n,'copied frame saved')}
  }
  check(await button('AI Tracking').count()===0, 'retired tracking button is absent')
  let lostSnapshot=true;const snapshotRequests=[]
  await page.route('**/api/datasets/annotations/*/snapshot',async route=>{
    snapshotRequests.push({key:route.request().headers()['idempotency-key'],body:route.request().postDataJSON()})
    const response=await route.fetch()
    if(lostSnapshot){lostSnapshot=false;await route.abort('failed')}else await route.fulfill({response})
  })
  await button('导出训练数据集').click()
  await page.getByRole('alert').filter({hasText:/导出|fetch|请求|连接|Failed/i}).waitFor()
  await page.reload();await page.getByTestId('annotation-hit').waitFor()
  await button('恢复数据集导出').click()
  await page.getByRole('dialog',{name:'导出训练数据集'}).waitFor()
  check(snapshotRequests.length===2&&snapshotRequests[0].key===snapshotRequests[1].key&&JSON.stringify(snapshotRequests[0].body)===JSON.stringify(snapshotRequests[1].body),'snapshot response loss refresh replays original key and body')
  await page.unroute('**/api/datasets/annotations/*/snapshot')
  await page.getByRole('dialog',{name:'导出训练数据集'}).waitFor()
  await page.screenshot({path:'output/playwright/direct-annotation-export-settings.png'})
  const directDownload=page.waitForEvent('download')
  await button('生成并下载 ZIP').click()
  const directFile=await directDownload
  await directFile.saveAs('output/playwright/direct-annotation-dataset.zip')
  check(true,'annotation exports a real training ZIP without creating a review task')
  await button('关闭').click()
  await page.reload();await ready();await button('导出训练数据集').click()
  await page.getByRole('dialog',{name:'导出训练数据集'}).waitFor()
  check(await page.getByRole('dialog',{name:'导出需要处理'}).count()===0,'complete annotations open export settings without an extra confirmation dialog')
  await button('下载 ZIP').waitFor();await button('关闭').click()
  sid=(await legacySubmit(page,token,mid)).session.id
  check((await api('/review/sessions/'+sid)).progress.submittedFrames===0, 'submission does not pre-submit B frames')
  if(modelMode==='simulated')await page.route('**/api/review/sessions/*/evidence',route=>route.fulfill({status:503,json:{message:'injected evidence read failure'}}))
  await page.goto(origin+'/review'); await openTask(page,`[data-session-id="${sid}"]`); await page.locator('.review-drawing').waitFor()
  if(await button('继续编辑').count()) await button('继续编辑').click()
  if(modelMode==='simulated') {
    await page.getByRole('alert').filter({hasText:'疑点读取失败'}).waitFor()
    check(await button('完成视频审查').isDisabled(),'evidence load failure cannot look like an empty successful queue')
    await page.unroute('**/api/review/sessions/*/evidence')
    await page.route('**/api/review/sessions/*/evidence',route=>route.fulfill({status:200,json:{groups:null}}))
    await button('重试读取疑点').click();await page.getByRole('alert').filter({hasText:'疑点读取失败'}).waitFor()
    check((await api('/review/sessions/'+sid)).progress.submittedFrames===0,'malformed successful evidence response never approves frames')
    await page.unroute('**/api/review/sessions/*/evidence');await button('重试读取疑点').click()
    await page.getByRole('region',{name:'集中审查疑点'}).count() // Region remains in the normal inspector.
    await poll(async()=>await page.locator('.review-doubt').count()>0,'detector uncertainty enters review queue')
    const doubtStart=(await api('/review/sessions/'+sid+'/evidence')).groups[0].startFrame+1
    await page.locator('.review-doubt').first().click()
    await poll(async()=>await page.getByRole('textbox',{name:'跳转帧号',exact:true}).inputValue()===String(doubtStart)&&await page.getByRole('textbox',{name:'跳转帧号',exact:true}).isEnabled(),'doubt opens its original frame')
    check((await page.locator('.review-drawing [data-box]').count())>0,'selecting a doubt keeps complete frame annotations visible')
    await page.screenshot({path:'output/playwright/concentrated-review-doubt.png',animations:'disabled'})
    for(const [width,height] of [[1366,768],[1180,760],[760,760],[390,760]]) {
      await page.setViewportSize({width,height})
      for(const theme of ['light','dark']) {
        if(await page.locator('html').getAttribute('data-theme')!==theme)await button(theme==='dark'?'切换深色主题':'切换浅色主题').click()
        check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`concentrated review has no horizontal page overflow ${width} ${theme}`)
        await page.screenshot({path:`output/playwright/concentrated-review-${width}-${theme}.png`,animations:'disabled',fullPage:width<960})
      }
    }
    await page.setViewportSize({width:1366,height:768})
    await button('进入专注').click();await poll(async()=>await page.locator('.review-page').getAttribute('data-focused')==='true','concentrated review enters focus')
    check(await page.locator('.review-doubt').first().isVisible(),'doubt selection remains available in focus')
    await page.screenshot({path:'output/playwright/concentrated-review-focus.png',animations:'disabled'})
    await button('退出专注').click()

    // Return through the actual frame navigation without creating a submission.
    const jumpInput=page.getByRole('textbox',{name:'跳转帧号',exact:true})
    await jumpInput.fill('1');await jumpInput.press('Enter')
    await poll(async()=>await jumpInput.isEnabled()&&await jumpInput.inputValue()==='1','return to first frame after doubt navigation')
    await poll(async()=> (await api('/review/sessions/'+sid)).progress.submittedFrames===0,'doubt navigation does not approve frames')
  }
  const r=await page.locator('[data-box="1"] rect').boundingBox(); assert(r)
  await page.mouse.move(r.x+r.width/2,r.y+r.height/2);await page.mouse.down();await page.mouse.move(r.x+r.width/2+8,r.y+r.height/2+3,{steps:4});await page.mouse.up()
  await poll(async ()=>(await api(`/review/sessions/${sid}/frames/0`)).hasDraft,'B draft saved')
  check((await api('/review/sessions/'+sid)).progress.submittedFrames===0,'B draft does not count as submitted')
  for(let i=0;i<3;i++) {await page.getByTestId('submit-frame').click();await poll(async ()=>(await api('/review/sessions/'+sid)).progress.submittedFrames===i+1,'B frame submitted')}
  await button('继续检查').click()
  check((await api('/review/sessions/'+sid)).state==='in_progress','100 percent B progress still requires explicit completion')
  await button('完成视频审查').click();await button('确认完成视频审查').click()
  await poll(async ()=>(await api('/review/sessions/'+sid)).state==='reviewed','B explicitly finished')
  const reviewed=await api('/review/sessions/'+sid)
  check(reviewed.workflow==='concentrated' && !!reviewed.finalVersionId,'explicit review completion creates the accepted final version without a C operation')
  check(await page.getByRole('button',{name:'对比确认',exact:true}).count()===0,'new workflow has only annotation and review navigation')
  await page.getByTestId('export-training').waitFor();await page.getByTestId('export-training').click()
  const download=page.waitForEvent('download');await button('生成并下载 ZIP').click()
  const file=await download;const bytes=fs.readFileSync(await file.path());check(bytes.subarray(0,2).toString()==='PK','annotation to review produces a real downloadable training ZIP')
  await button('关闭').click();await button('重新审查本帧').click();await button('确认重新审查').click()
  await poll(async ()=>(await api('/review/sessions/'+sid)).state==='in_progress','reopen frame returns to review')
  await poll(async()=>await page.getByTestId('export-training').count()===0,'reopening review removes current export entry after response')
  check(await page.getByTestId('export-training').count()===0,'reopening review closes training export again')
  check(errors.length===0,'no uncaught browser errors through the full user lifecycle')
  await page.screenshot({path:'output/playwright/critical-workflows.png'})
  await import('./endpoint-tracking-browser.mjs')
} finally {
  fs.mkdirSync('output/playwright',{recursive:true})
  fs.writeFileSync('output/playwright/critical-workflows-results.json',JSON.stringify({modelMode,checks,errors,requests},null,2))
  await page.screenshot({path:'output/playwright/critical-workflows-last.png'}).catch(()=>{})
  await browser.close()
}
