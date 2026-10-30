import test from 'node:test'
import assert from 'node:assert/strict'
import {zoomScrollDelta,boundedScale,canvasZoomKey} from '../../work/workbench-view/viewTransform.js'
test('canvas zoom keys work with Shift plus and preserve browser modifier shortcuts',()=>{
  assert.equal(canvasZoomKey({key:'+'}),'in');assert.equal(canvasZoomKey({key:'='}),'in')
  assert.equal(canvasZoomKey({key:'-'}),'out');assert.equal(canvasZoomKey({key:'0'}),'fit')
  for(const mod of ['ctrlKey','metaKey','altKey'])assert.equal(canvasZoomKey({key:'+', [mod]:true}),null)
})
test('zoom keeps a noncentral image coordinate under the pointer',()=>{
  const old={left:20,top:30,width:800,height:450},next={left:-180,top:-82.5,width:1200,height:675},point={x:620,y:300}
  const delta=zoomScrollDelta(old,next,point)
  assert.equal((point.x+delta.x-next.left)/next.width,(point.x-old.left)/old.width)
  assert.equal((point.y+delta.y-next.top)/next.height,(point.y-old.top)/old.height)
})
test('scale bounds support fitting 4K images in short viewports and avoid drift',()=>{
  assert.equal(boundedScale(.05-.015),.035);assert.equal(boundedScale(-1),.01)
  assert.equal(boundedScale(100),5);assert.equal(boundedScale(.1+.2),.3)
})
