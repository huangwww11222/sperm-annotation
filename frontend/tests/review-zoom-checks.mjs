import assert from 'node:assert/strict'
import fs from 'node:fs'
import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'
// The task list is a drawer that stays closed while a task is open.
const openTask=async(target,selector)=>{const card=target.locator(selector),toggle=target.getByTestId('toggle-workbench-library');for(let i=0;i<40;i++){if(await card.isVisible()){try{await card.click({timeout:1500});return}catch{}}else if(await toggle.isVisible())await toggle.click();await target.waitForTimeout(250)}await card.click()}

// This is a real layout control for tests, not a mocked ResizeObserver or zoom
// value: Chromium creates a 15px occupying scrollbar when the image overflows.
// Native headed checks may set REVIEW_ZOOM_NATIVE_SCROLLBARS=1 to omit it.
const classicScrollbars = `
  .review-viewport::-webkit-scrollbar { width: 15px; height: 15px; }
  .review-viewport::-webkit-scrollbar-track { background: #e4e7eb; }
  .review-viewport::-webkit-scrollbar-thumb { background: #8393a1; }
`

export async function reviewZoomChecks({ page, fixture, origin, check, output = 'output/playwright/review-zoom-regression', requireSessionSwitch = false }) {
  fs.mkdirSync(output, { recursive: true })
  const observations = [], mutations = [], readonlyErrors = []
  const recordMutation = request => {
    const path = new URL(request.url()).pathname
    if (request.method() !== 'GET' && path.startsWith('/api/review/') && path !== '/api/review/cursors') mutations.push({ method: request.method(), path })
  }
  page.on('request', recordMutation)
  let scrollbarStyle, readonlyPage
  const button = name => page.getByRole('button', { name, exact: true })
  const get = async path => {
    const response = await page.request.get(origin + '/api/review' + path, { headers: { Authorization: 'Bearer ' + fixture.token } })
    assert(response.ok(), await response.text())
    return response.json()
  }
  const businessState = async () => {
    const session = await get(`/sessions/${fixture.sid}`)
    const frames = await Promise.all(Array.from({ length: session.frameCount }, (_, index) => get(`/sessions/${fixture.sid}/frames/${index}`)))
    // Viewing may save a cursor, which is separate from annotation revisions.
    return { revision: session.revision, state: session.state, progress: session.progress, frames: frames.map(frame => ({ frameRevision: frame.frameRevision, state: frame.state, hasDraft: frame.hasDraft, patch: frame.patch, baselineObjects: frame.baselineObjects, effectiveObjects: frame.effectiveObjects })) }
  }
  const snapshot = async (label, target = page) => {
    // Wait through several actual rendering/ResizeObserver deliveries. An
    // immediate assertion would miss the old fit reset after clicking +.
    await target.waitForTimeout(250)
    const value = await target.locator('.review-drawing').evaluate(svg => {
      const viewport = svg.closest('.review-viewport'), view = svg.viewBox.baseVal
      return { zoom: Number(svg.getAttribute('width')) / view.width, width: Number(svg.getAttribute('width')), height: Number(svg.getAttribute('height')), sourceWidth: view.width, sourceHeight: view.height, viewportWidth: viewport.clientWidth, viewportHeight: viewport.clientHeight, scrollbarWidth: viewport.offsetWidth - viewport.clientWidth, scrollbarHeight: viewport.offsetHeight - viewport.clientHeight, scrollWidth: viewport.scrollWidth, scrollHeight: viewport.scrollHeight }
    })
    observations.push({ label, ...value })
    return value
  }
  const expectZoom = (value, expected, label) => check(Math.abs(value.zoom - expected) < .00001, `${label}: manual zoom remains ${(expected * 100).toFixed(1)}% after layout settles (actual ${(value.zoom * 100).toFixed(1)}%)`)
  const fitContained = (value, label) => check(value.width + 24 <= value.viewportWidth + 1 && value.height + 24 <= value.viewportHeight + 1, `${label}: fit contains the complete frame with its stage padding`)
  const screenshot = name => page.screenshot({ path: `${output}/${name}.png`, animations: 'disabled' })
  const go = async index => {
    await page.getByRole('textbox', { name: '跳转帧号' }).fill(String(index + 1))
    await page.getByRole('textbox', { name: '跳转帧号' }).press('Enter')
    await page.locator('.shell-context').filter({ hasText: `第 ${index + 1} /` }).waitFor()
    await page.waitForFunction(() => !document.querySelector('[data-testid="submit-frame"]')?.textContent.includes('处理中'))
  }
  const before = await businessState()
  try {
    await page.setViewportSize({ width: 1366, height: 768 })
    if (process.env.REVIEW_ZOOM_NATIVE_SCROLLBARS !== '1') scrollbarStyle = await page.addStyleTag({ content: classicScrollbars })
    await button('适应画面').click()
    const initial = await snapshot('initial-fit')
    fitContained(initial, 'initial fit')
    await screenshot('01-initial-fit')
    let current = initial
    for (let index = 0; index < 5; index++) {
      const expected = Math.min(5, current.zoom + .1)
      await button('放大画面').click()
      current = await snapshot(`plus-${index + 1}`)
      await screenshot(`02-plus-${index + 1}`)
      expectZoom(current, expected, `plus ${index + 1}`)
    }
    check(current.scrollWidth > current.viewportWidth || current.scrollHeight > current.viewportHeight, 'manual enlargement crosses fit and produces real image overflow')
    if (scrollbarStyle) check(current.scrollbarWidth >= 14 || current.scrollbarHeight >= 14, 'regression controls create actual occupying scrollbars, including in headless Chrome')
    while (current.zoom > Math.max(.1, initial.zoom - .1) + .00001) {
      const expected = Math.max(.1, current.zoom - .1)
      await button('缩小画面').click(); current = await snapshot('minus')
      expectZoom(current, expected, 'minus')
    }
    for (let index = 0; index < 5 && current.zoom <= initial.zoom + .2; index++) {
      const expected = Math.min(5, current.zoom + .1)
      await button('放大画面').click(); current = await snapshot('plus-after-minus')
      expectZoom(current, expected, 'enlarge after shrinking')
    }
    check(current.zoom > initial.zoom + .1, 'shrinking and enlarging again passes the original fitted size')
    await screenshot('03-enlarge-after-shrink')
    const manual = current.zoom
    await go(1); expectZoom(await snapshot('next-frame'), manual, 'frame navigation')
    await go(0); expectZoom(await snapshot('previous-frame'), manual, 'return to initial frame')
    await page.setViewportSize({ width: 1180, height: 760 })
    expectZoom(await snapshot('small-window'), manual, 'window resize')
    const toggle = page.getByTestId('toggle-workbench-library')
    await toggle.click(); expectZoom(await snapshot('toggle-library'), manual, 'library layout change')
    await screenshot('04-manual-small-window-library-toggle')
    await toggle.click()
    await button('适应画面').click()
    const fittedSmall = await snapshot('explicit-fit-small')
    fitContained(fittedSmall, 'explicit fit')
    check(fittedSmall.zoom < manual, 'the explicit fit button restores the complete image after manual enlargement')
    await page.setViewportSize({ width: 1600, height: 1000 })
    const fittedLarge = await snapshot('fit-follows-larger-window')
    fitContained(fittedLarge, 'automatic fit after resize')
    check(fittedLarge.zoom > fittedSmall.zoom + .01, 'explicit fit restores automatic fitting when the window changes')
    await page.setViewportSize({ width: 1180, height: 760 })
    const restoredSmall = await snapshot('fit-follows-smaller-window')
    fitContained(restoredSmall, 'automatic fit after shrinking window')
    check(Math.abs(restoredSmall.zoom - fittedSmall.zoom) < .02, 'automatic fit also follows a smaller window')
    await screenshot('05-fit-restored')

    if (requireSessionSwitch) {
      // The shared C fixture provides completed B tasks. Opening one gives a
      // real second source without claiming it, submitting it or changing F.
      const sessions = (await get('/sessions')).items
      const original = sessions.find(item => item.id === fixture.sid)
      const alternate = sessions.find(item => item.id !== fixture.sid && item.state === 'reviewed')
      check(!!alternate, 'shared fixtures include a different completed review video for source-switch checks')
      await button('放大画面').click(); await snapshot('manual-before-video-switch')
      await openTask(page,`[data-session-id="${alternate.id}"]`)
      await page.locator('.shell-context').filter({ hasText: alternate.media.name }).waitFor()
      await page.locator('.review-drawing').waitFor()
      if (await page.getByRole('dialog').count()) await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
      fitContained(await snapshot('different-video-initial-fit'), 'switching to a different video')
      await button('放大画面').click(); await snapshot('alternate-video-manual')
      await openTask(page,`[data-session-id="${fixture.sid}"]`)
      await page.locator('.shell-context').filter({ hasText: original.media.name }).waitFor()
      await page.waitForFunction(() => !document.querySelector('[data-testid="submit-frame"]')?.textContent.includes('处理中'))
      await page.locator('.review-drawing').waitFor()
      if (await page.getByRole('dialog').count()) await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
      fitContained(await snapshot('original-video-initial-fit'), 'returning to the original video')
      await screenshot('05b-video-switch-fit')
    }

    // A different account views the already claimed task using the real API.
    // No permission response or business state is synthesized by the test.
    readonlyPage = await page.context().browser().newPage({ viewport: { width: 1366, height: 768 } })
    readonlyPage.on('pageerror', error => readonlyErrors.push(String(error)))
    if (process.env.LEGACY_BROWSER === '1') await useLegacyBrowserAPIs(readonlyPage)
    readonlyPage.on('request', recordMutation)
    await readonlyPage.addInitScript(token => {
      localStorage.setItem('rare-sperm-token', token)
      localStorage.setItem('rare-sperm-auth', JSON.stringify({ id: '3', name: 'review-C', role: 'annotator' }))
    }, fixture.thirdToken)
    await readonlyPage.goto(origin + '/review')
    await openTask(readonlyPage,`[data-session-id="${fixture.sid}"]`)
    await readonlyPage.locator('.review-drawing').waitFor()
    if (await readonlyPage.getByRole('dialog').count()) await readonlyPage.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
    if (scrollbarStyle) await readonlyPage.addStyleTag({ content: classicScrollbars })
    await readonlyPage.getByRole('button', { name: '适应画面', exact: true }).click()
    const readonlyInitial = await snapshot('readonly-fit', readonlyPage)
    check(await readonlyPage.getByTestId('submit-frame').isDisabled() && (await readonlyPage.locator('.shell-alerts').innerText()).includes('仅可查看'), 'another account remains read only while its image controls are available')
    await readonlyPage.getByRole('button', { name: '放大画面', exact: true }).click()
    expectZoom(await snapshot('readonly-plus', readonlyPage), Math.min(5, readonlyInitial.zoom + .1), 'read only enlargement')
    await readonlyPage.screenshot({ path: `${output}/06-readonly-zoom.png`, animations: 'disabled' })
    check(readonlyErrors.length === 0, 'read only image controls have no browser runtime errors')
    check(JSON.stringify(await businessState()) === JSON.stringify(before), 'zoom, fitting, frame navigation and layout changes preserve A/B geometry, patches, draft flags, revisions and review progress')
    check(mutations.length === 0, 'image viewing never sends a review draft, submit, discard or completion request')
  } catch (error) {
    await screenshot('failure')
    throw error
  } finally {
    fs.writeFileSync(`${output}/observations.json`, JSON.stringify({ observations, mutations, readonlyErrors }, null, 2))
    if (readonlyPage) await readonlyPage.close()
    if (scrollbarStyle) await scrollbarStyle.evaluate(style => style.remove())
    await page.setViewportSize({ width: 1366, height: 768 })
    await button('适应画面').click()
    await page.waitForTimeout(250)
    page.off('request', recordMutation)
  }
}
