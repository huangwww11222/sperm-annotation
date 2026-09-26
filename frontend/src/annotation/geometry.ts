export interface Box { x: number; y: number; width: number; height: number }
export type Handle = 'nw' | 'ne' | 'sw' | 'se'
export const clamp = (v: number, min: number, max: number) => Math.min(max, Math.max(min, v))
export function moveBox(box: Box, dx: number, dy: number): Box {
  return { ...box, x: clamp(box.x + dx, 0, 100 - box.width), y: clamp(box.y + dy, 0, 100 - box.height) }
}
export function resizeBox(box: Box, dx: number, dy: number, handle: Handle, minW: number, minH: number): Box {
  minW = Math.min(minW, box.width); minH = Math.min(minH, box.height)
  let x1 = box.x, y1 = box.y, x2 = box.x + box.width, y2 = box.y + box.height
  if (handle.includes('w')) x1 = clamp(x1 + dx, 0, x2 - minW)
  else x2 = clamp(x2 + dx, x1 + minW, 100)
  if (handle.includes('n')) y1 = clamp(y1 + dy, 0, y2 - minH)
  else y2 = clamp(y2 + dy, y1 + minH, 100)
  return { x: x1, y: y1, width: x2 - x1, height: y2 - y1 }
}
export function hitHandle(px: number, py: number, b: Box, stageW: number, stageH: number): Handle | null {
  const corners: [Handle, number, number][] = [['nw', b.x, b.y], ['ne', b.x+b.width,b.y], ['sw',b.x,b.y+b.height], ['se',b.x+b.width,b.y+b.height]]
  const hit = corners.map(([h,x,y]) => ({ h, d: Math.hypot((px-x)*stageW/100, (py-y)*stageH/100) })).sort((a,b)=>a.d-b.d)[0]
  return hit && hit.d <= 9 ? hit.h : null
}
