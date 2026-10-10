// Added to the fixed tracking-recovery suite. Only the model output is simulated.
// Source frames, restart receipts, version checks, and workspace recovery are real.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'

// Publish simulated data from the backend's filesystem namespace. A host-side
// rename across Docker Desktop's bind mount can transiently disappear to the
// container even though production's same-container atomic publisher is done.
export const publishTrackingFixture = (fixture, payload) => execFileSync(
  process.env.TEST_PYTHON || 'work/review-venv/bin/python', ['-c', String.raw`
import json, os, re, sys, uuid
from pathlib import Path

request = json.load(sys.stdin)
root = Path.cwd().resolve()
host_root = Path(request['hostRoot'])
host_directory = Path(request['hostDirectory'])
assert host_root.is_absolute() and host_directory.is_absolute(), 'absolute fixture paths required'
relative = host_directory.relative_to(host_root)
assert relative.parts[0] == 'work' and relative.parts[1].startswith('e2e-confirm-ux-'), 'isolated fixture required'
mid = request['mediaId']
assert isinstance(mid, str) and re.fullmatch(r'[A-Za-z0-9_-]+', mid), 'invalid fixture media identity'
for name in ('APP_DATA_DIR', 'APP_DB_FILE', 'APP_STORAGE_DIR'):
    value = Path(os.environ[name]).resolve()
    isolated = value.relative_to(root / 'work')
    assert isolated.parts[0].startswith('e2e-confirm-ux-'), name
directory = (Path(os.environ['APP_STORAGE_DIR']) / 'media' / mid).resolve()
assert directory == (root / relative).resolve() and directory.name == mid, 'fixture runtime directory mismatch'
assert directory.is_dir() and (directory / 'media.json').is_file(), 'fixture media missing'

def publish(name, value, jsonl=False):
    destination = directory / name
    temporary = directory / (name + '.' + uuid.uuid4().hex + '.tmp')
    content = '\n'.join(json.dumps(row, ensure_ascii=False) for row in value) + '\n' if jsonl else json.dumps(value, ensure_ascii=False)
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)

if request['operation'] == 'pause':
    assert request['workspace']['lastPausedContext']['mediaId'] == mid
    publish('tracker_results.json', request['rows'], jsonl=True)
    publish('workspace_state.json', request['workspace'])
elif request['operation'] == 'model':
    body = request['body']
    assert body['mediaId'] == mid and type(body['startFrame']) is int
    rows = [json.loads(line) for line in (directory / 'tracker_results.json').read_text(encoding='utf-8').splitlines() if line.strip()]
    rows = [row for row in rows if row['frame_index'] < body['startFrame']]
    for frame in range(body['startFrame'], request['lastFrame'] + 1):
        rows.append({'frame_index':frame, 'source_frame_index':frame, 'objects':[{'object_id':obj['object_id'], 'name':obj['name'], 'bbox':obj['bbox'], 'source':'manual_sam3_tracker'} for obj in body['annotations']]})
    publish('tracker_results.json', rows, jsonl=True)
else:
    raise AssertionError('unknown fixture operation')
`], {input:JSON.stringify({...payload,hostRoot:process.cwd(),hostDirectory:fixture.directory,mediaId:fixture.mid}), encoding:'utf8', timeout:30000},
)

