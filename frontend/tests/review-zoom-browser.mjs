// Focused entry for the fixed workbench suite's zoom checks. Disposable B fixture
// required; uses the same real frames/API and does not submit or edit geometry.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { reviewZoomChecks } from './review-zoom-checks.mjs'
import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
// The task list is a drawer that stays closed while a task is open.
const openTask=async(target,selector)=>{const card=target.locator(selector),toggle=target.getByTestId('toggle-workbench-library');for(let i=0;i<40;i++){if(await card.isVisible()){try{await card.click({timeout:1500});return}catch{}}else if(await toggle.isVisible())await toggle.click();await target.waitForTimeout(250)}await card.click()}
const fixture = JSON.parse(fs.readFileSync(process.env.REVIEW_FIXTURE || 'work/browser-fixture.json'))
const origin = process.env.REVIEW_ORIGIN || process.env.CONFIRMATION_ORIGIN || 'http://127.0.0.1:5373'
const output = process.env.REVIEW_ZOOM_OUTPUT || 'output/playwright/review-zoom-regression'
const browser = await chromium.launch({ channel: process.env.CHROME_CHANNEL || 'chrome', headless: process.env.REVIEW_ZOOM_HEADED !== '1', ignoreDefaultArgs: ['--hide-scrollbars'] })
const page = await browser.newPage({ viewport: { width: 1366, height: 768 } })
const errors = [], logs = []
let checks = 0
page.on('pageerror', error => errors.push(String(error)))
page.on('console', message => logs.push({ type: message.type(), text: message.text() }))
try {
  if (process.env.LEGACY_BROWSER === '1') await useLegacyBrowserAPIs(page)
  await page.addInitScript(token => {
    localStorage.setItem('rare-sperm-token', token)
    localStorage.setItem('rare-sperm-auth', JSON.stringify({ id: '2', name: 'review-B', role: 'annotator' }))
  }, fixture.token)
  await page.goto(origin + '/review')
  await openTask(page,`[data-session-id="${fixture.sid}"]`)
  await page.locator('.review-drawing').waitFor()
  if (await page.getByRole('dialog').count()) await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
  await reviewZoomChecks({ page, fixture, origin, output, check: (value, label) => { assert(value, label); console.log('PASS', ++checks, label) } })
  assert.equal(errors.length, 0, 'no browser runtime errors')
  console.log('SUCCESS', checks, 'zoom checks')
} finally {
  fs.mkdirSync(output, { recursive: true })
  fs.writeFileSync(`${output}/console.json`, JSON.stringify({ checks, errors, logs }, null, 2))
  await browser.close()
}
