import {test} from 'node:test'
import assert from 'node:assert/strict'
import {pauseDiagnostics} from '../../work/annotation-validation/annotation/pauseDiagnostics.js'
const describe = (reasons,details={},objectId=7) => pauseDiagnostics(objectId,reasons,details,id=>'sperm '+id)
test('a small height crossing shows the actual server ratio and bound rather than a soft advisory metric',()=>{
  const value=describe(['manual_area_ratio=0.660 SOFT','manual_height_ratio=0.641 SOFT'],{
    area_ratio:.66,height_ratio:.641,geometry_thresholds:{manual_area_ratio:{min:.55,max:1.8},manual_height_ratio:{min:.65,max:1.5}},
    geometry_observation_count:3,geometry_observation_required:3,geometry_risk_start_frame:136,decision_frame_index:138,
  })
  assert.equal(value.triggerSummary,'高度为参考框的 64.1%，低于观察下限 65%；同一对象尺寸风险持续 3 帧，达到 3 帧暂停条件')
  assert.equal(value.pauseStartFrame,136);assert.equal(value.decisionFrame,138)
})
test('the aspect rule includes an exact upper boundary and explains immediate extremes',()=>{
  const value=describe(['manual_aspect_change=1.800 HARD'],{aspect_ratio_change:1.8,geometry_thresholds:{manual_aspect_change:{max:1.8}},geometry_extreme:true})
  assert.match(value.triggerSummary,/达到观察界限 1\.80 倍/);assert.match(value.triggerSummary,/立即暂停/)
})
test('all related overlap partners remain visible while unrelated or invalid pairs are rejected',()=>{
  const value=describe(['bbox_overlap_with=8 coverage=1 HARD'],{overlap_pairs:[
    {object_ids:[7,8],coverage:1,iou:.96,threshold:.95,suspected_merge:true,risk_streak:3,persistence_frames:3,reference_frame:115,reference_source:'confirmed-normal'},
    {object_ids:[9,7],coverage:.97,iou:.91},
    {object_ids:[7,8],coverage:1},
    {object_ids:[10,11],coverage:1},
    {object_ids:[7,null],coverage:1},
    {object_ids:[7,0],coverage:1},
  ]})
  assert.deepEqual(value.overlapPairs.map(item=>item.otherObjectId),[8,9])
  assert.equal(value.overlapPairs[0].referenceFrame,115)
  assert.match(value.triggerSummary,/#7 与 #8较小框覆盖 100%，观察界限 95%/)
})
test('legacy evidence remains readable without inventing thresholds or future reference frames',()=>{
  const value=describe(['manual_height_ratio=0.640 HARD'],{})
  assert.equal(value.triggerSummary,'高度为参考框的 64%')
  assert.equal(value.pauseStartFrame,undefined);assert.equal(value.decisionFrame,undefined)
  const missing=describe([],{decision_frame_index:null,pause_started_frame:'',geometry_observation_count:false})
  assert.equal(missing.pauseStartFrame,undefined);assert.equal(missing.decisionFrame,undefined)
})
test('confirmed mask collapse explains its scalar evidence without inventing a missing threshold',()=>{
  for(const details of [{mask_area_ratio:.17}, {current_mask_area:17,mask_reference_area:100}]) {
    const value=describe(['confirmed_mask_area_ratio=0.170 HARD'],{...details,geometry_extreme:true})
    assert.equal(value.triggerSummary,'目标掩膜仅为已确认正常样本的 17%，立即暂停')
    assert.equal(value.maskTrigger,true)
    assert(!value.triggerSummary.includes('下限') && !value.triggerSummary.includes('上限'))
  }
})
test('independent image risk explains its cause without promoting advisory shape changes',()=>{
  const value=describe(['image_foreign_core observations=1/3 HARD','manual_height_ratio=0.521 SOFT'],{
    height_ratio:.521,geometry_thresholds:{manual_height_ratio:{min:.65}},
    image_observation_count:3,image_observation_required:3,pause_started_frame:15,decision_frame_index:17,
  })
  assert.match(value.triggerSummary,/疑似混入了另一个光学主体/)
  assert.match(value.triggerSummary,/同一对象图像风险持续 3 帧/)
  assert(!value.triggerSummary.includes('高度'))
  assert.equal(value.pauseStartFrame,15);assert.equal(value.decisionFrame,17)
})
