"""Optional user-video UI fixture. Copies inputs only into isolated work/ data."""
import argparse,json,shutil,uuid
from pathlib import Path
from app import db,review_workflow as review,confirmation_workflow as confirmation
from app.config import DB_FILE,TRACK_DATA_DIR
from app.auth import sign_jwt
from app.review_schema import apply_review_schema
from app.review_repository import compute_file_sha256,upsert_media_revision,freeze_baseline,create_review_session
from app.tracker import _probe_video

p=argparse.ArgumentParser();p.add_argument('--source-dir',type=Path,required=True);args=p.parse_args()
assert '/work/e2e-confirm-ux-' in str(DB_FILE) and '/work/e2e-confirm-ux-' in str(TRACK_DATA_DIR)
db.init_db()
with db.connect() as c:apply_review_schema(c);review.migrate(c);confirmation.migrate(c)
assert db.get_user_by_id(3),'Run confirmation_browser_fixture first'
assets=[]
for original in sorted(args.source_dir.glob('*.mp4')):
    mid='ux-real-'+original.stem;directory=TRACK_DATA_DIR/mid;directory.mkdir(parents=True,exist_ok=True)
    video=directory/original.name
    if not video.exists():shutil.copy2(original,video)
    meta=_probe_video(video);assert meta['width']>=3840
    (directory/'media.json').write_text(json.dumps({'videoName':video.name,**meta}))
    boxes=[[meta['width']*.35,meta['height']*.4,meta['width']*.36,meta['height']*.41],
           [meta['width']*.5,meta['height']*.4,meta['width']*.51,meta['height']*.41],
           [meta['width']*.6,meta['height']*.5,meta['width']*.61,meta['height']*.51]]
    rows=[{'frame_index':fi,'source_frame_index':fi,'objects':[{'object_id':42+j,'name':f'sperm {42+j}','bbox':box,'source':'manual_sam3_tracker'} for j,box in enumerate(boxes)]} for fi in range(meta['frameCount'])]
    (directory/'tracker_results.json').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    manual=[dict(id='manual-0-42',objectId=42,source='manual',frameIndex=0,name='sperm 42',bbox=dict(x=35,y=40,width=1,height=1))]
    (directory/'workspace_state.json').write_text(json.dumps(dict(revision=0,updatedBy=3,currentFrame=0,manualAnnotations=manual,manualBaselines=[],deletedObjectIds=[],deletedFrameObjects=[],deletedTrackingIds=[],anomalyFrames=[],pausedAnomalies=[])))
    assets.append(dict(mid=mid,name=original.name,**meta))
small=min(assets,key=lambda x:x['frameCount']);video=TRACK_DATA_DIR/small['mid']/small['name']
revision=upsert_media_revision(small['mid'],compute_file_sha256(video),video.stat().st_size,small['width'],small['height'],small['fps'],small['frameCount'])
frames=[dict(frameIndex=fi,coverage='objects' if fi<2 else 'empty',objects=[dict(objectId=oid,bbox=[100+oid*200,200,160+oid*200,240],classKey='sperm') for oid in range(1,4)] if fi<2 else []) for fi in range(small['frameCount'])]
a=freeze_baseline(revision,small['mid'],3,frames);sid=create_review_session(a['id'])['id']
review.write('claim',sid,3,str(uuid.uuid4()),{})
for fi in range(small['frameCount']):
    patch=[dict(objectId=oid,bbox=[105+oid*200,200,165+oid*200,240]) for oid in range(1,4)] if fi<2 else []
    review.write('submit',sid,3,str(uuid.uuid4()),dict(expectedFrameRevision=0,patch=patch),fi)
cid=review.write('finish',sid,3,str(uuid.uuid4()),dict(expectedSessionRevision=review.get_session(sid,3)['revision']))['confirmationSessionId']
confirmation.write('claim',cid,3,str(uuid.uuid4()),{},response_mode='delta')
Path('work/real-4k-browser-fixture.json').write_text(json.dumps(dict(assets=assets,confirmation=cid,token=sign_jwt({'uid':3}))))
print('Created isolated copies and simulated labels for real-video UI validation; no GPU inference.')
