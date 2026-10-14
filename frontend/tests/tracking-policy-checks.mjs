// Called by the fixed independent-integrity suite. Accounts, annotations,
// confirmations and tracking jobs use the real UI/API. Only model outputs are
// simulated by backend/tests/browser_server.py; no frontend store is imported.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { execFileSync } from 'node:child_process'

export async function trackingPolicyChecks(page, {root, origin, api, poll, ready, out}) {
  assert(root.startsWith(path.resolve('work') + path.sep), 'Isolated input directory required')
  execFileSync(process.env.TEST_PYTHON || 'work/review-venv/bin/python', ['-c', String.raw`
import json, os, sys
from pathlib import Path
import cv2
import numpy as np
request = json.load(sys.stdin)
root = Path.cwd().resolve()
relative = Path(request['directory']).relative_to(Path(request['hostRoot']))
assert relative.parts[0] == 'work' and relative.parts[1].startswith('e2e-confirm-ux-')
for name in ('APP_DATA_DIR', 'APP_DB_FILE', 'APP_STORAGE_DIR'):
    assert Path(os.environ[name]).resolve().is_relative_to(root / 'work')
destination = root / relative / 'policy-input.avi'
assert destination.parent.is_dir()
writer = cv2.VideoWriter(str(destination), cv2.VideoWriter_fourcc(*'MJPG'), 8, (640,432))
assert writer.isOpened()
try:
    for frame in range(8):
        image = np.full((432,640,3), 20+frame*9, dtype=np.uint8)
        cv2.circle(image, (210+frame,155), 8, (190,190,190), -1)
        writer.write(image)
finally:
    writer.release()
capture = cv2.VideoCapture(str(destination))
assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 8
capture.release()
`], {input:JSON.stringify({directory:root,hostRoot:process.cwd()}),encoding:'utf8',timeout:30000})
  const input = fs.readFileSync(path.join(root, 'policy-input.avi'))
  const button = name => page.getByRole('button', {name,exact:true})
  const work = mid => api('/track/workspace/'+mid)
  const result = mid => api('/track/result/'+mid)
  const checks = [], evidence = []
  const check = (condition,message) => {assert(condition,message);checks.push(message);console.log('PASS tracking-policy',checks.length,message)}
  const state = async (mid, predicate, description) => {await poll(async()=>predicate(await work(mid)),description);return work(mid)}
  async function upload(label) {
    await ready()
    const pending = page.waitForResponse(r=>r.url().endsWith('/api/track/upload') && r.request().method()==='POST')
    await page.locator('input[type=file][accept="video/*"]').setInputFiles({name:label+'.avi',mimeType:'video/x-msvideo',buffer:Buffer.concat([input,Buffer.from(crypto.randomUUID())])})
    const response = await pending;assert.equal(response.status(),201,await response.text())
    const mid = (await response.json()).mediaId;await ready();return mid
  }
  async function draw(mid,name,count,rectangle) {
    await ready();await page.keyboard.press('Escape')
    await page.getByRole('textbox',{name:'对象名称',exact:true}).fill(name)
    await page.locator('.annotation-heading h2').click();await page.keyboard.press('b')
    const box = await page.getByTestId('annotation-hit').boundingBox();assert(box)
    const [x,y,w,h] = rectangle
    await page.mouse.move(box.x+box.width*x,box.y+box.height*y);await page.mouse.down()
    await page.mouse.move(box.x+box.width*(x+w),box.y+box.height*(y+h),{steps:6});await page.mouse.up()
    await state(mid,value=>value.manualAnnotations?.length===count,'policy manual object '+count+' saved')
  }
  async function awaitPause(mid,objects) {
    await page.getByTestId('confirm-tracking-normal').waitFor()
    await state(mid,value=>value.pausedAnomalies?.length===objects,'real pause saved')
    await ready()
    const paused=await work(mid)
    // Pause publication precedes the editor's debounced cursor save. Waiting
    // for a real terminal position prevents reload from restoring old frame 0.
    await poll(async()=>await page.getByRole('spinbutton',{name:'跳转帧号'}).inputValue()===String(paused.lastPausedContext.frameIndex+1),'UI navigated to first risk frame')
    await state(mid,value=>value.currentFrame===paused.lastPausedContext.frameIndex,'pause position durably saved before refresh')
    return work(mid)
  }
  async function completed(mid) {
    await poll(async()=> (await result(mid)).frames.length===8 && await page.getByTestId('tracking-feedback').count()===0,'policy resumed to the original eighth frame')
    await ready();check((await work(mid)).lastPausedContext===null,'completed branch has no stale pause')
    await poll(async()=>(await work(mid)).currentFrame===7,'end position saved')
  }
  const visible = locator => locator.evaluate(element=>{
    const rect=element.getBoundingClientRect(),middle=document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2)
    return rect.width>0 && rect.height>0 && rect.y>=0 && rect.bottom<=innerHeight && element.contains(middle)
  })

  // A modest 36% height change waits for persistence, then returns to the
  // FIRST affected frame. A normal decision must use that frame's real box.
  const geometry = await upload('policy-geometry')
  await draw(geometry,'policy-geometry',1,[.3,.3,.1,.1])
  await draw(geometry,'steady-object',2,[.65,.3,.1,.1])
  const originalManual = (await work(geometry)).manualAnnotations
  await button('AI Tracking').click();let paused=await awaitPause(geometry,1)
  check(paused.lastPausedContext.frameIndex===1,'ordinary size risk pauses at its first frame, not the third observation')
  check((await result(geometry)).frames.length===2,'unconfirmed future observations are not published as accepted annotations')
  const panel=page.getByTestId('tracking-feedback')
  check(await panel.getAttribute('data-decision-frame')==='3','UI distinguishes first risk from third-frame pause decision')
  const trigger=await page.getByTestId('tracking-trigger-evidence').innerText()
  const details=paused.pausedAnomalies[0].diagnostic.details
  const percent=value=>(value*100).toFixed(1).replace(/\.0$/,'')+'%'
  assert(Number.isFinite(details.height_ratio) && details.height_ratio<details.geometry_thresholds.manual_height_ratio.min,'scenario really crosses the server height bound')
  const heightText=percent(details.height_ratio),minimumText=percent(details.geometry_thresholds.manual_height_ratio.min)
  check(trigger.includes('高度')&&trigger.includes(heightText)&&trigger.includes(minimumText)&&trigger.includes('参考第 1 帧'),'UI shows exact saved height ratio, server bound and reference frame')
  check((await page.getByTestId('tracking-persistence-evidence').innerText()).includes('第 4 帧'),'UI explains the observation interval')
  await page.reload();await ready();await page.getByTestId('tracking-trigger-evidence').waitFor()
  check((await page.getByTestId('tracking-trigger-evidence').innerText()).includes(minimumText),'refresh reconstructs server-owned pause evidence before any new confirmation')
  for (const [width,height] of [[1366,768],[1180,760]]) {
    await page.setViewportSize({width,height})
    for (const theme of ['light','dark']) {
      if (await page.locator('html').getAttribute('data-theme')!==theme) await button(theme==='light'?'切换浅色主题':'切换深色主题').click()
      check(await visible(page.getByTestId('confirm-tracking-normal')),`size confirmation remains visible ${width}x${height} ${theme}`)
      check(await visible(page.getByRole('spinbutton',{name:'跳转帧号'})) && await visible(button('下一帧')),`pause frame navigation remains visible ${width}x${height} ${theme}`)
      await button('专注').click()
      check(await visible(page.getByTestId('confirm-tracking-normal')),`focused confirmation remains visible ${width}x${height} ${theme}`)
      check(await visible(page.getByRole('spinbutton',{name:'跳转帧号'})) && await visible(button('下一帧')),`focused pause frame navigation remains visible ${width}x${height} ${theme}`)
      await page.screenshot({path:path.join(out,`tracking-policy-${width}-${theme}-focused.png`)})
      await page.getByRole('button',{name:/^退出专注/}).click()
    }
  }
  const attempts=[]
  const feedbackRoute='**/api/track/feedback/'+geometry
  await page.route(feedbackRoute,async route=>{
    attempts.push({key:route.request().headers()['idempotency-key'],body:route.request().postDataJSON()})
    const response=await route.fetch()
    if(attempts.length===1) await route.abort('failed')
    else await route.fulfill({response})
  })
  check((await page.getByTestId('confirm-tracking-normal').innerText()).includes('本帧所有对象'),'button clearly confirms the entire frame')
  await page.getByTestId('confirm-tracking-normal').click()
  await page.getByTestId('retry-tracking-feedback').waitFor()
  check((await result(geometry)).frames.length===2,'lost frame-confirmation response does not start tracking')
  await page.reload();await ready()
  await poll(async()=> (await work(geometry)).lastPausedContext===null && await button('AI Tracking').isEnabled(),'checkpoint replay restored editable frame')
  check((await result(geometry)).frames.length===2,'refresh recovers the confirmed checkpoint without silently starting model inference')
  await button('AI Tracking').click();await completed(geometry)
  await page.unroute(feedbackRoute)
  check(attempts.length===2 && attempts[0].key===attempts[1].key && JSON.stringify(attempts[0].body)===JSON.stringify(attempts[1].body),'refresh replays the same atomic frame checkpoint request and key')
  const confirmed = await work(geometry)
  check(confirmed.trackingFeedbackEvents.length===2 && confirmed.trackingFeedbackEvents.every(event=>event.scope==='frame' && event.geometryReference.frameIndex===1 && event.geometryReference.checkpoint),'one checkpoint includes both the risky object and the unflagged object exactly once')
  check(JSON.stringify(confirmed.manualAnnotations)===JSON.stringify(originalManual),'normal confirmation preserves original manual annotations')
  await page.reload();await ready()
  check(await page.getByTestId('tracking-feedback').count()===0 && (await work(geometry)).trackingFeedbackEvents.length===2,'refresh retains the whole-frame checkpoint and never restores the old pause')
  evidence.push({scenario:'geometry',mediaId:geometry,pauseFrame:1,decisionFrame:3,confirmedFrame:confirmed.trackingFeedbackEvents[0].geometryReference.frameIndex})

  // Stable overlap exists in the manually drawn seed. Draw outside the first
  // box, then move it inside through the actual canvas; never preseed workspace.
  const stable = await upload('policy-seed-overlap')
  await draw(stable,'policy-seed-overlap',1,[.2,.3,.3,.3])
  await draw(stable,'policy-seed-overlap',2,[.56,.3,.25,.25])
  await page.getByRole('button',{name:/选择对象 #2 /}).click()
  let box=await page.getByTestId('annotation-hit').boundingBox();assert(box)
  await page.mouse.move(box.x+box.width*.685,box.y+box.height*.425);await page.mouse.down()
  await page.mouse.move(box.x+box.width*.35,box.y+box.height*.45,{steps:8});await page.mouse.up()
  await state(stable,value=>value.manualAnnotations.find(o=>o.objectId===2)?.bbox.x<30,'overlapping seed saved after real drag')
  await button('AI Tracking').click();await completed(stable)
  check(await page.getByTestId('tracking-feedback').count()===0,'unchanged existing overlap remains a warning through eight frames')
  check((await page.locator('.tracking-warning-summary').innerText()).includes('追踪变化'),'combined warnings do not mislabel overlap as motion')
  evidence.push({scenario:'stable-seed-overlap',mediaId:stable,frames:8})

  // One explicit frame inspection confirms every object and overlap pair.
  const overlap = await upload('policy-overlap')
  for(let index=0;index<3;index++) await draw(overlap,'policy-overlap',index+1,[.2+index*.2,.3,.1,.1])
  await button('AI Tracking').click();paused=await awaitPause(overlap,3)
  check(paused.lastPausedContext.frameIndex===1,'new overlap waits for persistent risk and returns the first affected frame')
  const partners=await page.getByTestId('tracking-overlap-pairs').innerText()
  check(partners.includes('#1 ↔ #2')&&partners.includes('#1 ↔ #3'),'first object displays every related pair instead of overwriting its earlier partner')
  check((await page.getByTestId('tracking-overlap-acceptance').innerText()).includes('所有重叠组合'),'confirmation explicitly covers all displayed pairs only')
  await page.getByTestId('confirm-tracking-normal').click()
  const checkpoint=await state(overlap,value=>value.trackingFeedbackEvents?.length===3,'whole-frame overlap checkpoint saved')
  check(checkpoint.pausedAnomalies.length===0 && new Set(checkpoint.trackingFeedbackEvents.map(event=>event.checkpointId)).size===1,'one explicit frame action atomically confirms all three objects')
  await completed(overlap);await page.reload();await ready()
  check((await work(overlap)).trackingFeedbackEvents.length===3 && await page.getByTestId('tracking-feedback').count()===0,'accepted overlap survives continued tracking and reload without immediate repeat pause')
  const calibration=page.locator('.tracking-calibration')
  check(await calibration.count()===1 && (await calibration.innerText()).includes('2 个重叠组合已确认'),'pure overlap confirmations expose their saved pair count and reset entry after reload')
  const storedEvents=(await work(overlap)).trackingFeedbackEvents
  const calibrationFor=id=>page.locator(`[data-calibration-object-id="${id}"]`)
  check(await calibrationFor(1).getByRole('button',{name:'恢复默认判断',exact:true}).isEnabled(),'pure overlap confirmation offers the existing reset operation')
  await calibrationFor(1).getByRole('button',{name:'恢复默认判断',exact:true}).click()
  const reset=await state(overlap,value=>value.trackingFeedbackEvents?.length===4,'pair reset persisted')
  check(reset.trackingFeedbackEvents.at(-1).decision==='reset' && reset.trackingFeedbackEvents.at(-1).objectId===1 && JSON.stringify(reset.trackingFeedbackEvents.slice(0,3))===JSON.stringify(storedEvents),'reset preserves original pair confirmation history and records the explicit reset')
  await poll(async()=>await calibrationFor(1).count()===0 && (await calibrationFor(2).innerText()).includes('1 个重叠组合已确认'),'related reset entries refreshed')
  check(await calibrationFor(3).count()===1 && (await calibrationFor(2).innerText()).includes('#3') && (await calibrationFor(3).innerText()).includes('#2'),'reset clears pairs involving the selected object and keeps the independent pair')
  await page.reload();await ready()
  check(await calibrationFor(1).count()===0 && await calibrationFor(2).getByRole('button',{name:'恢复默认判断',exact:true}).isVisible(),'pair reset and independent remaining reset entry survive refresh')
  evidence.push({scenario:'new-overlap',mediaId:overlap,confirmedObjects:3,allPartnerIds:[2,3]})

  // Severe collapse remains immediate; a lower interruption rate cannot mean
  // simply disabling size checks. This is the failure pair for stable geometry.
  const extreme=await upload('policy-extreme')
  await draw(extreme,'policy-extreme',1,[.3,.3,.1,.1])
  await button('AI Tracking').click();paused=await awaitPause(extreme,1)
  check(paused.lastPausedContext.frameIndex===1 && await page.getByTestId('tracking-feedback').getAttribute('data-decision-frame')==='1','extreme collapse still pauses immediately on the first bad frame')
  check((await page.getByTestId('tracking-feedback').innerText()).includes('立即暂停'),'immediate extreme pause is clearly explained')
  evidence.push({scenario:'extreme',mediaId:extreme,pauseFrame:1,decisionFrame:1})
  await page.screenshot({path:path.join(out,'tracking-policy-extreme.png')})
  return {checks,evidence,model:'simulated; real UI, tracking publication, feedback and source frames'}
}
