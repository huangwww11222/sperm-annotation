import type { Box } from '../review/geometry'
// Both A/B and the overlay use this identical source rectangle. Never clip either box.
// Zoom is relative to a full-frame square; at large changes it stops at their union.
export function comparisonCrop(a:Box,b:Box,width:number,height:number,zoom:number):string {
  const x1=Math.min(a[0],b[0]),y1=Math.min(a[1],b[1]),x2=Math.max(a[2],b[2]),y2=Math.max(a[3],b[3])
  const z=Math.max(3,Math.min(8,Number.isFinite(zoom)?zoom:5))
  const side=Math.max(Math.max(width,height)/z,x2-x1+16,y2-y1+16)
  const w=Math.min(width,side),h=Math.min(height,side)
  const x=Math.max(0,Math.min(width-w,(x1+x2-w)/2)),y=Math.max(0,Math.min(height-h,(y1+y2-h)/2))
  return [x,y,w,h].join(' ')
}
export const rect=(b:Box)=>({x:b[0],y:b[1],width:b[2]-b[0],height:b[3]-b[1]})
