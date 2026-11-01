import assert from 'node:assert/strict'
import test from 'node:test'
import { reviewPresentation, reviewCategory, reviewLabels, reviewLocationText, reviewTarget, reviewTargetZoom } from '../../work/annotation-validation/annotation/reviewPresentation.js'

const obj = (objectId, source = 'ai') => ({ id: `obj-${objectId}`, objectId, source, name: 'sperm', bbox: { x: 10, y: 10, width: 10, height: 10 } })
const item = (patch = {}) => ({ id: 'test', kind: 'doubt', category: 'position', frame: 0, startFrame: 0, endFrame: 4, objectIds: [1,2,3,4], message: '不该出现的诊断长句', check: null, ...patch })
test('only current-frame AI objects are emphasized, at most three; geometry stays untouched', () => {
  const objects = [obj(1,'manual'),obj(2),obj(3),obj(4),obj(5)]
  const before = structuredClone(objects)
  assert.deepEqual(reviewPresentation(item(),0,objects,800,450).objectIds,[2,3,4])
  const scoped = item({ observations: [{ frameIndex: 1, objectIds: [2], candidates: [] }, { frameIndex: 2, objectIds: [3], candidates: [] }] })
  assert.deepEqual(reviewPresentation(scoped,1,objects,800,450).objectIds,[2])
  assert.deepEqual(reviewPresentation(scoped,2,objects,800,450).objectIds,[3])
  assert.deepEqual(reviewPresentation(scoped,3,objects,800,450).objectIds,[])
  assert.deepEqual(objects,before)
})
test('only missing-head candidates make small location markers; invalid and duplicate candidates are excluded', () => {
  const candidates = [[0,0,20,20],[0,0,20,20],[30,0,50,20],[60,0,80,20],[90,0,110,20],[NaN,0,1,2]]
  assert.equal(reviewPresentation(item({candidates}),0,[],100,100).circles.length,0)
  const missing = item({ category:'missing', candidates })
  const circles = reviewPresentation(missing,0,[],100,100).circles
  assert.equal(circles.length,3)
  assert.deepEqual(circles[0],{key:'test:0:candidate-0',left:'10%',top:'10%',size:28,label:'1'})
  assert.equal(reviewPresentation(missing,1,[],100,100).circles.length,0)
  assert.equal(reviewPresentation(missing,0,[],0,100).circles.length,0)
  assert.equal(reviewPresentation(missing,5,[],100,100).circles.length,0)
})
test('later observations use their own candidate positions, never first-frame rectangles', () => {
  const scoped = item({ category:'missing', observations:[{frameIndex:1,objectIds:[],candidates:[[40,40,60,60]]}] })
  assert.deepEqual(reviewPresentation(scoped,1,[],100,100).circles,[{key:'test:1:candidate-0',left:'50%',top:'50%',size:28,label:'1'}])
})
test('legacy long messages never become user-facing category labels', () => {
  for (const message of ['自动标注仍有不确定性，请结合前后画面核对头部与身份','任意未识别原文']) {
    const label = reviewLabels[reviewCategory(item({category:undefined,message}))]
    assert.ok(label.length<=12 && !label.includes(message))
  }
})

test('location labels identify region, stable sequence and contiguous span', () => {
  assert.equal(reviewLocationText(item({region:'右下',index:2,countInFrame:4,endFrame:2})),'右下 · 2/4 · 连续 3 帧')
  assert.equal(reviewLocationText(item({region:'右下',index:2,countInFrame:4,endFrame:10,observations:[{frameIndex:0},{frameIndex:5},{frameIndex:10}]})),'右下 · 2/4 · 3 个观测帧')
  assert.equal(reviewLocationText(item({region:'中央',index:1,countInFrame:1,endFrame:0})),'中央 · 1/1')
  assert.ok(!reviewLocationText(item()).includes('不该出现'))
})
test('navigation converts only the active candidate frame and uses the effective AI box', () => {
  const missing=item({category:'missing',anchor:[0,0,20,20],observations:[{frameIndex:2,objectIds:[],candidates:[[400,225,480,270]]}]})
  assert.deepEqual(reviewTarget(missing,2,[],800,450),{x:50,y:50,width:10,height:10})
  assert.equal(reviewTarget(missing,3,[],800,450),null)
  assert.deepEqual(reviewTarget(missing,0,[],800,450),{x:0,y:0,width:2.5,height:100*20/450})
  const objects=[obj(1,'manual'),obj(2)]
  assert.deepEqual(reviewTarget(item(),0,objects,800,450),objects[1].bbox)
  assert.equal(reviewTarget(item({kind:'coverage'}),0,objects,800,450),null)
})
test('automatic target zoom only enlarges tiny locations and respects manual zoom', () => {
  assert.equal(reviewTargetZoom(1,400,5,false),4)
  assert.equal(reviewTargetZoom(2,800,5,false),2)
  assert.equal(reviewTargetZoom(1,400,5,true),1)
  assert.equal(reviewTargetZoom(2,100,1,false),10)
  assert.equal(reviewTargetZoom(2,0,5,false),2)
})
test('static targets use the same candidate marker and zoom target as missing', () => {
  const staticItem=item({category:'static',anchor:[20,30,40,50],candidates:[[20,30,40,50]],observations:[{frameIndex:0,objectIds:[],candidates:[[20,30,40,50]]}]})
  assert.equal(reviewLabels.static,'静止目标')
  assert.equal(reviewPresentation(staticItem,0,[],100,100).circles.length,1)
  assert.deepEqual(reviewTarget(staticItem,0,[],100,100),{x:20,y:30,width:20,height:20})
})
test('marker number agrees with the checklist and size adapts without adding a rectangle', () => {
  const p=reviewPresentation(item({category:'missing',index:4,candidates:[[0,0,20,20]]}),0,[],100,100,300)
  assert.equal(p.circles[0].size,60)
  assert.equal(p.circles[0].label,'4')
})
