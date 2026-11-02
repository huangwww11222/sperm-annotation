// Exercise the actual TypeScript helper with isolated browser storage and a
// receipt-aware fake transport. No business API, browser profile or files used.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/annotation/workspaceWrites.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const copy = value => JSON.parse(JSON.stringify(value))
const tick = () => new Promise(resolve => setImmediate(resolve))
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function statusError(status, message) { return Object.assign(new Error(message), { status }) }
function storage() {
  const data = new Map()
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, String(value)), removeItem: key => data.delete(key), data }
}
function harness(globals = {}) {
  const localStorage = storage(), sessionStorage = storage(), calls = [], receipts = new Map(), revisions = new Map(), commits = [], logs = []
  const hooks = { write: null, read: null }
  let identifier = 0
  const actor = () => JSON.parse(localStorage.getItem('rare-sperm-auth') || '{}').id || 'unknown'
  const setActor = id => localStorage.setItem('rare-sperm-auth', JSON.stringify({ id }))
  setActor('A')
  const send = async (kind, mediaId, body, key) => {
    const call = { actor: actor(), kind, mediaId, body: copy(body), key }
    calls.push(call)
    const commit = () => {
      const identity = `${call.actor}:${mediaId}:${key}`
      const digest = JSON.stringify([kind, body])
      const prior = receipts.get(identity)
      if (prior) {
        if (prior.digest !== digest) throw statusError(409, '同一重试标识不能用于不同操作')
        return copy(prior.result)
      }
      const revision = revisions.get(mediaId) || 0
      if (body.expectedRevision !== revision) throw statusError(409, '工作区版本已变化，请保留当前编辑并重新读取')
      const result = { ok: true, revision: revision + 1, normalMotionSamples: [], pausedAnomalies: [] }
      revisions.set(mediaId, result.revision)
      receipts.set(identity, { digest, result })
      commits.push(call)
      return copy(result)
    }
    return hooks.write ? hooks.write(call, commit) : commit()
  }
  const api = {
    saveWorkspaceState: (mediaId, body, key) => send('workspace', mediaId, body, key),
    submitFeedback: (mediaId, body, key) => send('feedback', mediaId, body, key),
    getWorkspaceState: async mediaId => {
      const call = { actor: actor(), kind: 'get', mediaId }
      calls.push(call)
      const read = () => ({ exists: true, revision: revisions.get(mediaId) || 0 })
      return hooks.read ? hooks.read(call, read) : read()
    },
  }
  const exports = {}
  runInNewContext(compiled, {
    exports, localStorage, sessionStorage, Error, ...globals,
    console: { warn: (...args) => logs.push(args), error: (...args) => logs.push(args) },
    require: name => name.includes('browserCompat') ? { createRequestId: () => `request-${++identifier}` } : { trackApi: api },
  }, { filename: 'workspaceWrites.compiled.cjs' })
  return { Writer: exports.WorkspaceWrites, api, calls, commits, revisions, receipts, hooks, logs, localStorage, sessionStorage, setActor }
}
const workspace = value => ({ manualAnnotations: [], currentFrame: value })
const feedback = { expectedRevision: 0, objectId: 7, frameIndex: 1, decision: 'normal', calibrate: true }
const writes = h => h.calls.filter(call => call.kind !== 'get')

test('same-media workspace and feedback serialize using each preceding committed revision', async () => {
  const h = harness(), writer = new h.Writer(), gate = deferred()
  writer.setRevision('m', 0)
  h.hooks.write = async (call, commit) => { if (call.key === 'first') await gate.promise; return commit() }
  const first = writer.write('m', 'workspace', workspace(1), 'first')
  const second = writer.write('m', 'feedback', feedback, 'second')
  const third = writer.write('m', 'workspace', workspace(3), 'third')
  await tick()
  assert.equal(writes(h).length, 1)
  gate.resolve()
  assert.deepEqual((await Promise.all([first, second, third])).map(result => result.revision), [1, 2, 3])
  assert.deepEqual(writes(h).map(call => call.body.expectedRevision), [0, 1, 2])
  assert.deepEqual(writes(h).map(call => call.kind), ['workspace', 'feedback', 'workspace'])
})

