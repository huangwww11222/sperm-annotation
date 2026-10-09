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
