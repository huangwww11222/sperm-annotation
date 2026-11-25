// Answers on their way to the server, one at a time and in order. Kept free of Vue so the
// failure rules can be tested directly: what could not be sent stays until it is, the latest
// choice wins, and an answer the server refuses as outdated is dropped, never forced through.

export interface SaveQueueHooks {
  changed: (waiting: number, sending: boolean, error: string) => void
  /** The server's copy has moved on: waiting answers no longer apply to what it holds. */
  outdated: (error: unknown) => boolean
  onOutdated: (error: unknown, id: string) => Promise<void>
}

export class SaveQueue {
  private jobs = new Map<string, { send: () => Promise<void> }>()
  private running: Promise<void> | null = null
  private error = ''
  constructor(private hooks: SaveQueueHooks) {}

  get size() { return this.jobs.size }
  private report(sending: boolean) { this.hooks.changed(this.jobs.size, sending, this.error) }

  /** ``send`` reads the current choice when its turn comes, so a later click replaces an unsent one. */
  add(id: string, send: () => Promise<void>) {
    this.jobs.delete(id)
    this.jobs.set(id, { send })
    this.report(!!this.running)
    void this.flush()
  }

  /** True when nothing is left unsaved. Also the retry after a failure. */
  async flush(): Promise<boolean> {
    while (this.running) await this.running
    if (!this.jobs.size) return true
    this.running = this.work().finally(() => { this.running = null })
    await this.running
    this.report(false)
    return !this.jobs.size
  }

  private async work() {
    this.error = ''
    this.report(true)
    while (this.jobs.size) {
      const [id, job] = this.jobs.entries().next().value as [string, { send: () => Promise<void> }]
      try { await job.send() } catch (error) {
        if (this.hooks.outdated(error)) { this.jobs.clear(); this.report(false); await this.hooks.onOutdated(error, id); return }
        this.error = (error as { message?: string })?.message || '保存失败'
        return
      }
      // Changed again while it was on its way: that newer choice is still to be sent.
      if (this.jobs.get(id) === job) this.jobs.delete(id)
      this.report(true)
    }
  }
}
