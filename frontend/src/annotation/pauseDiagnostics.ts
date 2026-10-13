// Pure presentation of server evidence. Thresholds and persistence decisions
// remain server-owned; this helper does not decide whether tracking can resume.
export type OverlapPairNotice = {
  otherObjectId: number
  label: string
  coverage?: number
  iou?: number
  referenceFrame?: number
  referenceSource?: string
  level?: string
}

const numberOf = (value: unknown): number | undefined => {
  if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return undefined
  const valueNumber = Number(value)
  return Number.isFinite(valueNumber) ? valueNumber : undefined
}
const recordOf = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : {}
const frameOf = (value: unknown) => {
  const frame = numberOf(value)
  return frame != null && Number.isInteger(frame) && frame >= 0 ? frame : undefined
}
const percent = (value: number) => `${(value * 100).toFixed(1).replace(/\.0$/, '')}%`

export function pauseDiagnostics(objectId: number, reasons: string[], details: Record<string, unknown>, nameOf: (id: number) => string) {
  const triggers: string[] = []
  const maskReason = reasons.find(reason => reason.startsWith('confirmed_mask_area_ratio='))
  const currentMaskArea = numberOf(details.current_mask_area), referenceMaskArea = numberOf(details.mask_reference_area)
  const maskRatio = numberOf(details.mask_area_ratio)
    ?? (currentMaskArea != null && referenceMaskArea != null && referenceMaskArea > 0 ? currentMaskArea/referenceMaskArea : undefined)
    ?? (maskReason ? numberOf(maskReason.split('=')[1]?.split(' ')[0]) : undefined)
  const maskTrigger = !!maskReason && maskRatio != null
  if (maskTrigger) triggers.push(`目标掩膜仅为已确认正常样本的 ${percent(maskRatio)}${details.geometry_extreme === true ? '，立即暂停' : ''}`)
  const thresholds = recordOf(details.geometry_thresholds)
  for (const reason of reasons) {
    const match = /^(?:manual|geometry)_(area_ratio|width_ratio|height_ratio|aspect_change)=([\d.eE+-]+)/.exec(reason)
    if (!match) continue
    const [ , metric, rawValue] = match
    const threshold = recordOf(thresholds[`manual_${metric}`] ?? thresholds[metric])
    const value = numberOf(details[metric === 'aspect_change' ? 'aspect_ratio_change' : metric] ?? rawValue)
    if (value == null) continue
    const minimum = numberOf(threshold.min), maximum = numberOf(threshold.max)
    const crossesMinimum = minimum != null && value < minimum
    const crossesMaximum = maximum != null && (metric === 'aspect_change' ? value >= maximum : value > maximum)
    // The server can include advisory SOFT metrics alongside the trigger. Do
    // not present a value inside the pause observation bounds as its cause.
    if ((minimum != null || maximum != null) && !crossesMinimum && !crossesMaximum) continue
    const label = metric === 'area_ratio' ? '面积' : metric === 'width_ratio' ? '宽度' : metric === 'height_ratio' ? '高度' : '长宽比变化'
    const valueText = metric === 'aspect_change' ? `${value.toFixed(2)} 倍` : `为参考框的 ${percent(value)}`
    const bound = crossesMinimum ? `，低于观察下限 ${percent(minimum!)}`
      : crossesMaximum ? `，${metric === 'aspect_change' ? `达到观察界限 ${maximum!.toFixed(2)} 倍` : `高于观察上限 ${percent(maximum!)}`}` : ''
    const text = `${label}${valueText}${bound}`
    if (!triggers.includes(text)) triggers.push(text)
  }
  const overlapPairs: OverlapPairNotice[] = []
  const pairs = Array.isArray(details.overlap_pairs) ? details.overlap_pairs : []
  for (const candidate of pairs) {
    const pair = recordOf(candidate)
    if (!Array.isArray(pair.object_ids) || pair.object_ids.length !== 2) continue
    const ids = pair.object_ids.map(numberOf)
    if (!ids.includes(objectId)) continue
    const other = ids.find(id => id !== objectId)
    if (other == null || !Number.isInteger(other) || other <= 0 || overlapPairs.some(item => item.otherObjectId === other)) continue
    const coverage = numberOf(pair.coverage), iou = numberOf(pair.iou)
    overlapPairs.push({otherObjectId: other, label: `#${objectId} ↔ #${other} ${nameOf(other)}`, coverage, iou,
      referenceFrame: frameOf(pair.reference_frame), referenceSource: typeof pair.reference_source === 'string' ? pair.reference_source : undefined,
      level: typeof pair.level === 'string' ? pair.level : undefined})
    if (pair.suspected_merge === true) {
      const threshold = numberOf(pair.threshold), streak = numberOf(pair.risk_streak), required = numberOf(pair.persistence_frames)
      triggers.push(`#${objectId} 与 #${other}${coverage != null ? `较小框覆盖 ${percent(coverage)}` : '重叠'}${threshold != null ? `，观察界限 ${percent(threshold)}` : ''}${streak != null && required != null ? `；该对象对风险持续 ${streak}/${required} 帧` : ''}`)
    }
  }
  const geometryStreak = numberOf(details.geometry_streak ?? details.geometry_observation_count), geometryPersistence = numberOf(details.geometry_persistence_frames ?? details.geometry_observation_required)
  if (details.geometry_extreme === true && !maskTrigger) {
    if (triggers.length) triggers[0] += '，立即暂停'
    else triggers.push('明显尺寸突变，立即暂停')
  }
  else if (geometryStreak != null && geometryPersistence != null && geometryStreak >= geometryPersistence) triggers.push(`同一对象尺寸风险持续 ${geometryStreak} 帧，达到 ${geometryPersistence} 帧暂停条件`)
  return {triggerSummary: triggers.join('；'), overlapPairs, maskTrigger,
    pauseStartFrame: frameOf(details.pause_started_frame ?? details.pause_frame_index ?? details.geometry_risk_start_frame),
    decisionFrame: frameOf(details.decision_frame_index),
  }
}