export async function trackingRestartChecks(page, fixture, origin, { redProbeOnly = false } = {}) {
  assert(fixture.directory.includes('/work/e2e-confirm-ux-'), 'restart fixture must be isolated')
  const resultFile = `${fixture.directory}/tracker_results.json`
  const item = name => page.getByTestId(`tracking-restart-${name}`)
  let checks = 0, lostReply = false, rejectOnce = false, modelStarts = [], seedWrites = [], restartAttempts = [], receipts = new Map()
  const check = (condition, message) => { assert(condition, message); console.log('PASS restart', ++checks, message) }
  const api = async (path, method = 'GET', body) => {
    const response = await page.request.fetch(origin + '/api' + path, {method, headers:{Authorization:`Bearer ${fixture.token}`, 'Idempotency-Key':randomUUID(),'X-Review-Contract':'2'}, data:body})
    assert(response.ok(), `${method} ${path}: ${response.status()} ${await response.text()}`)
    return response.json()
  }
  const attach = () => page.evaluate(async () => { window.ws = (await import(performance.getEntriesByType('resource').find(entry => entry.name.includes('/src/stores/workspace.ts')).name)).useWorkspace() })
  const idle = () => page.waitForFunction(() => window.ws && !window.ws.workspaceRestoring.value && !window.ws.exactFrameLoading.value && !window.ws.trackingRestartBusy?.value)
  const finish = () => page.waitForFunction(() => !window.ws.isAiBusy.value && !window.ws.trackingRestartBusy.value && window.ws.saveState.value === 'saved')
  async function pausedFixture() {
    const state = await api(`/track/workspace/${fixture.mid}`)
    const percent = {x:1500/3840*100,y:1000/2160*100,width:60/3840*100,height:30/2160*100}
    const manual = [0,6].map(frameIndex => ({id:`restart-manual-${frameIndex}`,objectId:1,name:'sperm 1',source:'manual',frameIndex,bbox:{...percent,x:percent.x+(frameIndex===6?1:0)}}))
    const rows = Array.from({length:8}, (_, frame_index) => ({frame_index,source_frame_index:frame_index,objects:[{object_id:1,name:'sperm',bbox:[1500,1000,1560,1030],source:'manual_sam3_tracker',anomaly_level:frame_index===5?'anomaly':'normal',anomaly_reasons:frame_index===5?['motion_normalized=3 HARD']:[]}]}))
    publishTrackingFixture(fixture,{operation:'pause',rows,workspace:{...state,revision:state.revision+1,currentFrame:2,manualAnnotations:manual,manualBaselines:[{objectId:1,name:'sperm 1',source:'manual',frameIndex:6,bbox:[1538.4,1000,1598.4,1030]}],pausedAnomalies:[{objectId:1,displayName:'sperm 1',title:'目标偏移',summary:'追踪偏移需检查',metrics:[],rawReasons:['motion_normalized=3 HARD'],suggestion:'查看前几帧',canLearn:true,acceptsGeometry:false}],lastPausedContext:{mediaId:fixture.mid,frameIndex:5}}})
    await page.reload(); await attach(); await idle()
    if (await page.evaluate(mid=>window.ws.selectedMedia.value?.serverMediaId!==mid,fixture.mid)) {await page.locator('.asset-card').filter({hasText:'4k-recovery.avi'}).click();await idle()}
    await page.waitForFunction(() => window.ws.currentFrame.value === 2 && window.ws.pausedAnomalies.value.length === 1)
    // Workspace metadata arrives before the source image and AI collection;
    // correction controls remain intentionally disabled until all are loaded.
    await idle()
    return manual
  }
  const manual = await pausedFixture()
  check(await item('open').count() > 0 && await item('open').isEnabled(), 'a paused track offers explicit correction and restart from an earlier frame')
  if (redProbeOnly) return checks

  const inView = locator => locator.evaluate(element => {const rect=element.getBoundingClientRect();return rect.x>=0&&rect.y>=0&&rect.right<=innerWidth&&rect.bottom<=innerHeight&&element.contains(document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2))})
  for (const [width,height] of [[1366,768],[1180,760]]) {
    await page.setViewportSize({width,height})
    for (const theme of ['light','dark']) {
      if (await page.locator('html').getAttribute('data-theme') !== theme) await page.getByRole('button',{name:theme==='light'?'切换浅色主题':'切换深色主题',exact:true}).click()
      for (const focused of [false,true]) {
        if (focused) await page.getByRole('button',{name:'专注',exact:true}).click()
        check(await inView(item('open')), `earlier-frame restart visible ${width}x${height} ${theme} focus=${focused}`)
        const beforeTool=await page.evaluate(()=>window.ws.activeTool.value)
        await item('open').click();await item('confirm').waitFor()
        check(await inView(item('confirm')), 'restart confirmation stays visible in the native modal')
        await page.keyboard.press('p');await page.keyboard.press('Tab');await page.keyboard.press('Shift+Tab')
        check(await page.evaluate(()=>window.ws.activeTool.value)===beforeTool&&await page.evaluate(()=>!!document.activeElement?.closest('dialog[open]')), 'modal isolates background shortcuts and retains keyboard focus')
        await page.keyboard.press('Escape')
        check(await page.evaluate(()=>document.activeElement?.getAttribute('data-testid'))==='tracking-restart-open','Escape restores the restart entry focus')
        if (focused) await page.getByRole('button',{name:/^退出专注/}).click()
      }
    }
  }

  const originalRows = fs.readFileSync(resultFile, 'utf8')
  await item('open').click(); await page.getByRole('dialog', {name:/从第 3 帧重新追踪/}).waitFor()
  check((await page.getByRole('dialog', {name:/从第 3 帧重新追踪/}).innerText()).includes('不视为“无异常”'), 'restart explicitly discards the obsolete AI branch without accepting its anomaly')
  await item('cancel').click()
  check(fs.readFileSync(resultFile,'utf8') === originalRows && (await api(`/track/workspace/${fixture.mid}`)).pausedAnomalies.length === 1, 'cancelling restart preserves the old results and pending anomaly')
  await page.getByRole('button', {name:'下一帧',exact:true}).click(); await idle()
  await page.evaluate(() => {window.ws.frameInput.value=5;return window.ws.seekToInputFrame()}); await idle()
  check(await item('open').count() === 0 && await page.getByRole('button',{name:'AI Tracking',exact:true}).isDisabled(), 'the paused frame itself still requires explicit anomaly feedback')
  await page.evaluate(() => {window.ws.frameInput.value=2;return window.ws.seekToInputFrame()}); await idle()

  await page.route('**/api/track/annotations', route => {seedWrites.push(route.request().postDataJSON());return route.continue()})
  await page.route('**/api/track', async route => {
    const body = route.request().postDataJSON(), key = route.request().headers()['idempotency-key']
    modelStarts.push({body,key})
    if (!receipts.has(key)) {
      const taskId = `restart-model-${receipts.size+1}`, last = body.startFrame+2
      receipts.set(key,{taskId,status:'queued',maxFrames:3,last,start:body.startFrame})
      publishTrackingFixture(fixture,{operation:'model',body,lastFrame:last})
    }
    await route.fulfill({status:202,json:receipts.get(key)})
  })
  await page.route('**/api/track/status/restart-model-*', async route => {
    const taskId = new URL(route.request().url()).pathname.split('/').at(-1), status = [...receipts.values()].find(value=>value.taskId===taskId)
    await route.fulfill({json:{status:'success',stage:'completed',processedFrames:3,lastProcessedFrame:status.last,reachedVideoEnd:false}})
  })
  await page.route('**/api/track/restart-branch/*', async route => {
    restartAttempts.push({key:route.request().headers()['idempotency-key'],body:route.request().postDataJSON()})
    if (rejectOnce) {rejectOnce=false;await route.fulfill({status:503,json:{message:'回退故障注入',requestId:'restart-write-failure'}});return}
    if (lostReply) {lostReply=false;const response=await route.fetch();assert(response.ok(),await response.text());await route.abort('failed');return}
    await route.continue()
  })
  await page.getByRole('button',{name:/选择对象 #1 /}).click();await page.keyboard.press('Alt+ArrowRight')
  await page.waitForFunction(()=>window.ws.saveState.value==='saved')
  const corrected = await page.evaluate(()=>window.ws.currentObjects.value.find(object=>object.objectId===1).bbox.x)
  rejectOnce=true
  await item('open').click();await item('confirm').click();await item('error').waitFor()
  check(fs.readFileSync(resultFile,'utf8') === originalRows && (await api(`/track/workspace/${fixture.mid}`)).pausedAnomalies.length === 1, 'restart failure preserves existing results and pause while retaining the manual correction')
  await item('retry').click();await finish()
  const firstAttempts=restartAttempts.slice(0,2)
  check(firstAttempts.length===2&&JSON.stringify(firstAttempts[0])===JSON.stringify(firstAttempts[1]), 'restart retries the exact versioned request and original idempotency key')
  check(modelStarts.at(-1).body.startFrame===2&&modelStarts.at(-1).body.annotations[0].bbox[0]>1500, 'new model input starts at the earlier corrected source frame with real pixel geometry')
  let durable=await api(`/track/workspace/${fixture.mid}`)
  check(durable.pausedAnomalies.length===0&&durable.lastPausedContext===null&&!durable.trackingFeedbackEvents?.some(event=>event.decision==='normal'||event.decision==='corrected'), 'restarting clears the obsolete pause without recording an anomaly acceptance')
  check(durable.manualAnnotations.some(object=>object.frameIndex===6&&object.bbox.x===manual[1].bbox.x), 'future manual annotations survive restart and autosave')
  await page.reload();await attach();await idle()
  durable=await api(`/track/workspace/${fixture.mid}`)
  check(durable.manualAnnotations.some(object=>object.frameIndex===6)&&await page.evaluate(()=>window.ws.pausedAnomalies.value.length===0), 'refresh retains future manual annotations and does not restore the stale pause')
  check(durable.manualAnnotations.some(object=>object.frameIndex===2&&object.bbox.x===corrected), 'earlier manual correction remains durable after refresh')

  await pausedFixture();lostReply=true
  const startsBefore=modelStarts.length
  await item('open').click();await item('confirm').click();await item('error').waitFor()
  const lost=restartAttempts.at(-1)
  check((await api(`/track/workspace/${fixture.mid}`)).pausedAnomalies.length===0&&modelStarts.length===startsBefore, 'lost committed restart response keeps the client waiting without starting a model')
  await page.reload();await attach();await idle();await item('error').waitFor()
  await item('retry').click();await finish()
  check(JSON.stringify(restartAttempts.at(-1))===JSON.stringify(lost)&&modelStarts.length===startsBefore+1, 'refresh resumes the original restart receipt and starts its corrected branch once')
  check(await item('error').count()===0&&await page.evaluate(()=>!window.ws.editingBlocked.value), 'restart response recovery restores a usable annotation editor')

  await pausedFixture()
  await page.getByRole('button',{name:/选择对象 #1 /}).click();await page.keyboard.press('Alt+ArrowRight');await page.waitForFunction(()=>window.ws.saveState.value==='saved')
  let releaseManual, manualEntered
  const entered=new Promise(resolve=>{manualEntered=resolve})
  await page.route('**/api/annotation/annotations/manual',async route=>{
    const response=await route.fetch();manualEntered();await new Promise(resolve=>{releaseManual=resolve});await route.fulfill({response})
  })
  const countBeforeNavigation=modelStarts.length, seedsBeforeNavigation=seedWrites.length
  await item('open').click();await item('confirm').click();await entered
  await item('cancel').click()
  check(await page.getByRole('button',{name:'下一帧',exact:true}).isDisabled(), 'normal user navigation cannot replace the seed frame during preparation')
  // The internal seek helper must remain available to AI itself. Exercise a
  // competing internal seek to prove the frozen seed cannot be mislabelled.
  await page.evaluate(()=>window.ws.seekVideo(window.ws.frameToTime(3)))
  releaseManual();await item('error').waitFor()
  check(modelStarts.length===countBeforeNavigation&&seedWrites.length===seedsBeforeNavigation&&(await item('error').innerText()).includes('起始帧已变化'), 'a changed preparation frame stops seed/model publication and keeps the original restart intent')
  await page.unroute('**/api/annotation/annotations/manual')
  await item('retry').click();await finish()
  check(modelStarts.at(-1).body.startFrame===2&&modelStarts.at(-1).body.annotations.every(object=>object.frameIndex===2), 'retry restores the original frame and uses its frozen source identity')

  await pausedFixture()
  let statusFailure=true
  const statusPath=`**/api/review/media/${fixture.mid}/submission-status`
  await page.route(statusPath,route=>{if(statusFailure){statusFailure=false;return route.fulfill({status:503,json:{message:'送审状态读取故障注入',requestId:'restart-submission-read-failure'}})}return route.continue()})
  const seedsBeforeStatus=seedWrites.length, startsBeforeStatus=modelStarts.length
  await item('open').click();await item('confirm').click();await item('error').waitFor()
  check(seedWrites.length===seedsBeforeStatus&&modelStarts.length===startsBeforeStatus&&await page.evaluate(()=>window.ws.trackingRestartPendingAction.value.phase==='start'), 'unreadable submission status preserves the committed branch intent and stops seed/model writes')
  const restartsBeforeStatusRetry=restartAttempts.length
  await item('retry').click();await finish()
  check(restartAttempts.length===restartsBeforeStatusRetry&&modelStarts.length===startsBeforeStatus+1, 'submission-status recovery continues its original branch without rewinding again')
  await page.unroute(statusPath)

  await pausedFixture();lostReply=true
  const startsBeforeSubmission=modelStarts.length, seedsBeforeSubmission=seedWrites.length
  await item('open').click();await item('confirm').click();await item('error').waitFor()
  const preview=await api(`/review/media/${fixture.mid}/completion-preview`)
  const submitted=await api(`/review/media/${fixture.mid}/complete`,'POST',{expectedSourceRevision:preview.sourceRevision,confirmComplete:true,explicitEmptyFrameRanges:preview.unknownFrameRanges})
  await page.reload();await attach();await idle();await item('error').waitFor()
  await item('retry').click()
  await page.waitForFunction(()=>window.ws.trackingRestartError.value.includes('已送审')&&!window.ws.trackingRestartBusy.value)
  check(modelStarts.length===startsBeforeSubmission&&seedWrites.length===seedsBeforeSubmission&&await page.evaluate(()=>window.ws.editingBlocked.value), 'a lost restart receipt replay cannot seed or start a source submitted in another window')
  check((await item('error').innerText()).includes('已送审'), 'submitted-source recovery stays read-only and explains the actual reason')
  // Keep this fixture's active workflow clean for the subsequent fixed suites.
  await api(`/review/sessions/${submitted.session.id}/withdraw`,'POST',{expectedSessionRevision:submitted.session.revision})
  await page.unroute('**/api/track/restart-branch/*')
  await page.unroute('**/api/track/annotations')
  await page.unroute('**/api/track/status/restart-model-*')
  await page.unroute('**/api/track')
  fs.writeFileSync('output/playwright/tracking-restart-checks.json',JSON.stringify({checks},null,2))
  return checks
}
