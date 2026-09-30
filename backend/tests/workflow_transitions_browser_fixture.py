"""Disposable A/B/C videos for withdrawal, duplicate reset and partial re-review."""
import json
from pathlib import Path
import uuid
import cv2
import numpy as np
from app import db, review_workflow as review, confirmation_workflow as confirmation, annotation_completion as completion
from app.auth import sign_jwt
from app.config import DB_FILE, TRACK_DATA_DIR
from app.review_schema import apply_review_schema

assert '/work/e2e-confirm-' in str(DB_FILE), DB_FILE
assert '/work/e2e-confirm-' in str(TRACK_DATA_DIR), TRACK_DATA_DIR
with db.connect() as c:
    apply_review_schema(c);review.migrate(c);confirmation.migrate(c)
result={'token':sign_jwt({'uid':1}), 'items':{}}
for index, mode in enumerate(['free', 'pending', 'started', 'confirm']):
    mid='transition-'+mode+'-'+uuid.uuid4().hex[:6]
    directory=TRACK_DATA_DIR/mid;directory.mkdir(parents=True)
    video=directory/(mode+'.avi')
    writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),10,(800,450))
    for fi in range(3):
        frame=np.full((450,800,3),60+index*30+fi,dtype=np.uint8)
        cv2.rectangle(frame,(100+fi,100),(140+fi,125),(180,200,210),-1)
        cv2.putText(frame,mid,(20,40),cv2.FONT_HERSHEY_SIMPLEX,.7,(235,235,235),1)
        writer.write(frame)
    writer.release()
    info={'width':800,'height':450,'fps':10,'frameCount':3}
    (directory/'media.json').write_text(json.dumps({'videoName':video.name,**info}))
    state={'revision':0,'updatedBy':1,'currentFrame':1,'manualAnnotations':[
        {'id':f'manual-{fi}-1','objectId':1,'source':'manual','frameIndex':fi,'name':'sperm 1',
         'bbox':{'x':12.5,'y':100/450*100,'width':5,'height':25/450*100}} for fi in range(3)],
        'manualBaselines':[],'deletedObjectIds':[],'deletedFrameObjects':[]}
    (directory/'workspace_state.json').write_text(json.dumps(state))
    item={'mid':mid,'video':str(video.resolve()),'name':video.name}
    if mode!='free':
        sent=completion.complete(directory,video,mid,1,uuid.uuid4().hex,
             {'expectedSourceRevision':completion.preview(directory,1,info)['sourceRevision'],'confirmComplete':True,'explicitEmptyFrameRanges':[]}, info)
        sid=sent['session']['id'];item['sid']=sid
        if mode!='pending':review.write('claim',sid,1,uuid.uuid4().hex,{})
        if mode=='confirm':
            for fi in range(3):
                patch=[{'objectId':1,'bbox':[105,100,145,125]}] if fi<2 else []
                review.write('submit',sid,1,uuid.uuid4().hex,{'expectedFrameRevision':0,'patch':patch},fi)
            cid=review.write('finish',sid,1,uuid.uuid4().hex,{'expectedSessionRevision':review.get_session(sid,1)['revision']})['confirmationSessionId']
            confirmation.write('claim',cid,1,uuid.uuid4().hex,{})
            for change in confirmation.list_changes(cid,1):
                confirmation.write('decide',cid,1,uuid.uuid4().hex,{'choice':'B','expectedDecisionRevision':change['decisionRevision']},change['changeId'])
            item['cid']=cid
    result['items'][mode]=item
Path('work/workflow-transitions-browser-fixture.json').write_text(json.dumps(result))
print('Created isolated withdrawal/reimport/re-review media.')
