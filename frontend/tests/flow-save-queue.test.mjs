import {test} from 'node:test'
import assert from 'node:assert/strict'
import {SaveQueue} from '../../work/annotation-validation/flow/saveQueue.js'

const make=(outdated=()=>false)=>{
  const seen=[],stale=[]
  const queue=new SaveQueue({changed:(waiting,sending,error)=>seen.push({waiting,sending,error}),outdated,onOutdated:async(error,id)=>{stale.push([error.code,id])}})
  return {queue,seen,stale}
}
const wait=ms=>new Promise(done=>setTimeout(done,ms))

test('answers go out one at a time and in order',async()=>{
  const {queue}=make(),log=[]
  let active=0
  for(const id of ['a','b','c'])queue.add(id,async()=>{active++;assert.equal(active,1);await wait(2);log.push(id);active--})
  assert.equal(await queue.flush(),true)
  assert.deepEqual(log,['a','b','c'])
})
test('the latest choice wins: before it is sent, and while it is being sent',async()=>{
  const {queue}=make(),sent=[]
  let choice='yes',gate
  queue.add('first',()=>new Promise(done=>{gate=done}))
  queue.add('card',async()=>{sent.push(choice)})
  choice='no'
  queue.add('card',async()=>{sent.push(choice)})
  gate()
  await queue.flush()
  assert.deepEqual(sent,['no'])
  // Changed while its own request is on the way: sent again.
  const sentAgain=[]
  let release
  choice='yes'
  queue.add('card',async()=>{sentAgain.push(choice);if(sentAgain.length===1)await new Promise(done=>{release=done})})
  choice='unsure'
  queue.add('card',async()=>{sentAgain.push(choice)})
  release()
  assert.equal(await queue.flush(),true)
  assert.deepEqual(sentAgain,['yes','unsure'])
})
test('what could not be sent stays, says why, and goes out on retry',async()=>{
  const {queue,seen}=make(),sent=[]
  let online=false
  queue.add('a',async()=>{if(!online)throw {message:'连接后端失败',status:0};sent.push('a')})
  assert.equal(await queue.flush(),false)
  assert.equal(queue.size,1)
  assert.deepEqual(seen.at(-1),{waiting:1,sending:false,error:'连接后端失败'})
  queue.add('b',async()=>{if(!online)throw {message:'连接后端失败',status:0};sent.push('b')})
  assert.equal(await queue.flush(),false)
  assert.equal(queue.size,2)  // Judging goes on; nothing is lost.
  online=true
  assert.equal(await queue.flush(),true)
  assert.deepEqual(sent,['a','b'])
  assert.deepEqual(seen.at(-1),{waiting:0,sending:false,error:''})
})
test('an answer the server calls outdated is dropped with the rest and the server copy is shown',async()=>{
  const {queue,stale}=make(error=>error.code==='FLOW_CONFLICT')
  let later=0
  queue.add('a',async()=>{throw {code:'FLOW_CONFLICT',message:'刚被别人改过'}})
  queue.add('b',async()=>{later++})
  assert.equal(await queue.flush(),true)
  assert.deepEqual(stale,[['FLOW_CONFLICT','a']])
  assert.equal(later,0)
  queue.add('c',async()=>{later++})
  await queue.flush()
  assert.equal(later,1)
})
