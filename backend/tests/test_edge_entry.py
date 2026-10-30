"""Motion gating and SAM3 handoff; synthetic observations, not model accuracy."""
from types import SimpleNamespace

import pytest

from app.edge_entry import EntryGate, EntryPropagation


def moving(gate, *, x=0, shift=(0,0), occupied=(), score=.9, begin=1):
    accepted=[]
    for f in range(begin,begin+6):
        left=x+(f-begin)*2
        accepted += gate.observe(f, [[left,40,left+10,50,score]], occupied, shift)
    return accepted


def test_inward_entry_accepted_once_and_initial_targets_excluded():
    gate=EntryGate(200,100,20)
    gate.observe(0, [], [])
    result=moving(gate)
    assert len(result)==1 and result[0]['samples'][0]['frame']==1
    assert not moving(gate,x=12,begin=7)
    initial=EntryGate(200,100,20)
    assert not moving(initial)


@pytest.mark.parametrize('kind', ['interior','static','camera','weak','existing','outward','ambiguous','bad_motion'])
def test_reject_non_entries(kind):
    gate=EntryGate(200,100,20);gate.observe(0,[],[])
    result=[]
    for f in range(1,8):
        x=30+f*2 if kind=='interior' else (12-f*2 if kind=='outward' else (0 if kind=='static' else (f-1)*2))
        rows=[[x,40,x+10,50,.55 if kind=='weak' else .9]]
        if kind=='ambiguous' and f>1: rows.append([x+11,40,x+21,50,.9])
        result += gate.observe(f,rows, [[x,40,x+10,50]] if kind=='existing' else [],
                               (2,0) if kind=='camera' else (0,0), kind!='bad_motion')
    assert not result


def test_resume_preserves_pending_and_blocked_candidates():
    gate=EntryGate(200,100,20);gate.observe(0,[],[])
    gate.observe(1,[[0,40,10,50,.9]],[])
    gate.observe(2,[[2,40,12,50,.9]],[])
    restored=EntryGate(200,100,20,gate.snapshot())
    assert len(moving(restored,x=4,begin=3))==1


def test_malformed_and_missing_observations_never_invent_entry():
    gate=EntryGate(200,100,20);gate.observe(0,[],[])
    assert not gate.observe(1,[[0,40,float('nan'),50,.9]],[])
    gate.observe(2,[[0,40,10,50,.9]],[])
    gate.observe(3,[],[])
    assert not moving(gate,x=10,begin=4)  # starts inside, not at edge


def test_handoff_encodes_birth_and_preserves_existing_session():
    calls=[];session=object()
    class Engine:
        def propagate_manual(self,s,max_frames,start_frame_idx):
            assert s is session
            try:
                for f in range(start_frame_idx,5):
                    calls.append(('infer',f));yield SimpleNamespace(frame_idx=f)
            finally: calls.append(('closed',start_frame_idx))
        def add_manual_boxes(self,s,frame,objects):
            assert s is session
            calls.append(('add',frame,objects))
    stream=EntryPropagation(Engine(),session,5)
    assert next(stream).frame_idx==0
    assert next(stream).frame_idx==1
    stream.add(1,[{'object_id':9,'bbox':[0,0,8,8]}])
    assert next(stream).frame_idx==2
    assert calls[2:5]==[('closed',0),('add',1,[{'object_id':9,'bbox':[0,0,8,8]}]),('infer',1)]
    assert [o.frame_idx for o in stream]==[3,4]
    stream.close()


def test_real_publication_backfill_and_sam_handoff(tmp_path, monkeypatch, caplog):
    import json
    import cv2
    import numpy as np
    from app import tracker, head_click
    from .test_tracking_adaptive_feedback import FakeWindow
    caplog.set_level('INFO',logger='review.tracking')
    video=tmp_path/'video.avi'
    writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),20,(200,100))
    image=np.random.default_rng(1).integers(0,255,(100,200,3),dtype=np.uint8)
    for _ in range(10):writer.write(image)
    writer.release()
    seed=tmp_path/'seed_frame_000000.json'
    seed.write_text(json.dumps({'frame':{'frameIndex':0},'annotations':[{'object_id':7,'bbox':[100,40,110,50],'source':'manual'}]}))
    (tmp_path/'workspace_state.json').write_text(json.dumps({'deletedObjectIds':[90]}))
    meta={'width':200,'height':100,'fps':20,'frameCount':10,'source_frame_indices':list(range(10))}
    calls=[]
    class Engine:
        model_id='fake';device='cpu';torch_dtype='float32'
        def make_tracker_session(self,frames):return {}
        def add_manual_boxes(self,session,frame,objects):
            calls.append((frame,objects))
            session.update({o['object_id']:o['bbox'] for o in objects})
        def propagate_manual(self,session,max_frames,start_frame_idx):
            for f in range(start_frame_idx,10):yield SimpleNamespace(frame_idx=f)
        def decode_tracker_output(self,session,output):
            return [SimpleNamespace(object_id=oid,bbox=box,score=.9,mask_area=None,sam3_object_id=oid) for oid,box in session.items()],None
    monkeypatch.setattr(tracker,'get_sam3_engine',lambda *a:Engine())
    monkeypatch.setattr(tracker,'SourceVideoWindow',lambda *a,**kw:FakeWindow(meta))
    count=[0]
    def detect(image):
        f=count[0];count[0]+=1
        return [] if f==0 else [[(f-1)*2,40,(f-1)*2+10,50,.95]]
    monkeypatch.setattr(head_click,'detect_edges',detect)
    result=tracker.track_video(str(video),str(seed),str(tmp_path/'tracker_results.json'),10,edge_discovery=True)
    assert result['anomaly_paused'] is None
    assert len(calls)==2 and calls[1][0]==4
    assert calls[1][1][0]['object_id']==91  # tombstone never reused
    assert [o['object_id'] for o in result['frames'][0]['objects']]==[7]
    assert all({o['object_id'] for o in row['objects']}=={7,91} for row in result['frames'][1:])
    for row in result['frames'][1:5]:
        o=next(o for o in row['objects'] if o['object_id']==91)
        assert o['source']=='edge_detector' and not o['entryOrigin']['humanConfirmed']
    assert 'tracking.edge_entry_added' in caplog.text
    saved=(tmp_path/'tracker_results.json').read_bytes()
    monkeypatch.setattr(head_click,'detect_edges',lambda image: (_ for _ in ()).throw(RuntimeError('detector failed')))
    with pytest.raises(RuntimeError,match='detector failed'):
        tracker.track_video(str(video),str(seed),str(tmp_path/'tracker_results.json'),10,edge_discovery=True)
    assert (tmp_path/'tracker_results.json').read_bytes()==saved


