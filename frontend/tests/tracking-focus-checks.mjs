// Real login/upload/drawing/feedback tests; only the host's named predictions
// are simulated. No frontend store import or prepared workspace is used.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

export async function trackingFocusChecks(page, {root, origin, api, poll, ready, out}) {
  const input=fs.readFileSync(path.join(root,'policy-input.avi'))
  const checks=[],evidence=[]
  const button=name=>page.getByRole('button',{name,exact:true})
  const check=(ok,message)=>{assert(ok,message);checks.push(message);console.log('PASS tracking-focus',checks.length,message)}
  const selected=async()=>page.locator('.annotation-overlay g.selected text').textContent().catch(()=>null)
  const tool=()=>page.locator('.annotation-toolbar .tool-btn.active').innerText()
  let mid
  const work=()=>api('/track/workspace/'+mid)
  async function upload(label){
    if((await page.getByTestId('annotation-page').getAttribute('class')).includes('focused'))await page.getByRole('button',{name:/^退出专注/}).click()
    await ready()
    const response=page.waitForResponse(r=>r.url().endsWith('/api/track/upload')&&r.request().method()==='POST')
    await page.locator('input[type=file][accept="video/*"]').setInputFiles({name:label+'.avi',mimeType:'video/x-msvideo',buffer:Buffer.concat([input,Buffer.from(crypto.randomUUID())])})
    const r=await response;assert.equal(r.status(),201,await r.text());mid=(await r.json()).mediaId;await ready()
  }
  async function draw(name,count,[x,y,w,h]){
    await ready();await page.keyboard.press('Escape');await page.getByRole('textbox',{name:'对象名称',exact:true}).fill(name)
    await page.locator('.annotation-heading h2').click();await page.keyboard.press('b')
    const b=await page.getByTestId('annotation-hit').boundingBox();assert(b)
    await page.mouse.move(b.x+b.width*x,b.y+b.height*y);await page.mouse.down();await page.mouse.move(b.x+b.width*(x+w),b.y+b.height*(y+h),{steps:6});await page.mouse.up()
    await poll(async()=>(await work()).manualAnnotations?.length===count,'focus manual '+count+' saved')
  }
  async function pause(n){
    await button('AI Tracking').click();await page.getByTestId('confirm-tracking-normal').waitFor()
    await poll(async()=>(await work()).pausedAnomalies.length===n,'focus pause list saved')
    await ready();await poll(async()=>{const w=await work();return w.currentFrame===w.lastPausedContext.frameIndex&&await page.getByRole('spinbutton',{name:'跳转帧号'}).inputValue()===String(w.currentFrame+1)},'focus pause frame saved')
  }
  async function focus(){if(!(await page.getByTestId('annotation-page').getAttribute('class')).includes('focused'))await button('专注').click()}
  async function jump(frame){const i=page.getByRole('spinbutton',{name:'跳转帧号'});await i.fill(String(frame+1));await i.press('Enter');await ready()}
  async function nudge(oid){
    const before=JSON.stringify((await work()).manualAnnotations.find(x=>x.frameIndex===1&&x.objectId===oid))
    await page.keyboard.press('Alt+ArrowRight')
    await poll(async()=>{const x=(await work()).manualAnnotations.find(x=>x.frameIndex===1&&x.objectId===oid);return x&&JSON.stringify(x)!==before},'focus selected object '+oid+' nudge saved')
  }
  async function complete(){await poll(async()=>await page.getByTestId('tracking-feedback').count()===0&&(await api('/track/result/'+mid)).frames.length===8,'focus confirmed branch reaches eighth frame');await ready();check((await work()).lastPausedContext===null,'resolved feedback automatically resumes without a stale pause')}
  async function screenshot(label){await page.screenshot({path:path.join(out,label+'.png')});evidence.push(label)}
  const chip=oid=>page.getByRole('button',{name:new RegExp('^选择暂停对象 #'+oid+' ')})
  const visible=locator=>locator.evaluate(element=>{const r=element.getBoundingClientRect(),at=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return r.width>0&&r.height>0&&r.y>=0&&r.bottom<=innerHeight&&element.contains(at)})
  async function layoutChecks(label,objectId=2){
    for(const [width,height]of[[1366,768],[1180,760]])for(const theme of['light','dark']){
      await page.setViewportSize({width,height})
      await page.evaluate(value=>{document.documentElement.dataset.theme=value},theme)
      await poll(async()=>visible(chip(objectId))&&visible(page.getByTestId('confirm-tracking-corrected')),'focus chip and corrected visible '+width+theme)
      check(await visible(page.getByRole('button',{name:'上一帧',exact:true})),label+': frame navigation remains visible '+width+' '+theme)
      await screenshot(label+'-'+width+'-'+theme)
    }
  }

  await page.setViewportSize({width:1366,height:768})
  await upload('focus-two');await draw('audit-pause-one',1,[.2,.3,.12,.15]);await draw('audit-pause-two',2,[.65,.3,.12,.15]);await pause(2)
  check((await selected())?.endsWith('one 1')&&(await tool()).includes('选择'),'first pause selects its actual object and the selection tool')
  await chip(2).click();check((await selected())?.endsWith('two 2')&&(await tool()).includes('选择'),'chip selects the same stable identity and tool')
  await chip(1).click();await nudge(1);await page.getByTestId('confirm-tracking-corrected').click()
  await poll(async()=>(await work()).pausedAnomalies.length===1,'corrected leaves single object')
  await ready();await poll(async()=>(await selected())?.endsWith('two 2'),'next pending object is selected')
  check(await chip(2).count()===1,'last pending object keeps its selectable chip')
  check((await tool()).includes('选择'),'advancing corrected feedback uses the selection tool')
  await focus();check(!await page.locator('[data-workbench-region=inspector]').isVisible(),'focus deliberately hides the inspector')
  await layoutChecks('focus-single-chip')
  await nudge(2)
  const before=(await work()).manualAnnotations.find(x=>x.frameIndex===1&&x.objectId===2).bbox
  const h=await page.locator('.annotation-overlay g.selected .box-handle').last().boundingBox();assert(h)
  await page.mouse.move(h.x+h.width/2,h.y+h.height/2);await page.mouse.down();await page.mouse.move(h.x+h.width/2+8,h.y+h.height/2+4,{steps:5});await page.mouse.up()
  await poll(async()=>{const x=(await work()).manualAnnotations.find(x=>x.frameIndex===1&&x.objectId===2)?.bbox;return x&&x.width!==before.width&&x.height!==before.height},'focus resize actually persisted')
  check(true,'focus movement and corner resizing persist the intended object')
  await page.getByTestId('confirm-tracking-corrected').click();await complete()

  await upload('focus-initial-single');await draw('audit-pause-single',1,[.2,.3,.12,.15]);await pause(1)
  check(await chip(1).count()===1&&(await selected())?.endsWith('single 1'),'a one-object initial pause keeps the chip and selects the object')
  await page.locator('.annotation-toolbar .tool-btn').filter({hasText:'画框'}).click()
  await poll(async()=>(await work()).editor?.activeTool==='bbox','chosen tool persisted before reload')
  const frameRoute='**/api/track/frame/'+mid+'/1'
  let waiting=false,release
  const gate=new Promise(resolve=>{release=resolve})
  await page.route(frameRoute,async route=>{waiting=true;await gate;await route.fulfill({status:500,json:{detail:'Independent frame read fault'}})})
  await page.reload({waitUntil:'domcontentloaded'});await poll(async()=>waiting&&await chip(1).count()===1,'paused frame read actually blocked')
  check(await chip(1).isDisabled()&&(await tool()).includes('画框'),'unready frame keeps selection locked and does not change the tool')
  release();await page.locator('.canvas-error').waitFor()
  check(await chip(1).isDisabled(),'frame read failure cannot retarget an unknown image')
  await page.unroute(frameRoute);await button('重试读取').click();await ready()
  await poll(async()=>(await selected())?.endsWith('single 1')&&(await tool()).includes('选择'),'read retry selects target only after actual frame recovery')
  await focus();await jump(0);await page.locator('.annotation-toolbar .tool-btn').filter({hasText:'画框'}).click()
  check(await chip(1).isDisabled(),'pause chips do not retarget objects while browsing an earlier frame')
  check((await tool()).includes('画框'),'browsing an earlier frame preserves the user’s chosen tool')
  await page.getByRole('button',{name:'返回第 2 帧',exact:true}).click();await ready()
  await poll(async()=>(await selected())?.endsWith('single 1')&&(await tool()).includes('选择'),'returning to ready pause frame selects its target')
  await nudge(1)
  // Lose only the confirmation response after its actual server commit.
  const feedbackRoute='**/api/track/feedback/'+mid
  let lost=false
  await page.route(feedbackRoute,async route=>{if(!lost){lost=true;const r=await route.fetch();assert(r.ok(),await r.text());await route.abort('failed')}else await route.continue()})
  await page.getByTestId('confirm-tracking-corrected').click();await page.getByTestId('retry-tracking-feedback').waitFor()
  check(await chip(1).isDisabled(),'response loss keeps the target chip locked for the original retry')
  const lockedSelection=await selected();await chip(1).click({force:true}).catch(()=>{})
  check(await selected()===lockedSelection,'pending retry does not switch selection')
  await page.getByTestId('retry-tracking-feedback').click();await page.unroute(feedbackRoute);await complete()

  // Explicitly selecting an unflagged object is part of whole-frame inspection.
  await upload('focus-inspection');await draw('audit-pause-one',1,[.15,.3,.12,.15]);await draw('audit-pause-two',2,[.55,.3,.12,.15]);await draw('steady-third',3,[.8,.6,.1,.1]);await pause(2)
  await nudge(1);await page.getByRole('button',{name:/^选择对象 #3 /}).click()
  await page.getByTestId('confirm-tracking-corrected').click();await poll(async()=>(await work()).pausedAnomalies.length===1,'unflagged inspection after corrected')
  check((await selected())?.endsWith('third 3'),'advancing feedback does not steal an explicitly inspected unflagged object')
  await chip(2).click();check((await selected())?.endsWith('two 2'),'explicit remaining chip restores selection after whole-frame inspection')
  await page.getByTestId('confirm-tracking-normal').click();await complete()

  await upload('focus-disappeared');await draw('audit-disappear-one',1,[.2,.3,.12,.15]);await draw('steady-two',2,[.65,.3,.12,.15]);await pause(1);await focus()
  const restore=page.getByTestId('restore-missing-tracking-object')
  check(await restore.isVisible(),'missing object has a visible same-identity recovery action in focus')
  await layoutChecks('focus-missing-object',1)
  check(await visible(restore),'same-identity recovery remains in the small focus viewport')
  await page.getByTestId('confirm-tracking-normal').click();await page.getByTestId('tracking-feedback').locator('[role=alert]').waitFor()
  check((await work()).pausedAnomalies.length===1,'a missing rectangle cannot be silently confirmed normal')
  await restore.click();await poll(async()=>(await work()).manualAnnotations.some(x=>x.objectId===1&&x.frameIndex===1),'missing stable identity restored')
  check((await selected())?.endsWith('one 1')&&(await tool()).includes('选择'),'same-ID restoration selects the recovered box for editing')
  check(!await restore.count(),'recovery action disappears only after the target rectangle exists')
  await screenshot('focus-missing-restored');await nudge(1);await page.getByTestId('confirm-tracking-corrected').click();await complete()
  return {checks,evidence,model:'simulated',businessFlows:'real UI and API'}
}