test('lost response retries the identical key and body without a second commit', async () => {
  const h = harness(), writer = new h.Writer()
  writer.setRevision('m', 0)
  let lose = true
  h.hooks.write = async (_call, commit) => { const result = commit(); if (lose) { lose = false; throw new Error('connection closed after commit') } return result }
  await assert.rejects(writer.write('m', 'workspace', workspace(1), 'same-key'), /connection closed/)
  assert.equal(JSON.parse(h.sessionStorage.getItem('annotation-write:A:m')).body.expectedRevision, 0)
  assert.equal((await writer.write('m', 'workspace', workspace(1), 'same-key')).revision, 1)
  assert.equal(h.commits.length, 1)
  assert.deepEqual(writes(h)[0], writes(h)[1])
  assert.equal(h.sessionStorage.getItem('annotation-write:A:m'), null)
})

test('page reload replays the persisted feedback request before a new workspace save', async () => {
  const h = harness(), original = new h.Writer()
  original.setRevision('m', 0)
  let lose = true
  h.hooks.write = async (_call, commit) => { const result = commit(); if (lose) { lose = false; throw new Error('lost feedback response') } return result }
  await assert.rejects(original.write('m', 'feedback', feedback, 'feedback-retry'), /lost feedback/)
  const restored = new h.Writer()
  await restored.replay('m')
  assert.equal((await restored.write('m', 'workspace', workspace(2), 'after-refresh')).revision, 2)
  assert.equal(h.commits.length, 2)
  assert.equal(writes(h)[0].key, writes(h)[1].key)
  assert.deepEqual(writes(h)[0].body, writes(h)[1].body)
})

test('a new intent first replays an ambiguous write then uses its resulting revision', async () => {
  const h = harness(), writer = new h.Writer()
  writer.setRevision('m', 0)
  let lose = true
  h.hooks.write = async (_call, commit) => { const result = commit(); if (lose) { lose = false; throw new Error('timeout') } return result }
  await assert.rejects(writer.write('m', 'workspace', workspace(1), 'old'), /timeout/)
  assert.equal((await writer.write('m', 'workspace', workspace(2), 'new')).revision, 2)
  assert.deepEqual(writes(h).map(call => [call.key, call.body.expectedRevision]), [['old', 0], ['old', 0], ['new', 1]])
})

test('409 blocks subsequent writes until an explicit reload supplies a fresh revision', async () => {
  const h = harness(), writer = new h.Writer()
  h.revisions.set('m', 4); writer.setRevision('m', 0)
  await assert.rejects(writer.write('m', 'workspace', workspace(1), 'stale'), error => error.status === 409)
  const count = h.calls.length
  await assert.rejects(writer.write('m', 'workspace', workspace(2), 'do-not-overwrite'), error => error.status === 409)
  assert.equal(h.calls.length, count)
  assert.equal(h.commits.length, 0)
  writer.setRevision('m', (await h.api.getWorkspaceState('m')).revision)
  assert.equal((await writer.write('m', 'workspace', workspace(3), 'reviewed-reload')).revision, 5)
})

test('confirmed generation reset drops old ambiguous request without replaying discarded boxes', async () => {
  const h = harness(), writer = new h.Writer()
  writer.setRevision('m', 0)
  h.hooks.write = async () => { throw new Error('response lost') }
  await assert.rejects(writer.write('m', 'workspace', workspace(1), 'old-boxes'))
  assert(h.sessionStorage.getItem('annotation-write:A:m'))
  h.revisions.set('m', 7)
  await writer.discardForReset('m', 7)
  h.hooks.write = null
  await writer.replay('m')
  assert.equal(h.sessionStorage.getItem('annotation-write:A:m'), null)
  assert.equal(writes(h).length, 1, 'discarded branch is never sent again')
  assert.equal((await writer.write('m', 'workspace', workspace(0), 'new-branch')).revision, 8)
})

