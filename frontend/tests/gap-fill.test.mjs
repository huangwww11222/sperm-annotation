import {test} from 'node:test'
import assert from 'node:assert/strict'
import {gapGuess} from '../../work/annotation-validation/annotation/gapFill.js'

const box=(x,y,width=2,height=2)=>({x,y,width,height})

test('a gap between two boxes is filled in proportion to the distance',()=>{
  const boxes=new Map([[8,box(10,20)],[12,box(30,40,4,4)]])
  assert.deepEqual(gapGuess(boxes,9),box(15,25,2.5,2.5))
  assert.deepEqual(gapGuess(boxes,11),box(25,35,3.5,3.5))
})

test('the nearest box on each side is used, not the first or last of the track',()=>{
  const boxes=new Map([[0,box(0,0)],[8,box(10,20)],[12,box(30,40)],[20,box(90,90)]])
  assert.deepEqual(gapGuess(boxes,10),box(20,30))
})

test('outside the track the nearest box is copied; no boxes gives nothing',()=>{
  const boxes=new Map([[5,box(10,20)],[7,box(30,40)]])
  assert.deepEqual(gapGuess(boxes,2),box(10,20))
  assert.deepEqual(gapGuess(boxes,9),box(30,40))
  assert.equal(gapGuess(new Map(),3),null)
})
