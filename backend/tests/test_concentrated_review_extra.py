import json
import pytest
from app import db, review_workflow as w, confirmation_workflow as cf, training_export
from .test_review_workflow import task, edit, done
from .test_concentrated_review import enable


def complete(sid):
    enable(sid)
    for fi in range(3): edit(sid,'submit',fi=fi)
    return done(sid)['session']


def test_reopen_invalidates_old_final_and_no_implicit_resubmit(task):
    accepted=complete(task); old=accepted['finalVersionId']
    payload=dict(expectedSessionRevision=accepted['revision'],frameIndex=1)
    reopened=w.write('reopen_review',task,2,'reopen-key',payload)
    assert reopened==w.write('reopen_review',task,2,'reopen-key',payload)
    assert reopened['session']['finalVersionId'] is None
    assert edit(task,'discard',fi=1)['frame']['state']=='unreviewed'
    with db.connect() as c, pytest.raises(w.ReviewError): training_export.eligible(c,[old])
    edit(task,'submit',fi=1)
    new=done(task)['session']['finalVersionId']
    assert new!=old
    with db.connect() as c:
        with pytest.raises(w.ReviewError): training_export.eligible(c,[old])
        assert training_export.eligible(c,[new])[0]['final']['id']==new
        assert c.execute('SELECT COUNT(*) FROM final_versions').fetchone()[0]==2


def test_finalization_failure_rolls_back_then_original_request_recovers(task):
    enable(task)
    for fi in range(3):edit(task,'submit',fi=fi)
    body=dict(expectedSessionRevision=w.get_session(task,2)['revision'])
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_final BEFORE INSERT ON final_versions BEGIN SELECT RAISE(ABORT,'injected final failure'); END")
    with pytest.raises(w.ReviewError):w.write('finish',task,2,'finish-retry',body)
    assert w.get_session(task,2)['state']=='in_progress'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM review_versions').fetchone()[0]==0
        c.execute('DROP TRIGGER fail_final')
    assert w.write('finish',task,2,'finish-retry',body)['session']['finalVersionId']


def test_legacy_c_cannot_change_review_accepted_version(task):
    s=complete(task)
    with db.connect() as c:
        cid=c.execute('SELECT confirmation_id FROM final_versions WHERE id=?',(s['finalVersionId'],)).fetchone()[0]
    with pytest.raises(w.ReviewError) as error:
        cf.write('reopen',cid,2,'illegal-c',{'expectedSessionRevision':cf.get_session(cid,2)['revision']})
    assert error.value.code=='REVIEW_ACCEPTANCE_ONLY'


def test_evidence_failure_and_unknown_are_not_checked(task,monkeypatch):
    from app import review_evidence as e
    class Reader:
        def read(self):raise OSError('disk failure')
        def release(self):pass
    monkeypatch.setattr(e,'open_capture_at',lambda *args:Reader())
    checker=e.ReviewEvidence('fake')
    result=checker.observe(5,{1:[0,0,2,2]})
    assert result['state']=='failed' and result['findings'][0]['kind']=='check_failed'
    assert checker.observe(6,{})['state']=='not_sampled'
    assert checker.observe(10,{})['state']=='failed'


def test_shared_candidate_is_only_suspicion():
    from app.review_evidence import compare_heads
    evidence=compare_heads({1:[0,0,10,10],2:[0,0,10,10]},[[0,0,10,10,.9]])
    assert len([e for e in evidence if e['kind']=='association_uncertain'])==2
    assert all('pause' not in e for e in evidence)


def test_tracking_quality_warning_collects_without_pause(tmp_path,monkeypatch,caplog):
    from app import tracker, review_evidence
    from .test_tracking_adaptive_feedback import _run
    real = tracker.track_video
    from . import test_tracking_adaptive_feedback as fixture
    monkeypatch.setattr(fixture,'track_video',lambda *a,**kw:real(*a,**kw,review_mode=True))
    monkeypatch.setattr(review_evidence.ReviewEvidence,'observe',lambda *a,**kw:dict(state='not_sampled',findings=[],backwardCheck='not_run'))
    monkeypatch.setattr(review_evidence,'backward_checks',lambda *a:None)
    result,_=_run(tmp_path,monkeypatch)
    assert result['lastProcessedFrame']==6
    assert result['anomaly_paused'] is None
    assert all('reviewEvidence' in r for r in result['frames'][1:])


