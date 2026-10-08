// Shared workbench and progressive disclosure acceptance; run with disposable B/C/UX fixtures.
// Uses real video frames, draft writes and C decisions. See docs/TESTING.md.
import assert from 'node:assert/strict'
import fs from 'node:fs'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const confirmation = JSON.parse(fs.readFileSync('work/confirmation-browser-fixture.json'))
const review = JSON.parse(fs.readFileSync('work/browser-fixture.json'))
const origin = process.env.CONFIRMATION_ORIGIN || 'http://127.0.0.1:5373'
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1366, height: 768 } })
const logs = [], errors = []
let checks = 0
page.on('console', message => logs.push({ type: message.type(), text: message.text() }))
page.on('pageerror', error => errors.push(String(error)))
const check = (value, message) => { assert(value, message); console.log('PASS', ++checks, message) }
const button = name => page.getByRole('button', { name, exact: true })
const cIdle = () => page.locator('.confirmation-page[data-busy="false"]').waitFor()
const bIdle = () => page.waitForFunction(() => !document.querySelector('[data-testid="submit-frame"]')?.textContent.includes('处理中'))
const inView = locator => locator.evaluate(element => {
  const rect = element.getBoundingClientRect()
  return rect.top >= 70 && rect.bottom <= innerHeight + 1 && rect.width > 0 && element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2))
})
const api = async (path, token) => {
  const response = await page.request.get(origin + '/api' + path, { headers: { Authorization: 'Bearer ' + token } })
  assert(response.ok(), await response.text())
  return response.json()
}
const frame = () => api(`/review/sessions/${review.sid}/frames/0`, review.token)
const cSession = (sid = confirmation.main.sid) => api(`/confirmation/sessions/${sid}`, confirmation.token)
const closeReviewDialog = async () => {
  if (await page.getByRole('dialog').count()) await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
}
async function openReview(expectCollapsed = false) {
  await page.goto(origin + '/review')
  await expandLibrary('review', expectCollapsed)
  await page.locator(`[data-session-id="${review.sid}"]`).click()
  await page.locator('.review-drawing').waitFor()
  await bIdle()
  await closeReviewDialog()
}
async function openConfirmation(sid = confirmation.main.sid, expectCollapsed = false) {
  if (new URL(page.url()).pathname !== '/confirm') await page.goto(origin + '/confirm')
  await expandLibrary('confirmation', expectCollapsed)
  await cIdle()
  await page.locator(`[data-session-id="${sid}"]`).click()
  await cIdle()
  if (await button('留在上次位置').count()) await button('留在上次位置').click()
}
async function expandLibrary(stage, expectCollapsed = false) {
  const toggle = page.getByTestId('toggle-workbench-library')
  await toggle.waitFor()
  if (expectCollapsed) check(await toggle.getAttribute('aria-expanded') === 'false' && !await page.locator('.workbench-library-content').isVisible(), `${stage}: collapsed video library preference survives a page change`)
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
      const library = await page.locator('[data-workbench-region=library]').boundingBox()
      const canvas = await page.locator('[data-workbench-region=canvas]').boundingBox()
      const inspector = await page.locator('[data-workbench-region=inspector]').boundingBox()
      check(library && canvas && inspector && library.x + library.width <= canvas.x + 2 && canvas.x + canvas.width <= inspector.x + 2,
        `${stage} ${size.width} ${dark ? 'dark' : 'light'}: library, image and operations keep the same left-to-right structure`)
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${stage}: no horizontal page overflow`)
      check(await inView(core), `${stage}: core operation is visible without scrolling`)
      if (progress) {
        const summary = page.getByRole('region', { name: progress })
        const box = await summary.boundingBox()
        check(await inView(summary) && box.y + box.height <= canvas.y + 2, `${stage}: progress stays prominent above the workspace`)
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
  await page.locator('.asset-card').filter({ hasText: 'ux-video.avi' }).click()
  await page.evaluate(async () => {
    const url = performance.getEntriesByType('resource').find(resource => resource.name.includes('/src/stores/workspace.ts')).name
    window.ws = (await import(url)).useWorkspace()
  })
  await page.waitForFunction(() => window.ws && !window.ws.workspaceRestoring.value && !window.ws.exactFrameLoading.value && window.ws.currentObjects.value.length === 2)
  await layout('annotation', null, button('AI Tracking'))
  await page.locator('.object-select').first().click()
  await page.locator('.annotation-object-details > summary').focus(); await page.keyboard.press(' ')
  check(await page.locator('.annotation-object-details').evaluate(element => element.open), 'Space opens annotation object details using the native summary action')
  await page.keyboard.press(' ')
  await page.setViewportSize({ width: 1180, height: 760 })
  await page.getByTestId('toggle-workbench-library').click()
  await button('专注').click()
  check((await page.locator('[data-workbench-region=canvas]').boundingBox()).width > 1100 && !await page.locator('[data-workbench-region=library]').isVisible() && !await page.locator('[data-workbench-region=inspector]').isVisible(), 'compact focus uses the whole width even when the library was collapsed')
  await page.getByRole('button', { name: /退出专注/ }).click()
  check(await page.getByTestId('toggle-workbench-library').getAttribute('aria-expanded') === 'false', 'leaving focus retains the collapsed library preference')
  await page.getByTestId('toggle-workbench-library').click()
  await page.setViewportSize({ width: 1366, height: 768 })
  const annotationBefore = await page.evaluate(() => ({ frame: window.ws.currentFrame.value, count: window.ws.currentObjects.value.length, focused: !!document.querySelector('.annotation-page.focused') }))
  await assertGuideIsolation('annotation', annotationBefore, () => page.evaluate(() => ({ frame: window.ws.currentFrame.value, count: window.ws.currentObjects.value.length, focused: !!document.querySelector('.annotation-page.focused') })), ['f', 'b', 'Delete'])

  const beforeCollapse = await page.locator('[data-workbench-region=canvas]').boundingBox()
  await page.getByTestId('toggle-workbench-library').click()
  check((await page.locator('[data-workbench-region=canvas]').boundingBox()).width > beforeCollapse.width, 'collapsing the library gives its space to the image workspace')
  await openReview(true)
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
  const rect = await page.locator('[data-box="1"] rect').boundingBox()
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2)
  await page.mouse.down(); await page.mouse.move(rect.x + rect.width / 2 + 14, rect.y + rect.height / 2 + 4, { steps: 4 }); await page.mouse.up(); await bIdle()
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
  check(await page.getByTestId('workbench-heading').locator('p').getAttribute('title') === longName && await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'a long video name remains available in its tooltip without widening the workbench')
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
  check((await page.getByTestId('current-item').innerText()).includes('对象 #2') && (await page.locator('.c-change-item[aria-current=true]').innerText()).includes('对象 #2') && JSON.stringify(displayedPair) === JSON.stringify(expectedPair), 'clicking the main full frame links the same object to navigation and both A/B crops')
  await contextObject(1).focus(); await page.keyboard.press('Enter'); await cIdle()
  check((await page.getByTestId('current-item').innerText()).includes('对象 #1'), 'Enter on a selectable full-frame object locates it')
  await contextObject(2).focus(); await page.keyboard.press(' '); await cIdle()
  check((await page.getByTestId('current-item').innerText()).includes('对象 #2'), 'Space on a selectable full-frame object locates it')
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
  check(await contextDialog.locator('[data-variant=full]').count() === 1 && (await page.getByTestId('current-item').innerText()).includes('对象 #2'), 'cached modal navigation retains the actual selected object and full frame')
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
  check((await page.getByTestId('current-item').innerText()).includes('对象 #2'), 'selecting grouped navigation opens the matching object')
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
  check((await page.getByTestId('current-item').innerText()).includes('对象 #45'), 'the last paginated change loads its matching source object')
  await button('上一页修改项').click(); await button('定位当前项').click()
  check((await page.locator('.c-change-item[aria-current=true]').innerText()).includes('对象 #45') && await page.locator('.c-change-item[aria-current=true]').isVisible(), 'locate restores the selected item on the correct navigation page')
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
  check(errors.length === 0, 'shared workbench paths have no uncaught browser exceptions')
  console.log('SUCCESS', checks, 'checks')
} catch (error) {
  await page.screenshot({ path: 'output/playwright/workbench-consistency-failure.png', fullPage: true })
  throw error
} finally {
  fs.writeFileSync('output/playwright/workbench-consistency-console.json', JSON.stringify({ checks, logs, errors }, null, 2))
  await browser.close()
}
