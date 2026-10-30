// Run the production shortcut handler, with only effects replaced. Layout,
// pointer capture and real draft isolation are covered by the browser suite.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source=fs.readFileSync(new URL('../src/pages/ReviewPage.vue',import.meta.url),'utf8').split('<script setup lang="ts">')[1].split('</script>')[0]
const ast=ts.createSourceFile('ReviewPage.ts',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TS)
const names=new Set(['keydown','toggleFocus'])
const functions=ast.statements.filter(n=>ts.isFunctionDeclaration(n)&&names.has(n.name?.text)).map(n=>n.getText(ast)).join('\n')
assert.equal(ast.statements.filter(n=>ts.isFunctionDeclaration(n)&&names.has(n.name?.text)).length,names.size)

function runtime(overrides={}){
  const effects=[],ref=value=>({value})
  const input={focus(){effects.push('jump-focus')},select(){effects.push('jump-select')},blur(){effects.push('jump-blur')}}
  const refs={exportOpen:ref(false),modal:ref(null),dialog:ref(null),session:ref({id:'s'}),frame:ref({frameIndex:12}),imageUrl:ref('blob:frame'),busy:ref(false),navigation:{panning:ref(false),spaceHeld:ref(false),blur(){effects.push('pan-reset')},captureAnchor(){effects.push('capture-anchor');return {x:.4,y:.7}},async restoreAnchor(){effects.push('restore-anchor')}},fi:ref(12),total:ref(30),working:ref([]),selected:ref(null),jumpInput:ref(input),hiddenBoxes:ref(false),overlay:ref(false),drawMode:ref(false),canEdit:ref(true),focused:ref(false),autoFit:ref(false),viewBlocked:ref(false),focusButton:ref(null),...overrides}
  const code=`const {${Object.keys(refs).join(',')}}=refs;let pointer=null,drawPointer=null;
  const esc=()=>effects.push('escape'),changeZoom=n=>effects.push('zoom:'+n),fit=()=>effects.push('fit'),doJump=()=>effects.push('jump'),navigate=n=>effects.push('frame:'+n),submit=()=>effects.push('submit'),undo=()=>effects.push('undo'),selectObject=()=>effects.push('select'),stopPlayback=()=>effects.push('stop'),deleteSelected=()=>effects.push('delete'),togglePlay=()=>effects.push('play'),changeFocus=async()=>{focused.value=!focused.value;effects.push('focus:'+focused.value)};
  ${functions}\n({keydown,toggleFocus,setPointer:p=>pointer=p,setDrawPointer:p=>drawPointer=p})`
  const document={querySelector:()=>null,activeElement:null}
  const api=vm.runInNewContext(ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,{refs,effects,document})
  function press(key,options={}){
    const event={key,keyCode:0,isComposing:false,repeat:false,ctrlKey:false,metaKey:false,altKey:false,shiftKey:false,prevented:false,preventDefault(){this.prevented=true},target:{closest:()=>null},...options}
    api.keydown(event);return event
  }
  return {refs,effects,press,document,...api}
}

test('review viewing shortcuts remain available during failed/pending writes without submitting',async()=>{
  const f=runtime({busy:{value:true},canEdit:{value:false}})
  f.press('+');f.press('=');f.press('-');f.press('0');f.press('h');f.press('f')
  await Promise.resolve();await Promise.resolve()
  assert(f.effects.includes('zoom:0.25'))
  assert(f.effects.includes('zoom:-0.25'))
  assert(f.effects.includes('fit'))
  assert.equal(f.refs.hiddenBoxes.value,true)
  assert.equal(f.refs.focused.value,true)
  assert(!f.effects.some(x=>['submit','undo','delete','play'].includes(x)))
})

test('Space holds pan mode on the canvas and preserves native button activation',()=>{
  const f=runtime()
  assert.equal(f.press(' ').prevented,true)
  assert.equal(f.refs.navigation.spaceHeld.value,true)
  assert(!f.effects.includes('play'))
  f.refs.navigation.spaceHeld.value=false
  assert.equal(f.press(' ',{target:{closest:s=>s.includes('button')?{}:null}}).prevented,false)
  assert.equal(f.refs.navigation.spaceHeld.value,false)
})

test('G locates the frame input while K plays and F enters focus',async()=>{
  const f=runtime()
  f.press('g');f.press('k');f.press('f');await Promise.resolve();await Promise.resolve()
  assert(f.effects.includes('jump-focus')&&f.effects.includes('jump-select'))
  assert.equal(f.effects.filter(x=>x==='play').length,1)
  assert.equal(f.effects.filter(x=>x==='focus:true').length,1)
})

test('frame stepping uses ten frames with Shift and clamps to real bounds',()=>{
  const f=runtime()
  f.press('ArrowLeft',{shiftKey:true});f.press('ArrowRight',{shiftKey:true})
  f.refs.fi.value=2;f.press('ArrowLeft',{shiftKey:true})
  f.refs.fi.value=28;f.press('ArrowRight',{shiftKey:true})
  assert.deepEqual(f.effects,['frame:2','frame:22','frame:0','frame:29'])
})

test('inputs, IME, help and business dialogs isolate viewing shortcuts',()=>{
  const f=runtime()
  f.press('+',{target:{closest:()=>({})}})
  f.press('Escape',{target:{closest:()=>({})}})
  f.press('h',{isComposing:true});f.press('h',{keyCode:229})
  f.refs.modal.value='complete';f.press('+');f.press('f');f.refs.modal.value=null
  f.document.querySelector=()=>({});f.press('=')
  assert.deepEqual(f.effects,[])
  assert.equal(f.refs.hiddenBoxes.value,false)
})

test('no image or an active edit gesture cannot change the image scale',()=>{
  const f=runtime()
  f.refs.imageUrl.value='';f.press('=');f.press('0');f.press('f')
  f.refs.imageUrl.value='blob:frame';f.setPointer({id:1});f.press('=');f.press('0')
  f.setPointer(null);f.setDrawPointer({id:2});f.press('-')
  assert.deepEqual(f.effects,[])
})

test('browser modified zoom and repeated submit are not taken as viewing actions',()=>{
  const f=runtime()
  assert.equal(f.press('=',{ctrlKey:true}).prevented,false)
  assert.equal(f.press('-',{metaKey:true}).prevented,false)
  f.press('Enter',{ctrlKey:true,repeat:true});assert.deepEqual(f.effects,[])
  f.press('Enter',{ctrlKey:true});assert.deepEqual(f.effects,['submit'])
})

test('manual focus preserves its anchor while fitted focus uses the larger viewport',async()=>{
  const manual=runtime();await manual.toggleFocus()
  assert(manual.effects.includes('capture-anchor')&&manual.effects.includes('restore-anchor'))
  assert(!manual.effects.includes('fit'))
  const fitted=runtime({autoFit:{value:true}});await fitted.toggleFocus()
  assert(fitted.effects.includes('fit'))
  assert(!fitted.effects.includes('restore-anchor'))
})


test('training export dialog isolates review viewing and editing shortcuts',()=>{
  const f=runtime({exportOpen:{value:true}})
  for(const key of ['+','ArrowRight','f','Delete','k'])f.press(key)
  assert.deepEqual(f.effects,[])
})
