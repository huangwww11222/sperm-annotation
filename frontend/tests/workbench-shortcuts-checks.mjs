import assert from 'node:assert/strict'
import fs from 'node:fs'
import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'
// The task list is a drawer that stays closed while a task is open.
const openTask=async(target,selector)=>{
  const card=target.locator(selector),toggle=target.getByTestId('toggle-workbench-library')
  const resume=target.getByRole('button',{name:'留在上次位置',exact:true})
  for(let i=0;i<40;i++){
    // Auto-restoring the task can show this dialog before the library opens.
    // Handle only this known viewing choice; do not dismiss errors/decisions.
    if(await resume.isVisible()){await resume.click();continue}
    if(await card.isVisible()){try{await card.click({timeout:1500});return}catch{}}
    else if(await toggle.isVisible()){try{await toggle.click({timeout:1500})}catch{}}
    await target.waitForTimeout(250)
  }
  await card.click()
}

// Real DOM/frames/API checks shared by the fixed workbench regression. Each
// stage receives a fresh browser context; no store import or geometry fixture
// reset is used. Viewing may save cursors, never annotations or C decisions.
export async function workbenchShortcutsChecks({ page, origin, review, confirmation, check, output = 'output/playwright/workbench-shortcuts', stages = ['A', 'B', 'C'] }) {
  fs.mkdirSync(output, { recursive: true })
  const observations = [], errors = [], mutations = []
  const browser = page.context().browser()
  const json = async (target, path, token) => {
    const response = await target.request.get(origin + '/api' + path, { headers: { Authorization: 'Bearer ' + token } })
    assert(response.ok(), await response.text())
    return response.json()
  }
  const business = async target => {
    const b = await json(target, `/review/sessions/${review.sid}`, review.token)
    const frames = await Promise.all(Array.from({ length: b.frameCount }, (_, fi) => json(target, `/review/sessions/${review.sid}/frames/${fi}`, review.token)))
    const c = await json(target, `/confirmation/sessions/${confirmation.main.sid}`, confirmation.token)
    const changes = await json(target, `/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)
    return JSON.stringify({ b: { revision: b.revision, state: b.state, progress: b.progress }, frames, c: { revision: c.revision, state: c.state, progress: c.progress, finalVersionId: c.finalVersionId }, changes })
  }
  const config = {
    A: { path: '/annotate', token: review.authorToken, id: 1, image: '.annotation-stage', viewport: '.canvas-scroll' },
    B: { path: '/review', token: review.token, id: 2, image: '.review-drawing', viewport: '.review-viewport' },
    C: { path: '/confirm', token: confirmation.token, id: 3, image: '.c-context [data-variant="full"]', viewport: '.c-context .c-frame-viewport' },
  }
  const snapshot = async (target, stage, label) => {
    await target.waitForTimeout(150)
    const data = await target.locator(config[stage].image).evaluate((el, viewportSelector) => {
      const image = el.getBoundingClientRect(), host = el.closest(viewportSelector), viewport = host.getBoundingClientRect()
      let width = image.width, height = image.height
      if (el instanceof SVGSVGElement) { const matrix = el.getScreenCTM(), box = el.viewBox.baseVal; width = box.width * Math.hypot(matrix.a, matrix.b); height = box.height * Math.hypot(matrix.c, matrix.d) }
      return { width, height, left: image.x, top: image.y, centerX:(viewport.x+viewport.width/2-image.x)/width, centerY:(viewport.y+viewport.height/2-image.y)/height, scrollLeft: host.scrollLeft, scrollTop: host.scrollTop, viewport: { x: viewport.x, y: viewport.y, width: viewport.width, height: viewport.height }, localCrops: Array.from(document.querySelectorAll('.c-crops .confirmation-image')).map(svg => svg.getAttribute('viewBox')) }
    }, config[stage].viewport)
    observations.push({ stage, label, ...data })
    return data
  }
  const blur = target => target.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur())
  const closeDialog = async target => {
    if (await target.getByRole('dialog').count()) {
      const close = target.getByRole('dialog').getByRole('button', { name: '关闭', exact: true })
      if (await close.count()) await close.click()
    }
  }
  async function open(stage, readonly = false) {
    const cfg = config[stage], token = readonly ? stage === 'B' ? review.thirdToken : review.token : cfg.token
    const target = await browser.newPage({ viewport: { width: 1366, height: 768 } })
    target.on('pageerror', error => errors.push({ stage, error: String(error) }))
    if (process.env.LEGACY_BROWSER === '1') await useLegacyBrowserAPIs(target)
    await target.addInitScript(({ token, id }) => {
      localStorage.setItem('rare-sperm-token', token)
      localStorage.setItem('rare-sperm-auth', JSON.stringify({ id: String(id), name: 'workbench-shortcuts', role: 'annotator' }))
    }, { token, id: readonly ? stage === 'B' ? 3 : 2 : cfg.id })
    await target.goto(origin + cfg.path)
    if (stage === 'B') await openTask(target,`[data-session-id="${review.sid}"]`)
    if (stage === 'C') {
      await target.locator('.confirmation-page[data-busy="false"]').waitFor()
      await openTask(target,`[data-session-id="${confirmation.main.sid}"]`)
      await target.locator('.confirmation-page[data-busy="false"]').waitFor()
      const resume = target.getByRole('button', { name: '留在上次位置', exact: true })
      if (await resume.count()) await resume.click()
    }
    await target.locator(cfg.image).waitFor()
    await closeDialog(target)
    if(stage==='B')await target.locator('.object-row').first().click()
    await blur(target)
    return target
  }
  async function panAndHide(target, stage, label) {
    await blur(target)
    // Start away from scroll boundaries. Dragging a box with Space held must
    // pan the view rather than move the annotation or select a C item.
    for(let i=0;i<18;i++){const overflow=await target.locator(config[stage].viewport).evaluate(el=>Math.max(el.scrollWidth-el.clientWidth,el.scrollHeight-el.clientHeight));if(overflow>100)break;await target.keyboard.press('+');await target.waitForTimeout(40)}
    await target.locator(config[stage].viewport).evaluate(el => { el.scrollLeft=(el.scrollWidth-el.clientWidth)/2;el.scrollTop=(el.scrollHeight-el.clientHeight)/2 })
    const before=await snapshot(target,stage,label+'-pan-before')
    const selectedBefore=stage==='C'?await target.locator('.c-change-item.selected').getAttribute('aria-label'):stage==='B'?await target.locator('.object-row.selected').getAttribute('data-row'):null
    const rect=await target.locator(config[stage].viewport).boundingBox()
    const x=rect.x+rect.width*.45,y=rect.y+rect.height*.45
    await target.keyboard.down('Space');await target.mouse.move(x,y);await target.mouse.down();await target.mouse.move(x+35,y+22,{steps:4});await target.mouse.up();await target.keyboard.up('Space')
    const after=await snapshot(target,stage,label+'-space-pan')
    check(Math.abs(after.scrollLeft-before.scrollLeft)>5||Math.abs(after.scrollTop-before.scrollTop)>5,`${stage} ${label}: Space+drag pans the enlarged image`)
    await target.mouse.move(x,y);await target.mouse.down({button:'middle'});await target.mouse.move(x-28,y-18,{steps:4});await target.mouse.up({button:'middle'})
    const middle=await snapshot(target,stage,label+'-middle-pan')
    const selectedAfter=stage==='C'?await target.locator('.c-change-item.selected').getAttribute('aria-label'):stage==='B'?await target.locator('.object-row.selected').getAttribute('data-row'):null
    check(selectedAfter===selectedBefore,`${stage} ${label}: panning preserves the selected object or change`)
    check(Math.abs(middle.scrollLeft-after.scrollLeft)>5||Math.abs(middle.scrollTop-after.scrollTop)>5,`${stage} ${label}: middle-button drag pans without editing boxes`)
    if(stage!=='A'){
      const shapes=stage==='B'?'.review-drawing .review-object':'.c-context .context-boxes rect'
      const beforeCount=await target.locator(shapes).count()
      check(beforeCount>0,`${stage}: the fixture contains real object shapes before hiding`)
      await target.keyboard.press('h')
      const visible=await target.locator(shapes).evaluateAll(elements=>elements.filter(el=>{for(let p=el;p;p=p.parentElement){const style=getComputedStyle(p);if(style.display==='none'||style.visibility==='hidden'||style.opacity==='0')return false}return true}).length)
      check(visible===0,`${stage}: H hides annotation shapes while retaining the image`)
      check(await target.locator(config[stage].image+' image').isVisible(),`${stage}: hiding boxes keeps the actual source image visible`)
      await target.keyboard.press('h')
      check(await target.locator(shapes).count()===beforeCount,`${stage}: H restores the same object shapes`)
    }else{
      const hide=target.getByRole('button',{name:'显示或隐藏标注',exact:true})
      await target.keyboard.press('h');check(await hide.getAttribute('aria-pressed')==='true','A: H hides annotations');await target.keyboard.press('h');check(await hide.getAttribute('aria-pressed')==='false','A: H restores annotations')
    }
  }
  async function cancelThenClick(target,stage){
    await blur(target)
    if(stage==='B')await target.locator('.object-row[data-row="1"]').click();else await target.locator('.c-change-item').first().click()
    await blur(target)
    const viewport=target.locator(config[stage].viewport),rect=await viewport.boundingBox()
    await viewport.evaluate(el=>{el.__cancelPointer=null;el.addEventListener('pointerdown',event=>{el.__cancelPointer=event.pointerId},{capture:true,once:true})})
    await target.mouse.move(rect.x+rect.width*.5,rect.y+rect.height*.5);await target.mouse.down({button:'middle'});await target.mouse.move(rect.x+rect.width*.5+24,rect.y+rect.height*.5+18,{steps:3})
    await viewport.evaluate(el=>{if(el.hasPointerCapture(el.__cancelPointer))el.releasePointerCapture(el.__cancelPointer)})
    await target.waitForTimeout(30);await target.mouse.up({button:'middle'});await target.waitForTimeout(30)
    await target.locator(stage==='B'?'.review-drawing [data-box="2"] > rect':'.c-context [data-object-id="2"]').click()
    await target.waitForTimeout(150)
    const selected=stage==='B'?await target.locator('.object-row.selected').getAttribute('data-row'):await target.locator('.c-change-item.selected').getAttribute('aria-label')
    check(stage==='B'?selected==='2':selected.includes('对象 2'),`${stage}: first real object click after cancelled pointer capture remains effective`)
  }
  async function compositionIsolation(target,stage){
    await blur(target)
    const read=()=>target.evaluate(()=>{const root=document.querySelector('.annotation-page,.review-page,.confirmation-page');return {focus:!!document.querySelector('.shell.is-focus'),hidden:document.querySelector('.review-viewport')?.classList.contains('annotations-hidden')??document.querySelector('[aria-label="显示标注框"],[aria-label="显示或隐藏标注"]')?.getAttribute('aria-pressed')}})
    const before=await snapshot(target,stage,'composition-before'),state=await read()
    await target.evaluate(()=>{for(const dispatchTarget of [document.body,window])for(const flags of [{isComposing:true},{keyCode:229}])for(const key of ['+','h','f']){dispatchTarget.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true,...flags}));dispatchTarget.dispatchEvent(new KeyboardEvent('keyup',{key,bubbles:true,...flags}))}})
    const after=await snapshot(target,stage,'composition-after')
    check(Math.abs(after.width-before.width)<1,`${stage}: composing and legacy 229 keys on body/window do not zoom`)
    check(JSON.stringify(await read())===JSON.stringify(state),`${stage}: composing and legacy 229 keys do not toggle boxes or focus`)
  }
  async function activeResizeIsolation(target){
    await blur(target)
    const handle=target.locator('.resize-handle[data-handle="br"]')
    await handle.scrollIntoViewIfNeeded()
    const rect=await handle.boundingBox(),box=target.locator('.review-drawing [data-box="2"] > rect')
    const geometry=()=>box.evaluate(el=>['x','y','width','height'].map(key=>el.getAttribute(key)))
    const beforeGeometry=await geometry(),beforeData=await business(target),before=await snapshot(target,'B','active-resize-before')
    await target.mouse.move(rect.x+rect.width/2,rect.y+rect.height/2);await target.mouse.down();await target.mouse.move(rect.x+rect.width/2+18,rect.y+rect.height/2+12,{steps:3})
    await target.keyboard.press('+');await target.keyboard.press('0');await target.keyboard.down('Control');await target.mouse.wheel(0,-120);await target.keyboard.up('Control')
    const after=await snapshot(target,'B','active-resize-after-zoom-keys')
    check(Math.abs(after.width-before.width)<1,'B: +, 0 and Ctrl+wheel cannot change scale during a real corner resize')
    await target.keyboard.press('Escape');await target.mouse.up();await target.waitForTimeout(150)
    check(JSON.stringify(await geometry())===JSON.stringify(beforeGeometry),'B: Esc cancels an active resize and restores the original local box')
    check(await business(target)===beforeData,'B: cancelled active resize creates no saved draft or revision')
  }
  async function basicZoom(target, stage, label) {
    const initial = await snapshot(target, stage, label + '-initial')
    await target.keyboard.press('+')
    const plus = await snapshot(target, stage, label + '-plus')
    await target.screenshot({ path: `${output}/${stage}-${label}-plus.png`, animations: 'disabled' })
    check(plus.width > initial.width + 1 && plus.height > initial.height + 1, `${stage} ${label}: + enlarges the actual source image`)
    check(Math.abs(plus.centerX-initial.centerX)<.03&&Math.abs(plus.centerY-initial.centerY)<.03,`${stage} ${label}: keyboard zoom preserves the viewport anchor`)
    await target.keyboard.press('-')
    const minus = await snapshot(target, stage, label + '-minus')
    check(minus.width < plus.width - 1, `${stage} ${label}: - shrinks the actual source image`)
    await target.keyboard.press('=')
    check((await snapshot(target, stage, label + '-equals')).width > minus.width + 1, `${stage} ${label}: = also enlarges without needing Shift`)
    await target.keyboard.press('0')
    const fit = await snapshot(target, stage, label + '-fit')
    check(fit.width <= fit.viewport.width + 1 && fit.height <= fit.viewport.height + 1, `${stage} ${label}: 0 fits the complete source frame`)
    const viewport = await target.locator(config[stage].viewport).boundingBox()
    await target.mouse.move(viewport.x + viewport.width * .55, viewport.y + viewport.height * .55)
    for (const modifier of ['Control', 'Meta']) {
      const before = await snapshot(target, stage, label + '-' + modifier + '-before')
      await target.keyboard.down(modifier); await target.mouse.wheel(0, -120); await target.keyboard.up(modifier)
      const after = await snapshot(target, stage, label + '-' + modifier + '-after')
      check(after.width > before.width + 1, `${stage} ${label}: ${modifier}+wheel enlarges the image`)
    }
    if(stage!=='A'){
      for(let i=0;i<30;i++){const enough=await target.locator(config[stage].viewport).evaluate(el=>el.scrollWidth-el.clientWidth>100&&el.scrollHeight-el.clientHeight>100);if(enough)break;await target.keyboard.press('+');await target.waitForTimeout(30)}
      await target.locator(config[stage].viewport).evaluate(el=>{el.scrollLeft=(el.scrollWidth-el.clientWidth)/2;el.scrollTop=(el.scrollHeight-el.clientHeight)/2;el.__rapidShortcutWheels=[];if(!el.__rapidShortcutHandler){el.__rapidShortcutHandler=event=>el.__rapidShortcutWheels.push({time:event.timeStamp,ctrl:event.ctrlKey,delta:event.deltaY});el.addEventListener('wheel',el.__rapidShortcutHandler)}})
      const before=await snapshot(target,stage,label+'-rapid-wheel-before')
      const point={x:before.viewport.x+before.viewport.width*.55,y:before.viewport.y+before.viewport.height*.55}
      const anchor={x:(point.x-before.left)/before.width,y:(point.y-before.top)/before.height}
      await target.mouse.move(point.x,point.y);await target.keyboard.down('Control')
      await Promise.all([target.mouse.wheel(0,-45),target.mouse.wheel(0,-45)])
      await target.keyboard.up('Control')
      const after=await snapshot(target,stage,label+'-rapid-wheel-after')
      const events=await target.locator(config[stage].viewport).evaluate(el=>el.__rapidShortcutWheels)
      observations.push({stage,label:label+'-rapid-wheel-events',events})
      check(after.width>before.width+1,`${stage} ${label}: rapid wheel events enlarge the image`)
      check(Math.abs((point.x-after.left)/after.width-anchor.x)*after.width<3&&Math.abs((point.y-after.top)/after.height-anchor.y)*after.height<3,`${stage} ${label}: two rapid wheel events preserve the same pointer anchor within 3px`)
    }
    if (stage === 'C') check(JSON.stringify(initial.localCrops) === JSON.stringify((await snapshot(target, stage, label + '-crop-isolation')).localCrops), 'C full-frame shortcuts do not alter A/B local comparison crops')
  }
  try {
    for (const stage of stages) {
      const target = await open(stage)
      const before = await business(target)
      const record = request => {
        const path = new URL(request.url()).pathname
        if (request.method() !== 'GET' && path.startsWith('/api/') && !path.endsWith('/cursor') && path !== '/api/review/cursors') mutations.push({ stage, method: request.method(), path })
      }
      target.on('request', record)
      try {
        const libraryToggle = target.getByTestId('toggle-workbench-library')
        const libraryBefore = await libraryToggle.getAttribute('aria-expanded')
        await blur(target); await target.keyboard.press('m')
        check(await libraryToggle.getAttribute('aria-expanded') !== libraryBefore, `${stage}: M toggles the video drawer`)
        await target.keyboard.press('m')
        check(await libraryToggle.getAttribute('aria-expanded') === libraryBefore, `${stage}: a second M restores the drawer without changing the task`)
        await compositionIsolation(target,stage)
        await basicZoom(target, stage, 'editable')
        await panAndHide(target,stage,'editable')
        if(stage!=='A'){
          await cancelThenClick(target,stage)
          if(stage==='B')await activeResizeIsolation(target)
          await target.keyboard.press('g')
          const jump=target.getByRole('textbox',{name:stage==='B'?'跳转帧号':'修改项序号'})
          check(await jump.evaluate(el=>el===document.activeElement),`${stage}: G focuses the navigation input`)
          await jump.fill('1');await target.keyboard.press('Enter');await blur(target)
          await snapshot(target,stage,'navigation-ready')
          if(stage==='C'){
            const first=await target.locator('.c-change-item.selected').getAttribute('aria-label')
            await target.keyboard.press('ArrowRight');await target.locator('.confirmation-page[data-busy="false"]').waitFor()
            check(await target.locator('.c-change-item.selected').getAttribute('aria-label')!==first,'C: right arrow still browses the next modification')
            await target.keyboard.press('ArrowLeft');await target.locator('.confirmation-page[data-busy="false"]').waitFor()
            check(await target.locator('.c-change-item.selected').getAttribute('aria-label')===first,'C: left arrow returns without choosing A or B')
          }else{
            await target.keyboard.press('k')
            await target.getByRole('button',{name:'暂停',exact:true}).waitFor({timeout:2000})
            check(true,'B: K starts video playback')
            await target.keyboard.press('k')
            await target.getByRole('button',{name:'播放',exact:true}).waitFor({timeout:2000})
            check(true,'B: K pauses video playback')
            await target.keyboard.press('g');await jump.fill('1');await target.keyboard.press('Enter');await snapshot(target,stage,'return-first-frame');await closeDialog(target);await target.locator('.object-row').first().click();await blur(target)
          }
        }
        if(stage==='C'){
          const main=await snapshot(target,stage,'before-context-dialog')
          await target.locator('.c-context-title').getByRole('button',{name:'展开',exact:true}).click()
          const dialog=target.getByRole('dialog'),svg=dialog.locator('[data-variant="full"]')
          await svg.waitFor();await blur(target);await target.waitForTimeout(150)
          const initial=await svg.evaluate(el=>el.getBoundingClientRect().width)
          await target.keyboard.press('+');await target.waitForTimeout(150)
          check(await svg.evaluate(el=>el.getBoundingClientRect().width)>initial+1,'C: expanded context has working independent keyboard zoom')
          check(Math.abs((await snapshot(target,stage,'context-dialog-main-preserved')).width-main.width)<1,'C: expanded context zoom leaves the main viewport scale unchanged')
          const selected=await target.locator('.c-change-item.selected').getAttribute('aria-label')
          await target.keyboard.press('a');await target.keyboard.press('b');await target.keyboard.press('ArrowRight')
          check(await target.locator('.c-change-item.selected').getAttribute('aria-label')===selected,'C: expanded context isolates decision and item navigation shortcuts')
          await target.screenshot({path:`${output}/C-expanded-context-zoom.png`,animations:'disabled'})
          await target.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});await blur(target)
        }
        // Text editing and help modal must not leak shortcuts into the canvas.
        const input = stage === 'A' ? target.getByPlaceholder('查找素材…') : stage === 'B' ? target.getByRole('textbox', { name: '跳转帧号' }) : target.getByRole('textbox', { name: '修改项序号' })
        // A's search lives inside the collapsed media drawer. Focusing its
        // hidden DOM node does not move browser focus and would test body keys.
        const library = target.getByTestId('toggle-workbench-library')
        if (stage === 'A' && await library.getAttribute('aria-expanded') === 'false') await library.click()
        await input.waitFor({ state: 'visible' })
        const inputBefore=await input.inputValue()
        await input.focus()
        check(await input.evaluate(element => document.activeElement === element), `${stage}: shortcut isolation starts with the visible input actually focused`)
        const textBefore = await snapshot(target, stage, 'input-before')
        await target.keyboard.press('+'); await target.keyboard.press('-'); await target.keyboard.press('0'); await target.keyboard.press('f')
        check(Math.abs((await snapshot(target, stage, 'input-after')).width - textBefore.width) < 1, `${stage}: zoom and focus shortcuts stay inside text inputs`)
        await input.fill(inputBefore)
        if (stage === 'A') await library.click()
        await blur(target)
        await target.keyboard.press('?'); await target.getByRole('dialog').waitFor()
        const helpBefore = await snapshot(target, stage, 'help-before')
        await target.keyboard.press('+'); await target.keyboard.press('0'); await target.keyboard.press('f'); await target.keyboard.press('h')
        check(Math.abs((await snapshot(target, stage, 'help-after')).width - helpBefore.width) < 1, `${stage}: help dialog isolates underlying canvas shortcuts`)
        await target.keyboard.press('Escape'); await target.getByRole('dialog').waitFor({ state: 'hidden' })
        await blur(target)
        const normal = await snapshot(target, stage, 'before-focus')
        await target.keyboard.press('f')
        const focused = await snapshot(target, stage, 'focused')
        // Normal mode already dedicates almost all space to the canvas. Focus
        // must fill the window and enlarge it, not meet the old layout's 15%
        // gain (B/C retain their decision/submit inspector in both modes).
        check(await target.locator('.shell.is-focus').evaluate(element => {
          const rect = element.getBoundingClientRect()
          return Math.abs(rect.x) < 1 && Math.abs(rect.y) < 1 && Math.abs(rect.width - innerWidth) < 1 && Math.abs(rect.height - innerHeight) < 1
        }), `${stage}: F expands the workbench to all four window edges`)
        check(focused.viewport.width >= normal.viewport.width - 1 && focused.viewport.height >= normal.viewport.height - 1 && focused.viewport.width * focused.viewport.height > normal.viewport.width * normal.viewport.height + 1, `${stage}: F creates a larger working canvas without reducing either dimension`)
        await target.screenshot({ path: `${output}/${stage}-focused.png`, animations: 'disabled' })
        await basicZoom(target, stage, 'focused')
        await panAndHide(target,stage,'focused')
        await target.keyboard.press('Escape')
        const restored = await snapshot(target, stage, 'exit-focus')
        check(await target.locator('.shell.is-focus').count() === 0 && Math.abs(restored.viewport.width - normal.viewport.width) < 1 && Math.abs(restored.viewport.height - normal.viewport.height) < 1, `${stage}: Esc exits focus and restores both normal canvas dimensions without submitting or deciding`)
        await target.setViewportSize({width:1180,height:760})
        const dark=target.getByRole('button',{name:'切换深色主题',exact:true});if(await dark.count())await dark.click()
        await blur(target);await target.keyboard.press('0');await snapshot(target,stage,'dark-small-normal')
        const controls=target.getByRole('group',{name:'画面缩放',exact:true})
        const controlsVisible=()=>controls.evaluate(el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1})
        check(await controlsVisible(),`${stage}: image controls remain visible in a 1180px dark window`)
        await target.screenshot({path:`${output}/${stage}-1180-dark-normal.png`,animations:'disabled'})
        await target.keyboard.press('f');await snapshot(target,stage,'dark-small-focused')
        check(await controlsVisible(),`${stage}: focused image controls remain visible in a 1180px dark window`)
        await target.screenshot({path:`${output}/${stage}-1180-dark-focused.png`,animations:'disabled'})
        if(stage!=='A'){
          for(const [width,height] of [[760,760],[390,844]]){
            await target.setViewportSize({width,height});await snapshot(target,stage,`focus-narrow-${width}`)
            await target.screenshot({path:`${output}/${stage}-${width}-dark-focused.png`,fullPage:true,animations:'disabled'})
            const action=stage==='B'?target.getByTestId('submit-frame'):target.locator('.c-decision').getByRole('button',{name:'保留原标注 A',exact:false})
            check(await action.isVisible(),`${stage}: core ${stage==='B'?'submit':'confirmation'} operation remains reachable at ${width}px in focus`)
            await action.scrollIntoViewIfNeeded()
            check(await action.evaluate(el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1&&el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}),`${stage}: the ${width}px focused operation can be brought fully into view and hit directly`)
            await target.screenshot({path:`${output}/${stage}-${width}-dark-focused-action.png`,animations:'disabled'})
          }
        }
        await target.keyboard.press('Escape');await blur(target)
        check(await business(target) === before, `${stage}: viewing shortcuts preserve all A/B frames, revisions, review progress and C decisions`)
        if (stage !== 'A') {
          const readonly = await open(stage, true)
          try { await basicZoom(readonly, stage, 'readonly');await panAndHide(readonly,stage,'readonly') } finally { await readonly.close() }
          check(await business(target) === before, `${stage}: another account can zoom without changing claimed work`)
        }
      } finally { target.off('request', record); await target.close() }
    }
    check(errors.length === 0, 'shared shortcut workflows have no browser runtime errors')
    check(mutations.length === 0, 'viewing shortcuts never send annotation/review drafts, submissions or C decisions')
  } finally { fs.writeFileSync(`${output}/observations.json`, JSON.stringify({ observations, errors, mutations }, null, 2)) }
}
