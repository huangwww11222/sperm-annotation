// Execute the production viewing handlers against a deterministic stage. The
// test never duplicates its guards or invokes the annotation persistence store.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source=fs.readFileSync(new URL('../src/pages/AnnotatePage.vue',import.meta.url),'utf8').split('<script setup lang="ts">')[1].split('</script>')[0]
const ast=ts.createSourceFile('AnnotatePage.ts',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TS)
const names=new Set(['zoomAt','fitView','keys'])
const functions=ast.statements.filter(n=>ts.isFunctionDeclaration(n)&&names.has(n.name?.text)).map(n=>n.getText(ast)).join('\n')
assert.equal(ast.statements.filter(n=>ts.isFunctionDeclaration(n)&&names.has(n.name?.text)).length,names.size)

function runtime(){
  const effects=[],ref=value=>({value}),zoom=ref(1)
  const stage={getBoundingClientRect:()=>({left:10,top:20,width:800*zoom.value,height:450*zoom.value})}
  const scroller={getBoundingClientRect:()=>({left:0,top:0,width:600,height:300}),scrollBy:(x,y)=>effects.push(['scroll',x,y]),scrollTo:(x,y)=>effects.push(['fit-scroll',x,y])}
  const refs={zoom,stageRef:ref(stage),scrollContainerRef:ref(scroller),pauseStageMargins:ref(null),help:ref(false),focused:ref(false),hiddenBoxes:ref(false),spaceHeld:ref(false),mediaBusy:ref(false),workspaceRestoring:ref(false),jumpFrameInput:ref(null),reviewing:ref(false),userZoomedInReview:ref(false)}
  const code=`const {${Object.keys(refs).join(',')}}=refs;let pointerActive=false,pan=null;
    const cancelReviewLocate=()=>effects.push('cancel-review-locate'),cancelPendingPauseView=()=>effects.push('cancel-view'),cancelAnnotationGesture=()=>effects.push('cancel-edit'),zoomReset=()=>{zoom.value=1;effects.push('reset')},fit=()=>effects.push('fit'),zoomIn=n=>{zoom.value+=n;effects.push('zoom-in')},zoomOut=n=>{zoom.value-=n;effects.push('zoom-out')},nextTick=async()=>{},toggleFocus=()=>effects.push('focus'),canvasZoomKey=()=>null;
    ${functions}\n({zoomAt,fitView,keys,setPointer:x=>pointerActive=x,setPan:x=>pan=x})`
  const api=vm.runInNewContext(ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,{refs,effects,document:{querySelector:()=>null}})
  const event=(key,options={})=>({key,keyCode:0,isComposing:false,repeat:false,ctrlKey:false,metaKey:false,altKey:false,shiftKey:false,target:{closest:()=>null},preventDefault(){effects.push('prevent')},...options})
  return {...api,refs,effects,event}
}

test('annotation enlargement and fitting cannot interrupt an active box drag',async()=>{
  const f=runtime();f.setPointer(true)
  await f.zoomAt(.25,{x:200,y:150});f.fitView()
  assert.equal(f.refs.zoom.value,1)
  assert.deepEqual(f.effects,[])
})

test('annotation zoom and fit cannot change scale during viewport panning',async()=>{
  const f=runtime();f.setPan({id:2})
  await f.zoomAt(-.25);f.fitView()
  assert.equal(f.refs.zoom.value,1)
  assert.deepEqual(f.effects,[])
})

test('after gesture release zoom and fit keep their existing normal behavior',async()=>{
  const f=runtime();f.setPointer(true);f.setPointer(false)
  // One zoom step multiplies the view by 1.25 (wheel ticks use fractional steps).
  await f.zoomAt(1,{x:200,y:150})
  assert.equal(f.refs.zoom.value,1.25)
  assert(f.effects.includes('zoom-in'))
  assert(f.effects.some(x=>Array.isArray(x)&&x[0]==='scroll'))
  f.fitView();assert.equal(f.refs.zoom.value,1)
  assert.equal(f.effects.filter(x=>x==='cancel-edit').length,1)
  assert(f.effects.includes('fit'))
})

test('explicit review zoom and fit reserve scale control for the user',async()=>{
  const f=runtime();f.refs.reviewing.value=true
  await f.zoomAt(1)
  assert.equal(f.refs.userZoomedInReview.value,true)
  f.refs.userZoomedInReview.value=false
  f.fitView()
  assert.equal(f.refs.userZoomedInReview.value,true)
})

test('legacy IME key code 229 cannot enter focus or invoke canvas actions',()=>{
  const f=runtime()
  f.keys(f.event('f',{keyCode:229}));f.keys(f.event('h',{keyCode:229}));f.keys(f.event(' ',{keyCode:229}))
  assert.deepEqual(f.effects,[])
  assert.equal(f.refs.hiddenBoxes.value,false)
  assert.equal(f.refs.spaceHeld.value,false)
})
