// Exercise the real page's request/restore functions with deterministic transport
// faults. The API double implements frame CAS and immutable request receipts;
// actual browser + FastAPI coverage remains in review-presence-browser.mjs.
import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const root=new URL('../src/',import.meta.url)
const pageSource=fs.readFileSync(new URL('pages/ReviewPage.vue',root),'utf8').split('<script setup lang="ts">')[1].split('</script>')[0]
const ast=ts.createSourceFile('ReviewPage.ts',pageSource,ts.ScriptTarget.Latest,true,ts.ScriptKind.TS)
const names=new Set(['rememberOperation','runPending','perform','commitAddition','restoreOperation'])
const functions=ast.statements.filter(node=>ts.isFunctionDeclaration(node)&&names.has(node.name?.text)).map(node=>node.getText(ast)).join('\n')
assert.equal(ast.statements.filter(node=>ts.isFunctionDeclaration(node)&&names.has(node.name?.text)).length,names.size)
const geometryExports={}
vm.runInNewContext(ts.transpileModule(fs.readFileSync(new URL('review/geometry.ts',root),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:geometryExports})
const copy=x=>JSON.parse(JSON.stringify(x))
const initial={objectId:1,annotationId:'a:f0:o1',classKey:'sperm',bbox:[10,20,30,40]}
const added={objectId:2,annotationId:'a:f0:o2',classKey:'sperm',bbox:[60,70,90,100]}
const freshBox=[72,70,102,100]

function fixture(fault) {
  const storage=new Map(),receipts=new Map(),calls=[]
  const database={revision:1,frameRevision:0,objects:[copy(initial)],reserved:false}
  let sequence=0,failed=false
  const session=()=>({id:'review-session',revision:database.revision,permissions:{canEdit:true},resume:{firstUnsubmittedFrameIndex:0,lastViewedFrameIndex:0}})
  const frame=()=>({frameIndex:0,frameRevision:database.frameRevision,state:'draft',hasDraft:true,baselineObjects:[copy(initial)],effectiveObjects:copy(database.objects),patch:geometryExports.patches([initial],database.objects)})
  const api={
    session:async()=>session(),
    frame:async()=>frame(),
    reserveObject:async(sid,revision,key)=>{
      calls.push({kind:'reserve',sid,revision,key})
      if(receipts.has(key))return copy(receipts.get(key))
      assert.equal(revision,database.revision)
      database.reserved=true;database.revision++
      const result={session:session(),object:{objectId:2,classKey:'sperm',origin:'review'}}
      receipts.set(key,copy(result))
      if(fault==='reserve-response'&&!failed){failed=true;throw new Error('response lost')}
      return result
    },
    mutate:async(sid,fi,kind,body,key)=>{
      calls.push(copy({kind,sid,fi,body,key}))
      if(receipts.has(key))return copy(receipts.get(key))
      if(body.expectedFrameRevision!==database.frameRevision)throw {status:409,code:'FRAME_REVISION_CONFLICT',message:'frame changed'}
      if(fault==='draft-before-commit'&&!failed){failed=true;throw new Error('request interrupted')}
      const patch=new Map(body.patch.map(p=>[p.objectId,p]))
      database.objects=[initial,...(patch.has(2)?[{...added,bbox:patch.get(2).bbox}]:[])]
      database.frameRevision++;database.revision++
      const result={session:session(),frame:frame()}
      receipts.set(key,copy(result))
      if(fault==='draft-response'&&!failed){failed=true;throw new Error('response lost')}
      return result
    },
  }
  function runtime() {
    const refs={session:{value:session()},frame:{value:frame()},working:{value:copy(database.objects)},busy:{value:false},error:{value:''},saveLabel:{value:''},newBox:{value:copy(added.bbox)},catalogError:{value:''},objectChoice:{value:'new'},addClass:{value:'sperm'},pendingObject:{value:null},pendingTask:{value:null},catalog:{value:[{objectId:1,classKey:'sperm'}]},history:{value:[]},selected:{value:null},objectFilter:{value:'all'},objectQuery:{value:''},modal:{value:'add'},taskTarget:{value:null},returnReason:{value:''},taskError:{value:''}}
    const aliases=Object.keys(refs).join(',')
    const prelude=`
      const {${aliases}}=refs;
      let pending=null,requestPromise=null,alive=true;
      const clone=x=>JSON.parse(JSON.stringify(x)),operationJournal=()=> 'review-intent';
      const fi={get value(){return frame.value.frameIndex}};
      const patch={get value(){return geometry.patches(frame.value.baselineObjects,working.value)}};
      const addCandidates={get value(){return catalog.value.filter(o=>!working.value.some(w=>w.objectId===o.objectId))}};
      function updateSession(s){session.value=s}
      function applyFrame(f){frame.value=f;working.value=clone(f.effectiveObjects)}
      function showError(e){error.value=e.message||String(e)}
      async function open(detail){session.value=detail;frame.value=getFrame();working.value=clone(frame.value.effectiveObjects);newBox.value=null;modal.value=null;pending=null}
      async function loadFrame(){frame.value=getFrame();working.value=clone(frame.value.effectiveObjects);return true}
      async function loadCatalog(){catalog.value=[{objectId:1,classKey:'sperm'},{objectId:2,classKey:'sperm'}]}
    `
    const exported=`\n({commitAddition,restoreOperation,perform,state:()=>({pending,working:working.value,error:error.value,pendingObject:pendingObject.value})})`
    const code=ts.transpileModule(prelude+functions+exported,{compilerOptions:{module:ts.ModuleKind.None,target:ts.ScriptTarget.ES2022}}).outputText
    return vm.runInNewContext(code,{refs,api,geometry:geometryExports,getFrame:frame,createRequestId:()=>`request-${++sequence}`,sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},console:{info(){},error(){}}})
  }
  function externalEdit(){database.objects=[copy(initial),{...copy(added),bbox:copy(freshBox)}];database.frameRevision++;database.revision++}
  return {runtime,externalEdit,database,calls,storage,api}
}

