import test from 'node:test'
import assert from 'node:assert/strict'
import { equal,patches,metrics,dragBox,crop } from '../../work/review-geometry/geometry.js'
test('object key order from Python must not produce dirty drafts',()=>{
 assert(equal([{objectId:1,bbox:[1,2,3,4]}],[{bbox:[1,2,3,4],objectId:1}]))
 assert(!equal([{objectId:1,bbox:[1,2,3,4]}],[{bbox:[2,2,3,4],objectId:1}]))
})
test('subpixel changes round to .001 px and net-zero edits have no patch',()=>{
 const a=[{objectId:1,annotationId:'x',bbox:[1,2,3,4]}]
 assert.deepEqual(patches(a,[{...a[0],bbox:[1.0001,2,3,4]}]),[])
 assert.deepEqual(patches(a,[{...a[0],bbox:[1.12345,2,3,4]}]),[{objectId:1,bbox:[1.123,2,3,4]}])
})
test('moving a box clamps translation without changing size',()=>{
 assert.deepEqual(dragBox([10,20,30,40],-100,100,'move',100,100),[0,80,20,100])
})
test('corner resizing keeps opposite corner fixed and positive dimensions',()=>{
 assert.deepEqual(dragBox([10,20,30,40],100,100,'tl',100,100),[29.999,39.999,30,40])
 assert.deepEqual(dragBox([10,20,30,40],-100,-100,'br',100,100),[10,20,10.001,20.001])
 assert.deepEqual(dragBox([10,20,30,40],-100,100,'bl',100,100),[0,20,30,100])
 assert.deepEqual(dragBox([10,20,30,40],100,-100,'tr',100,100),[10,0,100,40])
})
test('IoU has no inclusive-pixel offset and shift uses center, not corner',()=>{
 assert.equal(metrics([0,0,10,10],[0,0,10,10]),null)
 assert.deepEqual(metrics([0,0,10,10],[0,0,20,10]),{iou:.5,shift:5,dw:10,dh:0,type:'尺寸调整'})
 assert.equal(metrics([0,0,10,10],[20,20,30,30]).iou,0)
})
test('paired crops use one clamped union for A and B',()=>{
 assert.equal(crop([0,0,10,10],[80,80,100,100],100,100),'0 0 100 100')
})
