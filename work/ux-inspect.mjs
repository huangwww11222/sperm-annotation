import fs from 'node:fs'
import {chromium} from '/Users/patient/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'
const f=JSON.parse(fs.readFileSync('work/confirmation-browser-fixture.json'))
const b=await chromium.launch({channel:'chrome',headless:true}),p=await b.newPage({viewport:{width:1440,height:900}})
p.on('pageerror',e=>console.log('ERROR',String(e)))
await p.addInitScript(f=>{localStorage.setItem('rare-sperm-token',f.token);localStorage.setItem('rare-sperm-auth',JSON.stringify({id:'3',name:'测试用户'}))},f)
await p.goto('http://127.0.0.1:5373/annotate');await p.waitForSelector('.annotation-stage');await p.waitForTimeout(1500)
console.log(await p.locator('.annotation-workbench').boundingBox(),await p.locator('.annotation-stage').boundingBox())
await p.screenshot({path:'output/playwright/ux-annotation-light.png'})
await p.getByRole('button',{name:'审查模式',exact:false}).click();await p.waitForTimeout(1200);await p.screenshot({path:'output/playwright/ux-review-light.png'})
await p.getByRole('button',{name:'对比确认',exact:false}).click();await p.waitForTimeout(1200);await p.screenshot({path:'output/playwright/ux-confirm-light.png'})
await b.close()