@pytest.mark.parametrize("pause_before_confirmation", [False, True])
def test_pending_entry_across_rounds_backfills_preserving_old_frames(tmp_path, monkeypatch, pause_before_confirmation):
    import json
    from app import tracker, edge_entry
    from .test_tracking_adaptive_feedback import FakeWindow
    seed=tmp_path/'seed_frame_000003.json'
    seed.write_text(json.dumps({'frame':{'frameIndex':3},'annotations':[{'object_id':7,'bbox':[100,40,110,50],'source':'manual'}]}))
    old=[{'frame_index':f,'source_frame_index':f,'objects':[{'object_id':7,'bbox':[100,40,110,50],'name':'old','score':.9,'source':'manual_sam3_tracker'}], 'edgeDiscovery':{'version':1,'tracks':[]}} for f in range(4)]
    if pause_before_confirmation:
        old[3]['objects'][0].update(anomaly_reasons=['adjacent_center_shift=100 HARD'], anomaly_details={'current_bbox':[100,40,110,50]})
    output=tmp_path/'tracker_results.json';output.write_text('\n'.join(json.dumps(r) for r in old)+'\n')
    meta={'width':200,'height':100,'fps':20,'frameCount':7,'source_frame_indices':[3,4,5,6]}
    class Reader:
        def __init__(self,*a): self.gate=self;self.closed=False
        def prime_frame(self,frame):assert frame==3
        def snapshot(self):return {'version':1,'tracks':[]}
        def close(self):self.closed=True
        def observe(self,frame,occupied,allow_new):
            return [{'samples':[{'frame':f,'box':[f*2,10,f*2+10,20],'score':.95} for f in range(1,5)]}] if frame==4 else []
    class Engine:
        model_id='fake';device='cpu';torch_dtype='float32'
        def make_tracker_session(self,frames):return {}
        def add_manual_boxes(self,session,frame,objects):session.update({o['object_id']:o['bbox'] for o in objects})
        def propagate_manual(self,session,max_frames,start_frame_idx):
            for f in range(start_frame_idx,4):yield SimpleNamespace(frame_idx=f)
        def decode_tracker_output(self,session,out):
            return [SimpleNamespace(object_id=oid,bbox=box,score=.9,mask_area=None,sam3_object_id=oid) for oid,box in session.items()],None
    monkeypatch.setattr(tracker,'_probe_video',lambda _:meta)
    monkeypatch.setattr(tracker,'SourceVideoWindow',lambda *a,**kw:FakeWindow(meta))
    monkeypatch.setattr(tracker,'get_sam3_engine',lambda *a:Engine())
    monkeypatch.setattr(edge_entry,'EdgeReader',Reader)
    if pause_before_confirmation:
        from app.services.anomaly_detector import AnomalyFrame, AnomalyReport, AnomalyLevel
        original_push=tracker.AnomalyDetector.push
        def push(self,frame,boxes,**kwargs):
            report=original_push(self,frame,boxes,**kwargs)
            if frame==4:
                af=AnomalyFrame(frame,7,AnomalyLevel.ANOMALY,['adjacent_center_shift=100 HARD'],{'pause_started_frame':3,'current_bbox':boxes[7]})
                return AnomalyReport(frame,{7:AnomalyLevel.ANOMALY},[af],True,3)
            return report
        monkeypatch.setattr(tracker.AnomalyDetector,'push',push)
    result=tracker.track_video(str(tmp_path/'video.avi'),str(seed),str(output),4,edge_discovery=True)
    if pause_before_confirmation:
        assert result['lastProcessedFrame']==3
        assert all({o['object_id'] for o in row['objects']}=={7} for row in result['frames'])
        return
    assert result['anomaly_paused'] is None
    rows=result['frames']
    assert rows[0]==old[0]
    assert [r['source_frame_index'] for r in rows]==list(range(7))
    assert all({o['object_id'] for o in r['objects']}=={7,8} for r in rows[1:])
    assert rows[1]['objects'][0]==old[1]['objects'][0]


def test_initial_border_target_cannot_rebirth_after_detector_dropout():
    gate=EntryGate(200,100,20)
    gate.observe(0,[[0,40,10,50,.9]],[])
    gate.observe(1,[],[])
    assert not moving(gate,x=2,begin=2)


def test_nonconsecutive_frames_cannot_count_as_continuous_motion():
    gate=EntryGate(200,100,20);gate.observe(0,[],[])
    gate.observe(1,[[0,40,10,50,.9]],[])
    assert not moving(gate,x=2,begin=5)