test('definite 422 failure permits a corrected new request without replaying invalid input', async () => {
  const h = harness(), writer = new h.Writer()
  writer.setRevision('m', 0)
  h.hooks.write = async (call, commit) => { if (call.body.currentFrame < 0) throw statusError(422, 'invalid frame'); return commit() }
  await assert.rejects(writer.write('m', 'workspace', workspace(-1), 'invalid'), error => error.status === 422)
  assert.equal(h.sessionStorage.getItem('annotation-write:A:m'), null)
  assert.equal((await writer.write('m', 'workspace', workspace(0), 'corrected')).revision, 1)
  assert.deepEqual(writes(h).map(call => call.key), ['invalid', 'corrected'])
})

test('switching accounts never replays another actor pending request', async () => {
  const h = harness(), writer = new h.Writer()
  writer.setRevision('m', 0)
  let lose = true
  h.hooks.write = async (call, commit) => { const result = commit(); if (call.actor === 'A' && lose) { lose = false; throw new Error('lost A response') } return result }
  await assert.rejects(writer.write('m', 'workspace', workspace(1), 'A-pending'), /lost A/)
  h.setActor('B')
  assert.equal((await writer.write('m', 'workspace', workspace(2), 'B-new')).revision, 2)
  assert.deepEqual(writes(h).map(call => [call.actor, call.key]), [['A', 'A-pending'], ['B', 'B-new']])
  assert(h.sessionStorage.getItem('annotation-write:A:m'))
  assert.equal(h.sessionStorage.getItem('annotation-write:B:m'), null)
})

test('queued work for the old account is never dispatched under the newly logged-in account', async () => {
  const h = harness(), writer = new h.Writer(), gate = deferred()
  writer.setRevision('m', 0)
  h.hooks.write = async (call, commit) => { const result = commit(); if (call.key === 'A-first') await gate.promise; return result }
  const first = writer.write('m', 'workspace', workspace(1), 'A-first')
  const queued = writer.write('m', 'workspace', workspace(2), 'A-queued')
  const firstRejected = assert.rejects(first, /账号已切换/), queuedRejected = assert.rejects(queued, /账号已切换/)
  await tick(); h.setActor('B')
  assert.equal((await writer.write('m', 'workspace', workspace(3), 'B-first')).revision, 2)
  gate.resolve(); await Promise.all([firstRejected, queuedRejected])
  assert.equal((await writer.write('m', 'workspace', workspace(4), 'B-next')).revision, 3)
  assert(!writes(h).some(call => call.key === 'A-queued'))
  assert.deepEqual(writes(h).filter(call => call.actor === 'B').map(call => call.body.expectedRevision), [1, 2])
})

test('a late old-account 409 cannot clear or poison the new-account pending write', async () => {
  const h = harness(), writer = new h.Writer(), aGate = deferred(), bGate = deferred()
  writer.setRevision('m', 0)
  h.hooks.write = async (call, commit) => {
    if (call.actor === 'A') { await aGate.promise; throw statusError(409, '工作区版本已变化') }
    if (call.key === 'B-pending') await bGate.promise
    return commit()
  }
  const a = writer.write('m', 'workspace', workspace(1), 'A-fails-later')
  const aRejected = assert.rejects(a)
  await tick(); h.setActor('B')
  const b = writer.write('m', 'workspace', workspace(2), 'B-pending')
  await tick(); assert(h.sessionStorage.getItem('annotation-write:B:m'))
  aGate.resolve(); await aRejected
  const preserved = h.sessionStorage.getItem('annotation-write:B:m')
  bGate.resolve(); await b
  assert(preserved, 'late A failure must not remove B retry protection')
  assert.equal((await writer.write('m', 'workspace', workspace(3), 'B-next')).revision, 2)
})

test('account switch while loading revision cannot schedule a write with another account snapshot', async () => {
  const h = harness(), writer = new h.Writer(), gate = deferred()
  h.hooks.read = async (call, read) => { const result = read(); if (call.actor === 'A') await gate.promise; return result }
  const a = writer.write('m', 'workspace', workspace(1), 'A-after-get')
  const rejected = assert.rejects(a, /账号已切换/)
  await tick(); h.setActor('B')
  assert.equal((await writer.write('m', 'workspace', workspace(2), 'B-current')).revision, 1)
  gate.resolve(); await rejected
  assert.deepEqual(writes(h).map(call => call.actor), ['B'])
})

