"""Isolated saved-old-result fixture. No model inference or business data."""
import json
import os
import sys
from pathlib import Path
from app import main


def install(mid, reassigned=False):
    root=Path(os.environ['APP_STORAGE_DIR']).resolve()
    assert 'e2e-confirm-ux-' in str(root) and root.is_relative_to(Path(__file__).resolve().parents[2]/'work')
    directory=main.media_dir(mid)
    state=json.loads((directory/'workspace_state.json').read_text())
    info=main.source_media_info(directory);end=info['frameCount']-1
    tail=next(o for o in state['manualAnnotations'] if o.get('frameIndex')==end)
    tail['objectId']=91;tail['name']='rare sperm 91'
    if reassigned:
        tail.update(objectId=1,endpointAnchorHash='a'*64,endpointOriginalObjectId=91)
    else:
        tail.pop('endpointAnchorHash',None);tail.pop('endpointOriginalObjectId',None)
    state['revision']+=1
    b=tail['bbox'];w,h=info['width'],info['height'];box=[b['x']*w/100,b['y']*h/100,(b['x']+b['width'])*w/100,(b['y']+b['height'])*h/100]
    rows=[]
    for fi in range(end+1):
        check=dict(state='checked',policy='joint-endpoints-v2',anchorKind='human-terminal',anchorFrame=end,anchorHash='a'*64,firstFrame=0,lastFrame=end,matches={},unmatchedTailIds=[91],checkedFrames=end+1)
        if fi==end:check['terminalAnnotations']=[dict(object_id=91,bbox=box,source='manual',name='rare sperm 91')]
        padded=[box[0]-20,box[1]-20,box[2]+20,box[3]+20]
        objects=[dict(object_id=1,bbox=padded,source='sam3',name='rare sperm 1')]
        findings=[dict(kind='endpoint_identity_uncertain',objectId=None,tailObjectId=91,bbox=box,message='frozen test reverse observation')]
        if reassigned:
            old=[padded[0]-120,padded[1],padded[2]-120,padded[3]]
            objects[0]['bbox']=old
            objects.append(dict(object_id=2,bbox=padded,source='sam3',name='rare sperm 2'))
            check['matches']={'91':1};check['unmatchedTailIds']=[]
            findings=[dict(kind='endpoint_disagreement',objectId=1,tailObjectId=91,bbox=old,reverseBox=box,message='frozen incorrect endpoint association')]
        rows.append(dict(source_frame_index=fi,frame_index=fi,objects=objects,reviewEvidence=dict(state='checked',findings=findings,endpointCheck=check,jointCheck=dict(state='checked',policy='joint-head-v1',uncertainObjectIds=[]))))
    (directory/'tracker_results.json').write_text('\n'.join(json.dumps(r) for r in rows))
    (directory/'workspace_state.json').write_text(json.dumps(state))


if __name__=='__main__':install(sys.argv[1],len(sys.argv)>2 and sys.argv[2]=='reassigned')
