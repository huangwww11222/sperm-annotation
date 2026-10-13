type FeedbackReferenceEvent = {
  objectId: number
  decision: 'normal' | 'corrected' | 'reset'
  geometryReference?: {frameIndex: number}
  overlapReferences?: Array<{objectIds: [number, number]}>
}

// Saved human evidence only. Actual applicability (source frame, newer manual
// correction and geometric match) is determined by the backend detector.
export function savedTrackingCalibration(samples: Array<{objectId: number}>, events: FeedbackReferenceEvent[], deleted: number[] = []) {
  const counts = new Map<number, number>(), shapes = new Map<number, number>()
  const pairs = new Map<string, [number, number]>()
  const deletedIds = new Set(deleted)
  for (const sample of samples) counts.set(sample.objectId, (counts.get(sample.objectId) || 0) + 1)
  for (const event of events) {
    if (event.decision === 'reset' || event.decision === 'corrected') {
      shapes.delete(event.objectId)
      for (const [key, ids] of pairs) if (ids.includes(event.objectId)) pairs.delete(key)
    } else {
      if (event.geometryReference) shapes.set(event.objectId, event.geometryReference.frameIndex)
      for (const reference of event.overlapReferences ?? []) {
        const ids = reference.objectIds
        if (ids?.length !== 2 || ids[0]===ids[1] || ids.some(id=>!Number.isInteger(id)||id<=0)) continue
        const pair = [...ids].sort((a,b)=>a-b) as [number, number]
        pairs.set(pair.join(':'), pair)
      }
    }
  }
  const partners = new Map<number, Set<number>>()
  for (const [a,b] of pairs.values()) {
    if (deletedIds.has(a)||deletedIds.has(b)) continue
    if (!partners.has(a)) partners.set(a,new Set())
    if (!partners.has(b)) partners.set(b,new Set())
    partners.get(a)!.add(b); partners.get(b)!.add(a)
  }
  return [...new Set([...counts.keys(), ...shapes.keys(), ...partners.keys()])]
    .filter(objectId=>!deletedIds.has(objectId))
    .map(objectId=>({objectId,sampleCount:counts.get(objectId)??0,geometryFrame:shapes.get(objectId),
      overlapCount:partners.get(objectId)?.size??0,overlapPartnerIds:[...(partners.get(objectId)??[])].sort((a,b)=>a-b)}))
}
