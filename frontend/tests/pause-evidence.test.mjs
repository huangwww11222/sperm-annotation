import {test} from 'node:test'
import assert from 'node:assert/strict'
import {presentPauseEvidence,objectNumberText} from '../../work/annotation-validation/annotation/pauseEvidence.js'
import {pauseDiagnostics} from '../../work/annotation-validation/annotation/pauseDiagnostics.js'

test('whole-span explosion displays the exact saved ratio and upper boundary',()=>{
  const result=presentPauseEvidence('整体跨度为参考框的 201.1%，高于观察上限 200%，立即暂停',[])
  const metric=result.triggerMetrics[0]
  assert.equal(result.triggerMetrics.length,1)
  assert.equal(metric.label,'整体跨度');assert.equal(metric.current,'201.1%');assert.equal(metric.bound,200)
  assert.equal(metric.reference,'观察上限 200%');assert.equal(metric.immediate,true)
  assert(metric.scale.current>metric.scale.bound&&metric.scale.current<=100)
})

test('a lower-bound height trigger and persistence remain distinct evidence',()=>{
  const result=presentPauseEvidence('高度为参考框的 64.1%，低于观察下限 65%；同一对象尺寸风险持续 3 帧，达到 3 帧暂停条件',[])
  assert.equal(result.triggerMetrics[0].current,'64.1%');assert.equal(result.triggerMetrics[0].reference,'观察下限 65%')
  assert.equal(result.triggerMetrics[0].immediate,false)
  assert(result.triggerMetrics[0].scale.current<result.triggerMetrics[0].scale.bound)
  assert.deepEqual(result.triggerNotes,['同一对象尺寸风险持续 3 帧，达到 3 帧暂停条件'])
})

test('legacy ratios and mask evidence do not invent an unavailable comparison bound',()=>{
  for(const [summary,label,current] of [
    ['高度为参考框的 64%','高度','64%'],
    ['目标掩膜仅为已确认正常样本的 17%，立即暂停','目标掩膜面积','17%'],
  ]){
    const result=presentPauseEvidence(summary,[]),metric=result.triggerMetrics[0]
    assert.equal(metric.label,label);assert.equal(metric.current,current)
    assert.equal(metric.bound,undefined);assert.equal(metric.scale,undefined)
    assert.equal(metric.reference,'未记录比较界限')
  }
})

test('aspect multiples and all overlap partners retain their own recorded units',()=>{
  const result=presentPauseEvidence('长宽比变化 1.80 倍，达到观察界限 1.80 倍；#7 与 #8较小框覆盖 98.8%，观察界限 95%；#7 与 #9较小框覆盖 97%，观察界限 95%',[])
  assert.deepEqual(result.triggerMetrics.map(metric=>[metric.label,metric.current,metric.bound,metric.unit]),[
    ['长宽比变化','1.80倍',1.8,'倍'],['#7 与 #8 覆盖率','98.8%',95,'%'],['#7 与 #9 覆盖率','97%',95,'%'],
  ])
})

test('mismatched or malformed boundaries never generate a misleading comparison scale',()=>{
  for(const summary of ['高度为参考框的 64%，低于观察下限 1.8 倍','高度为参考框的 64%，低于观察下限 NaN%']){
    const metric=presentPauseEvidence(summary,[]).triggerMetrics[0]
    assert.equal(metric.bound,undefined);assert.equal(metric.scale,undefined)
  }
  const unknown='高度为参考框的 6..4%，低于观察下限 65%'
  assert.deepEqual(presentPauseEvidence(unknown,[]).triggerNotes,[unknown])
  assert.equal(presentPauseEvidence(unknown,[]).triggerMetrics.length,0)
})

test('an unknown image identity cause remains readable instead of becoming a size alert',()=>{
  const cause='两个追踪结果持续共用同一可见主体，身份尚未辨明'
  const result=presentPauseEvidence(cause+'；'+cause,['高度倍率 0.52','面积倍率 0.25'])
  assert.equal(result.triggerMetrics.length,0);assert.deepEqual(result.triggerNotes,[cause])
  assert.deepEqual(result.additionalMetrics,[{label:'高度',value:'0.52 倍'},{label:'面积',value:'0.25 倍'}])
})

test('advisory geometry cannot replace a hard image pause cause in the full presentation chain',()=>{
  const reasons=['manual_height_ratio=0.521 SOFT','image_foreign_core observations=3/3 HARD']
  const details={height_ratio:.521,geometry_thresholds:{manual_height_ratio:{min:.65}},image_observation_count:3,image_observation_required:3}
  const saved=JSON.stringify({reasons,details})
  const diagnostic=pauseDiagnostics(7,reasons,details,id=>'sperm '+id)
  const result=presentPauseEvidence(diagnostic.triggerSummary,['高度倍率 0.52','中心偏移(px) 3.00'])
  assert.equal(result.triggerMetrics.length,0)
  assert(result.triggerNotes.some(note=>note.includes('疑似混入了另一个光学主体')))
  assert(!result.triggerNotes.some(note=>note.includes('高度')))
  assert.equal(JSON.stringify({reasons,details}),saved,'formatting must not modify saved pause evidence')
})

test('a genuine diagonal pause keeps its cause while soft aspect data stays auxiliary',()=>{
  const diagnostic=pauseDiagnostics(14,['manual_aspect_change=2.400 SOFT','manual_diagonal_ratio=2.011 HARD'],{
    aspect_ratio_change:2.4,diagonal_ratio:2.011,geometry_extreme:true,
    geometry_thresholds:{manual_aspect_change:{max:1.8},manual_diagonal_ratio:{min:.4,max:2}},
  },id=>'sperm '+id)
  const result=presentPauseEvidence(diagnostic.triggerSummary,['长宽比倍率 2.40'])
  assert.deepEqual(result.triggerMetrics.map(metric=>metric.label),['整体跨度'])
  assert.equal(result.triggerMetrics[0].bound,200)
  assert.deepEqual(result.additionalMetrics,[{label:'长宽比',value:'2.40 倍'}])
})

test('auxiliary values retain px, coverage percent, and missing-value transparency',()=>{
  const input=Object.freeze(['中心偏移(px) 3.40','重叠覆盖率 0.988','面积倍率 0.66','未知记录','非有限 NaN'])
  const result=presentPauseEvidence(undefined,input)
  assert.equal(result.triggerMetrics.length,0);assert.equal(result.triggerNotes.length,0)
  assert.deepEqual(result.additionalMetrics,[
    {label:'中心偏移',value:'3.40 px'},{label:'重叠覆盖率',value:'98.8%'},{label:'面积',value:'0.66 倍'},
    {label:'未知记录',value:'—'},{label:'非有限 NaN',value:'—'},
  ])
})

test('displaying readable object numbers preserves raw saved evidence and names',()=>{
  const raw='#14 ↔ #15 rare sperm 15 · 参考第 57 帧；#14 与 #20共用主体'
  assert.equal(objectNumberText(raw),'对象 14 ↔ 对象 15 rare sperm 15 · 参考第 57 帧；对象 14 与 对象 20共用主体')
  assert.equal(raw,'#14 ↔ #15 rare sperm 15 · 参考第 57 帧；#14 与 #20共用主体')
  assert.equal(objectNumberText('rare sperm 15 · 无编号 # unknown'),'rare sperm 15 · 无编号 # unknown')
})
