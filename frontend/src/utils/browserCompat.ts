/** Chrome 93 and HTTP intranets: neither AbortSignal.timeout nor randomUUID is required. */
export function createRequestId(): string {
  // getRandomValues is available over HTTP. Keep cryptographic randomness for
  // idempotency keys; Date.now/Math.random are not a safe substitute.
  const bytes = new Uint8Array(16)
  globalThis.crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

/** The operation must include reading the response body, not only fetch headers. */
export async function withRequestTimeout<T>(
  milliseconds: number,
  operation: (signal: AbortSignal) => Promise<T>,
  callerSignal?: AbortSignal | null,
): Promise<T> {
  const controller = new AbortController()
  let timedOut = false
  const cancel = () => controller.abort()
  if (callerSignal?.aborted) throw new DOMException('请求已取消', 'AbortError')
  callerSignal?.addEventListener('abort', cancel, { once: true })
  const timer = setTimeout(() => { timedOut = true; controller.abort() }, milliseconds)
  try {
    const result = await operation(controller.signal)
    // Some JSON readers catch an aborted body and return {}. Never report that
    // as a successful mutation; callers must retain their original retry key.
    if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
    return result
  } catch (error) {
    if (timedOut) throw new DOMException('请求超时，请重试', 'TimeoutError')
    throw error
  } finally {
    clearTimeout(timer)
    callerSignal?.removeEventListener('abort', cancel)
  }
}