test('legacy pending entries also retain the originating account across a late response', async () => {
  const h = harness(), writer = new h.Writer(), gate = deferred()
  h.sessionStorage.setItem('annotation-write:A:m', JSON.stringify({ key: 'old-A', kind: 'workspace', body: { ...workspace(1), expectedRevision: 0 } }))
  h.hooks.write = async (call, commit) => { const result = commit(); if (call.key === 'old-A') await gate.promise; return result }
  const replay = writer.replay('m')
  const replayOutcome = replay.then(() => ({ ok: true }), error => ({ error }))
  await tick(); h.setActor('B')
  assert.equal((await writer.write('m', 'workspace', workspace(2), 'B-current')).revision, 2)
  gate.resolve()
  const outcome = await replayOutcome
  assert(outcome.error, 'old-account replay must reject its result after account switch')
  assert.equal((await writer.write('m', 'workspace', workspace(3), 'B-next')).revision, 3)
})

test('a malformed successful write receipt cannot clear pending state or remove version protection', async () => {
  const h = harness(), writer = new h.Writer()
  writer.setRevision('m', 0)
  let malformed = true
  h.hooks.write = async (_call, commit) => { const result = commit(); if (malformed) { malformed = false; return {} } return result }
  await assert.rejects(writer.write('m', 'workspace', workspace(1), 'malformed-response'))
  assert(h.sessionStorage.getItem('annotation-write:A:m'), 'unverified response must keep original retry intent')
  assert.equal((await writer.write('m', 'workspace', workspace(1), 'malformed-response')).revision, 1)
  assert.equal(h.commits.length, 1)
  assert.deepEqual(writes(h).map(call => call.body.expectedRevision), [0, 0])
})

// Run the real tracking finalizer after a delayed response. This verifies the
// observable cleanup behavior without starting a model or touching user data.
const trackingSource = readFileSync(new URL('../src/stores/workspace/tracking.ts', import.meta.url), 'utf8')
const trackingTree = ts.createSourceFile('tracking.ts', trackingSource, ts.ScriptTarget.Latest, true)
let trackingOperation
function findTrackingOperation(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(trackingTree) === 'performTracking') trackingOperation = node.initializer
  ts.forEachChild(node, findTrackingOperation)
}
findTrackingOperation(trackingTree)
assert(trackingOperation?.body, 'the actual tracking operation must be available to the cleanup harness')
const trackingFinalizer = trackingOperation.body.statements.find(node => ts.isTryStatement(node))?.finallyBlock
assert(trackingFinalizer, 'the actual tracking operation must finish with cleanup')
const abandonedRule = trackingOperation.body.statements.find(node => ts.isVariableStatement(node) && node.getText(trackingTree).includes('const restartAbandoned'))
assert(abandonedRule, 'the actual restart ownership rule must be available to the cleanup harness')
const delayedTrackingCleanup = ts.transpileModule(
  `(async () => { ${abandonedRule.getText(trackingTree)} try { await responseGate } finally ${trackingFinalizer.getText(trackingTree)} })()`,
  { compilerOptions: { target: ts.ScriptTarget.ES2022 } },
).outputText
function trackingCleanupHarness({ restart = true, launched = false, unconfirmedJob = false, succeeded = false } = {}) {
  const gate = deferred(), scheduled = [], timers = []
  const context = {
    responseGate: gate.promise,
    restartIntent: restart ? { actorId: 'actor-A' } : undefined,
    launched,
    restartSerial: 7,
    runtime: { restartEpoch: 7 },
    currentActor: 'actor-A',
    mediaId: 'media-A',
    currentMediaId: { value: 'media-A' },
    // media-B tracks in the background; its state must survive media-A cleanup.
    trackingJobs: new Map([['media-B', { taskId: 'job-B' }], ...(unconfirmedJob ? [['media-A', { taskId: 'job-A' }]] : [])]),
    trackingMedia: new Set(['media-A', 'media-B']),
    trackingProgressByMedia: { value: { 'media-A': '正在追踪第 3 帧', 'media-B': '排队中' } },
    trackingStatusByMedia: { value: { 'media-A': { stage: 'tracking' }, 'media-B': { stage: 'queued' } } },
    trackingFinishedByMedia: { value: {} },
    trackingSucceeded: succeeded,
    setTimeout: (fn, ms) => timers.push({ fn, ms }),
    scheduleWorkspaceStateSave: mediaId => scheduled.push(mediaId),
  }
  context.useAuth = () => ({ user: { value: { id: context.currentActor } } })
  const completion = runInNewContext(delayedTrackingCleanup, context)
  return { context, scheduled, timers, completion, resolve: gate.resolve }
}
const otherVideoIntact = context => {
  assert(context.trackingMedia.has('media-B') && context.trackingJobs.has('media-B'), 'another video keeps tracking')
  assert.equal(context.trackingProgressByMedia.value['media-B'], '排队中', 'another video keeps its progress')
  assert.equal(context.trackingStatusByMedia.value['media-B']?.stage, 'queued', 'another video keeps its progress bar')
}

