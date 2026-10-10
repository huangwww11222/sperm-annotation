import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'
import { statisticsExportChecks } from './statistics-export-checks.mjs'
// Uses only the disposable confirmation fixtures and isolated test servers.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const fixture = JSON.parse(fs.readFileSync('work/confirmation-browser-fixture.json'))
const origin = process.env.CONFIRMATION_ORIGIN || 'http://127.0.0.1:5373'
const browser = await chromium.launch({ channel: process.env.CHROME_CHANNEL || 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
if (process.env.LEGACY_BROWSER === '1') await useLegacyBrowserAPIs(page)
const errors = [], logs = []
let checks = 0
page.on('pageerror', e => errors.push(String(e)))
page.on('console', m => logs.push({ type: m.type(), text: m.text() }))
const check = (ok, msg) => { assert(ok, msg); console.log('PASS', ++checks, msg) }
const idle = () => page.waitForSelector('.confirmation-page[data-busy="false"]')
const button = name => page.getByRole('button', { name, exact: true })
const headers = { Authorization: `Bearer ${fixture.token}`, 'X-Review-Contract': '2' }
const api = (path, method='GET', data) => page.request.fetch(origin+'/api'+path, { method, data, headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() } })
const session = async () => (await api('/confirmation/sessions/'+fixture.main.sid)).json()
const exportButton = () => page.getByTestId('export-training')
async function openExport() {
  await exportButton().click()
  await page.getByRole('dialog').getByRole('heading',{name:'导出已确认训练数据集'}).waitFor()
  await page.waitForFunction(() => !!document.querySelector('.training-export-summary'))
}
async function downloadFrom(name, file) {
  const [d] = await Promise.all([page.waitForEvent('download'), button(name).click()])
  await d.saveAs(file)
  await page.waitForFunction(() => !Array.from(document.querySelectorAll('.training-export-actions button')).every(b=>b.disabled))
}
const taskId = () => page.locator('.training-export-id').innerText().then(s=>s.split('：')[1])
try {
  await page.addInitScript(({token}) => {
    localStorage.setItem('rare-sperm-token',token)
    localStorage.setItem('rare-sperm-auth',JSON.stringify({id:'3',name:'confirm-C',role:'annotator'}))
  },fixture)
  await page.goto(origin+'/annotate')
  check(await button('导出训练数据集').count()===0,'manual annotation page no longer exposes training export')
  const old = await api('/export/dataset','POST',{mediaId:'raw',annotations:[]})
  check(old.status()===410,'old direct endpoint cannot export unreviewed data')
  await button('对比确认').click();await idle()
  await page.locator(`[data-session-id="${fixture.main.sid}"]`).click();await idle()
  check(await exportButton().count()===0,'pending confirmation has no training-export entry')
  await button('领取并开始确认').click();await idle()
  for (const c of ['b','a','b']) {await page.getByTestId('choose-'+c).click();await idle()}
  check(await exportButton().count()===0,'all choices alone do not unlock export')
  await button('确认完成').click();await idle()
  check(await exportButton().isEnabled(),'explicit video confirmation unlocks export')
  const firstVersion=(await session()).finalVersionId
  await openExport()
  check(await page.getByRole('combobox',{name:'数据集格式'}).inputValue()==='yolo','YOLO is the default migrated format')
  check((await page.locator('.training-export-summary').innerText()).includes('4'),'preview includes all four source frames')
  const item=await page.getByTestId('current-item').innerText()
  await page.keyboard.press('ArrowLeft');await page.keyboard.press('b');await page.keyboard.press('Meta+z')
  check(await page.getByTestId('current-item').innerText()===item,'export dialog isolates confirmation shortcuts')
  await page.screenshot({path:'output/playwright/training-export-dialog.png'})
  await downloadFrom('生成并下载 ZIP','output/playwright/training-yolo.zip')
  const firstJob=await taskId()
  check((await page.locator('.training-export-job').innerText()).includes('已生成（YOLO）'),'real background job reaches ready and downloads')
  const inspected=JSON.parse(execFileSync(process.env.TEST_PYTHON || 'work/review-venv/bin/python',['-c',`
import zipfile,json,yaml,cv2,numpy as np,sys
with zipfile.ZipFile(sys.argv[1]) as z:
 m=json.loads(z.read('manifest.json'));cfg=yaml.safe_load(z.read('data.yaml'))
 assert 'path' not in cfg and cfg['names']=={0:'debris',1:'sperm'}
 assert m['counts']['frameCount']==4 and m['counts']['objectCount']==6
 samples={s['frameIndex']:s for s in m['samples']}
 assert z.read(samples[3]['label'])==b''
 for s in samples.values():
  im=cv2.imdecode(np.frombuffer(z.read(s['image']),np.uint8),cv2.IMREAD_COLOR)
  assert im.shape[:2]==(450,800)
 row=list(map(float,z.read(samples[0]['label']).decode().splitlines()[0].split()))
 expected=[1,238.375/800,176.5/450,62.5/800,61/450]
 assert all(abs(a-b)<1e-9 for a,b in zip(row,expected))
 assert len(z.read(samples[2]['label']).decode().splitlines())==2
 print(json.dumps({'frames':len(samples),'blank':3,'classes':cfg['names'],'version':m['finalVersionIds'][0]}))
`,'output/playwright/training-yolo.zip'],{encoding:'utf8'}))
  check(inspected.frames===4&&inspected.version===firstVersion,'ZIP contains decodable source frames and exact final-version provenance')
  check(Object.keys(inspected.classes).length===2,'class mapping, B fractional coordinates, empty frames and unchanged labels verified inside ZIP')
  // Lost create response must replay the original job even after a full page reload.
  await page.getByRole('combobox',{name:'数据集格式'}).selectOption('both')
  let lost=true, keys=[], actualId
  await page.route('**/api/datasets/exports',async route=>{
    keys.push(route.request().headers()['idempotency-key'])
    if(lost){lost=false;const r=await route.fetch();actualId=(await r.json()).exportId;await route.abort('failed')}
    else await route.continue()
  })
  await button('重新生成 ZIP').click();await page.getByRole('alert').waitFor()
  check((await page.getByRole('alert').innerText()).length>0,'lost export response displays recoverable error')
  await button('关闭').click();await page.reload();await idle();await openExport()
  await page.getByRole('alert').waitFor()
  check((await page.getByRole('alert').innerText()).includes('上次创建导出'),'reload restores uncertain export request')
  await downloadFrom('重试导出请求','output/playwright/training-both.zip')
  check(keys.length===2&&keys[0]===keys[1]&&await taskId()===actualId,'retry returns same server job without duplicate export')
  await page.unroute('**/api/datasets/exports')
  // Guarantee a poll by returning a queued view of a valid server job, then fail the first status query.
  await page.getByRole('combobox',{name:'数据集格式'}).selectOption('coco')
  await page.route('**/api/datasets/exports',async route=>{
    const r=await route.fetch(),d=await r.json();d.state='queued';d.completedFrames=0;d.manifest=null
    await route.fulfill({response:r,json:d})
  })
  await page.route('**/api/datasets/exports/train_*',route=>route.fulfill({status:503,contentType:'application/json',body:'{"message":"Injected polling failure","requestId":"export-poll-test"}'}))
  await button('重新生成 ZIP').click();await page.getByRole('alert').waitFor()
  check((await page.getByRole('alert').innerText()).includes('export-poll-test'),'poll failure exposes diagnostic request ID')
  await page.unroute('**/api/datasets/exports/train_*');await page.unroute('**/api/datasets/exports')
  await downloadFrom('重试导出请求','output/playwright/training-coco.zip')
  const cocoJob=await taskId()
  check(cocoJob!==firstJob,'new settings create a separate export job')
  const coco=JSON.parse(execFileSync(process.env.TEST_PYTHON || 'work/review-venv/bin/python',['-c',`
import zipfile,json,sys
with zipfile.ZipFile(sys.argv[1]) as z:
 assert 'data.yaml' not in z.namelist()
 a=json.loads(z.read('annotations/instances_train.json'));b=json.loads(z.read('annotations/instances_val.json'))
 print(json.dumps({'images':len(a['images'])+len(b['images']),'boxes':len(a['annotations'])+len(b['annotations'])}))
`,'output/playwright/training-coco.zip'],{encoding:'utf8'}))
  check(coco.images===4&&coco.boxes===6,'COCO migration also exports all actual frames and final boxes')
  await page.route('**/api/datasets/exports/*/download',route=>route.fulfill({status:503,contentType:'application/json',body:'{"message":"Injected download failure"}'}))
  await button('下载 ZIP').click();await page.getByRole('alert').waitFor()
  check(await button('重新生成 ZIP').isEnabled(),'download error allows regeneration as well as retry')
  await page.unroute('**/api/datasets/exports/*/download')
  await downloadFrom('重试导出请求','output/playwright/training-coco-retry.zip')
  check(await taskId()===cocoJob,'failed download retries existing ready archive')
  await button('关闭').click()
  await button('重新确认').click();await button('开始重新确认').click();await idle()
  check(await exportButton().count()===0,'reopening removes training export entry')
  check((await api(`/datasets/exports/${cocoJob}/download`)).status()===409,'previous archive download is blocked during reconfirmation')
  const gated=await api('/datasets/exports','POST',{finalVersionIds:[firstVersion],format:'yolo',splitRatio:.8})
  check(gated.status()===409,'direct API creation cannot bypass reconfirmation gate')
  await page.getByTestId('complete-video').click();await button('确认完成').click();await idle()
  const secondVersion=(await session()).finalVersionId
  check(secondVersion!==firstVersion,'reconfirmation produces a new final version')
  check((await api(`/datasets/exports/${firstJob}/download`)).status()===409,'superseded final-version training archives stay blocked')
  await openExport();await downloadFrom('生成并下载 ZIP','output/playwright/training-current.zip')
  check((await api('/datasets/exports/'+await taskId())).status()===200,'new confirmed version can export normally')
  const currentJob = await taskId()
  await button('关闭').click()
  await page.locator(`[data-session-id="${fixture.zero.sid}"]`).click();await idle()
  await button('领取并开始确认').click();await idle()
  await page.getByTestId('complete-video').click();await button('确认完成').click();await idle()
  await openExport()
  check(await page.locator('.training-export-job').count()===0,'switching confirmed videos does not reuse another video export job')
  check(await page.getByRole('combobox',{name:'数据集格式'}).inputValue()==='yolo','new video starts with its own default export settings')
  await button('关闭').click()
  await page.locator(`[data-session-id="${fixture.main.sid}"]`).click();await idle();await openExport()
  check(await taskId()===currentJob,'returning to a video restores only its own export job')
  await statisticsExportChecks(page,fixture,origin)
  check(errors.length===0,'no uncaught browser errors across export and recovery flows')
  console.log(`SUCCESS ${checks} checks`)
} finally {
  fs.writeFileSync('output/playwright/training-export-console.json',JSON.stringify({errors,logs},null,2))
  await page.screenshot({path:'output/playwright/training-export-last.png'})
  await browser.close()
}
