import {test} from 'node:test'
import assert from 'node:assert/strict'
import {buildQueue,liveFlags,nextOpen} from '../../work/annotation-validation/annotation/targetQueue.js'

test('possible swaps first and asked as same-or-not, then manual, partial, weak, then the rest',()=>{
  const flags={5:[{kind:'weak'}],7:[{kind:'cross',frame:20,with:8}],8:[{kind:'cross',frame:20,with:7}],3:[{kind:'partial',frame:12,last:23}]}
  const queue=buildQueue([1,2,3,4,5,7,8,9],flags,new Set([9]))
  assert.deepEqual(queue.map(e=>e.objectId),[7,8,9,3,5,1,2,4,5])
  assert.deepEqual(queue.slice(0,2).map(e=>[e.ask,e.suspectFrame]),[['same',20],['same',20]])
  assert.ok(queue.slice(2).every(e=>e.ask==='what'))
  assert.deepEqual(queue.filter(e=>e.repeat).map(e=>e.objectId),[5])  // One hidden second look at a weak target.
})
test('short rounds get no repeat card',()=>{
  assert.equal(buildQueue([1,2,3],{2:[{kind:'weak'}]},new Set()).some(e=>e.repeat),false)
})
test('next open card wraps and stops when all are answered',()=>{
  const queue=[1,2,3].map(objectId=>({objectId,ask:'what',repeat:false}))
  const done=new Set([1,2])
  assert.equal(nextOpen(queue,e=>done.has(e.objectId),0),2)
  assert.equal(nextOpen(queue,e=>done.has(e.objectId),2),2)
  done.add(3)
  assert.equal(nextOpen(queue,e=>done.has(e.objectId),0),-1)
})
test('a possible swap is no longer asked once the target has no boxes on both sides of it',()=>{
  const flags={15:[{kind:'cross',frame:6,with:23}]}
  // Split at frame 4: the target keeps frames 0-3, nothing left around frame 6.
  const split=buildQueue([15],flags,new Set(),{15:[0,1,2,3]})
  assert.deepEqual(split.map(e=>[e.ask,e.suspectFrame]),[['what',undefined]])
  // Boxes still on both sides: the question stays.
  const whole=buildQueue([15,16],flags,new Set(),{15:[0,3,5,6,7,9],16:[0,1]})
  assert.deepEqual(whole.map(e=>[e.objectId,e.ask,e.suspectFrame]),[[15,'same',6],[16,'what',undefined]])
  // Only after it (the part before was split off): nothing to compare.
  assert.equal(buildQueue([15],flags,new Set(),{15:[6,7,8]})[0].ask,'what')
})

test('a possible swap is asked only when the target is on check frames both before and after it',()=>{
  const cross=[{kind:'cross',frame:15,with:8}]
  const frames=Array.from({length:30},(_,i)=>i)
  // Check frames 10 and 20 surround the swap: one answer would label two things in the training set.
  assert.deepEqual(liveFlags(cross,frames,[0,10,20,29]),cross)
  // The target is on only one check frame (its track runs 12–29 here): the swap never reaches the data.
  assert.deepEqual(liveFlags(cross,frames.slice(12),[0,10,20,29]).length,0)
  const late=[{kind:'jump',frame:25}]
  assert.deepEqual(liveFlags(late,frames.slice(0,28),[0,10,20,29]).length,0)
  // No check frames known (older video): unchanged behaviour.
  assert.deepEqual(liveFlags(cross,frames.slice(12)),cross)
  const queue=buildQueue([7,8],{7:cross},new Set(),{7:frames.slice(12),8:frames},[0,10,20,29])
  assert.deepEqual(queue.map(e=>e.ask),['what','what'])
})
