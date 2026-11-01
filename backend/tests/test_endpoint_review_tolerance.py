"""Geometry tolerance remains available to association; identity evidence stays off the checklist."""
import copy
import json
import pytest
from app import annotation_review as review, db
from .test_review_completion import source, INFO
from .test_review_workflow import task


def finding(a=None, b=None, **extra):
    return dict(kind='endpoint_disagreement',objectId=1,message='人工首尾独立追踪位置不一致',
                bbox=a or [51,503,82,539],reverseBox=b or [45,501,92,559],**extra)


def test_small_contained_head_boxes_do_not_create_review_items():
    rows=[dict(source_frame_index=0,objects=[],reviewEvidence=dict(findings=[finding()]))]
    before=copy.deepcopy(rows)
    assert review.doubt_items(rows)==[]
    assert rows==before # Raw evidence and geometry remain available for identity recovery.


@pytest.mark.parametrize('a,b',[
    ([10,10,20,20],[30,10,40,20]), # another head / real displacement
    ([10,10,20,20],[12,10,32,30]), # near centre alone cannot excuse low containment
    ([10,10,40,40],[23,23,27,27]), # severe collapse
    ([10,10,20,20],[0,0,40,40]),  # severe expansion
])
def test_large_differences_remain_geometrically_distinct_but_not_identity_tasks(a,b):
    rows=[dict(source_frame_index=0,reviewEvidence=dict(findings=[finding(a,b)]))]
    from app.endpoint_tracking import endpoint_disagreement_requires_review
    assert endpoint_disagreement_requires_review(finding(a,b))
    assert review.doubt_items(rows)==[]


def test_missing_reverse_and_identity_signals_remain_raw_evidence_only():
    f=finding();f.pop('reverseBox')
    rows=[dict(source_frame_index=0,reviewEvidence=dict(findings=[f,finding(),dict(kind='subject_identity',objectId=2,message='身份存疑',bbox=[10,10,20,20])]))]
    before=copy.deepcopy(rows)
    assert review.doubt_items(rows)==[]
    assert rows==before


def test_threshold_is_resolution_relative():
    from app.endpoint_tracking import endpoint_disagreement_requires_review
    f=finding()
    for multiplier in [1,2,4]:
        scaled={**f,'bbox':[v*multiplier for v in f['bbox']],'reverseBox':[v*multiplier for v in f['reverseBox']]}
        assert not endpoint_disagreement_requires_review(scaled)
    # A substantially shifted head must still be reviewed, even with similar size.
    assert endpoint_disagreement_requires_review(finding([0,0,30,30],[14,0,44,30]))


def test_open_old_checklist_refreshes_once_preserves_checks_and_requires_remaining_risk(source):
    rows=[json.loads(s) for s in (source/'tracker_results.json').read_text().splitlines()]
    rows[0]['reviewEvidence']=dict(findings=[finding([64,48,128,96],[60,44,132,100])])
    (source/'tracker_results.json').write_text('\n'.join(json.dumps(r) for r in rows))
    state=review.start('complete-media',source,INFO,1)
    rid=state['reviewId']
    coverage=next(i for i in state['items'] if i['kind']=='coverage')
    review.check('complete-media',rid,coverage['id'],'ok',1)
    # Old saved round: numerous object-level doubts, some already checked.
    old=dict(id='doubt-old-1',kind='doubt',findingKind='endpoint_disagreement',startFrame=0,endFrame=0,frame=0,objectId=1,message='旧首尾差异')
    with db.connect() as c:
        c.execute('UPDATE annotation_reviews SET items_json=? WHERE id=?',(json.dumps([old]+[i for i in state['items'] if i['kind']!='doubt']),rid))
        c.execute('INSERT INTO annotation_review_checks VALUES (?,?,?,?,?)',(rid,old['id'],'ok',2,'old-check'))
        if 'evidence_policy' in {r[1] for r in c.execute('PRAGMA table_info(annotation_reviews)')}:
            c.execute('UPDATE annotation_reviews SET evidence_policy=NULL WHERE id=?',(rid,))
    rows[0]['reviewEvidence']['findings'].append(dict(kind='detector_mismatch',objectId=2,message='真正位置风险'))
    (source/'tracker_results.json').write_text('\n'.join(json.dumps(r) for r in rows))
    before=(source/'tracker_results.json').read_bytes()
    updated=review.status('complete-media',source,INFO)['review']
    doubts=[i for i in updated['items'] if i['kind']=='doubt']
    assert len(doubts)==1 and doubts[0]['objectIds']==[2] and not doubts[0]['check']
    assert next(i for i in updated['items'] if i['id']==coverage['id'])['check']['result']=='ok'
    with db.connect() as c:
        saved=dict(review.latest(c,'complete-media'))
        assert json.loads(saved['checklist_history_json'])[0]['items'][0]['id']=='doubt-old-1'
        assert c.execute('SELECT result FROM annotation_review_checks WHERE review_id=? AND item_id=?',(rid,old['id'])).fetchone()[0]=='ok'
    assert review.status('complete-media',source,INFO)['review']==updated
    assert (source/'tracker_results.json').read_bytes()==before
    with pytest.raises(Exception,match='没有检查'):
        review.pass_review('complete-media',source,INFO,rid,1)


def test_finished_review_is_not_rewritten_by_tolerance_upgrade(source):
    state=review.start('complete-media',source,INFO,1)
    for item in state['items']:
        review.check('complete-media',state['reviewId'],item['id'],'empty' if item['kind']=='unknown' else 'ok',1)
    review.pass_review('complete-media',source,INFO,state['reviewId'],1)
    with db.connect() as c:
        c.execute('UPDATE annotation_reviews SET evidence_policy=NULL WHERE id=?',(state['reviewId'],))
        before=dict(review.latest(c,'complete-media'))
    assert review.status('complete-media',source,INFO)['status']=='passed'
    with db.connect() as c:assert dict(review.latest(c,'complete-media'))==before


def test_refresh_failure_does_not_discard_saved_items_and_recovers(source,monkeypatch):
    state=review.start('complete-media',source,INFO,1)
    with db.connect() as c:
        c.execute('UPDATE annotation_reviews SET evidence_policy=NULL WHERE id=?',(state['reviewId'],))
        before=dict(review.latest(c,'complete-media'))
    build=review.build_items
    def fail(*a,**kw):raise RuntimeError('injected checklist rebuild failure')
    monkeypatch.setattr(review,'build_items',fail)
    with pytest.raises(RuntimeError):review.status('complete-media',source,INFO)
    with db.connect() as c:assert dict(review.latest(c,'complete-media'))==before
    monkeypatch.setattr(review,'build_items',build)
    assert review.status('complete-media',source,INFO)['status']=='open'
