import { chromium } from '/Users/patient/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'
import fs from 'node:fs'
const fixture=JSON.parse(fs.readFileSync('work/browser-fixture.json'))
const browser=await chromium.launch({channel:'chrome',headless:true})
const page=await browser.newPage({viewport:{width:1440,height:900}})
page.on('console',m=>console.log(m.type(),m.text()))
page.on('pageerror',e=>console.log('PAGEERROR',e))
await page.addInitScript(({token})=>{localStorage.setItem('rare-sperm-token',token);localStorage.setItem('rare-sperm-auth',JSON.stringify({id:'2',name:'review-B',role:'annotator'}))},fixture)
await page.goto('http://127.0.0.1:5373/review')
await page.locator('.video-card').first().click()
await page.locator('.review-drawing').waitFor()
await page.screenshot({path:'output/playwright/review-initial.png',fullPage:true})
console.log(await page.locator('.review-page').innerText())
await browser.close()