def test_background_task_continues_bounded_windows_without_manual_seeds(tmp_path,monkeypatch):
    from app import main
    from app.schemas import TrackRequest
    calls=[]
    def track(*args,**kw):
        start=kw['start_frame'];calls.append(kw)
        end=start+2
        return dict(frames=[{'source_frame_index':end,'objects':[{'object_id':1,'bbox':[1,2,3,4],'source':'manual_sam3_tracker'}]}],lastProcessedFrame=end,reachedVideoEnd=end==4,anomaly_paused=None)
    monkeypatch.setattr(main,'track_video',track)
    monkeypatch.setattr(main.annotation_state,'read_state',lambda *a:{})
    monkeypatch.setattr(main.annotation_state,'recover_pause_publication',lambda *a:None)
    request=TrackRequest(mediaId='isolated',startFrame=0,maxFrames=3,reviewMode=True,annotations=[])
    main._run_tracking_task('review-test',request,tmp_path/'video.avi',tmp_path/'seed.json',tmp_path/'result.json')
    assert [c['start_frame'] for c in calls]==[0,2]
    assert calls[1]['continuation_row']['objects'][0]['source']=='manual_sam3_tracker'
    assert main.TASKS.pop('review-test')['status']=='success'
    assert not (tmp_path/'seed.json').exists()


def test_resubmitted_reopen_frame_has_normal_discard_semantics(task):
    accepted=complete(task)
    w.write('reopen_review',task,2,'reopen-once',dict(expectedSessionRevision=accepted['revision'],frameIndex=1))
    edit(task,'submit',fi=1)
    edit(task,'draft',[{'objectId':1,'bbox':[11,20,31,40]}],fi=1)
    assert edit(task,'discard',fi=1)['frame']['state']=='submitted'


@pytest.mark.parametrize('candidate', [[0,0,float('nan'),4,.9],[0,0,2,4,float('nan')],[2,0,1,4,.9]])
def test_invalid_model_observation_is_not_a_successful_comparison(candidate):
    from app.review_evidence import compare_heads
    with pytest.raises(ValueError):compare_heads({1:[0,0,2,4]},[candidate])


@pytest.mark.parametrize('mode',['complete','partial','invalid','disagreement','failed'])
def test_backward_evidence_requires_complete_valid_output(monkeypatch,mode):
    from types import SimpleNamespace
    from app.review_evidence import backward_checks
    from app.services import sam3_engine
    class Window:
        def __init__(self,*a,**kw):pass
        def __enter__(self):return [0,1,2,3]
        def __exit__(self,*a):pass
    class Engine:
        def make_tracker_session(self,images):return images
        def add_manual_boxes(self,*a):pass
        def propagate_manual(self,*a,**kw):
            if mode=='failed':raise RuntimeError('model error')
            for i in range(2 if mode=='partial' else 4):yield SimpleNamespace(frame_idx=i)
        def decode_tracker_output(self,s,o):
            b=[0,0,2,2] if mode!='disagreement' else [8,8,10,10]
            if mode=='invalid':b[2]=float('nan')
            return [SimpleNamespace(object_id=1,bbox=b)],None
    monkeypatch.setattr(sam3_engine,'SourceVideoWindow',Window)
    rows=[dict(source_frame_index=i,objects=[dict(object_id=1,bbox=[0,0,2,2])],reviewEvidence=dict(state='not_sampled',findings=[],backwardCheck='not_run')) for i in range(4)]
    rows[-1]['reviewEvidence']['findings']=[dict(objectId=1,kind='detector_mismatch',message='test')]
    backward_checks('isolated',Engine(),rows)
    expected='checked' if mode in ('complete','disagreement') else 'failed'
    assert rows[-1]['reviewEvidence']['backwardCheck']==expected
    if mode=='disagreement':assert any(f['kind']=='backward_disagreement' for r in rows for f in r['reviewEvidence']['findings'])
