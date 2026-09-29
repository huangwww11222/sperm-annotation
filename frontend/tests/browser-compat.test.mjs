import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { webcrypto } from 'node:crypto'

const code = readFileSync(new URL('../../work/browser-compat/browserCompat.js', import.meta.url), 'utf8')
const { createRequestId, withRequestTimeout } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))

test('HTTP / Chrome 93 identifiers need only getRandomValues and preserve UUID v4 entropy', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues: array => webcrypto.getRandomValues(array) } })
  try {
    const ids = Array.from({ length: 10000 }, createRequestId)
    assert.equal(new Set(ids).size, ids.length)
    assert(ids.every(id => /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id)))
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'crypto', descriptor)
    else delete globalThis.crypto
  }
})

const untilAbort = signal => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true }))

test('timeout works without AbortSignal.timeout and aborts response body reads', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(AbortSignal, 'timeout')
  Object.defineProperty(AbortSignal, 'timeout', { configurable: true, value: undefined })
  try {
    await assert.rejects(withRequestTimeout(10, async signal => {
      // Headers have arrived, but reading the response body is still pending.
      const response = { ok: true, blob: () => untilAbort(signal) }
      return response.blob()
    }), { name: 'TimeoutError' })
  } finally { Object.defineProperty(AbortSignal, 'timeout', descriptor) }
})

test('swallowed response body abort cannot become a successful mutation', async () => {
  await assert.rejects(withRequestTimeout(10, async signal => untilAbort(signal).catch(() => ({}))), { name: 'TimeoutError' })
})

test('successful and failed operations clear timeout instead of aborting later', async () => {
  let successSignal, failedSignal
  assert.equal(await withRequestTimeout(10, async signal => { successSignal = signal; return 42 }), 42)
  const failure = new Error('503')
  await assert.rejects(withRequestTimeout(10, async signal => { failedSignal = signal; throw failure }), error => error === failure)
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(successSignal.aborted, false)
  assert.equal(failedSignal.aborted, false)
})

test('caller cancellation is preserved and its listener is removed after completion', async () => {
  const controller = new AbortController()
  const waiting = withRequestTimeout(500, async signal => untilAbort(signal), controller.signal)
  controller.abort()
  await assert.rejects(waiting, { name: 'AbortError' })
  await assert.rejects(withRequestTimeout(500, async () => assert.fail('must not start'), controller.signal), { name: 'AbortError' })
  const complete = new AbortController()
  let completedSignal
  await withRequestTimeout(500, async signal => { completedSignal = signal }, complete.signal)
  complete.abort()
  assert.equal(completedSignal.aborted, false)
})
