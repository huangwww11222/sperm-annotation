import type { Box } from '../review/geometry'
export function presenceCopy(a:Box|null,b:Box|null) {
  if(!a&&b)return {kind:'add',title:'补标',a:'不采用补标',b:'采用补标',aState:'原标注中无此框',bState:'审查建议补标'}
  if(a&&!b)return {kind:'delete',title:'删除',a:'保留原框',b:'确认删除',aState:'原标注中的框',bState:'审查建议删除此框'}
  return {kind:'update',title:'调整',a:'保留原标注 A',b:'采用审查标注 B',aState:'原始标注',bState:'审查标注'}
}
// Both A/B and the overlay use this identical source rectangle. Never clip either box.
// Zoom is relative to a full-frame square; at large changes it stops at their union.
export function comparisonCrop(a:Box|null,b:Box|null,width:number,height:number,zoom:number):string {
  if(!a&&!b)throw new Error('修改项缺少两侧标注，无法定位')
  a=a||b!;b=b||a
  const x1=Math.min(a[0],b[0]),y1=Math.min(a[1],b[1]),x2=Math.max(a[2],b[2]),y2=Math.max(a[3],b[3])
  const z=Math.max(3,Math.min(8,Number.isFinite(zoom)?zoom:5))
  const side=Math.max(Math.max(width,height)/z,x2-x1+16,y2-y1+16)
  const w=Math.min(width,side),h=Math.min(height,side)
  const x=Math.max(0,Math.min(width-w,(x1+x2-w)/2)),y=Math.max(0,Math.min(height-h,(y1+y2-h)/2))
  return [x,y,w,h].join(' ')
}
export const rect=(b:Box)=>({x:b[0],y:b[1],width:b[2]-b[0],height:b[3]-b[1]})
