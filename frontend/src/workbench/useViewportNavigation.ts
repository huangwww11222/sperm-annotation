import { nextTick, onUnmounted, ref, type Ref } from 'vue'
import { boundedScale, zoomScrollDelta, type Point } from './viewTransform'

interface Options {
  viewport:Ref<HTMLElement|null>;stage:Ref<HTMLElement|SVGSVGElement|null>;scale:Ref<number>
  minScale?:number;maxScale?:number;blocked:()=>boolean;beforeZoom?:()=>void
}
export function useViewportNavigation(options:Options) {
  const spaceHeld=ref(false),panning=ref(false)
  let pan:{id:number;x:number;y:number;left:number;top:number}|null=null
  let suppressClick=false,clickTimer:ReturnType<typeof setTimeout>|null=null
  async function zoomBy(delta:number,point?:Point) {
    const viewport=options.viewport.value,stage=options.stage.value
    if(!viewport||!stage||document.querySelector('[data-user-guide][open]')||options.blocked()||panning.value||!Number.isFinite(delta))return
    const bounded=boundedScale(options.scale.value+delta,options.minScale,options.maxScale)
    const next=delta<0?Math.min(options.scale.value,bounded):Math.max(options.scale.value,bounded)
    if(next===options.scale.value)return
    const before=stage.getBoundingClientRect(),bounds=viewport.getBoundingClientRect()
    const anchor=point||{x:bounds.left+viewport.clientWidth/2,y:bounds.top+viewport.clientHeight/2}
    options.beforeZoom?.();options.scale.value=next
    await nextTick()
    if(options.viewport.value!==viewport||options.stage.value!==stage)return
    const shift=zoomScrollDelta(before,stage.getBoundingClientRect(),anchor)
    viewport.scrollBy(shift.x,shift.y)
  }
  function startPan(event:PointerEvent) {
    const viewport=options.viewport.value
    if(!viewport||document.querySelector('[data-user-guide][open]')||options.blocked()||pan||!(event.button===1||(event.button===0&&spaceHeld.value)))return false
    event.preventDefault();event.stopPropagation()
    pan={id:event.pointerId,x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop}
    panning.value=true;suppressClick=true
    if(clickTimer)clearTimeout(clickTimer)
    viewport.setPointerCapture(event.pointerId)
    return true
  }
  function movePan(event:PointerEvent) {
    if(!pan||pan.id!==event.pointerId)return false
    event.preventDefault();event.stopPropagation()
    options.viewport.value?.scrollTo(pan.left-(event.clientX-pan.x),pan.top-(event.clientY-pan.y))
    return true
  }
  function cancelPan() {
    if(!pan)return
    const previous=pan;pan=null;panning.value=false;suppressClick=false
    if(clickTimer){clearTimeout(clickTimer);clickTimer=null}
    if(previous&&options.viewport.value?.hasPointerCapture(previous.id))options.viewport.value.releasePointerCapture(previous.id)
  }
  function endPan(event:PointerEvent) {
    if(!pan||pan.id!==event.pointerId)return false
    event.preventDefault();event.stopPropagation();cancelPan();suppressClick=true
    clickTimer=setTimeout(()=>{suppressClick=false;clickTimer=null},0)
    return true
  }
  function consumeClick(event:Event) {
    if(!suppressClick)return false
    event.preventDefault();event.stopPropagation();suppressClick=false;return true
  }
  function keyUp(event:KeyboardEvent){if(event.key===' '||event.code==='Space')spaceHeld.value=false}
  function blur(){spaceHeld.value=false;cancelPan();suppressClick=false}
  function captureAnchor() {
    const viewport=options.viewport.value,stage=options.stage.value
    if(!viewport||!stage)return null
    const a=stage.getBoundingClientRect(),v=viewport.getBoundingClientRect()
    return a.width&&a.height?{x:(v.left+viewport.clientWidth/2-a.left)/a.width,y:(v.top+viewport.clientHeight/2-a.top)/a.height}:null
  }
  async function restoreAnchor(anchor:Point|null) {
    await nextTick()
    const viewport=options.viewport.value,stage=options.stage.value
    if(!anchor||!viewport||!stage)return
    const a=stage.getBoundingClientRect(),v=viewport.getBoundingClientRect()
    viewport.scrollBy(a.left+anchor.x*a.width-v.left-viewport.clientWidth/2,a.top+anchor.y*a.height-v.top-viewport.clientHeight/2)
  }
  onUnmounted(()=>{blur();if(clickTimer)clearTimeout(clickTimer)})
  return {spaceHeld,panning,zoomBy,startPan,movePan,endPan,cancelPan,keyUp,blur,consumeClick,captureAnchor,restoreAnchor}
}
