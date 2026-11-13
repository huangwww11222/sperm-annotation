// Where a target most likely is on a frame it has no box: between its nearest boxes before and
// after, in proportion to the distance; next to the only side it has a box on otherwise.
// Boxes are workspace percentages {x, y, width, height}.
export type PercentBox = { x: number; y: number; width: number; height: number }

export function gapGuess(boxes: Map<number, PercentBox>, frame: number): PercentBox | null {
  let prev = -1, next = -1
  for (const f of boxes.keys()) {
    if (f < frame && f > prev) prev = f
    if (f > frame && (next < 0 || f < next)) next = f
  }
  if (prev < 0 && next < 0) return null
  if (prev < 0) return { ...boxes.get(next)! }
  if (next < 0) return { ...boxes.get(prev)! }
  const a = boxes.get(prev)!, b = boxes.get(next)!, w = (frame - prev) / (next - prev)
  const mix = (u: number, v: number) => +(u + (v - u) * w).toFixed(4)
  return { x: mix(a.x, b.x), y: mix(a.y, b.y), width: mix(a.width, b.width), height: mix(a.height, b.height) }
}
