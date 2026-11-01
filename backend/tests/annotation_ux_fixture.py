"""Disposable videos for annotation UI testing; never run against business storage."""
import json
from pathlib import Path
import cv2
import numpy as np
from app.config import DB_FILE, TRACK_DATA_DIR
assert '/work/e2e-confirm-ux' in DB_FILE.as_posix(), DB_FILE
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
    # A reverse-only SAM3 path for an uncovered head, plus explicit exit evidence
    # for the two tracked objects before the empty final frame. Review consumes
    # these saved observations without running a model.
    for fi in range(20,25):
        rows[fi]['reviewEvidence']={'findings':[{'kind':'endpoint_identity_uncertain','objectId':None,
                                                'tailObjectId':9,'bbox':[650,350,690,390]}]}
    rows[59]['reviewEvidence']={'findings':[],'boundaryExit':{'exitedObjectIds':[1,2]},
                                'endpointCheck':{'state':'checked','matches':{},'unmatchedTailIds':['9'],
                                                 'unmatchedAliases':{},'observed':True,'anchorFrame':59}}
    (directory/'media.json').write_text(json.dumps({'videoName':video.name,'width':w,'height':h,'fps':15,'frameCount':60}))
    (directory/'tracker_results.json').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    (directory/'workspace_state.json').unlink(missing_ok=True)
Path('work/ux-fixture-ready.txt').write_text('ux-video and ux-portrait: 60 real frames each')
# Review rounds belong to the media being reset; drop them so each suite starts unreviewed.
import sqlite3
with sqlite3.connect(DB_FILE) as conn:
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM annotation_reviews WHERE media_id IN ('ux-video','ux-portrait')")]
        conn.executemany('DELETE FROM annotation_review_checks WHERE review_id=?', [(i,) for i in ids])
        conn.execute("DELETE FROM annotation_reviews WHERE media_id IN ('ux-video','ux-portrait')")
    except sqlite3.OperationalError:
        pass  # Fresh database: review tables not migrated yet.
