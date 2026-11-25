// Microfluidic mode (decision 0008): pure rules the page shares, tested without a browser.

export type FlowState = 'new' | 'finding' | 'failed' | 'scan' | 'wall' | 'cards' | 'done'
export type FlowAnswer = 'yes' | 'no' | 'unsure'
export type FlowStep = 'scan' | 'wall' | 'cards' | 'done'

export interface FlowProgress {
  runId: string; targets: number; pages: number; pagesDone: number; second: number | null; secondDone: number
  yes: number; unsure: number; scanNeeded: boolean; scanDone: boolean; state: FlowStep
  lostFrames?: number; detector?: 'ok' | 'none' | 'failed'; unsteady?: boolean; slow?: boolean
  lastActorId: number | null; lastActorName?: string | null; lastActivityAt: string | null
}
export interface FlowRecheckStats { sperm: number; bothYes: number; firstMissed: number; secondMissed: number; differences: number }
/** A second, blind round over the same targets. Optional; the video counts as finished with or without it. */
export interface FlowRecheck { id: string; state: 'open' | 'settling' | 'done'; startedBy: number; progress: FlowProgress; samePerson: boolean; stats?: FlowRecheckStats }
export interface FlowVerdict { answer: FlowAnswer; dropped: number[] }
export interface FlowDifference extends FlowTile { first: FlowVerdict; second: FlowVerdict; settled: (FlowVerdict & { revision: number }) | null }
export interface FlowTask { taskId: string; status: string; stage: string; message: string; processedFrames: number | null; totalFrames: number | null }
export interface FlowItem {
  mediaId: string; videoName: string; folderPath: string; sample: string; frameCount: number; fps: number
  width: number; height: number; sizeBytes: number; task: FlowTask | null; state: FlowState; progress: FlowProgress | null
  recheck?: FlowRecheck | null; imported?: boolean
}
export interface FlowTile { key: string; n: number; best: number; frames: number[] }

export const ANSWERS: { key: FlowAnswer; label: string; tone: string }[] = [
  { key: 'yes', label: '是精子', tone: 'yes' },
  { key: 'no', label: '不是', tone: 'no' },
  { key: 'unsure', label: '看不清', tone: 'unsure' },
]
const TODO: FlowState[] = ['scan', 'wall', 'cards']

/** How far one video is, 0–1. The two passes weigh by how long they take: a page of 24 about as long as four cards. */
export function donePart(item: Pick<FlowItem, 'state' | 'task' | 'progress'>): number {
  const p = item.progress
  if (item.state === 'finding') return item.task?.totalFrames ? Math.min(1, (item.task.processedFrames ?? 0) / item.task.totalFrames) : 0
  if (!p) return 0
  if (item.state === 'done') return 1
  const wall = p.pages * 4, cards = p.second ?? Math.max(4, Math.round(p.targets * .06))
  return Math.min(.99, (p.pagesDone * 4 + p.secondDone) / Math.max(1, wall + cards))
}

/** Added after "finished": a re-check under way never turns a finished video back into an unfinished one. */
export function recheckText(recheck: FlowRecheck | null | undefined): string {
  if (!recheck) return ''
  if (recheck.state === 'done') return ' · 复查过'
  if (recheck.state === 'settling') return ' · 复查待定'
  return ` · 复查 ${Math.round(donePart({ state: recheck.progress.state, task: null, progress: recheck.progress }) * 100)}%`
}
/** First difference nobody has settled, after ``from`` and wrapping; -1 when all are. */
export function nextDifference(list: FlowDifference[], from: number): number {
  return nextCard(list.map(entry => entry.key), key => !!list.find(entry => entry.key === key)!.settled, from)
}

/** The one line under a video's name. */
export function stateText(item: FlowItem): { text: string; tone: 'muted' | 'busy' | 'todo' | 'ok' | 'bad' } {
  const p = item.progress, percent = `${Math.round(donePart(item) * 100)}%`
  if (item.state === 'finding') return { text: item.task?.status === 'queued' ? '排队找目标' : item.task?.stage === 'linking' ? '正在连成目标' : item.task?.stage === 'drawing' ? `准备小图 ${percent}` : `找目标 ${percent}`, tone: 'busy' }
  if (item.state === 'failed') return { text: '找目标失败', tone: 'bad' }
  if (item.state === 'new' || !p) return { text: '还没找目标', tone: 'muted' }
  if (item.state === 'done') return { text: `判完 · ${p.yes} 个精子${recheckText(item.recheck)}`, tone: 'ok' }
  const started = p.pagesDone || p.secondDone || p.scanDone
  if (!started) return { text: `待判 · ${p.targets} 个目标`, tone: 'todo' }
  return { text: `判了 ${percent}`, tone: 'todo' }
}

