import assert from 'node:assert/strict'

export async function reviewLocationChecks(page, { state, ready, check, poll, reviewState, mediaId }) {
  const missing = state.items.filter(i => i.category === 'missing')
  const right = missing.find(i => i.frame === 1 && i.index === 4)
  assert(right)
  const row = i => page.locator(`[data-review-item="${i.id}"]`)
  const marker = () => page.locator('.review-candidate-circle')
  const button = name => page.getByRole('button', { name, exact: true })
  const viewport = () => page.locator('.canvas-scroll').evaluate(e => ({ left:e.scrollLeft,top:e.scrollTop }))
  const beforeChecked = (await reviewState(mediaId)).review.checked
  const geometry = await page.evaluate(() => window.ws.currentObjects.value.map(o => ({id:o.id,bbox:o.bbox})))
  const visible = () => marker().evaluate(e => {
    const r=e.getBoundingClientRect(),cx=r.x+r.width/2,cy=r.y+r.height/2
    const sc=document.querySelector('.canvas-scroll'),v=sc.getBoundingClientRect()
    return cx>=v.left && cx<=v.right && cy>=v.top && cy<=v.bottom && document.elementFromPoint(cx,cy)?.closest('.canvas-scroll')===sc
  })
  const beforeZoom = await page.evaluate(() => window.ws.zoom.value)
  await row(right).click(); await ready()
  await poll(visible,'candidate revealed after automatic target zoom')
  check(await page.evaluate(() => window.ws.zoom.value)>beforeZoom, 'tiny candidate enlarges automatically before the user sets a review zoom')
  check(await marker().count()===1 && await marker().getAttribute('data-review-index')==='4', 'one location per item has the same stable number as the list')
  check((await row(right).innerText()).includes('右下 · 4/4 · 3 个观测帧'), 'same-frame rows identify region, sequence, and sparse detector observations without claiming every intervening frame')
  check(new Set(await page.locator('[data-review-group=missing] .review-row').allTextContents()).size===missing.length, 'missing location rows are distinguishable')
  check(await marker().evaluate(e => e.getBoundingClientRect().height>=28 && getComputedStyle(e).animationIterationCount==='2'), 'candidate marker has a readable size and only two pulses')
  await page.screenshot({path:'output/playwright/review-location-auto.png'})

  await button('适应画面').click()
  for (let i=0;i<6;i++) await button('放大画面').click()
  const manualZoom = await page.evaluate(() => window.ws.zoom.value)
  await page.locator('.canvas-scroll').evaluate(e => e.scrollTo(0,0))
  check(!await visible(), 'fixture candidate starts outside the manually zoomed viewport')
  await row(right).click(); await ready(); await poll(visible,'right-bottom candidate after manual zoom')
  check(await page.evaluate(() => window.ws.zoom.value)===manualZoom, 'locating a candidate respects manual zoom and pans it into view')
  const oldScroll = await viewport()
  await page.getByTestId('review-relocate').click(); await ready()
  assert.deepEqual(await viewport(),oldScroll)
  check(true, 'relocating an already visible location does not move the viewport')
  await button('上一处').click(); await ready()
  check(await marker().getAttribute('data-review-index')==='3', 'previous location updates the card and marker together')
  await button('下一处').click(); await ready()
  check(await marker().getAttribute('data-review-index')==='4' && await visible(), 'next location returns to the same numbered candidate')
  await page.screenshot({path:'output/playwright/review-location-manual-zoom.png'})

  const failed = missing.find(i => i.frame===39)
  const failPattern = `**/api/track/frame/${mediaId}/39`
  const fail = route => route.fulfill({status:503,contentType:'application/json',body:'{"detail":"injected frame failure"}'})
  await page.route(failPattern,fail)
  const beforeFailureScroll = await viewport()
  await row(failed).click()
  await page.waitForFunction(() => !!window.ws.frameError.value && !window.ws.exactFrameLoading.value)
  check(await marker().isHidden(), 'failed source frame hides its candidate instead of marking the old image')
  assert.deepEqual(await viewport(),beforeFailureScroll)
  check(await page.evaluate(() => window.ws.zoom.value)===manualZoom, 'frame read failure does not zoom or pan an unrelated frame')
  await page.screenshot({path:'output/playwright/review-location-read-failed.png'})
  await page.unroute(failPattern,fail)
  await page.getByTestId('review-relocate').click(); await ready(); await poll(visible,'source frame retry reveals its location')
  check(await page.evaluate(() => window.ws.currentFrame.value)===39 && await visible(), 'explicit relocate retries the failed frame and restores usable positioning')

  const delayed = missing.find(i => i.frame===49)
  let arrived, release
  const started = new Promise(r => { arrived=r }), gate = new Promise(r => { release=r })
  const delayPattern = `**/api/track/frame/${mediaId}/49`
  const delay = async route => { arrived(); await gate; await route.continue() }
  await page.route(delayPattern,delay)
  await row(delayed).click(); await started
  await row(right).click(); await ready(); await poll(visible,'latest location wins while earlier frame is delayed')
  const afterSwitch = await viewport()
  release(); await page.waitForTimeout(300); await page.unroute(delayPattern,delay)
  assert.deepEqual(await viewport(),afterSwitch)
  check(await page.getByTestId('review-current').getAttribute('data-item')===right.id && await page.evaluate(() => window.ws.currentFrame.value)===1, 'late old frame cannot steal the latest review location')
  check(await page.evaluate(() => window.ws.zoom.value)===manualZoom, 'manual zoom survives navigation and repeated review status refreshes')
  const after = (await reviewState(mediaId)).review.checked
  check(after===beforeChecked, 'positioning, retry and location switching never mark an item checked')
  assert.deepEqual(await page.evaluate(() => window.ws.currentObjects.value.map(o => ({id:o.id,bbox:o.bbox}))),geometry)
  check(true, 'location navigation preserves all annotation identities and geometry')
}
