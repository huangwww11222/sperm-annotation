import fs from 'node:fs'
import assert from 'node:assert/strict'
import {chromium} from '/Users/patient/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'
const fixture=JSON.parse(fs.readFileSync('work/confirmation-browser-fixture.json'))
const browser=await chromium.launch({channel:'chrome',headless:true}),page=await browser.newPage({viewport:{width:1366,height:768}})
try{
 await page.addInitScript(({token,main})=>{localStorage.setItem('rare-sperm-token',token);localStorage.setItem('rare-sperm-auth',JSON.stringify({id:'3',name:'confirm-C',role:'annotator'}));localStorage.setItem('confirmation-prefs:3',JSON.stringify({sid:main.sid}))},fixture)
 await page.goto('http://127.0.0.1:5373/confirm');await page.waitForSelector('.confirmation-page[data-busy="false"]')
 await page.evaluate(()=>scrollTo(0,0));const b=await page.getByTestId('choose-b').boundingBox();assert(b.y+b.height<768)
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
 await page.screenshot({path:'output/playwright/confirmation-completed-1366.png',fullPage:true})
 await page.evaluate(()=>scrollTo(0,200));const sidebar=await page.locator('.confirmation-sidebar').boundingBox();assert(sidebar.y>=74)
 console.log('Layout verified: decision buttons visible at 1366×768; no horizontal overflow; sticky sidebar clears app header.')
}finally{await browser.close()}
