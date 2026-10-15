import {test} from 'node:test'
import assert from 'node:assert/strict'
import {moveBox,resizeBox,hitHandle,revealBoxScroll} from '../../work/annotation-helpers/geometry.js'
import {FrameCache} from '../../work/annotation-helpers/frameCache.js'
test('pixel hit radius is stable at different zoom/aspect ratios',()=>{
  const box={x:10,y:20,width:5,height:5}
  for(const [w,h] of [[800,450],[2000,500],[450,800]]){
    assert.equal(hitHandle(10-5/w*100,20,box,w,h),'nw')
    assert.equal(hitHandle(10-11/w*100,20,box,w,h),null)
  }
})
test('move and all resize handles remain inside source bounds with subpixel precision',()=>{
  const box={x:10.125,y:20.25,width:5.5,height:7.75}
  assert.deepEqual(moveBox(box,-100,200),{...box,x:0,y:92.25})
  for(const handle of ['nw','ne','sw','se'])for(const dx of [-200,-.125,200])for(const dy of [-200,.125,200]){
    const b=resizeBox(box,dx,dy,handle,.25,.5)
    assert(b.x>=0&&b.y>=0&&b.x+b.width<=100&&b.y+b.height<=100)
    assert(b.width>=.25&&b.height>=.5)
  }
  assert.equal(moveBox(box,.125,0).x,10.25)
})
test('object localization preserves a visible view and reveals offscreen targets with bounded pan',()=>{
  const viewport={x:100,y:100,width:300,height:200},scroll={left:200,top:300,maxLeft:1000,maxTop:1000}
  assert.deepEqual(revealBoxScroll({x:140,y:140,width:30,height:40},viewport,scroll),{left:200,top:300})
  const offscreen={x:500,y:420,width:30,height:40},next=revealBoxScroll(offscreen,viewport,scroll)
  const visible={...offscreen,x:offscreen.x-(next.left-scroll.left),y:offscreen.y-(next.top-scroll.top)}
  assert(visible.x>=viewport.x&&visible.y>=viewport.y&&visible.x+visible.width<=viewport.x+viewport.width&&visible.y+visible.height<=viewport.y+viewport.height)
  const edge=revealBoxScroll({x:-500,y:-300,width:20,height:20},viewport,scroll)
  assert.equal(edge.left,0);assert.equal(edge.top,0)
  const oversized=revealBoxScroll({x:300,y:50,width:800,height:50},viewport,scroll)
  assert.equal(300+800/2-(oversized.left-scroll.left),viewport.x+viewport.width/2)
  assert.deepEqual(revealBoxScroll({x:NaN,y:0,width:1,height:1},viewport,scroll),{left:200,top:300})
})
test('cache coalesces concurrent requests and respects LRU entry budget',async()=>{
  const c=new FrameCache(2,100);let calls=0
  const read=k=>c.get(k,async()=>{calls++;return new Blob([k])})
  const [a,b]=await Promise.all([read('a'),read('a')]);assert.equal(calls,1);assert.equal(a,b)
  await read('b');await read('a');await read('c');await read('a');assert.equal(calls,3)
  await read('b');assert.equal(calls,4)
})
test('cache bounds bytes and retries failures',async()=>{
  const c=new FrameCache(20,4);let calls=0
  const read=k=>c.get(k,async()=>{calls++;return new Blob(['123'])})
  await read('a');await read('b');await read('a');assert.equal(calls,3)
  await assert.rejects(c.get('failed',async()=>{throw new Error('decode')}))
  assert.equal(await (await c.get('failed',async()=>new Blob(['ok']))).text(),'ok')
})
test('clear prevents stale pending loads repopulating a new media cache',async()=>{
  const c=new FrameCache();let resolve
  const old=c.get('a',()=>new Promise(r=>{resolve=r}));c.clear()
  resolve(new Blob(['old']));await old
  assert.equal(await (await c.get('a',async()=>new Blob(['new']))).text(),'new')
})
test('invalidating an undecodable successful reply allows a fresh download',async()=>{
  const c=new FrameCache();let calls=0
  const read=()=>c.get('frame',async()=>new Blob([++calls===1?'broken JPEG':'valid JPEG']))
  assert.equal(await (await read()).text(),'broken JPEG');c.invalidate('frame')
  assert.equal(await (await read()).text(),'valid JPEG');assert.equal(calls,2)
})
test('invalidating an in-flight frame prevents it replacing a newer cache entry',async()=>{
  const c=new FrameCache();let resolve
  const old=c.get('frame',()=>new Promise(r=>{resolve=r}));c.invalidate('frame')
  await c.get('frame',async()=>new Blob(['new']))
  resolve(new Blob(['old']));await old
  assert.equal(await (await c.get('frame',async()=>new Blob(['unexpected']))).text(),'new')
})
