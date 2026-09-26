"""Disposable videos for annotation UI testing; never run against business storage."""
import json
from pathlib import Path
import cv2
import numpy as np
from app.config import DB_FILE, TRACK_DATA_DIR
assert '/work/e2e-confirm-ux' in str(DB_FILE), DB_FILE
for mid, w, h in [('ux-video',800,450), ('ux-portrait',450,800)]:
    directory = TRACK_DATA_DIR / mid
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / (mid+'.avi')
    writer = cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),15,(w,h))
    rows=[]
    for fi in range(60):
        frame=np.full((h,w,3),35+fi*2,dtype=np.uint8)
        for x,y in [(120+fi,130),(270+fi,280)]:
            cv2.ellipse(frame,(x,y),(20,12),20,0,360,(220,230,240),-1)
        cv2.putText(frame,f'Frame {fi+1}',(15,35),cv2.FONT_HERSHEY_SIMPLEX,0.7,(240,240,240),1)
        writer.write(frame)
        rows.append({'frame_index':fi,'timestamp_ms':round(fi/15*1000),'objects':[] if fi==59 else [
            {'object_id':j+1,'name':'sperm','bbox':[x-22,y-16,x+22,y+16],'score':.96}
            for j,(x,y) in enumerate([(120+fi,130),(270+fi,280)])]})
    writer.release()
    (directory/'media.json').write_text(json.dumps({'videoName':video.name,'width':w,'height':h,'fps':15,'frameCount':60}))
    (directory/'tracker_results.json').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    (directory/'workspace_state.json').unlink(missing_ok=True)
Path('work/ux-fixture-ready.txt').write_text('ux-video and ux-portrait: 60 real frames each')
