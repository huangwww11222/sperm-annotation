import {test} from 'node:test'
import assert from 'node:assert/strict'
import {validateTrackingResult} from '../../work/annotation-validation/annotation/trackingResult.js'
const object={objectId:7,frameIndex:2,bbox:[1,2,4,5]}
const frame={frameIndex:2,timestampMs:200,annotations:[object]}
const result={state:'available',frames:[frame],count:1}
test('valid empty, original-frame results and compatible single frame remain readable',()=>{
  for(const value of [result,frame,{state:'available',frames:[],count:0},{state:'not_generated',frames:[],count:0}]) validateTrackingResult(value)
})
test('successful malformed JSON cannot be interpreted as empty annotations',()=>{
  for(const value of [null,{}, {...result,frames:null}, {...result,count:0}, {...result,state:'not_generated'},
    {...result,frames:[frame,frame],count:2}, {...result,frames:[{...frame,annotations:null}]},
    {...result,frames:[{...frame,annotations:[object,object]}]},
    {...result,frames:[{...frame,annotations:[{...object,objectId:0}]}]},
    {...result,frames:[{...frame,annotations:[{...object,frameIndex:0}]}]},
    {...result,frames:[{...frame,annotations:[{...object,bbox:[1,2,0,5]}]}]},
    {...result,frames:[{...frame,annotations:[{...object,bbox:[1,2,NaN,5]}]}]}]) assert.throws(()=>validateTrackingResult(value))
})

import { reconcileTerminalIdentities } from '../../work/annotation-validation/annotation/trackingResult.js'
test('terminal-local IDs reconcile once and preserve source identity for retry / refresh',()=>{
  const hash='a'.repeat(64), objects=[{id:'head',source:'manual',objectId:1,frameIndex:0},{id:'tail',source:'manual',objectId:91,frameIndex:5}]
  const terminal={frameIndex:5,anchorHash:hash,matches:{91:1}}
  const result=reconcileTerminalIdentities(objects,terminal)
  assert.equal(result[0],objects[0]);assert.equal(result[1].objectId,1);assert.equal(result[1].endpointOriginalObjectId,91)
  assert.equal(objects[1].objectId,91)
  assert.deepEqual(reconcileTerminalIdentities(result,terminal),result)
  assert.deepEqual(reconcileTerminalIdentities(objects,undefined),objects)
})

test('malformed endpoint identity mappings cannot silently change human object IDs',()=>{
  const hash='a'.repeat(64)
  for(const terminalIdentity of [{frameIndex:2,anchorHash:hash,matches:{91:-1}},{frameIndex:2,anchorHash:hash,matches:{91:1,92:1}},{frameIndex:2,anchorHash:'bad',matches:{91:1}}]){
    assert.throws(()=>validateTrackingResult({...result,terminalIdentity}))
  }
  validateTrackingResult({...result,terminalIdentity:{frameIndex:2,anchorHash:hash,matches:{91:1}}})
})

test('corrected same-run association reuses the original tail ID without moving the human box',()=>{
  const hash='a'.repeat(64),box={x:10,y:20,width:3,height:4}
  const objects=[{id:'tail',source:'manual',objectId:17,endpointOriginalObjectId:37,endpointAnchorHash:hash,frameIndex:25,bbox:box}]
  const fixed=reconcileTerminalIdentities(objects,{frameIndex:25,anchorHash:hash,matches:{37:18}})
  assert.equal(fixed[0].objectId,18)
  assert.equal(fixed[0].endpointOriginalObjectId,37)
  assert.equal(fixed[0].bbox,box)
  assert.equal(objects[0].objectId,17)
  assert.deepEqual(reconcileTerminalIdentities(fixed,{frameIndex:25,anchorHash:hash,matches:{37:18}}),fixed)
})

test('synthesis status cannot claim complete when frame coverage is partial',()=>{
  validateTrackingResult({...result,jointTracking:{state:'checked',checkedFrames:1}})
  validateTrackingResult({...result,jointTracking:{state:'failed',checkedFrames:0}})
  for(const jointTracking of [{state:'checked',checkedFrames:0},{state:'checked',checkedFrames:2},{state:'failed',checkedFrames:NaN},{state:'unknown',checkedFrames:1}])assert.throws(()=>validateTrackingResult({...result,jointTracking}))
})