for (const [reason, switchContext] of [
  ['account changed', context => { context.currentActor = 'actor-B' }],
  ['restart was cancelled or page unmounted', context => { context.runtime.restartEpoch++ }],
  ['media changed', context => { context.currentMediaId.value = 'media-B' }],
]) {
  test(`a late unlaunched restart releases only its own video when ${reason}`, async () => {
    const h = trackingCleanupHarness()
    switchContext(h.context)
    h.resolve(); await h.completion
    assert(!h.context.trackingMedia.has('media-A'), 'the operation owns and releases its video')
    otherVideoIntact(h.context)
    assert.deepEqual(h.scheduled, [], 'a stale operation must not schedule a save')
  })
}

test('a launched restart keeps running after the user opens another video and does not save the hidden video', async () => {
  const h = trackingCleanupHarness({ launched: true })
  h.context.runtime.restartEpoch++; h.context.currentMediaId.value = 'media-B'
  h.resolve(); await h.completion
  assert(!h.context.trackingMedia.has('media-A'))
  otherVideoIntact(h.context)
  assert.deepEqual(h.scheduled, [])
})

test('a matching restart clears its state and schedules its completed workspace', async () => {
  const h = trackingCleanupHarness()
  h.resolve(); await h.completion
  assert(!h.context.trackingMedia.has('media-A'))
  assert.equal(h.context.trackingProgressByMedia.value['media-A'], undefined)
  assert.equal(h.context.trackingStatusByMedia.value['media-A'], undefined, 'its progress bar is removed')
  assert.equal(h.context.trackingFinishedByMedia.value['media-A'], undefined, 'a run that did not succeed never shows the finished state')
  otherVideoIntact(h.context)
  assert.deepEqual(h.scheduled, ['media-A'])
})

test('a successful run shows its finished state briefly and only for its own video', async () => {
  const h = trackingCleanupHarness({ restart: false, succeeded: true })
  h.resolve(); await h.completion
  assert.equal(h.context.trackingStatusByMedia.value['media-A'], undefined)
  assert.equal(h.context.trackingFinishedByMedia.value['media-A'], true)
  assert.equal(h.timers.length, 1)
  h.timers[0].fn()
  assert.equal(h.context.trackingFinishedByMedia.value['media-A'], undefined, 'the finished state leaves on its own')
  otherVideoIntact(h.context)
})

test('an unconfirmed job keeps its video locked against saving', async () => {
  const h = trackingCleanupHarness({ restart: false, unconfirmedJob: true })
  h.resolve(); await h.completion
  assert(h.context.trackingJobs.has('media-A'), 'the original request stays available for retry')
  assert.deepEqual(h.scheduled, [])
})

test('ordinary tracking finishing in the background releases its video without saving it', async () => {
  const h = trackingCleanupHarness({ restart: false })
  h.context.currentMediaId.value = 'media-B'
  h.resolve(); await h.completion
  assert(!h.context.trackingMedia.has('media-A'))
  otherVideoIntact(h.context)
  assert.deepEqual(h.scheduled, [])
})