/** The next video with something to judge, after the current one and wrapping; null when there is none. */
export function nextTodo<T extends { mediaId: string; state: FlowState }>(items: T[], currentId: string | null): T | null {
  const at = items.findIndex(item => item.mediaId === currentId)
  for (let step = 1; step <= items.length; step++) {
    const item = items[(at + step + items.length) % items.length]!
    if (item.mediaId !== currentId && TODO.includes(item.state)) return item
  }
  return null
}

/** Square tiles as large as fit: columns and edge length. */
export function fitGrid(count: number, width: number, height: number, gap: number, maxSize = Infinity, maxCols = 12): { cols: number; size: number } {
  let best = { cols: 1, size: 0 }
  for (let cols = 1; cols <= Math.min(maxCols, Math.max(1, count)); cols++) {
    const rows = Math.ceil(Math.max(1, count) / cols)
    const size = Math.floor(Math.min((width - gap * (cols - 1)) / cols, (height - gap * (rows - 1)) / rows, maxSize))
    if (size > best.size) best = { cols, size }
  }
  return best.size > 0 ? best : { cols: Math.min(6, Math.max(1, count)), size: 40 }
}

/** CSS background position of frame k in a film strip of n equal cells. */
export const cellPosition = (n: number, k: number) => n > 1 ? `${k / (n - 1) * 100}% 0` : '0 0'

/** Next frame of a playing strip, past the frames that were clicked off. */
export function stepFrame(n: number, k: number, off: ReadonlySet<number> = new Set()): number {
  for (let step = 1; step <= n; step++) {
    const next = (k + step) % n
    if (!off.has(next)) return next
  }
  return k
}

/** First wall page nobody has handed in; -1 once all are. */
export function firstOpenPage(pageCount: number, saved: Record<string, unknown>): number {
  for (let page = 0; page < pageCount; page++) if (!(String(page) in saved)) return page
  return -1
}

/** Next card without an answer after ``from``, wrapping; -1 when all have one. */
export function nextCard(keys: string[], answered: (key: string) => boolean, from: number): number {
  for (let step = 1; step <= keys.length; step++) {
    const at = (from + step + keys.length) % keys.length
    if (!answered(keys[at]!)) return at
  }
  return -1
}

/** Clicking a frame off or back on. At least one frame always stays. */
export function toggleDropped(frames: number[], dropped: number[], frame: number): number[] {
  if (dropped.includes(frame)) return dropped.filter(f => f !== frame)
  if (!frames.includes(frame) || frames.filter(f => !dropped.includes(f)).length <= 1) return dropped
  return [...dropped, frame].sort((a, b) => a - b)
}

export interface FlowMisses { planted: number; plantedMissed: number; blindMarks: number; blindUnmatched: number; yes: number; secondPassOnly: number; recheck?: FlowRecheckStats & { videos: number } }
/** Measured misses in plain words; nothing is claimed before there is enough to measure. */
export function missLines(m: FlowMisses | null): string[] {
  if (!m) return []
  const percent = (part: number, whole: number) => `${Math.round(part / whole * 100)}%`
  const out: string[] = []
  if (m.planted) out.push(`混进去的已知精子：${m.planted} 个里漏点 ${m.plantedMissed} 个${m.planted >= 20 ? `（${percent(m.plantedMissed, m.planted)}）` : ''}`)
  if (m.blindMarks) out.push(`整帧查漏：点出 ${m.blindMarks} 个，程序没有候选的 ${m.blindUnmatched} 个${m.blindMarks >= 20 ? `（${percent(m.blindUnmatched, m.blindMarks)}）` : ''}`)
  if (m.yes) out.push(`判为精子 ${m.yes} 个，其中第一遍没点、第二遍补回 ${m.secondPassOnly} 个`)
  const r = m.recheck
  if (r?.videos && r.sperm) out.push(`盲复查 ${r.videos} 段：共 ${r.sperm} 个精子，第一次漏 ${r.firstMissed} 个${r.sperm >= 20 ? `（${percent(r.firstMissed, r.sperm)}）` : ''}，第二次漏 ${r.secondMissed} 个`)
  return out
}

/** Videos under their folders, in list order. */
export function byFolder(items: FlowItem[]): { folder: string; items: FlowItem[] }[] {
  const out: { folder: string; items: FlowItem[] }[] = []
  for (const item of items) {
    const last = out[out.length - 1]
    if (last && last.folder === item.folderPath) last.items.push(item)
    else out.push({ folder: item.folderPath, items: [item] })
  }
  return out
}
