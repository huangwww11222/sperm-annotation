"""Disposable real-video sources for review additions/deletions and returns."""
import json
import os
from pathlib import Path
import uuid

import cv2
import numpy as np
from app import db, review_workflow as review, confirmation_workflow as confirmation, annotation_completion as completion
from app.auth import hash_password, sign_jwt
from app.config import DB_FILE, TRACK_DATA_DIR
from app.review_schema import apply_review_schema

assert '/work/e2e-confirm-' in DB_FILE.as_posix(), DB_FILE
assert '/work/e2e-confirm-' in TRACK_DATA_DIR.as_posix(), TRACK_DATA_DIR
db.init_db()
for name in ['confirm-A', 'confirm-B', 'confirm-C']:
    if not db.get_user(name):
        db.create_user(name, hash_password('confirmation-test'))
with db.connect() as c:
    apply_review_schema(c); review.migrate(c); confirmation.migrate(c)
result={'token':sign_jwt({'uid':1}),'items':{}}
for mode in ['main','pending','return','race-draft','race-reserve']:
    mid='presence-'+mode+'-'+uuid.uuid4().hex[:8]
    directory=TRACK_DATA_DIR/mid;directory.mkdir(parents=True)
    video=directory/('presence-'+mode+'.avi')
    writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),10,(800,450))
    annotations=[]
    for fi in range(4):
        image=np.full((450,800,3),100+fi*3,dtype=np.uint8)
        ids=([1,2] if fi<2 else [] if fi==2 else [1]) if mode=='main' else [1]
        for oid in ids:
            x,y=(100,100) if oid==1 else (400,210)
            cv2.ellipse(image,(x+25,y+20),(15,8),30,0,360,(220,220,220),-1)
            cv2.line(image,(x+25,y+20),(x+65,y+50),(180,180,180),2)
            annotations.append({'id':f'presence-{fi}-{oid}','objectId':oid,'source':'manual','frameIndex':fi,'name':f'sperm {oid}','bbox':{'x':x/8,'y':y/4.5,'width':60/8,'height':60/4.5}})
        cv2.putText(image,f'{mode} frame {fi+1}',(20,40),cv2.FONT_HERSHEY_SIMPLEX,.8,(235,235,235),1)
        writer.write(image)
    writer.release()
    info={'width':800,'height':450,'fps':10,'frameCount':4}
    (directory/'media.json').write_text(json.dumps({'videoName':video.name,**info}))
    workspace={'revision':0,'updatedBy':1,'currentFrame':0,'manualAnnotations':annotations,'manualBaselines':[],'deletedObjectIds':[],'deletedFrameObjects':[]}
    (directory/'workspace_state.json').write_text(json.dumps(workspace))
    sent=completion.complete(directory,video,mid,1,uuid.uuid4().hex,{'expectedSourceRevision':completion.preview(directory,1,info)['sourceRevision'],'confirmComplete':True,'explicitEmptyFrameRanges':[{'start':2,'end':2}] if mode=='main' else []},info)
    result['items'][mode]={'mid':mid,'sid':sent['session']['id'],'name':video.name,'workspace':workspace}
target=Path(os.environ.get('REVIEW_PRESENCE_FIXTURE','work/review-presence-browser-fixture.json'))
target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(result))
print('Created isolated review-presence sources and immutable A snapshots.')