test('lost committed new-box draft refresh replays original body/key without overwriting newer geometry',async()=>{
  const f=fixture('draft-response'),first=f.runtime()
  await first.commitAddition()
  const original=f.calls.find(c=>c.kind==='draft')
  assert(original)
  const journal=JSON.parse(f.storage.get('review-intent'))
  assert.deepEqual(journal.frame.body,original.body)
  assert.equal(journal.frame.key,original.key)
  f.externalEdit()
  const restored=f.runtime();await restored.restoreOperation();await restored.perform('draft')
  assert.deepEqual(f.calls.filter(c=>c.kind==='draft')[1],original)
  assert.deepEqual(f.database.objects.find(o=>o.objectId===2).bbox,freshBox)
  assert.equal(f.calls.filter(c=>c.kind==='reserve').length,1)
  assert.equal(f.storage.has('review-intent'),false)
})

test('interrupted draft refresh keeps original CAS and retains intent on concurrent edit conflict',async()=>{
  const f=fixture('draft-before-commit'),first=f.runtime()
  await first.commitAddition();const original=f.calls.find(c=>c.kind==='draft')
  f.externalEdit()
  const restored=f.runtime();await restored.restoreOperation()
  assert.equal(await restored.perform('draft'),null)
  assert.deepEqual(f.calls.filter(c=>c.kind==='draft')[1],original)
  assert.deepEqual(f.database.objects.find(o=>o.objectId===2).bbox,freshBox)
  assert.equal(restored.state().pending.key,original.key)
  assert.equal(JSON.parse(f.storage.get('review-intent')).frame.key,original.key)
})

test('successful replay of an older receipt does not replace freshly loaded current geometry on screen',async()=>{
  const f=fixture('draft-response'),first=f.runtime()
  await first.commitAddition();f.externalEdit()
  const restored=f.runtime();await restored.restoreOperation();await restored.perform('draft')
  assert.deepEqual(copy(restored.state().working.find(o=>o.objectId===2).bbox),freshBox)
})

test('failed current-state refresh after replay retains exact intent until reads recover',async()=>{
  const f=fixture('draft-response'),first=f.runtime()
  await first.commitAddition();f.externalEdit()
  const original=f.calls.find(c=>c.kind==='draft'),read=f.api.frame
  f.api.frame=async()=>{throw new Error('frame read interrupted')}
  const restored=f.runtime();await restored.restoreOperation()
  assert.equal(await restored.perform('draft'),null)
  assert.equal(restored.state().pending.key,original.key)
  assert.equal(JSON.parse(f.storage.get('review-intent')).frame.key,original.key)
  assert.deepEqual(f.database.objects.find(o=>o.objectId===2).bbox,freshBox)
  f.api.frame=read
  assert(await restored.perform('draft'))
  assert.deepEqual(copy(restored.state().working.find(o=>o.objectId===2).bbox),freshBox)
  assert.equal(f.storage.has('review-intent'),false)
})

test('lost reservation response does not rebase the original box over another window using that identity',async()=>{
  const f=fixture('reserve-response'),first=f.runtime()
  await first.commitAddition()
  assert(f.database.reserved)
  f.externalEdit()
  const restored=f.runtime();await restored.restoreOperation();await restored.commitAddition()
  assert.deepEqual(f.database.objects.find(o=>o.objectId===2).bbox,freshBox)
  const writes=f.calls.filter(c=>c.kind==='draft')
  assert(writes.length===0||writes[0].body.expectedFrameRevision===0)
})
