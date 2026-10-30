import test from 'node:test'
import assert from 'node:assert/strict'
import { patches } from '../../work/review-geometry/geometry.js'
const object=(id,bbox=[10,20,30,40])=>({objectId:id,annotationId:`a-${id}`,classKey:'sperm',bbox})
test('review patches use stable IDs rather than array positions',()=>{
  assert.deepEqual(patches([object(1),object(2,[50,50,80,80])],[object(2,[50,50,80,80]),object(1)]),[])
})
test('deleting an original box records an explicit delete',()=>{
  assert.deepEqual(patches([object(1)],[]),[{objectId:1,op:'delete'}])
})
test('adding a box records identity, category and normalized geometry',()=>{
  assert.deepEqual(patches([],[object(5,[1.12345,2,20,30])]),[{objectId:5,op:'add',classKey:'sperm',bbox:[1.123,2,20,30]}])
})
test('restoring deleted original and deleting a new box are net zero',()=>{
  assert.deepEqual(patches([object(1)],[object(1)]),[])
  assert.deepEqual(patches([],[]),[])
})
test('a frame can add, delete and resize without changing the original snapshot',()=>{
  const a=[object(1),object(2)],snapshot=JSON.stringify(a)
  assert.deepEqual(patches(a,[object(2,[10,20,35,40]),object(3)]),[
    {objectId:1,op:'delete'},
    {objectId:2,bbox:[10,20,35,40]},
    {objectId:3,op:'add',classKey:'sperm',bbox:[10,20,30,40]},
  ])
  assert.equal(JSON.stringify(a),snapshot)
})
