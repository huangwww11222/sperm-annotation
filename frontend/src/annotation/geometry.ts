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

/** Reveal a screen-space rectangle with the least possible pan, without zooming. */
export function revealBoxScroll(box: Box, viewport: Box, scroll: {left:number;top:number;maxLeft:number;maxTop:number}, margin=12): {left:number;top:number} {
  const axis=(start:number,length:number,viewStart:number,viewLength:number,current:number,max:number)=>{
    const inset=Math.min(margin,viewLength/4),low=viewStart+inset,high=viewStart+viewLength-inset
    const delta=length>high-low ? start+length/2-(low+high)/2 : start<low ? start-low : start+length>high ? start+length-high : 0
    return clamp(current+delta,0,Math.max(0,max))
  }
  if (![...Object.values(box),...Object.values(viewport),...Object.values(scroll)].every(Number.isFinite) || viewport.width<=0 || viewport.height<=0) return {left:scroll.left,top:scroll.top}
  return {left:axis(box.x,box.width,viewport.x,viewport.width,scroll.left,scroll.maxLeft),top:axis(box.y,box.height,viewport.y,viewport.height,scroll.top,scroll.maxTop)}
}
