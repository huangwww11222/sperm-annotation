import { createRequestId } from '../utils/browserCompat'
import { trackApi, type TrackWorkspaceState, type TrackingFeedbackInput } from '../api/trackApi'

export type WorkspaceDraft = Omit<TrackWorkspaceState, 'exists' | 'mediaId'>
type PendingWrite = { scope?: string; key: string; kind: 'workspace' | 'feedback'; body: WorkspaceDraft | TrackingFeedbackInput }
type WriteResult = { revision: number; [key: string]: unknown }

/** Serialize writes and retain the exact request on an ambiguous network failure.
 * The server receipt is checked before the revision, so replay never applies twice.
 * Session storage is scoped to the signed-in actor and contains no auth token.
 */
export class WorkspaceWrites {
  private revisions = new Map<string, number>()
  private queues = new Map<string, Promise<unknown>>()
  private pending = new Map<string, PendingWrite>()
  private conflicts = new Map<string, Error>()
  private storageKey(mediaId: string) {
    let actor = 'unknown'
    try { actor = String(JSON.parse(localStorage.getItem('rare-sperm-auth') || '{}').id || actor) } catch {}
    return `annotation-write:${actor}:${mediaId}`
  }
  private read(mediaId: string) {
    const scope = this.storageKey(mediaId)
    if (this.pending.has(scope)) return this.pending.get(scope)
    try {
      const value = JSON.parse(sessionStorage.getItem(scope) || 'null') as PendingWrite | null
      if (value?.key && ['workspace', 'feedback'].includes(value.kind)) {
        if (value.scope && value.scope !== scope) throw new Error('待重试请求的账号不匹配')
        value.scope = scope
        this.pending.set(scope, value)
        return value
      }
    } catch (error) { console.warn('[annotation.pending_read_failed]', { mediaId, error }) }
    return undefined
  }
  private remember(mediaId: string, value?: PendingWrite, scope = this.storageKey(mediaId)) {
    if (value) this.pending.set(scope, value); else this.pending.delete(scope)
    try {
      if (value) sessionStorage.setItem(scope, JSON.stringify(value))
      else sessionStorage.removeItem(scope)
    } catch (error) { console.warn('[annotation.pending_cache_failed]', { mediaId, error }) }
  }
  setRevision(mediaId: string, revision: number) { this.revisions.set(this.storageKey(mediaId), revision); this.conflicts.delete(this.storageKey(mediaId)) }
  getRevision(mediaId: string) { return this.revisions.get(this.storageKey(mediaId)) }
  discardForReset(mediaId: string, revision: number) {
    // Only a confirmed server generation change authorizes dropping an old
    // ambiguous request. Ordinary conflicts must retain the reload requirement.
    return this.enqueue(mediaId, async () => {
      this.remember(mediaId)
      this.setRevision(mediaId, revision)
    })
  }
  private async send(mediaId: string, request: PendingWrite): Promise<WriteResult> {
    const scope = request.scope || this.storageKey(mediaId)
    request.scope = scope
    if (scope !== this.storageKey(mediaId)) throw new Error('账号已切换，请重新加载当前账号的工作区')
    this.remember(mediaId, request, scope)
    try {
      const result = request.kind === 'workspace'
        ? await trackApi.saveWorkspaceState(mediaId, request.body as WorkspaceDraft, request.key)
        : await trackApi.submitFeedback(mediaId, request.body as TrackingFeedbackInput, request.key)
      if (!Number.isInteger(result.revision) || result.revision < 0) throw new Error('服务器未返回有效保存版本，请使用原请求重试')
      if (request.scope && request.scope !== this.storageKey(mediaId)) throw new Error('账号已切换，请重新加载当前账号的工作区')
      this.revisions.set(scope, result.revision)
      this.remember(mediaId, undefined, scope)
      return result as unknown as WriteResult
    } catch (error) {
      // A definite validation/conflict response is not a lost response. Never
      // silently rebase it onto someone else's changes. Reload is required.
      const status = Number((error as { status?: number }).status)
      if (status >= 400 && status < 500 && status !== 408 && status !== 429) {
        this.remember(mediaId, undefined, scope)
        if (status === 409 && error instanceof Error && /工作区版本|重试标识/.test(error.message)) this.conflicts.set(scope, error)
      }
      console.error('[annotation.write_failed]', { mediaId, kind: request.kind, key: request.key, error })
      throw error
    }
  }
  private enqueue<T>(mediaId: string, operation: () => Promise<T>) {
    const scope = this.storageKey(mediaId)
    const task = (this.queues.get(scope) || Promise.resolve()).catch(() => {}).then(() => {
      if (scope !== this.storageKey(mediaId)) throw new Error('账号已切换，请重新加载当前账号的工作区')
      return operation()
    })
    this.queues.set(this.storageKey(mediaId), task)
    void task.finally(() => { if (this.queues.get(scope) === task) this.queues.delete(scope) }).catch(() => {})
    return task
  }
  replay(mediaId: string) {
    return this.enqueue(mediaId, async () => {
      const pending = this.read(mediaId)
      if (pending) await this.send(mediaId, pending)
    })
  }
  write(mediaId: string, kind: PendingWrite['kind'], body: PendingWrite['body'], key = createRequestId()) {
    return this.enqueue(mediaId, async () => {
      if (this.conflicts.has(this.storageKey(mediaId))) throw this.conflicts.get(this.storageKey(mediaId))
      const pending = this.read(mediaId)
      if (pending) {
        const result = await this.send(mediaId, pending)
        if (pending.key === key) return result
      }
      if (!this.revisions.has(this.storageKey(mediaId))) {
        const scope = this.storageKey(mediaId)
        const state = await trackApi.getWorkspaceState(mediaId)
        if (scope !== this.storageKey(mediaId)) throw new Error('账号已切换，请重新加载当前账号的工作区')
        this.setRevision(mediaId, state.revision || 0)
      }
      const request = { scope: this.storageKey(mediaId), kind, key, body: { ...body, expectedRevision: this.revisions.get(this.storageKey(mediaId))! } }
      return this.send(mediaId, request)
    })
  }
}
