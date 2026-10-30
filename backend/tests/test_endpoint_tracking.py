"""Independent endpoint anchors: no array/ID identity assumptions or silent pass."""
from types import SimpleNamespace
import copy
import pytest


def rows(n=6):
    return [dict(source_frame_index=i,frame_index=i,objects=[dict(object_id=1,bbox=[i,0,i+10,10])],reviewEvidence=dict(state='not_sampled',findings=[],backwardCheck='not_run')) for i in range(n)]


class Engine:
    def __init__(self, fail=False, partial=False):self.fail=fail;self.partial=partial;self.calls=[]
    def make_tracker_session(self,images):return SimpleNamespace(images=list(images),objects=[])
    def add_manual_boxes(self,s,frame,objects):s.objects=copy.deepcopy(objects);self.calls.append(copy.deepcopy(objects))
    def propagate_manual(self,s,max_frames,start_frame_idx):
        if self.fail:raise RuntimeError('injected GPU failure')
        for i in range(len(s.images)-(1 if self.partial else 0)):yield SimpleNamespace(frame_idx=i)
    def decode_tracker_output(self,s,o):
        return [SimpleNamespace(object_id=v['object_id'],bbox=[s.images[o.frame_idx],0,s.images[o.frame_idx]+10,10]) for v in s.objects],None


@pytest.fixture
def window(monkeypatch):
    from app.services import sam3_engine
    class Window:
        def __init__(self,video,start_frame,max_frames):self.frames=list(range(start_frame,start_frame+max_frames))
        def __enter__(self):return self.frames
        def __exit__(self,*a):pass
    monkeypatch.setattr(sam3_engine,'SourceVideoWindow',Window)


def test_independent_tail_ids_match_by_trajectory_and_preserve_labels(window):
    from app.endpoint_tracking import verify_endpoints
    original=rows();before=copy.deepcopy(original);engine=Engine()
    report=verify_endpoints('fake',engine,original,[dict(object_id=91,bbox=[5,0,15,10],source='manual')],window_size=3)
    assert report['state']=='checked' and report['matches']=={'91':1}
    assert len(engine.calls)==3  # bounded windows, never load entire video
    assert all(a['objects']==b['objects'] for a,b in zip(original,before))
    assert all(r['reviewEvidence']['endpointCheck']['state']=='checked' for r in original)
    assert all(not r['reviewEvidence']['findings'] for r in original)


@pytest.mark.parametrize('partial',[False,True])
def test_failed_reverse_is_explicit_and_does_not_damage_forward(window,partial):
    from app.endpoint_tracking import verify_endpoints
    value=rows();before=copy.deepcopy(value)
    report=verify_endpoints('fake',Engine(fail=not partial,partial=partial),value,[dict(object_id=99,bbox=[5,0,15,10])],window_size=3)
    assert report['state']=='failed'
    assert [r['objects'] for r in value]==[r['objects'] for r in before]
    assert all(r['reviewEvidence']['endpointCheck']['state']=='failed' for r in value)
    assert any(f['kind']=='check_failed' for f in value[0]['reviewEvidence']['findings'])


def test_ambiguous_trajectories_are_not_assigned_by_id_or_array_order(window):
    from app.endpoint_tracking import verify_endpoints
    value=rows()
    for r in value:r['objects'].append(dict(object_id=2,bbox=r['objects'][0]['bbox']))
    report=verify_endpoints('fake',Engine(),value,[dict(object_id=1,bbox=[5,0,15,10])],window_size=3)
    assert report['matches']=={}
    assert any(f['kind']=='endpoint_identity_uncertain' for f in value[0]['reviewEvidence']['findings'])


def test_empty_human_tail_is_insufficient_not_pass(window):
    from app.endpoint_tracking import verify_endpoints
    value=rows();report=verify_endpoints('fake',Engine(),value,[],window_size=3)
    assert report['state']=='insufficient' and report['matches']=={}
    assert any(f['kind']=='endpoint_identity_uncertain' for f in value[-1]['reviewEvidence']['findings'])


def test_manual_endpoint_validation_rejects_ai_stale_and_duplicates():
    from app.endpoint_tracking import validate_endpoints
    state=dict(manualAnnotations=[dict(source='manual',objectId=1,frameIndex=0,bbox=dict(x=0,y=0,width=10,height=10)),dict(source='manual',objectId=8,frameIndex=5,bbox=dict(x=5,y=0,width=10,height=10))])
    head=[dict(source='manual',object_id=1,bbox=[0,0,10,10])];tail=[dict(source='manual',object_id=8,bbox=[5,0,15,10])]
    validate_endpoints(state,head,tail,100,100,6)
    for bad in [[dict(tail[0],source='ai')],[dict(tail[0],bbox=[6,0,16,10])],tail*2,[]]:
        with pytest.raises(ValueError):validate_endpoints(state,head,bad,100,100,6)


