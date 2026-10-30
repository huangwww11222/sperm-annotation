export interface Point { x:number;y:number }
export interface Bounds {left:number;top:number;width:number;height:number}
export function zoomScrollDelta(before:Bounds,after:Bounds,point:Point):Point {
  if(before.width<=0||before.height<=0)return {x:0,y:0}
  return {x:after.left+(point.x-before.left)/before.width*after.width-point.x,
    y:after.top+(point.y-before.top)/before.height*after.height-point.y}
}
export function boundedScale(value:number,min=.01,max=5) {
  return Math.max(min,Math.min(max,Math.round(value*1000000)/1000000))
}
// Ctrl/Command +/- remains the browser's own zoom. These are canvas keys.
export function canvasZoomKey(event:{key:string;ctrlKey?:boolean;metaKey?:boolean;altKey?:boolean}) {
  if(event.ctrlKey||event.metaKey||event.altKey)return null
  if(event.key==='+'||event.key==='=')return 'in'
  if(event.key==='-'||event.key==='Subtract')return 'out'
  if(event.key==='0')return 'fit'
  return null
}
