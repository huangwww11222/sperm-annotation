import test from 'node:test'
import assert from 'node:assert/strict'
import { comparisonCrop,rect,presenceCopy } from '../../work/confirmation-geometry/confirmation/geometry.js'
test('A/B swap never changes the shared crop',()=>{
 const a=[200,140,260,200],b=[207.125,147,272,211]
 assert.equal(comparisonCrop(a,b,800,450,5),comparisonCrop(b,a,800,450,5))
})
test('zoom changes local crop while preserving both boxes and source boundaries',()=>{
 const a=[0,0,10,10],b=[35,26,50,40]
 const far=comparisonCrop(a,b,800,450,3).split(' ').map(Number),near=comparisonCrop(a,b,800,450,8).split(' ').map(Number)
 assert(near[2]<far[2]);assert.equal(near[0],0);assert.equal(near[1],0)
 assert(near[2]>=b[2]&&near[3]>=b[3])
 assert.equal(comparisonCrop([0,0,10,10],[780,430,800,450],800,450,8),'0 0 800 450')
})
test('random crops keep both complete geometries visible, including wide/tall videos',()=>{
 for(const [w,h] of [[800,450],[320,1200],[20,20]])for(let n=0;n<100;n++){
  const a=[w*n/200,h*n/200,w*n/200+w/4,h*n/200+h/4],b=[w/2,h/2,w,h]
  const [x,y,cw,ch]=comparisonCrop(a,b,w,h,3+n%6).split(' ').map(Number)
  assert(x>=0&&y>=0&&x+cw<=w+.001&&y+ch<=h+.001)
  for(const q of [a,b])assert(q[0]>=x-.001&&q[1]>=y-.001&&q[2]<=x+cw+.001&&q[3]<=y+ch+.001)
 }
})
test('subpixel box geometry is retained',()=>{
 assert.deepEqual(rect([1.125,2.25,20.625,50.5]),{x:1.125,y:2.25,width:19.5,height:48.25})
})
test('added and deleted annotations show the same real source crop on both sides',()=>{
 const box=[700,360,760,420]
 assert.equal(comparisonCrop(null,box,800,450,5),comparisonCrop(box,null,800,450,5))
 const [x,y,w,h]=comparisonCrop(null,box,800,450,5).split(' ').map(Number)
 assert(x<=box[0]&&y<=box[1]&&x+w>=box[2]&&y+h>=box[3])
 assert.throws(()=>comparisonCrop(null,null,800,450,5),/标注/)
})
test('confirmation labels distinguish accepting absence from accepting a box',()=>{
 const box=[1,2,3,4]
 assert.equal(presenceCopy(null,box).a,'不采用补标')
 assert.equal(presenceCopy(null,box).b,'采用补标')
 assert.equal(presenceCopy(box,null).a,'保留原框')
 assert.equal(presenceCopy(box,null).b,'确认删除')
 assert.equal(presenceCopy(box,box).a,'保留原标注 A')
})
