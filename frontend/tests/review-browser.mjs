import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'
// Real browser acceptance test. Run against the isolated fixture documented in docs/TESTING.md.
import assert from 'node:assert/strict'
import fs from 'node:fs'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const fixture=JSON.parse(fs.readFileSync(process.env.REVIEW_FIXTURE || 'work/browser-fixture.json'))
const origin=process.env.REVIEW_ORIGIN || 'http://127.0.0.1:5373'
const browser=await chromium.launch({channel:process.env.CHROME_CHANNEL || 'chrome',headless:true})
const page=await browser.newPage({viewport:{width:1440,height:900}})
if (process.env.LEGACY_BROWSER === '1') await useLegacyBrowserAPIs(page)
const events=[], errors=[]
page.on('console',m=>events.push({type:m.type(),text:m.text()}));page.on('pageerror',e=>errors.push(String(e)))
let assertions=0
const check=(condition,message)=>{assert(condition,message);console.log('PASS',++assertions,message)}
const api=async(path)=>{
  const r=await page.request.get(origin+'/api/review'+path,{headers:{Authorization:'Bearer '+fixture.token}})
  assert.equal(r.status(),200,await r.text());return r.json()
}
const getFrame=(i=0)=>api(`/sessions/${fixture.sid}/frames/${i}`)
const session=()=>api(`/sessions/${fixture.sid}`)
const waitSaved=()=>page.waitForFunction(()=>!document.querySelector('[data-testid="submit-frame"]')?.textContent.includes('处理中'))
const closeDialog=async()=>{if(await page.getByRole('dialog').count())await page.getByRole('dialog').getByRole('button',{name:'关闭',exact:true}).click()}
const drag=async(dx=15,dy=5)=>{
 const r=await page.locator('[data-box="1"] rect').boundingBox();assert(r)
 await page.mouse.move(r.x+r.width/2,r.y+r.height/2);await page.mouse.down();await page.mouse.move(r.x+r.width/2+dx,r.y+r.height/2+dy,{steps:4});await page.mouse.up()
 await waitSaved()
}
const go=async(i)=>{
 await page.getByRole('textbox',{name:'跳转帧号'}).fill(String(i+1));await page.getByRole('button',{name:'确定',exact:true}).click()
 await page.waitForFunction(n=>document.querySelector('[aria-label="跳转帧号"]').value===String(n),i+1)
 await page.locator('.review-center .section-head').filter({hasText:`第 ${i+1} / 3 帧`}).waitFor();await waitSaved();await closeDialog()
 await page.locator('.review-page').click({position:{x:600,y:12}})
}
try {
 await page.addInitScript(({token})=>{localStorage.setItem('rare-sperm-token',token);localStorage.setItem('rare-sperm-auth',JSON.stringify({id:'2',name:'review-B',role:'annotator'}))},fixture)
 await page.goto(origin+'/review');await page.locator(`[data-session-id="${fixture.sid}"]`).click();await page.locator('.review-drawing').waitFor();await waitSaved();await closeDialog()
 check((await session()).progress.submittedFrames===0,'opening/browsing does not submit')
 await page.keyboard.press(']');check(await page.locator('[data-row="1"]').evaluate(e=>e.classList.contains('selected')),'bracket selects object and comparison row')
 await page.keyboard.press(']');await page.keyboard.press(']');check(await page.locator('[data-row="2"]').evaluate(e=>e.classList.contains('selected')),'object selection does not wrap')
 await page.keyboard.press('f');check(await page.getByRole('textbox',{name:'跳转帧号'}).evaluate(e=>e===document.activeElement),'F focuses frame input')
 await page.keyboard.press('ArrowRight');check((await page.getByRole('textbox',{name:'跳转帧号'}).inputValue())==='1','input arrow does not navigate')
 await page.keyboard.press('Escape')
 await drag();check((await getFrame()).hasDraft,'drag auto-saves server draft')
 check((await session()).progress.submittedFrames===0,'draft excluded from progress')
 await page.keyboard.press('o');check(await page.locator('.original-box').count()===1,'O overlays immutable original')
 check(await page.locator('.compare-pair svg').count()===2,'real paired frame crops available')
 await page.screenshot({path:'output/playwright/review-edited.png',fullPage:true})
 const draft=await getFrame()
 await page.reload();await page.locator(`[data-session-id="${fixture.sid}"]`).click();await page.getByRole('dialog').waitFor()
 check((await page.getByRole('dialog').innerText()).includes('未提交草稿'),'refresh offers draft recovery')
 await page.getByRole('button',{name:'继续编辑',exact:true}).click()
 check((await getFrame()).patch[0].bbox[0]===draft.patch[0].bbox[0],'draft geometry survives refresh')
 await page.getByTestId('submit-frame').click();await page.locator('.review-center .section-head').filter({hasText:'第 2 / 3 帧'}).waitFor();await waitSaved()
 check((await session()).progress.submittedFrames===1,'explicit submit commits and advances')
 await go(0);await drag(10,0)
 check((await session()).progress.submittedFrames===0,'reedit removes submitted progress')
 await page.getByRole('button',{name:'放弃本帧草稿'}).click();await page.getByRole('button',{name:'放弃修改',exact:true}).click();await waitSaved()
 check(JSON.stringify((await getFrame()).patch)===JSON.stringify(draft.patch),'discard restores last successful B')
 check((await session()).progress.submittedFrames===1,'discard restores submitted progress')
 // Simulate a committed write whose HTTP response was lost; retry must use the same key.
 let dropped=false, keys=[]
 await page.route('**/api/review/sessions/*/frames/*/draft',async route=>{
   keys.push(route.request().headers()['idempotency-key'])
   if(!dropped){dropped=true;await route.fetch();await route.abort('failed')}else await route.continue()
 })
 await drag(8,2);await page.getByRole('alert').waitFor()
 const committedDraft=await getFrame()
 check(committedDraft.hasDraft,'injected lost response really persisted on backend')
 await page.getByRole('button',{name:'人工标注',exact:true}).click();await waitSaved()
 // Guard retries the same request and may leave on success.
 check(keys.length>=2&&keys[0]===keys[1],'leave guard retries uncertain save with same idempotency key')
 await page.getByRole('button',{name:'审查模式',exact:true}).click();await page.locator(`[data-session-id="${fixture.sid}"]`).click();await closeDialog();await page.locator('.review-drawing').waitFor();await waitSaved();await closeDialog()
 check((await getFrame()).frameRevision===committedDraft.frameRevision,'retry produces no extra frame revision')
 await page.unroute('**/api/review/sessions/*/frames/*/draft')
 await page.getByTestId('submit-frame').click();await page.locator('.review-center .section-head').filter({hasText:'第 2 / 3 帧'}).waitFor();await waitSaved()
 await page.getByTestId('submit-frame').click();await page.locator('.review-center .section-head').filter({hasText:'第 3 / 3 帧'}).waitFor();await waitSaved()
 check(await page.locator('[data-box]').count()===0,'empty frame renders without invented boxes')
 await page.getByTestId('submit-frame').click();await page.getByRole('dialog').waitFor()
 check((await session()).state==='in_progress','last submission does not auto-finish')
 await page.getByRole('button',{name:'继续检查',exact:true}).click()
 await go(0)
 check(await page.getByTestId('submit-frame').isDisabled(),'submitted frame disallows duplicate submit')
 await page.getByRole('button',{name:'完成视频审查',exact:true}).click();await page.getByRole('button',{name:'确认完成视频审查',exact:true}).click();await waitSaved()
 check((await session()).state==='reviewed','explicit completion freezes B')
 await closeDialog();await page.locator('[data-row="1"]').click();check(await page.locator('.compare-pair svg').count()===2,'readonly selection and crops still work')
 check(await page.getByTestId('submit-frame').isDisabled(),'completed video cannot submit')
 const frozen=await getFrame();await drag(30,20)
 check(JSON.stringify((await getFrame()).patch)===JSON.stringify(frozen.patch),'readonly drag cannot mutate frozen B')
 await page.setViewportSize({width:1366,height:768});await page.screenshot({path:'output/playwright/review-completed-1366.png',fullPage:true})
 check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'1366 viewport has no horizontal page overflow')
 check(errors.length===0,'no JavaScript runtime exceptions')
} finally {
 fs.writeFileSync('output/playwright/review-browser.log',JSON.stringify({assertions,errors,events},null,2))
 await browser.close()
}
