import {test} from 'node:test'
import assert from 'node:assert/strict'
import {savedTrackingCalibration} from '../../work/annotation-validation/annotation/trackingCalibration.js'
const pair=(a,b)=>({objectIds:[a,b]})
const normal=(id,references)=>({objectId:id,decision:'normal',overlapReferences:references})
test('pure overlap confirmations expose both endpoints, all partners and one count for repeated pair evidence',()=>{
  const events=[normal(1,[pair(1,2),pair(1,3)]),normal(2,[pair(2,1),pair(2,3)]),normal(3,[pair(3,1),pair(3,2)])]
  const values=savedTrackingCalibration([],events)
  assert.deepEqual(values.map(v=>[v.objectId,v.overlapCount,v.overlapPartnerIds]),[[1,2,[2,3]],[2,2,[1,3]],[3,2,[1,2]]])
  assert(events.every(e=>e.decision==='normal'),'display computation must not alter the audit events')
})
test('reset or corrected clears both ends of related pairs while preserving independent pairs, motion and shape entries',()=>{
  const base=[normal(1,[pair(1,2),pair(1,3)]),normal(2,[pair(2,3)])]
  for(const decision of ['reset','corrected']) {
    const values=savedTrackingCalibration([{objectId:4}], [...base,{objectId:5,decision:'normal',geometryReference:{frameIndex:115}},{objectId:1,decision}])
    assert(!values.some(v=>v.objectId===1))
    assert.deepEqual(values.find(v=>v.objectId===2).overlapPartnerIds,[3])
    assert.deepEqual(values.find(v=>v.objectId===3).overlapPartnerIds,[2])
    assert.equal(values.find(v=>v.objectId===4).sampleCount,1)
    assert.equal(values.find(v=>v.objectId===5).geometryFrame,115)
  }
  assert.deepEqual(savedTrackingCalibration([],base,[1]).map(v=>[v.objectId,v.overlapPartnerIds]),[[2,[3]],[3,[2]]])
})
