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
function harness() {
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
    exports, localStorage, sessionStorage, Error,
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
