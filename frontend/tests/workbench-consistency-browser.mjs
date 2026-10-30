// Shared workbench and progressive disclosure acceptance; run with disposable B/C/UX fixtures.
// Uses real video frames, draft writes and C decisions. See docs/TESTING.md.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { reviewZoomChecks } from './review-zoom-checks.mjs'
import { workbenchShortcutsChecks } from './workbench-shortcuts-checks.mjs'
import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
// The task list is a drawer that stays closed while a task is open.
const openTask=async(target,selector)=>{const card=target.locator(selector),toggle=target.getByTestId('toggle-workbench-library');for(let i=0;i<40;i++){if(await card.isVisible()){try{await card.click({timeout:1500});return}catch{}}else if(await toggle.isVisible())await toggle.click();await target.waitForTimeout(250)}await card.click()}
// The media list is a drawer that stays closed while a video is open.
const openAsset=async(target,locator)=>{const toggle=target.getByTestId('toggle-workbench-library');for(let i=0;i<40;i++){if(await locator.isVisible()){try{await locator.click({timeout:1500});return}catch{}}else if(await toggle.isVisible())await toggle.click();await target.waitForTimeout(250)}await locator.click()}
const confirmation = JSON.parse(fs.readFileSync('work/confirmation-browser-fixture.json'))
const review = JSON.parse(fs.readFileSync('work/browser-fixture.json'))
const origin = process.env.CONFIRMATION_ORIGIN || 'http://127.0.0.1:5373'
const browser = await chromium.launch({ channel: process.env.CHROME_CHANNEL || 'chrome', headless: true, ignoreDefaultArgs: ['--hide-scrollbars'] })
const page = await browser.newPage({ viewport: { width: 1366, height: 768 } })
if (process.env.LEGACY_BROWSER === '1') await useLegacyBrowserAPIs(page)
const logs = [], errors = [], libraryHits = []
let checks = 0
page.on('console', message => logs.push({ type: message.type(), text: message.text() }))
page.on('pageerror', error => errors.push(String(error)))
const check = (value, message) => { assert(value, message); console.log('PASS', ++checks, message) }
const button = name => page.getByRole('button', { name, exact: true })
const cIdle = () => page.locator('.confirmation-page[data-busy="false"]').waitFor()
const bIdle = () => page.waitForFunction(() => !document.querySelector('[data-testid="submit-frame"]')?.textContent.includes('处理中'))
const inView = locator => locator.evaluate(element => {
  const rect = element.getBoundingClientRect()
  // The 48px header now carries page context; occlusion is checked by elementFromPoint.
  return rect.top >= 0 && rect.bottom <= innerHeight + 1 && rect.width > 0 && element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2))
})
const api = async (path, token) => {
  const response = await page.request.get(origin + '/api' + path, { headers: { Authorization: 'Bearer ' + token } })
  assert(response.ok(), await response.text())
  return response.json()
}
const frame = () => api(`/review/sessions/${review.sid}/frames/0`, review.token)
const cSession = (sid = confirmation.main.sid) => api(`/confirmation/sessions/${sid}`, confirmation.token)
const closeReviewDialog = async () => {
  if (await page.getByRole('dialog').count()) {
    await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
    await page.getByRole('dialog').waitFor({ state: 'hidden' })
  }
}
async function openReview(expectCollapsed = false) {
  await page.goto(origin + '/review')
  await expandLibrary('review', expectCollapsed)
  await openTask(page,`[data-session-id="${review.sid}"]`)
  await page.locator('.review-drawing').waitFor()
  await bIdle()
  await closeReviewDialog()
}
async function openConfirmation(sid = confirmation.main.sid, expectCollapsed = false) {
  if (new URL(page.url()).pathname !== '/confirm') await page.goto(origin + '/confirm')
  await expandLibrary('confirmation', expectCollapsed)
  await cIdle()
  await openTask(page,`[data-session-id="${sid}"]`)
  await cIdle()
  if (await button('留在上次位置').count()) await button('留在上次位置').click()
}
async function expandLibrary(stage, expectCollapsed = false) {
  const toggle = page.getByTestId('toggle-workbench-library')
  await toggle.waitFor()
  // Entering a stage with no task open shows its list so a task can be picked;
  // the list closes again once a task opens (checked by the open helpers).
  if (expectCollapsed) check(await toggle.getAttribute('aria-expanded') === 'true' && await page.locator('.shell-drawer').isVisible(), `${stage}: entering a stage without an open task shows its video list`)
  if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click()
}
async function selectTheme(dark) {
  const next = button(dark ? '切换深色主题' : '切换浅色主题')
  if (await next.count()) await next.click()
}
async function contextLayout(width, height, label) {
  const full = page.locator('.c-context [data-variant=full]')
  const context = await page.locator('.c-context').boundingBox(), local = await page.locator('.c-local-comparison').boundingBox()
  check(context.x + context.width <= local.x + 2 && context.width > local.width && await inView(full), `${label}: the full frame occupies the main left view beside the A/B comparison`)
  const readSvg = svg => {
    const view = svg.viewBox.baseVal, m = svg.getScreenCTM(), r = svg.getBoundingClientRect(), host = (svg.closest('.c-context') || svg.closest('figure')).getBoundingClientRect()
    const scaleX = Math.hypot(m.a, m.b), scaleY = Math.hypot(m.c, m.d)
    const points = [[view.x, view.y], [view.x + view.width, view.y], [view.x, view.y + view.height], [view.x + view.width, view.y + view.height]].map(([x, y]) => new DOMPoint(x, y).matrixTransform(m))
    return { width: view.width, height: view.height, scaleX, scaleY, renderedWidth: view.width * scaleX, renderedHeight: view.height * scaleY, contained: points.every(p => p.x >= Math.max(r.left, host.left, 0) - 1 && p.x <= Math.min(r.right, host.right, innerWidth) + 1 && p.y >= Math.max(r.top, host.top, 72) - 1 && p.y <= Math.min(r.bottom, host.bottom, innerHeight) + 1) }
  }
  const geometry = await full.evaluate(readSvg)
  const crops = await Promise.all((await page.locator('.c-crops svg').all()).map(svg => svg.evaluate(readSvg)))
  const mainReadable = width > height ? geometry.renderedWidth >= 380 && geometry.renderedHeight >= 210 : geometry.renderedHeight >= 280
  check(mainReadable && crops.length === 2 && crops.every(crop => crop.renderedHeight >= 110 && crop.contained), `${label}: actual source pixels occupy a useful size in the full frame and both A/B images (${geometry.renderedWidth.toFixed(0)}×${geometry.renderedHeight.toFixed(0)}; crops ${crops.map(crop => crop.renderedHeight.toFixed(0)).join('/')})`)
  check(geometry.width === width && geometry.height === height && Math.abs(geometry.scaleX - geometry.scaleY) < .0001 && geometry.contained, `${label}: all four source corners are visible with equal horizontal and vertical scale`)
}
async function layout(stage, progress, core) {
  for (const size of [{ width: 1366, height: 768 }, { width: 1180, height: 760 }]) {
    await page.setViewportSize(size)
    for (const dark of [false, true]) {
      await selectTheme(dark)
      const rail = await page.locator('.shell-rail').boundingBox()
      const canvas = await page.locator('[data-workbench-region=canvas]').boundingBox()
      const inspector = await page.locator('[data-workbench-region=inspector]').boundingBox()
      check(rail && canvas && inspector && rail.x + rail.width <= canvas.x + 2 && canvas.x + canvas.width <= inspector.x + 2,
        `${stage} ${size.width} ${dark ? 'dark' : 'light'}: list rail, image and operations keep the same left-to-right structure`)
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${stage}: no horizontal page overflow`)
      check(await inView(core), `${stage}: core operation is visible without scrolling`)
      if (progress) {
        const summary = page.getByTestId('video-progress')
        const box = await summary.boundingBox()
        check(await inView(summary) && box.y + box.height <= canvas.y + 2 && (await summary.getAttribute('title') !== null || (await summary.innerText()).length > 0), `${stage}: ${progress} stays in the header above the workspace`)
      }
      if (stage === 'confirmation') await contextLayout(800, 450, `landscape ${size.width} ${dark ? 'dark' : 'light'}`)
      await page.screenshot({ path: `output/playwright/workbench-${stage}-${size.width}-${dark ? 'dark' : 'light'}.png`, animations: 'disabled' })
    }
  }
  await selectTheme(false)
  await page.setViewportSize({ width: 1366, height: 768 })
}
async function assertGuideIsolation(stage, before, after, keys) {
  await button('使用说明').click()
  await page.getByRole('dialog', { name: '使用说明', exact: true }).waitFor()
  await page.locator('.user-guide-content').focus()
  for (const key of keys) await page.keyboard.press(key)
  check(JSON.stringify(await after()) === JSON.stringify(before), `${stage}: help keyboard input does not change the underlying work`)
  await page.keyboard.press('Escape')
  check(await button('使用说明').evaluate(element => document.activeElement === element), `${stage}: closing help restores focus to its launcher`)
}
const readablePixels = locator => locator.evaluate(svg => {
  const view = svg.viewBox.baseVal, matrix = svg.getScreenCTM()
  return { width: view.width * Math.hypot(matrix.a, matrix.b), height: view.height * Math.hypot(matrix.c, matrix.d) }
})
const inViewport = locator => locator.evaluate(element => {
  const rect = element.getBoundingClientRect()
  return rect.top >= 0 && rect.bottom <= innerHeight && rect.left >= 0 && rect.right <= innerWidth && rect.width > 0 && rect.height > 0 && element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2))
})
async function narrowLibrary(stage, list, size, target) {
  await page.setViewportSize(size)
  const toggle = page.getByTestId('toggle-workbench-library')
  if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click()
  const listBox = await list.boundingBox()
  check(listBox && listBox.height >= 64, `${stage} ${size.width}: expanded video library retains a usable scrollable list below its filters`)
  await toggle.click()
  check(!await page.locator('.shell-drawer').isVisible() && await page.locator('[data-workbench-region=canvas]').isVisible(),
    `${stage} ${size.width}: collapsing the list returns the whole area to the image`)
  await toggle.click()
  check(await list.isVisible() && await toggle.getAttribute('aria-expanded') === 'true', `${stage} ${size.width}: expanding restores video navigation`)
  const reorderedProgress=false
  await target.scrollIntoViewIfNeeded()
  const hit=await target.evaluate(element => {
    const rect = element.getBoundingClientRect(), list = element.closest('.asset-list,.review-videos,.c-video-list').getBoundingClientRect()
    const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2
    const target=document.elementFromPoint(x,y),progress=document.querySelector('[data-testid=video-progress]'),pr=progress?.getBoundingClientRect(),box=r=>({x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right})
    return {passed:rect.width > 40 && rect.height >= 32 && x > Math.max(0, list.left) && x < Math.min(innerWidth, list.right) && y > Math.max(0, list.top) && y < Math.min(innerHeight, list.bottom) && element.contains(target),
      scrollY,target:box(rect),list:box(list),progress:pr?box(pr):null,progressPosition:progress?getComputedStyle(progress).position:null,
      hit:target?{tag:target.tagName,text:target.textContent?.slice(0,100),inProgress:!!target.closest('[data-testid=video-progress]')}:null}
  })
  libraryHits.push({stage,width:size.width,reorderedProgress,...hit})
  check(hit.passed, `${stage} ${size.width}: an actual video entry is visible and hit-testable inside the list`)
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${stage} ${size.width}: header and workbench do not cause horizontal page overflow`)
}
async function narrowGuide(stage, readState) {
  for (const dark of [false, true]) {
    await selectTheme(dark)
    const before = JSON.stringify(await readState())
    await button('使用说明').click()
    const dialog = page.getByRole('dialog', { name: '使用说明', exact: true })
    await dialog.waitFor()
    const geometry = await dialog.evaluate(element => {
      const box = element.getBoundingClientRect(), nav = element.querySelector('.user-guide-nav').getBoundingClientRect(), content = element.querySelector('.user-guide-content'), text = content.getBoundingClientRect()
      return { contained: box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight, stacked: nav.bottom <= text.top + 2, textWidth: text.width, textHeight: text.height, font: parseFloat(getComputedStyle(content).fontSize), noTextOverflow: content.scrollWidth <= content.clientWidth + 1, focusWithin: element.contains(document.activeElement), focusCount: element.querySelectorAll('button:not(:disabled),input,[tabindex="0"]').length }
    })
    check(geometry.contained && geometry.stacked && geometry.textWidth >= 280 && geometry.textHeight >= 200 && geometry.font >= 12 && geometry.noTextOverflow, `${stage} 390 ${dark ? 'dark' : 'light'}: help is fully contained with readable body below its contents navigation`)
    check(geometry.focusWithin, `${stage} 390: opening help places keyboard focus inside its modal`)
    for (let count = 0; count < geometry.focusCount + 2; count++) {
      await page.keyboard.press('Tab')
      assert(await dialog.evaluate(element => element.contains(document.activeElement)), `${stage}: Tab escaped the help dialog`)
    }
    await page.locator('.user-guide-content').focus()
    for (const key of ['a', 'b', 'Control+Enter', 'ArrowRight', 'Delete', 'f']) await page.keyboard.press(key)
    check(JSON.stringify(await readState()) === before, `${stage} 390: help keyboard input cannot change frame, object, submission or decisions`)
    await page.screenshot({ path: `output/playwright/workbench-help-${stage}-390-${dark ? 'dark' : 'light'}.png`, animations: 'disabled' })
    await page.keyboard.press('Escape')
    check(!await dialog.isVisible() && await button('使用说明').evaluate(element => element === document.activeElement), `${stage} 390: Escape closes help and restores its launcher focus`)
  }
  await selectTheme(false)
}
async function narrowWorkbenchChecks() {
  const desktop = { width: 1366, height: 768 }
  const sizes = [{ width: 760, height: 900 }, { width: 390, height: 844 }]
  await page.setViewportSize(desktop)
  await page.goto(origin + '/annotate')
  await expandLibrary('annotation')
  await openAsset(page,page.locator('.asset-card').filter({ hasText: 'ux-video.avi' }))
  await page.locator('.object-select').first().waitFor()
  await page.waitForFunction(() => !document.querySelector('.canvas-status') && document.querySelector('.exact-media')?.complete)
  const annotationFrame = await page.getByRole('spinbutton', { name: '跳转帧号' }).inputValue()
  for (const size of sizes) {
    const video = page.locator('.asset-card').filter({ hasText: 'ux-video.avi' })
    await narrowLibrary('annotation', page.locator('.asset-list'), size, video)
    await video.click()
    await page.locator('.object-select').first().focus(); await page.keyboard.press('Enter')
    check(await page.locator('.object-select').first().getAttribute('aria-pressed') === 'true', `annotation ${size.width}: keyboard selection remains available below the image`)
    check(await page.locator('.object-select').first().evaluate(element => parseFloat(getComputedStyle(element).fontSize) >= 12), `annotation ${size.width}: object IDs keep a readable text size`)
    if (size.width === 760) check((await page.locator('.annotation-stage').boundingBox()).width > 350, 'annotation 760: source image is wider than the former 327px canvas column')
    await button('专注').click()
    check((await page.locator('[data-workbench-region=canvas]').boundingBox()).width >= size.width - 24 && await inViewport(page.getByRole('button', { name: /退出专注/ })), `annotation ${size.width}: focus uses the window width and keeps its exit visible`)
    await page.getByRole('button', { name: /退出专注/ }).click()
    check(await page.getByRole('spinbutton', { name: '跳转帧号' }).inputValue() === annotationFrame && await page.locator('.object-select').first().getAttribute('aria-pressed') === 'true', `annotation ${size.width}: leaving focus restores the same frame and object`)
    if (size.width === 390) await narrowGuide('annotation', async () => ({ frame: await page.getByRole('spinbutton', { name: '跳转帧号' }).inputValue(), selected: await page.locator('.object-select[aria-pressed=true]').getAttribute('aria-label'), focused: await page.locator('.shell-annotate').evaluate(element => element.classList.contains('is-focus')) }))
    await page.screenshot({ path: `output/playwright/workbench-annotation-narrow-${size.width}.png`, animations: 'disabled', fullPage: true })
  }
  await page.setViewportSize(desktop); await page.evaluate(() => scrollTo(0, 0))
  check(await inView(button('首尾双向追踪')), 'annotation: desktop core action recovers after narrow layouts and focus')

  await openReview()
  const reviewBefore = await api(`/review/sessions/${review.sid}`, review.token)
  for (const size of sizes) {
    const video = page.locator(`[data-session-id="${review.sid}"]`)
    await narrowLibrary('review', page.locator('.review-videos'), size, video)
    await video.click(); await bIdle(); await closeReviewDialog()
    await page.locator('[data-row="2"]').focus(); await page.keyboard.press('Enter')
    check(await page.locator('[data-box="2"]').evaluate(element => element.classList.contains('chosen')), `review ${size.width}: keyboard list selection still locates the matching box`)
    check(await page.locator('[data-row="2"] b').evaluate(element => parseFloat(getComputedStyle(element).fontSize) >= 12), `review ${size.width}: object IDs keep a readable text size`)
    await button('适应画面').click()
    if (size.width === 760) check((await readablePixels(page.locator('.review-drawing'))).width > 350, 'review 760: actual source pixels are wider than the former 327px canvas column')
    await button('下一帧').click(); await bIdle()
    check(await page.getByRole('textbox', { name: '跳转帧号' }).inputValue() === '2', `review ${size.width}: image navigation reaches the next real frame`)
    await button('上一帧').click(); await bIdle()
    // The earlier real edit intentionally left a draft on frame 1. Returning
    // normally presents its resume prompt; dismiss it without discarding or
    // submitting before exercising the next responsive layout.
    await closeReviewDialog()
    check(await page.getByRole('textbox', { name: '跳转帧号' }).inputValue() === '1', `review ${size.width}: navigation returns to the original frame`)
    check((await api(`/review/sessions/${review.sid}`, review.token)).progress.submittedFrames === reviewBefore.progress.submittedFrames, `review ${size.width}: resize, selection and navigation do not submit frames`)
    if (size.width === 390) await narrowGuide('review', async () => ({ frame: await page.getByRole('textbox', { name: '跳转帧号' }).inputValue(), submitted: (await api(`/review/sessions/${review.sid}`, review.token)).progress.submittedFrames, objects: (await frame()).effectiveObjects }))
    await page.screenshot({ path: `output/playwright/workbench-review-narrow-${size.width}.png`, animations: 'disabled', fullPage: true })
  }
  await page.setViewportSize(desktop); await page.evaluate(() => scrollTo(0, 0))
  check(await inView(page.getByTestId('submit-frame')) && await inView(page.getByTestId('video-progress')), 'review: desktop progress and submission recover after narrow navigation')

  await openConfirmation(confirmation.main.sid)
  const decisionsBefore = JSON.stringify((await api(`/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)).items)
  for (const size of sizes) {
    const video = page.locator(`[data-session-id="${confirmation.main.sid}"]`)
    await narrowLibrary('confirmation', page.locator('.c-video-list'), size, video)
    await video.click(); await cIdle()
    if (await button('留在上次位置').count()) await button('留在上次位置').click()
    const full = await page.locator('.c-context').boundingBox(), local = await page.locator('.c-local-comparison').boundingBox()
    const crops = await Promise.all((await page.locator('.c-crops figure').all()).map(figure => figure.boundingBox()))
    check(full && local && full.y + full.height <= local.y + 2 && crops.length === 2 && Math.abs(crops[0].y - crops[1].y) <= 2 && crops[0].x + crops[0].width <= crops[1].x + 2,
      `confirmation ${size.width}: complete frame sits above side-by-side A/B crops`)
    if (size.width === 760) check((await readablePixels(page.locator('.c-context [data-variant=full]'))).width > 327, 'confirmation 760: actual full-frame pixels improve on the former 327px canvas column')
    const object = page.locator('.c-context .context-boxes [data-object-id="2"]')
    await object.focus(); await page.keyboard.press('Enter'); await cIdle()
    check((await page.getByTestId('current-item').innerText()).includes('对象 2'), `confirmation ${size.width}: keyboard full-frame selection links the same comparison object`)
    const current = await page.getByRole('textbox', { name: '修改项序号' }).inputValue()
    check(await page.locator('.shell-bottom .c-queue-nav').evaluate(element =>
      [...element.querySelectorAll('button,input')].every(control => {
        const rect = control.getBoundingClientRect()
        return rect.width > 0 && rect.left >= 0 && rect.right <= innerWidth
      })), `confirmation ${size.width}: all item navigation controls fit without horizontal scrolling`)
    await button('下一修改项 →').click(); await cIdle(); await button('← 上一修改项').click(); await cIdle()
    check(await page.getByRole('textbox', { name: '修改项序号' }).inputValue() === current, `confirmation ${size.width}: modification navigation returns to the same item`)
    check(JSON.stringify((await api(`/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)).items) === decisionsBefore, `confirmation ${size.width}: responsive layout and keyboard browsing preserve all saved choices`)
    check(await page.locator('.c-change-item').first().evaluate(element => parseFloat(getComputedStyle(element).fontSize) >= 12), `confirmation ${size.width}: modification names keep a readable text size`)
    if (size.width === 390) await narrowGuide('confirmation', async () => ({ current: await page.getByTestId('current-item').innerText(), choices: (await api(`/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)).items }))
    await page.screenshot({ path: `output/playwright/workbench-confirmation-narrow-${size.width}.png`, animations: 'disabled', fullPage: true })
  }
  await page.setViewportSize(desktop); await page.evaluate(() => scrollTo(0, 0))
  check(await inView(page.getByTestId('choose-a')) && await inView(page.getByTestId('choose-b')) && await inView(page.getByTestId('video-progress')), 'confirmation: desktop comparison controls and progress recover after narrow browsing')
}

try {
  fs.mkdirSync('output/playwright', { recursive: true })
  await page.addInitScript(({ confirmation, review }) => {
    const reviewing = location.pathname === '/review'
    localStorage.setItem('rare-sperm-token', (reviewing ? review : confirmation).token)
    localStorage.setItem('rare-sperm-auth', JSON.stringify({ id: reviewing ? '2' : '3', name: reviewing ? 'review-B' : 'confirm-C', role: 'annotator' }))
  }, { confirmation, review })
  await page.route('**/api/track/media', async route => {
    const response = await route.fetch(), data = await response.json()
    data.items = data.items.filter(media => media.mediaId.startsWith('ux-'))
    await route.fulfill({ response, json: data })
  })

  await page.goto(origin + '/annotate')
  await expandLibrary('annotation')
  await openAsset(page,page.locator('.asset-card').filter({ hasText: 'ux-video.avi' }))
  await page.evaluate(async () => {
    const url = performance.getEntriesByType('resource').find(resource => resource.name.includes('/src/stores/workspace.ts')).name
    window.ws = (await import(url)).useWorkspace()
  })
  await page.waitForFunction(() => window.ws && !window.ws.workspaceRestoring.value && !window.ws.exactFrameLoading.value && window.ws.currentObjects.value.length === 2)
  await layout('annotation', null, button('首尾双向追踪'))
  await page.locator('.object-select').first().click()
  await page.locator('.object-select').nth(1).focus(); await page.keyboard.press(' ')
  check(await page.locator('.object-select').nth(1).getAttribute('aria-pressed') === 'true', 'Space activates an object chip like a native button')
  await page.setViewportSize({ width: 1180, height: 760 })
  if (await page.getByTestId('toggle-workbench-library').getAttribute('aria-expanded') === 'true') await page.getByTestId('toggle-workbench-library').click()
  await button('专注').click()
  check((await page.locator('[data-workbench-region=canvas]').boundingBox()).width > 1100 && !await page.locator('[data-workbench-region=library]').isVisible() && !await page.locator('[data-workbench-region=inspector]').isVisible(), 'compact focus uses the whole width even when the library was collapsed')
  await page.getByRole('button', { name: /退出专注/ }).click()
  check(await page.getByTestId('toggle-workbench-library').getAttribute('aria-expanded') === 'false', 'leaving focus retains the collapsed library preference')
  await page.getByTestId('toggle-workbench-library').click()
  await page.setViewportSize({ width: 1366, height: 768 })
  const annotationBefore = await page.evaluate(() => ({ frame: window.ws.currentFrame.value, count: window.ws.currentObjects.value.length, focused: !!document.querySelector('.shell-annotate.is-focus') }))
  await assertGuideIsolation('annotation', annotationBefore, () => page.evaluate(() => ({ frame: window.ws.currentFrame.value, count: window.ws.currentObjects.value.length, focused: !!document.querySelector('.shell-annotate.is-focus') })), ['f', 'b', 'Delete'])

  const beforeCollapse = await page.locator('[data-workbench-region=canvas]').boundingBox()
  await page.getByTestId('toggle-workbench-library').click()
  check((await page.locator('[data-workbench-region=canvas]').boundingBox()).width > beforeCollapse.width, 'collapsing the library gives its space to the image workspace')
  await openReview(true)
  await reviewZoomChecks({ page, fixture: review, origin, check, requireSessionSwitch: true })
  await layout('review', '视频审查进度', page.getByTestId('submit-frame'))
  check(await page.locator('[data-row]').count() === 2, 'review defaults to all current-frame objects')
  check(await page.locator('.stats-grid,.review-table').count() === 0, 'review no longer repeats a statistics card and full geometry table')
  const position = await page.evaluate(() => [scrollX, scrollY])
  await page.locator('[data-row="1"]').click()
  check(await page.locator('[data-row="1"]').evaluate(element => element.classList.contains('selected')) && await page.locator('[data-box="1"]').evaluate(element => element.classList.contains('chosen')), 'list and image select the same review object')
  check(JSON.stringify(await page.evaluate(() => [scrollX, scrollY])) === JSON.stringify(position), 'selecting an object does not scroll the whole page')
  check(!await page.locator('.review-object-details').evaluate(element => element.open), 'review geometry and paired crops start folded')
  await page.locator('.review-object-details > summary').focus(); await page.keyboard.press(' ')
  check(await page.locator('.review-object-details').evaluate(element => element.open) && await button('播放').count() === 1, 'Space on review details expands them without starting video playback')
  const crops = page.locator('.compare-pair svg')
  check(await crops.count() === 2 && await crops.nth(0).getAttribute('viewBox') === await crops.nth(1).getAttribute('viewBox'), 'expanded review A/B crops share the exact same source region')
  await page.locator('.review-object-details > summary').click()
  // Use the existing real draft gesture at an enlarged manual scale. This
  // protects pointer-to-source conversion as well as the display-only checks.
  const sourceBeforeZoomDrag = await frame()
  const getReviewScale = () => page.locator('.review-drawing').evaluate(svg => Number(svg.getAttribute('width')) / svg.viewBox.baseVal.width)
  while (await getReviewScale() < 1.2) await button('放大画面').click()
  await page.waitForTimeout(250)
  const dragScale = await getReviewScale()
  let rect = await page.locator('[data-box="1"] rect').boundingBox()
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2)
  await page.mouse.down(); await page.mouse.move(rect.x + rect.width / 2 + 14, rect.y + rect.height / 2 + 4, { steps: 4 }); await page.mouse.up(); await bIdle()
  const draggedFrame = await frame()
  const beforeBox = sourceBeforeZoomDrag.effectiveObjects.find(object => object.objectId === 1).bbox
  const afterBox = draggedFrame.effectiveObjects.find(object => object.objectId === 1).bbox
  check(dragScale >= 1.2 && Math.abs(afterBox[0] - beforeBox[0] - 14 / dragScale) < .02 && Math.abs(afterBox[1] - beforeBox[1] - 4 / dragScale) < .02, 'manual enlargement preserves exact screen-to-source coordinates when saving a real review drag')
  await button('↶ 撤销').click(); await bIdle()
  check(JSON.stringify((await frame()).effectiveObjects) === JSON.stringify(sourceBeforeZoomDrag.effectiveObjects), 'undo after an enlarged review drag restores the real server geometry')
  rect = await page.locator('[data-box="1"] rect').boundingBox()
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2)
  await page.mouse.down(); await page.mouse.move(rect.x + rect.width / 2 + 14, rect.y + rect.height / 2 + 4, { steps: 4 }); await page.mouse.up(); await bIdle()
  await button('适应画面').click()
  check((await frame()).hasDraft && (await api(`/review/sessions/${review.sid}`, review.token)).progress.submittedFrames === 0, 'editing from the compact workbench saves a draft without submitting')
  await button('仅已调整').click()
  check(await page.locator('[data-row]').count() === 1 && await page.locator('[data-row="1"]').count() === 1, 'only-adjusted filter keeps the modified object')
  await page.locator('.object-filter-options').getByRole('button', { name: '全部', exact: true }).click()
  check(await page.locator('[data-row]').count() === 2, 'all-object navigation remains available after filtering')
  const reviewState = async () => ({ revision: (await frame()).frameRevision, frame: await page.getByRole('textbox', { name: '跳转帧号' }).inputValue(), submitted: (await api(`/review/sessions/${review.sid}`, review.token)).progress.submittedFrames })
  await assertGuideIsolation('review', await reviewState(), reviewState, ['Control+Enter', 'ArrowRight', ' '])

  await page.getByTestId('toggle-workbench-library').click()
  const longName = '高倍率微流控视频_长文件名布局测试_'.repeat(24) + '.avi'
  await page.route('**/api/confirmation/sessions/**', async route => {
    if (!new URL(route.request().url()).pathname.startsWith('/api/confirmation/sessions/' + confirmation.main.sid)) return route.continue()
    const response = await route.fetch(), data = await response.json()
    if (data.media) data.media.name = longName
    if (data.session?.media) data.session.media.name = longName
    await route.fulfill({ response, json: data })
  })
  await openConfirmation(confirmation.main.sid, true)
  await button('领取并开始确认').click(); await cIdle()
  await layout('confirmation', '视频确认进度', page.getByTestId('choose-a'))
  check(await page.locator('.shell-context strong').first().getAttribute('title') === longName && await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'a long video name remains available in its tooltip without widening the workbench')
  check(await inView(page.getByTestId('choose-b')), 'both A and B choices are visible on the first screen')
  check(await page.locator('.c-frame-table,.c-queue-items').count() === 0, 'confirmation has one unified change navigation instead of duplicate table and numbered queue')
  check(await page.locator('.c-details,.c-metrics').count() === 0 && await page.getByText('查看几何详情', { exact: false }).count() === 0, 'confirmation removes the unused numeric geometry panel and its launcher')
  check(await page.locator('[data-variant="full"]').getAttribute('viewBox') === '0 0 800 450', 'full-frame context keeps all source pixels')
  check(await inView(button('← 上一修改项')) && await inView(button('下一修改项 →')), 'change navigation remains below the image and visible without scrolling')
  const decisionsBeforeContext = (await api(`/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)).items
  const contextObject = id => page.locator(`.c-context .context-boxes [data-object-id="${id}"]`)
  await contextObject(2).click(); await cIdle()
  const expectedObject = decisionsBeforeContext.find(item => item.frameIndex === 0 && item.objectId === 2)
  const displayedPair = await page.locator('.c-crops svg').evaluateAll(svgs => svgs.map(svg => { const box = svg.querySelector('.confirm-box-a,.confirm-box-b'); return ['x', 'y', 'width', 'height'].map(key => Number(box.getAttribute(key))) }))
  const expectedPair = [expectedObject.beforeBbox, expectedObject.afterBbox].map(box => [box[0], box[1], box[2] - box[0], box[3] - box[1]])
  check((await page.getByTestId('current-item').innerText()).includes('对象 2') && (await page.locator('.c-change-item[aria-current=true]').innerText()).includes('对象 2') && JSON.stringify(displayedPair) === JSON.stringify(expectedPair), 'clicking the main full frame links the same object to navigation and both A/B crops')
  await contextObject(1).focus(); await page.keyboard.press('Enter'); await cIdle()
  check((await page.getByTestId('current-item').innerText()).includes('对象 1'), 'Enter on a selectable full-frame object locates it')
  await contextObject(2).focus(); await page.keyboard.press(' '); await cIdle()
  check((await page.getByTestId('current-item').innerText()).includes('对象 2'), 'Space on a selectable full-frame object locates it')
  check(JSON.stringify((await api(`/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)).items) === JSON.stringify(decisionsBeforeContext), 'full-frame pointer and keyboard navigation never save a choice or geometry')
  await page.getByRole('textbox', { name: '修改项序号' }).fill('3'); await button('前往').click(); await cIdle()
  const unchanged = contextObject(2)
  check(await unchanged.getAttribute('role') !== 'button' && await unchanged.getAttribute('tabindex') !== '0', 'unchanged objects remain context without misleading keyboard actions')
  const unchangedBox = await unchanged.boundingBox()
  await page.mouse.click(unchangedBox.x + unchangedBox.width / 2, unchangedBox.y + unchangedBox.height / 2)
  check((await page.locator('.c-step-nav').innerText()).includes('修改项 3 / 3') && JSON.stringify((await api(`/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)).items) === JSON.stringify(decisionsBeforeContext), 'clicking an unchanged object preserves the current modification and all decisions')
  await page.getByRole('textbox', { name: '修改项序号' }).fill('1'); await button('前往').click(); await cIdle()
  await page.locator('.c-context').getByRole('button', { name: '展开', exact: true }).click()
  await page.getByRole('dialog', { name: '完整帧上下文' }).waitFor()
  check(await page.getByRole('dialog').locator('[data-variant=full]').getAttribute('viewBox') === '0 0 800 450', 'expanded context remains the complete source frame')
  await page.getByRole('dialog', { name: '完整帧上下文' }).getByRole('button', { name: '关闭', exact: true }).focus(); await page.keyboard.press('Tab')
  check(await page.getByRole('dialog').evaluate(dialog => dialog.contains(document.activeElement) && document.activeElement.matches('svg [role=button][tabindex="0"]')), 'expanded-context Tab cycle includes its selectable SVG objects')
  await page.keyboard.press('Shift+Tab')
  check(await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).evaluate(button => button === document.activeElement), 'expanded-context reverse Tab returns to Close without leaving the modal')
  await page.keyboard.press('b')
  check((await cSession()).progress.decided === 0, 'expanded frame dialog isolates decision shortcuts')
  let unavailableImageReads = 0
  await page.route('**/api/track/frame/*/0', route => { unavailableImageReads++; return route.fulfill({ status: 503, json: { message: 'Injected context image failure' } }) })
  await page.getByRole('dialog', { name: '完整帧上下文' }).locator('.context-boxes [data-object-id="2"]').click()
  const contextDialog = page.getByRole('dialog', { name: '完整帧上下文' })
  await cIdle()
  check(unavailableImageReads === 0 && !await contextDialog.getByRole('alert').count() && (await cSession()).progress.decided === 0, 'same-frame context reuses its loaded image during a frame endpoint outage without making decisions')
  await page.unroute('**/api/track/frame/*/0')
  check(await contextDialog.locator('[data-variant=full]').count() === 1 && (await page.getByTestId('current-item').innerText()).includes('对象 2'), 'cached modal navigation retains the actual selected object and full frame')
  const cursorKeys = []
  await page.route('**/api/confirmation/sessions/*/cursor', async route => {
    cursorKeys.push(route.request().headers()['idempotency-key'])
    if (cursorKeys.length === 1) { await route.fetch(); await route.abort('failed') } else await route.continue()
  })
  await contextDialog.locator('.context-boxes [data-object-id="1"]').click(); await cIdle()
  await contextDialog.getByRole('alert').waitFor()
  check(await contextDialog.getByRole('button', { name: '重试原请求', exact: true }).isVisible() && !await contextDialog.getByRole('button', { name: '重试加载', exact: true }).count(), 'a lost cursor response exposes the original-request retry inside the context modal')
  const committedCursor = (await cSession()).resume.cursorRevision
  await contextDialog.getByRole('button', { name: '重试原请求', exact: true }).click(); await cIdle()
  check(cursorKeys.length === 2 && cursorKeys[0] === cursorKeys[1] && (await cSession()).resume.cursorRevision === committedCursor && JSON.stringify((await api(`/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)).items) === JSON.stringify(decisionsBeforeContext), 'context cursor recovery replays the same key without a duplicate revision or decision')
  await page.unroute('**/api/confirmation/sessions/*/cursor')
  await page.locator('.c-change-item').first().click(); await cIdle()
  const fullBox = await page.locator('[data-variant="full"]').getAttribute('viewBox')
  await page.getByRole('slider', { name: '局部缩放' }).fill('8')
  check(await page.locator('[data-variant="full"]').getAttribute('viewBox') === fullBox, 'local zoom leaves the full-frame context intact')
  await page.getByRole('slider', { name: '局部缩放' }).fill('5')
  await page.getByRole('checkbox').uncheck()
  await page.getByTestId('choose-b').click(); await cIdle()
  check((await cSession()).progress.decided === 1, 'choosing B still saves one decision immediately')
  await page.locator('.c-change-filters').getByRole('button', { name: /^已确认 / }).click()
  check(await page.locator('.c-change-item').count() === 1, 'chosen filter shows only the saved decision')
  await page.locator('.c-change-filters').getByRole('button', { name: /^待确认 / }).click()
  check(await page.locator('.c-change-item').count() === 2, 'pending filter shows only remaining decisions')
  check((await page.locator('.c-step-nav').innerText()).includes('修改项 1 / 3'), 'filtering navigation does not silently change the current decision target')
  await page.locator('.c-change-filters').getByRole('button', { name: /^全部 / }).click()
  check(await page.locator('.c-change-item').count() === 3, 'all decisions are available in the unified navigation')
  await page.locator('.c-change-item').nth(1).click(); await cIdle()
  check((await page.getByTestId('current-item').innerText()).includes('对象 2'), 'selecting grouped navigation opens the matching object')
  await page.locator('.c-change-item').first().click(); await cIdle()
  check((await cSession()).progress.decided === 1, 'browsing away and back does not alter saved choices')
  const confirmationState = async () => (await api(`/confirmation/sessions/${confirmation.main.sid}/changes`, confirmation.token)).items.map(item => ({ id: item.changeId, revision: item.decisionRevision, decision: item.decision }))
  await assertGuideIsolation('confirmation', await confirmationState(), confirmationState, ['a', 'b', 'Control+z'])

  await openConfirmation(confirmation.zero.sid)
  await button('领取并开始确认').click(); await cIdle()
  check((await cSession(confirmation.zero.sid)).state !== 'confirmed' && await inView(page.getByTestId('complete-video')) && await page.getByTestId('complete-video').isEnabled(), 'zero-change video has a visible explicit completion action')
  await page.getByTestId('complete-video').click(); await button('确认完成').click(); await cIdle()
  check((await cSession(confirmation.zero.sid)).state === 'confirmed', 'zero-change completion still creates the final video version')
  await openConfirmation(confirmation.many.sid)
  await button('领取并开始确认').click(); await cIdle()
  check(await page.locator('.c-change-item').count() === 40 && (await page.locator('.c-frame-group h3').allTextContents()).join() === '第 1 帧', 'a real 45-change video starts with 40 items grouped by source frame')
  await button('下一页修改项').click()
  check(await page.locator('.c-change-item').count() === 5, 'navigation exposes the remaining five real changes on its second page')
  const beforeLocate = await page.evaluate(() => [scrollX, scrollY])
  await button('定位当前项').click()
  check(await page.locator('.c-change-item').count() === 40 && JSON.stringify(await page.evaluate(() => [scrollX, scrollY])) === JSON.stringify(beforeLocate) && await inView(page.getByTestId('choose-a')), 'locating the current item changes only the list page and preserves workspace position')
  await button('下一页修改项').click()
  await page.locator('.c-change-item').last().click(); await cIdle()
  check((await page.getByTestId('current-item').innerText()).includes('对象 45'), 'the last paginated change loads its matching source object')
  await button('上一页修改项').click(); await button('定位当前项').click()
  check((await page.locator('.c-change-item[aria-current=true]').innerText()).includes('对象 45') && await page.locator('.c-change-item[aria-current=true]').isVisible(), 'locate restores the selected item on the correct navigation page')
  check((await cSession(confirmation.many.sid)).progress.decided === 0, 'pagination and locating never create confirmation decisions')
  await openConfirmation(confirmation.portrait.sid)
  await button('领取并开始确认').click(); await cIdle()
  const portraitBefore = JSON.stringify((await api(`/confirmation/sessions/${confirmation.portrait.sid}/changes`, confirmation.token)).items)
  for (const size of [{ width: 1366, height: 768 }, { width: 1180, height: 760 }]) {
    await page.setViewportSize(size)
    await contextLayout(450, 800, `portrait ${size.width}`)
    check(await inView(page.getByTestId('choose-a')) && await inView(page.getByTestId('choose-b')) && await inView(button('下一修改项 →')), `portrait ${size.width}: decisions and navigation remain visible`)
    await page.screenshot({ path: `output/playwright/workbench-confirmation-portrait-${size.width}.png`, animations: 'disabled' })
  }
  check(JSON.stringify((await api(`/confirmation/sessions/${confirmation.portrait.sid}/changes`, confirmation.token)).items) === portraitBefore, 'resizing a portrait view does not modify any choice or annotation')
  await narrowWorkbenchChecks()
  await workbenchShortcutsChecks({ page, origin, review, confirmation, check })
  check(errors.length === 0, 'shared workbench paths have no uncaught browser exceptions')
  console.log('SUCCESS', checks, 'checks')
} catch (error) {
  await page.screenshot({ path: 'output/playwright/workbench-consistency-failure.png', fullPage: true })
  throw error
} finally {
  fs.writeFileSync('output/playwright/workbench-consistency-console.json', JSON.stringify({ checks, logs, errors, libraryHits }, null, 2))
  await browser.close()
}
