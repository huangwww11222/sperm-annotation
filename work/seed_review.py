import json
from pathlib import Path
import cv2
import numpy as np
from app.config import DB_FILE, TRACK_DATA_DIR
from app import db
from app.auth import hash_password, sign_jwt
from app.review_schema import apply_review_schema
from app import review_workflow as w
from app.review_repository import upsert_media_revision, freeze_baseline, create_review_session,compute_file_sha256
# This script only creates isolated test data; never point it at a real database.
assert '/work/' in str(DB_FILE) and '/e2e-' in str(DB_FILE), DB_FILE
DB_FILE.parent.mkdir(parents=True,exist_ok=True)
db.init_db()
for name in ['review-A','review-B','review-C']:
    if not db.get_user(name): db.create_user(name,hash_password('review-test'))
with db.connect() as c:
    apply_review_schema(c);w.migrate(c)
directory=TRACK_DATA_DIR/'review-fixture';directory.mkdir(parents=True,exist_ok=True)
video=directory/'review-fixture.avi'
writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),10,(800,450))
frames=[]
for i in range(3):
    image=np.random.default_rng(42+i).integers(80,140,(450,800,3),dtype=np.uint8)
    boxes=[[200,140,260,200],[400,210,470,290]] if i<2 else []
    for x1,y1,x2,y2 in boxes:cv2.ellipse(image,((x1+x2)//2,(y1+y2)//2),((x2-x1)//3,(y2-y1)//3),30,0,360,(210,210,210),-1)
    cv2.putText(image,f'Frame {i+1}',(20,40),cv2.FONT_HERSHEY_SIMPLEX,1,(255,255,255),2)
    writer.write(image)
    frames.append(dict(frameIndex=i,coverage='objects' if boxes else 'empty',objects=[dict(objectId=j+1,bbox=b,classKey='sperm') for j,b in enumerate(boxes)]))
writer.release()
(directory/'media.json').write_text(json.dumps(dict(videoName=video.name,width=800,height=450,fps=10,frameCount=3)))
m=upsert_media_revision('review-fixture',compute_file_sha256(video),video.stat().st_size,800,450,10,3)
a=freeze_baseline(m,'review-fixture',1,frames)
s=create_review_session(a['id'])
Path('work/browser-fixture.json').write_text(json.dumps(dict(sid=s['id'],token=sign_jwt({'uid':2}),thirdToken=sign_jwt({'uid':3}))))
(directory/'workspace_state.json').write_text(json.dumps(dict(updatedBy=1,manualAnnotations=[],deletedTrackingIds=[])))
(directory/'tracker_results.json').write_text('\n'.join(json.dumps(dict(source_frame_index=f['frameIndex'],objects=[dict(object_id=o['objectId'],bbox=o['bbox'],name='sperm') for o in f['objects']])) for f in frames))
print(s['id'])