test('ordinary tracking on the open video saves its workspace', async () => {
  const h = trackingCleanupHarness({ restart: false })
  h.context.currentActor = 'actor-B'; h.context.runtime.restartEpoch++
  h.resolve(); await h.completion
  assert(!h.context.trackingMedia.has('media-A'))
  assert.deepEqual(h.scheduled, ['media-A'])
})

const box=(frame,x=10)=>({id:`box-${frame}`,objectId:1,frameIndex:frame,source:'manual',bbox:{x,y:10,width:5,height:5}})
test('collaboration merges different frames but reports simultaneous edits to one head',()=>{
  const h=harness(),api={};runInNewContext(compiled,{exports:api,Error,require:()=>({})})
  const base={manualAnnotations:[box(0),box(1)]}
  const local={manualAnnotations:[box(0,20),box(1)]},remote={manualAnnotations:[box(0),box(1,30)]}
  const result=api.mergeWorkspace(base,local,remote)
  assert.deepEqual(Array.from(result.manualAnnotations,o=>o.bbox.x),[20,30])
  assert.throws(()=>api.mergeWorkspace(base,local,{manualAnnotations:[box(0,40),box(1)]}),e=>e.code==='WORKSPACE_MERGE_CONFLICT'&&e.entries.length===1)
})
test('collaboration never merges across reset generations or restores a remotely deleted object',()=>{
  const api={};runInNewContext(compiled,{exports:api,Error,require:()=>({})})
  assert.throws(()=>api.mergeWorkspace({generationId:'old'},{generationId:'old'},{generationId:'new'}),/覆盖/)
  assert.throws(()=>api.mergeWorkspace({manualAnnotations:[box(0)]},{manualAnnotations:[box(0,20)]},{manualAnnotations:[],deletedObjectIds:[1]}),e=>e.code==='WORKSPACE_MERGE_CONFLICT')
})
test('view-only changes are kept local and do not write or advance shared revisions',async()=>{
  const h=harness(),writer=new h.Writer();writer.setBaseline('m',{exists:true,mediaId:'m',revision:0,manualAnnotations:[]})
  await writer.write('m','workspace',{manualAnnotations:[],currentFrame:8,display:{zoom:3}},'view')
  assert.equal(writes(h).length,0)
})