def test_terminal_mapping_freezes_single_manual_box_preserving_raw_ids(tmp_path):
    import json
    from app.annotation_completion import source
    value=rows(2);value[-1]['reviewEvidence']['endpointCheck']=dict(state='checked',matches={'91':1},terminalAnnotations=[dict(object_id=91,bbox=[1,0,11,10])])
    manual=[dict(source='manual',objectId=91,frameIndex=1,name='rare sperm 91',bbox=dict(x=1,y=0,width=10,height=10))]
    original=dict(updatedBy=1,manualAnnotations=manual)
    (tmp_path/'workspace_state.json').write_text(json.dumps(original))
    (tmp_path/'tracker_results.json').write_text('\n'.join(json.dumps(r) for r in value))
    info=dict(width=100,height=100,frameCount=2)
    _,frames=source(tmp_path,1,info)
    assert list(frames[1])==[1] and frames[1][1]['bbox']==[1,0,11,10]
    assert json.loads((tmp_path/'workspace_state.json').read_text())==original
    original['deletedFrameObjects']=[dict(frameIndex=1,objectId=91)]
    original['manualAnnotations']=[]
    (tmp_path/'workspace_state.json').write_text(json.dumps(original))
    assert source(tmp_path,1,info)[1][1]=={}


def test_endpoint_disagreement_is_reported_without_replacing_good_forward(window):
    from app.endpoint_tracking import verify_endpoints
    value=rows()
    value[2]['objects'][0]['bbox']=[40,0,50,10]
    verify_endpoints('fake',Engine(),value,[dict(object_id=91,bbox=[5,0,15,10])],window_size=3)
    assert value[2]['objects'][0]['bbox']==[40,0,50,10]
    assert value[2]['reviewEvidence']['findings'][0]['kind']=='endpoint_disagreement'
    assert not value[1]['reviewEvidence']['findings']


def test_complete_reverse_failure_is_frozen_with_review_groups(window):
    from app.endpoint_tracking import verify_endpoints
    from app.concentrated_review import group_findings
    value=rows()
    verify_endpoints('fake',Engine(fail=True),value,[dict(object_id=91,bbox=[5,0,15,10])])
    groups=group_findings(value)
    assert len(groups)==1 and groups[0]['kind']=='check_failed' and groups[0]['endFrame']==5


def test_enter_and_exit_tracks_stay_explicitly_unpaired(window):
    from app.endpoint_tracking import verify_endpoints
    value=rows()
    for r in value[:3]:r['objects'].append(dict(object_id=3,bbox=[80,80,90,90]))
    report=verify_endpoints('fake',Engine(),value,[dict(object_id=91,bbox=[5,0,15,10])],window_size=3)
    assert report['matches']=={'91':1}
    assert all(any(f.get('objectId')==3 and f['kind']=='endpoint_identity_uncertain' for f in r['reviewEvidence']['findings']) for r in value[:3])
    assert all(not r['reviewEvidence']['findings'] for r in value[3:])


def test_two_tail_tracks_cannot_both_claim_one_forward_identity(window):
    from app.endpoint_tracking import verify_endpoints
    value=rows()
    report=verify_endpoints('fake',Engine(),value,[dict(object_id=91,bbox=[5,0,15,10]),dict(object_id=92,bbox=[5,0,15,10])],window_size=3)
    assert report['matches']=={} and set(report['unmatchedTailIds'])=={91,92}


def test_broken_forward_coverage_never_looks_checked(window):
    from app.endpoint_tracking import verify_endpoints
    value=rows();del value[2]
    assert verify_endpoints('fake',Engine(),value,[dict(object_id=91,bbox=[5,0,15,10])])['state']=='failed'


def test_deleted_forward_identity_cannot_be_resurrected_by_manual_tail_alias(tmp_path):
    import json
    from app.annotation_completion import source
    value=rows(2);value[-1]['reviewEvidence']['endpointCheck']=dict(state='checked',matches={'91':1},terminalAnnotations=[])
    state=dict(updatedBy=1,deletedObjectIds=[1],manualAnnotations=[dict(source='manual',objectId=91,frameIndex=1,bbox=dict(x=1,y=0,width=10,height=10))])
    (tmp_path/'workspace_state.json').write_text(json.dumps(state))
    (tmp_path/'tracker_results.json').write_text('\n'.join(json.dumps(r) for r in value))
    assert source(tmp_path,1,dict(width=100,height=100,frameCount=2))[1][1]=={}


from .test_review_workflow import task


def test_frozen_review_retains_original_tail_snapshot(task):
    from app import db,confirmation_workflow as cf
    from app.concentrated_review import register,evidence
    value=rows(3)
    for r in value:r['reviewEvidence']['endpointCheck']=dict(state='checked',matches={'91':1},anchorHash='a'*64)
    value[-1]['reviewEvidence']['endpointCheck']['terminalAnnotations']=[dict(object_id=91,bbox=[2,0,12,10],source='manual')]
    with db.connect() as c:
        cf.migrate(c);register(c,task,value,'source-hash');c.commit()
        frozen=evidence(c,dict(id=task))
        value[-1]['reviewEvidence']['endpointCheck']['terminalAnnotations'][0]['bbox']=[80,80,90,90]
        assert frozen['endpointVerification']['terminalAnnotations'][0]['bbox']==[2,0,12,10]
        assert evidence(c,dict(id=task))['endpointVerification']['terminalAnnotations'][0]['object_id']==91
