// Executed inside the fixed training-export suite, against its isolated real DB.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'
import { useLegacyBrowserAPIs } from './legacy-browser-profile.mjs'

export async function statisticsExportChecks(page, fixture, origin) {
  await useLegacyBrowserAPIs(page)
  let count = 0
  const check = (condition, message) => { assert(condition, message); console.log('PASS statistics', ++count, message) }
  const item = name => page.getByTestId(`statistics-export-${name}`)
  const open = async () => { await item('open').click(); await page.getByRole('dialog', {name:'导出统计数据'}).waitFor() }
  const ready = () => item('download').waitFor({state:'visible'})
  const jobId = async () => (await item('job-id').innerText()).match(/stats_[a-zA-Z0-9_-]+/)?.[0]
  const download = async (trigger, filename) => {
    const [event] = await Promise.all([page.waitForEvent('download'), trigger.click()])
    await event.saveAs(filename)
    await page.waitForTimeout(80)
  }
  await page.getByRole('button', {name:'关闭', exact:true}).click()
  await page.getByRole('button', {name:'标注记录', exact:true}).click()
  await item('open').waitFor()
  check(await item('open').isEnabled(), 'statistics export is available in annotation records')
  for (const [width, height] of [[1366,768], [1180,760]]) {
    await page.setViewportSize({width,height})
    for (const theme of ['light','dark']) {
      const current = await page.locator('html').getAttribute('data-theme')
      if (current !== theme) await page.getByRole('button', {name:theme === 'dark' ? '切换深色主题':'切换浅色主题'}).click()
      const box = await item('open').boundingBox()
      check(box && box.x >= 0 && box.y >= 0 && box.x+box.width <= width && box.y+box.height <= height, `export entry visible ${width}x${height} ${theme}`)
      await open()
      const dialog = page.getByRole('dialog', {name:'导出统计数据'})
      check((await dialog.innerText()).includes('整个系统'), 'dialog explains instance-wide scope rather than current records filter')
      const body = await dialog.boundingBox()
      check(body && body.x >= 0 && body.y >= 0 && body.x+body.width <= width && body.y+body.height <= height, 'modal fits viewport')
      await page.screenshot({path:`output/playwright/statistics-export-${width}-${theme}.png`})
      await page.keyboard.press('Escape')
      check(await page.evaluate(() => document.activeElement?.getAttribute('data-testid')) === 'statistics-export-open', 'Escape restores launcher focus')
    }
  }
  await open(); await item('confirm').click(); await ready()
  const firstId = await jobId()
  check(Boolean(firstId), 'real asynchronous export reaches ready')
  await download(item('download'), 'output/playwright/statistics-data.zip')
  const inspected = JSON.parse(execFileSync(process.env.TEST_PYTHON || 'work/review-venv/bin/python', ['-c', `
import csv,hashlib,io,json,sys,zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
 m=json.loads(z.read('manifest.json'))
 assert m['schemaVersion']=='annotation-confirmation-statistics-v1'
 assert m['exportMode']=='full' and m['sourceSystemId']
 assert not any(n.endswith(('.db','.sqlite','.mp4','.avi')) for n in z.namelist())
 for e in m['files']:
  b=z.read(e['name']);assert hashlib.sha256(b).hexdigest()==e['sha256']
  rows=[json.loads(line) for line in b.decode('utf-8-sig').splitlines() if line] if e['format']=='jsonl' else list(csv.DictReader(io.StringIO(b.decode('utf-8-sig'))))
  assert len(rows)==e['rowCount']
 users=list(csv.DictReader(io.StringIO(z.read('users.csv').decode('utf-8-sig'))))
 assert users and set(users[0])=={'id','username','created_at'}
 assert b'password_hash' not in z.read('users.csv')
 frames=[json.loads(l) for l in z.read('final_version_frames.jsonl').decode().splitlines()]
 assert frames and any(not f['objects_json'] for f in frames)
 assert all(isinstance(f['objects_json'],list) for f in frames)
 versions=list(csv.DictReader(io.StringIO(z.read('final_versions.csv').decode('utf-8-sig'))))
 assert any(v['is_current']=='1' for v in versions) and any(v['is_current']=='0' for v in versions)
 print(json.dumps({'files':len(m['files']),'frames':len(frames),'exportId':m['exportId']}))
`, 'output/playwright/statistics-data.zip'], {encoding:'utf8'}))
  check(inspected.files === 17 && inspected.exportId === firstId, 'real ZIP checksums, A/B/C/F, empty frames, version history and excluded credentials verified')
  check((await page.request.get(`${origin}/api/statistics/exports/${firstId}/download`)).status() === 401, 'anonymous archive access denied')
  check((await page.request.get(`${origin}/api/statistics/exports/${firstId}`, {headers:{Authorization:`Bearer ${fixture.authorToken}`}})).status() === 404, 'another account cannot access the generated task')
  let lost = true, createdId, keys = []
  await page.route('**/api/statistics/exports', async route => {
    keys.push(route.request().headers()['idempotency-key'])
    if (lost) { lost=false; const response=await route.fetch(); createdId=(await response.json()).exportId; await route.abort('failed') }
    else await route.continue()
  })
  await item('regenerate').click(); await item('error').waitFor()
  check(await item('regenerate').count() === 0 || !(await item('regenerate').isEnabled()), 'uncertain creation cannot be replaced with a new request')
  await page.reload(); await item('open').waitFor(); await open(); await item('error').waitFor()
  await item('retry').click(); await ready()
  check(keys.length === 2 && keys[0] === keys[1] && await jobId() === createdId, 'refresh and retry preserve the original key and task after lost committed response')
  await page.unroute('**/api/statistics/exports')
  await page.route('**/api/statistics/exports/*/download', route => route.fulfill({status:503,contentType:'application/json',json:{message:'下载故障注入',requestId:'statistics-download-test'}}))
  await item('download').click(); await item('error').waitFor()
  check((await item('error').innerText()).includes('statistics-download-test'), 'download error stays visible with diagnostic request ID')
  await page.unroute('**/api/statistics/exports/*/download')
  await download(item('retry'), 'output/playwright/statistics-retry.zip')
  check(await jobId() === createdId, 'download retry does not regenerate the archive')
  await page.route('**/api/statistics/exports', async route => {
    const response=await route.fetch(), data=await response.json()
    data.state='queued'; data.stage='queued'; data.downloadAllowed=false
    await route.fulfill({response,json:data})
  })
  await page.route(/\/api\/statistics\/exports\/stats_[^/]+$/, route => route.fulfill({status:503,contentType:'application/json',json:{message:'查询故障注入',requestId:'statistics-status-test'}}))
  await item('regenerate').click(); await item('error').waitFor()
  check((await item('error').innerText()).includes('statistics-status-test'), 'status failure does not masquerade as successful or empty output')
  await page.unroute(/\/api\/statistics\/exports\/stats_[^/]+$/)
  await page.unroute('**/api/statistics/exports')
  await item('retry').click(); await ready()
  const recoveredId = await jobId()
  check(recoveredId && recoveredId !== createdId, 'poll retry resumes its own new task')
  await item('close').click()
  // Expired jobs are distinct from transient failure and permit explicit regeneration.
  await page.route(/\/api\/statistics\/exports\/stats_[^/]+$/, route => route.fulfill({status:410,contentType:'application/json',json:{message:'导出已过期，请重新生成',code:'STATISTICS_EXPORT_EXPIRED'}}))
  await open(); await item('error').waitFor()
  await page.unroute(/\/api\/statistics\/exports\/stats_[^/]+$/)
  await item('regenerate').click(); await ready()
  check(await jobId() !== recoveredId, 'expired job requires explicit new generation and recovers')
  // A missing artifact is permanent for this job, not a download retry loop.
  await page.route('**/api/statistics/exports/*/download', route => route.fulfill({status:410,contentType:'application/json',json:{message:'临时ZIP丢失',code:'STATISTICS_EXPORT_FILE_MISSING'}}))
  await item('download').click(); await item('error').waitFor()
  check(await item('regenerate').isEnabled(), 'missing archive offers explicit regeneration instead of endless download retry')
  await page.unroute('**/api/statistics/exports/*/download')
  await item('regenerate').click(); await ready()
  const intactId = await jobId()
  await item('close').click()
  await page.route(/\/api\/statistics\/exports\/stats_[^/]+$/, route => route.fulfill({status:200,contentType:'application/json',json:{state:'ready',downloadAllowed:true,progress:null}}))
  await open(); await item('error').waitFor()
  check((await item('error').innerText()).includes('损坏'), 'malformed HTTP 200 cannot replace valid task state')
  await page.unroute(/\/api\/statistics\/exports\/stats_[^/]+$/)
  await item('retry').click(); await ready()
  check(await jobId() === intactId, 'valid query restores the same task after malformed success')
  await item('close').click()
  await page.getByRole('button', {name:'使用说明',exact:true}).click()
  await page.getByRole('button', {name:'标注记录与统计导出',exact:true}).click()
  check((await page.getByRole('article', {name:'说明正文'}).innerText()).includes('导出统计数据'), 'shared user guide explains statistics export')
  await page.getByRole('button', {name:'返回工作台',exact:true}).click()
  fs.writeFileSync('output/playwright/statistics-export-checks.json', JSON.stringify({checks:count, exportId:firstId, files:inspected.files},null,2))
  return count
}