test('two clients merge disjoint edits using fresh keys and keep exact replays after response loss',async()=>{
  const h=harness(),a=new h.Writer(),b=new h.Writer()
  let server={exists:true,mediaId:'m',revision:0,manualAnnotations:[box(0),box(1)]}
  h.hooks.read=()=>copy(server)
  let lose=false
  h.hooks.write=(call,commit)=>{const result=commit();if(result.revision>server.revision)server={...copy(call.body),exists:true,mediaId:'m',revision:result.revision};if(lose){lose=false;throw new Error('lost merged response')}return result}
  a.setBaseline('m',server);b.setBaseline('m',server)
  await a.write('m','workspace',{manualAnnotations:[box(0,20),box(1)]},'a')
  let first=true
  h.hooks.write=(call,commit)=>{const result=commit();if(result.revision>server.revision)server={...copy(call.body),exists:true,mediaId:'m',revision:result.revision};if(first){first=false;throw new Error('lost merged response')}return result}
  await assert.rejects(b.write('m','workspace',{manualAnnotations:[box(0),box(1,30)]},'b'),/lost merged/)
  await b.write('m','workspace',{manualAnnotations:[box(0),box(1,30)]},'after-replay')
  assert.equal(server.revision,2)
  assert.deepEqual(server.manualAnnotations.map(o=>o.bbox.x),[20,30])
  const mergedCalls=writes(h).filter(c=>c.body.expectedRevision===1)
  assert.equal(mergedCalls[0].key,mergedCalls[1].key)
  assert.deepEqual(mergedCalls[0].body,mergedCalls[1].body)
  assert.notEqual(mergedCalls[0].key,'b')
  assert.deepEqual(server.manualAnnotations.map(o=>o.bbox.x),[20,30])
})
test('same-head conflicts retain the draft and explicit resolution rechecks the remote value',async()=>{
  const h=harness(),a=new h.Writer(),b=new h.Writer()
  let server={exists:true,mediaId:'m',revision:0,manualAnnotations:[box(0)]}
  h.hooks.read=()=>copy(server)
  h.hooks.write=(call,commit)=>{const result=commit();server={...copy(call.body),exists:true,mediaId:'m',revision:result.revision};return result}
  a.setBaseline('m',server);b.setBaseline('m',server)
  await a.write('m','workspace',{manualAnnotations:[box(0,20)]})
  await assert.rejects(b.write('m','workspace',{manualAnnotations:[box(0,30)]}),e=>e.code==='WORKSPACE_MERGE_CONFLICT')
  const conflict=b.getConflict('m'),entry=conflict.entries[0]
  assert(await b.getDraft('m'))
  await b.resolve('m',{[entry.key]:{value:'local',remote:entry.remote}})
  assert.equal(server.manualAnnotations[0].bbox.x,30)
  assert.equal(await b.getDraft('m'),null)
})
test('explicit deletion choice preserves the complete identity instead of silently dropping other frames',()=>{
  const api={};runInNewContext(compiled,{exports:api,Error,require:()=>({})})
  const base={manualAnnotations:[box(0),box(1)]},local={manualAnnotations:[],deletedObjectIds:[1]},remote={manualAnnotations:[box(0,30),box(1)]}
  let conflict;try{api.mergeWorkspace(base,local,remote)}catch(e){conflict=e}
  assert.equal(conflict.entries.length,1)
  const key=conflict.entries[0].key
  const keep=api.mergeWorkspace(base,local,remote,{[key]:{value:'remote',remote:conflict.entries[0].remote}})
  assert.equal(keep.manualAnnotations.length,2);assert.equal(keep.deletedObjectIds.length,0)
  const remove=api.mergeWorkspace(base,local,remote,{[key]:{value:'local',remote:conflict.entries[0].remote}})
  assert.equal(remove.manualAnnotations.length,0);assert.deepEqual(Array.from(remove.deletedObjectIds),[1])
})
test('merged manual references keep the latest adopted frame once per identity',()=>{
  const api={};runInNewContext(compiled,{exports:api,Error,require:()=>({})})
  const base={manualAnnotations:[]},local={manualAnnotations:[box(1)],manualBaselines:[{objectId:1,frameIndex:1,bbox:[1,2,3,4]}]},remote={manualAnnotations:[box(0)],manualBaselines:[{objectId:1,frameIndex:0,bbox:[2,3,4,5]}]}
  const result=api.mergeWorkspace(base,local,remote)
  assert.equal(result.manualBaselines.length,1);assert.equal(result.manualBaselines[0].frameIndex,1)
})
test('reset generation archives an old draft without replaying it or blocking the new workspace',async()=>{
  const h=harness(),writer=new h.Writer()
  await writer.saveDraft('m',{generationId:'old',manualAnnotations:[]},{generationId:'old',manualAnnotations:[box(0)]})
  const result=await writer.restoreDraft('m',{exists:true,mediaId:'m',revision:8,generationId:'new',manualAnnotations:[]})
  assert.equal(result,null);assert.equal(writes(h).length,0)
  assert.equal(await writer.getDraft('m'),null)
  assert(h.sessionStorage.getItem('annotation-write:A:m:draft:archive:old'))
})
test('independent new objects that picked the same number stay separate, including replay after remapping',()=>{
  const api={};runInNewContext(compiled,{exports:api,Error,require:()=>({})})
  const local={manualAnnotations:[{...box(0,10),creationId:'new-A'}]},remote={manualAnnotations:[{...box(1,70),creationId:'new-B'}]}
  const result=api.mergeWorkspace({manualAnnotations:[]},local,remote)
  assert.equal(new Set(result.manualAnnotations.map(o=>o.objectId)).size,2)
  assert.equal(api.mergeWorkspace({manualAnnotations:[]},local,result).manualAnnotations.length,2)
  const aligned=api.alignWorkspaceIds(local,result)
  assert.equal(aligned.manualAnnotations[0].objectId,result.manualAnnotations.find(o=>o.creationId==='new-A').objectId)
})
test('an older completed save cannot clear a newer local draft',async()=>{
  const h=harness(),writer=new h.Writer(),gate=deferred(),entered=deferred()
  const base={exists:true,mediaId:'m',revision:0,manualAnnotations:[box(0)]}
  writer.setBaseline('m',base)
  h.hooks.write=async(call,commit)=>{entered.resolve();await gate.promise;return commit()}
  const saving=writer.write('m','workspace',{manualAnnotations:[box(0,20)]})
  await entered.promise
  await writer.saveDraft('m',base,{manualAnnotations:[box(0,30)]})
  gate.resolve();await saving
  assert.equal((await writer.getDraft('m')).local.manualAnnotations[0].bbox.x,30)
})
test('collaboration never reverts the independent endpoint canonical identity mapping',()=>{
  const api={};runInNewContext(compiled,{exports:api,Error,require:()=>({})})
  const first={...box(0),creationId:'first'},tail={...box(5),objectId:91,creationId:'tail'}
  const base={manualAnnotations:[first,tail]},local={manualAnnotations:[first,{...tail,objectId:1,endpointOriginalObjectId:91,endpointAnchorHash:'verified-endpoint'}]}
  const result=api.mergeWorkspace(base,local,base)
  assert.deepEqual(Array.from(result.manualAnnotations,o=>o.objectId),[1,1])
})
test('a prior single-frame tombstone does not hide a later whole-object deletion conflict',()=>{
  const api={};runInNewContext(compiled,{exports:api,Error,require:()=>({})})
  const base={manualAnnotations:[box(1)],deletedFrameObjects:[{objectId:1,frameIndex:0}]}
  const local={...base,manualAnnotations:[],deletedObjectIds:[1]},remote={...base,manualAnnotations:[box(1,30)]}
  let conflict;try{api.mergeWorkspace(base,local,remote)}catch(error){conflict=error}
  assert.equal(conflict.entries[0].key,'object-delete:1')
  const kept=api.mergeWorkspace(base,local,remote,{[conflict.entries[0].key]:{value:'remote',remote:conflict.entries[0].remote}})
  assert.equal(kept.deletedObjectIds.length,0);assert.equal(kept.manualAnnotations[0].bbox.x,30)
})

