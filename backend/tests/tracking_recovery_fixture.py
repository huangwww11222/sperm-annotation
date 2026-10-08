"""Disposable genuine 4K source and annotation inputs for recovery UI tests."""
import json
from pathlib import Path
import cv2
import numpy as np
from app.auth import sign_jwt
from app.config import DB_FILE, TRACK_DATA_DIR

assert '/work/e2e-confirm-ux' in str(DB_FILE)
assert '/work/e2e-confirm-ux' in str(TRACK_DATA_DIR)
mid='ux-4k-recovery'
directory=TRACK_DATA_DIR/mid
directory.mkdir(parents=True,exist_ok=True)
video=directory/'4k-recovery.avi'
writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),25,(3840,2160))
assert writer.isOpened()
for fi in range(8):
    frame=np.full((2160,3840,3),40+fi*5,dtype=np.uint8)
    cv2.rectangle(frame,(1500+fi,1000),(1560+fi,1030),(210,210,210),-1)
    writer.write(frame)
writer.release()
(directory/'media.json').write_text(json.dumps({'videoName':video.name,'width':3840,'height':2160,'fps':25,'frameCount':8}))
(directory/'workspace_state.json').write_text(json.dumps({'revision':0,'updatedBy':3,'currentFrame':0,'manualBaselines':[],
    'manualAnnotations':[{'id':'manual-0-1','objectId':1,'source':'manual','frameIndex':0,'name':'sperm 1','bbox':{'x':1500/3840*100,'y':1000/2160*100,'width':60/3840*100,'height':30/2160*100}}],
    'deletedObjectIds':[],'deletedFrameObjects':[],'deletedTrackingIds':[],'anomalyFrames':[],'pausedAnomalies':[]}))
(directory/'tracker_results.json').write_text('\n'.join(json.dumps({'frame_index':fi,'source_frame_index':fi,'objects':[{'object_id':1,'bbox':[1500+fi,1000,1560+fi,1030],'source':'manual_sam3_tracker','name':'sperm 1'}]}) for fi in range(8))+'\n')
Path('work/tracking-recovery-fixture.json').write_text(json.dumps({'mid':mid,'directory':str(directory.resolve()),'token':sign_jwt({'uid':3})}))
print('Created isolated genuine 3840x2160 video and recovery inputs.')
