import {test} from 'node:test'
import assert from 'node:assert/strict'
import {byFolder,cellPosition,donePart,firstOpenPage,fitGrid,missLines,nextCard,nextDifference,nextTodo,stateText,stepFrame,toggleDropped} from '../../work/annotation-validation/flow/flowLogic.js'

const progress=(over={})=>({runId:'r',targets:100,pages:5,pagesDone:0,second:null,secondDone:0,yes:0,unsure:0,scanNeeded:false,scanDone:false,state:'wall',lastActorId:null,lastActivityAt:null,...over})
const item=(state,over={},task=null)=>({mediaId:'m',videoName:'v',folderPath:'',sample:'',frameCount:60,fps:20,width:4096,height:3000,sizeBytes:1,task,state,progress:['new','finding','failed'].includes(state)?null:progress({state,...over})})

test('the list says what each video needs next',()=>{
  assert.equal(stateText(item('new')).text,'还没找目标')
  assert.equal(stateText(item('finding',{}, {status:'queued',stage:'queued'})).text,'排队找目标')
  assert.equal(stateText(item('finding',{}, {status:'running',stage:'detecting',processedFrames:30,totalFrames:60})).text,'找目标 50%')
  assert.deepEqual(stateText(item('failed')),{text:'找目标失败',tone:'bad'})
  assert.equal(stateText(item('wall')).text,'待判 · 100 个目标')
  assert.match(stateText(item('wall',{pagesDone:2})).text,/^判了 \d+%$/)
  assert.deepEqual(stateText(item('done',{yes:7})),{text:'判完 · 7 个精子',tone:'ok'})
})
test('progress never shows 100% before a video is done, and never goes backwards',()=>{
  let last=0
  for(let p=0;p<=5;p++){const part=donePart(item('wall',{pagesDone:p}));assert.ok(part>=last&&part<1);last=part}
  for(let k=0;k<6;k++){const part=donePart(item('cards',{pagesDone:5,second:6,secondDone:k}));assert.ok(part<1)}
  assert.equal(donePart(item('done',{pagesDone:5,second:0})),1)
})
test('the next video is one that still needs a person, after the current one and wrapping',()=>{
  const items=[['a','wall'],['b','done'],['c','finding'],['d','cards'],['e','failed']].map(([mediaId,state])=>({mediaId,state}))
  assert.equal(nextTodo(items,'a').mediaId,'d')
  assert.equal(nextTodo(items,'d').mediaId,'a')
  assert.equal(nextTodo(items,null).mediaId,'a')
  assert.equal(nextTodo([{mediaId:'a',state:'wall'}],'a'),null)
})
test('tiles fill the space without ever leaving it',()=>{
  for(const [n,w,h] of [[24,1600,800],[24,900,900],[5,1600,800],[1,500,300],[9,600,420]]){
    const {cols,size}=fitGrid(n,w,h,10)
    assert.ok(size*cols+10*(cols-1)<=w&&size*Math.ceil(n/cols)+10*(Math.ceil(n/cols)-1)<=h,`${n} ${w}x${h}`)
  }
  assert.deepEqual(fitGrid(24,1600,800,10),{cols:6,size:192})
  assert.equal(fitGrid(9,2000,1000,8,210,6).size<=210,true)
  assert.ok(fitGrid(24,0,0,10).size>0)  // Before the first measurement.
})
test('a playing strip skips the frames that were clicked off',()=>{
  assert.deepEqual([0,1,2,3].map(k=>stepFrame(4,k)),[1,2,3,0])
  assert.equal(stepFrame(4,0,new Set([1,2])),3)
  assert.equal(stepFrame(1,0),0)
  assert.equal(cellPosition(5,4),'100% 0')
  assert.equal(cellPosition(1,0),'0 0')
})
test('judging resumes at what is still open',()=>{
  assert.equal(firstOpenPage(3,{0:{},2:{}}),1)
  assert.equal(firstOpenPage(3,{0:{},1:{},2:{}}),-1)
  const answered=new Set(['b'])
  assert.equal(nextCard(['a','b','c'],key=>answered.has(key),0),2)
  assert.equal(nextCard(['a','b','c'],key=>answered.has(key),2),0)
  assert.equal(nextCard(['b'],key=>answered.has(key),-1),-1)
})
test('frames can be clicked off and back on, but one always stays',()=>{
  assert.deepEqual(toggleDropped([3,4,5],[],4),[4])
  assert.deepEqual(toggleDropped([3,4,5],[4],3),[3,4])
  const two=[3,4]
  assert.equal(toggleDropped([3,4,5],two,5),two)
  assert.deepEqual(toggleDropped([3,4,5],[3,4],4),[3])
  assert.deepEqual(toggleDropped([3,4],[],9),[])
})
test('measured misses: no percentage from a handful, nothing claimed from nothing',()=>{
  assert.deepEqual(missLines({planted:0,plantedMissed:0,blindMarks:0,blindUnmatched:0,yes:0,secondPassOnly:0}),[])
  assert.deepEqual(missLines(null),[])
  assert.deepEqual(missLines({planted:5,plantedMissed:1,blindMarks:0,blindUnmatched:0,yes:0,secondPassOnly:0}),['混进去的已知精子：5 个里漏点 1 个'])
  assert.match(missLines({planted:50,plantedMissed:2,blindMarks:40,blindUnmatched:1,yes:30,secondPassOnly:4}).join('\n'),/（4%）[\s\S]*（3%）[\s\S]*补回 4 个/)
})
test('videos are grouped by folder in list order',()=>{
  const items=[['','a'],['甲','b'],['甲','c'],['乙','d']].map(([folderPath,mediaId])=>({folderPath,mediaId}))
  assert.deepEqual(byFolder(items).map(group=>[group.folder,group.items.length]),[['',1],['甲',2],['乙',1]])
})
test('a re-check never turns a finished video into an unfinished one',()=>{
  const recheck=(state,over={})=>({id:'r~1',state,startedBy:1,samePerson:false,progress:progress({runId:'r~1',...over})})
  const done={...item('done',{yes:7}),recheck:recheck('open',{pagesDone:2})}
  assert.match(stateText(done).text,/^判完 · 7 个精子 · 复查 \d+%$/)
  assert.equal(nextTodo([{...done,mediaId:'x'}],null),null)
  assert.match(stateText({...done,recheck:recheck('settling')}).text,/复查待定$/)
  assert.match(stateText({...done,recheck:recheck('done')}).text,/复查过$/)
})
test('settling goes through the differences that are still open',()=>{
  const list=['a','b','c'].map(key=>({key,settled:key==='b'?{answer:'yes',dropped:[],revision:1}:null}))
  assert.equal(nextDifference(list,0),2)
  assert.equal(nextDifference(list,2),0)
  assert.equal(nextDifference(list.map(entry=>({...entry,settled:{}})),0),-1)
})