// Browser cache that cannot be opened (quota, disabled site data, corrupted profile).
const brokenIndexedDB = { open: () => { const request = {}; setImmediate(() => request.onerror?.()); return request } }
test('a broken local recovery cache warns but never blocks saving to the server',async()=>{
  const h=harness({indexedDB:brokenIndexedDB}),writer=new h.Writer(),warnings=[]
  writer.onStorageWarning=message=>warnings.push(message)
  let server={exists:true,mediaId:'m',revision:0,manualAnnotations:[box(0)]}
  h.hooks.read=()=>copy(server)
  h.hooks.write=(call,commit)=>{const result=commit();server={...copy(call.body),exists:true,mediaId:'m',revision:result.revision};return result}
  writer.setBaseline('m',server)
  const result=await writer.write('m','workspace',{manualAnnotations:[box(0,20)]})
  assert.equal(result.revision,1);assert.equal(server.manualAnnotations[0].bbox.x,20)
  assert(warnings.length>0,'the user is told that refresh recovery is unavailable')
  assert.equal(await writer.getDraft('m'),null)
})
test('conflict entries say which side deleted the object and where it is',()=>{
  const api={};runInNewContext(compiled,{exports:api,Error,require:()=>({})})
  let conflict;try{api.mergeWorkspace({manualAnnotations:[box(3)]},{manualAnnotations:[box(3,20)]},{manualAnnotations:[],deletedObjectIds:[1]})}catch(e){conflict=e}
  const entry=conflict.entries[0]
  assert.equal(entry.deleted,true);assert.equal(entry.localDeleted,false);assert.equal(entry.frameIndex,3);assert.equal(entry.objectId,1)
  try{api.mergeWorkspace({manualAnnotations:[box(2)]},{manualAnnotations:[box(2,20)]},{manualAnnotations:[box(2,30)]})}catch(e){conflict=e}
  assert.equal(conflict.entries[0].deleted,false);assert.equal(conflict.entries[0].frameIndex,2)
})
