// Presentation only: pauseDiagnostics has already selected the server's
// triggering evidence. Auxiliary measurements must never become pause causes.
export type PauseEvidenceMetric = {
  label: string
  current: string
  reference: string
  value: number
  bound?: number
  unit: '%' | '倍'
  immediate: boolean
  scale?: { current: number; bound: number }
}
export type PauseAuxiliaryMetric = { label: string; value: string }

// IDs remain stable in data. Only their readable inline presentation changes.
export const objectNumberText = (text: string) => text.replace(/#(\d+)/g, '对象 $1')

const finite = (value: string | undefined) => {
  if (value == null || !value.trim()) return undefined
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 ? number : undefined
}

function metric(label: string, raw: string, rest: string, unit: '%' | '倍'): PauseEvidenceMetric | null {
  const value = finite(raw)
  if (value == null) return null
  const boundary = /(?:低于观察下限|高于观察上限|达到观察界限|观察界限)\s*([\d.]+)\s*(%|倍)/.exec(rest)
  const bound = boundary?.[2] === unit ? finite(boundary[1]) : undefined
  const direction = rest.includes('低于观察下限') ? '观察下限' : rest.includes('高于观察上限') ? '观察上限' : '观察界限'
  const max = bound == null ? undefined : Math.max(value, bound, unit === '%' ? 100 : 1) * 1.12
  return {
    label, current: `${raw}${unit}`, value, bound, unit,
    reference: bound == null ? '未记录比较界限' : `${direction} ${boundary![1]}${unit}`,
    immediate: rest.includes('立即暂停'),
    scale: max == null || max <= 0 ? undefined : { current: value / max * 100, bound: bound! / max * 100 },
  }
}

export function presentPauseEvidence(triggerSummary: string | undefined, measurements: readonly string[]) {
  const triggerMetrics: PauseEvidenceMetric[] = [], triggerNotes: string[] = []
  for (const part of (triggerSummary || '').split('；').map(value => value.trim()).filter(Boolean)) {
    const ratio = /^(面积|宽度|高度|整体跨度)为参考框的\s*([\d.]+)%(.*)$/.exec(part)
    const mask = /^目标掩膜仅为已确认正常样本的\s*([\d.]+)%(.*)$/.exec(part)
    const aspect = /^长宽比变化\s*([\d.]+)\s*倍(.*)$/.exec(part)
    const overlap = /^(#\d+ 与 #\d+)较小框覆盖\s*([\d.]+)%(.*)$/.exec(part)
    const parsed = ratio ? metric(ratio[1]!, ratio[2]!, ratio[3]!, '%')
      : mask ? metric('目标掩膜面积', mask[1]!, mask[2]!, '%')
      : aspect ? metric('长宽比变化', aspect[1]!, aspect[2]!, '倍')
      : overlap ? metric(`${overlap[1]} 覆盖率`, overlap[2]!, overlap[3]!, '%') : null
    if (parsed) triggerMetrics.push(parsed)
    else if (!triggerNotes.includes(part)) triggerNotes.push(part)
  }
  const additionalMetrics: PauseAuxiliaryMetric[] = measurements.map(text => {
    const match = /^(.*?)\s+(-?[\d.]+)$/.exec(text.trim())
    if (!match || !Number.isFinite(Number(match[2]))) return { label: text, value: '—' }
    const label = match[1]!, raw = match[2]!
    if (label === '重叠覆盖率') return { label, value: `${(Number(raw) * 100).toFixed(1)}%` }
    if (label.endsWith('(px)')) return { label: label.slice(0, -4), value: `${raw} px` }
    if (label.endsWith('倍率')) return { label: label.slice(0, -2), value: `${raw} 倍` }
    return { label, value: raw }
  })
  return { triggerMetrics, triggerNotes, additionalMetrics }
}
