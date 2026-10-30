import { nextTick, onUnmounted, ref } from 'vue'

export function useWorkbenchFocus() {
  const focused=ref(false)
  let overflow='',pageY=0,header:HTMLElement|null=null,inert=false,aria:string|null=null
  let tabs:Array<[HTMLElement,string|null]>=[]
  function restore() {
    document.body.style.overflow=overflow
    if(header){header.inert=inert;if(aria===null)header.removeAttribute('aria-hidden');else header.setAttribute('aria-hidden',aria)}
    for(const [element,value]of tabs){if(value===null)element.removeAttribute('tabindex');else element.setAttribute('tabindex',value)}
    tabs=[];header=null
  }
  async function toggleFocus() {
    if(!focused.value){
      overflow=document.body.style.overflow;pageY=window.scrollY
      header=document.querySelector<HTMLElement>('.app-header');inert=header?.inert||false;aria=header?.getAttribute('aria-hidden')??null
      if(header){
        tabs=Array.from(header.querySelectorAll<HTMLElement>('button,a,input,select,textarea,[tabindex]')).map(el=>[el,el.getAttribute('tabindex')])
        for(const [el]of tabs)el.setAttribute('tabindex','-1')
        const active=document.activeElement
        if(active instanceof HTMLElement&&header.contains(active))active.blur()
        header.inert=true;header.setAttribute('aria-hidden','true')
      }
      document.body.style.overflow='hidden';focused.value=true
    }else{focused.value=false;restore();await nextTick();window.scrollTo(0,pageY)}
    await nextTick()
  }
  onUnmounted(()=>{if(focused.value)restore()})
  return {focused,toggleFocus}
}
