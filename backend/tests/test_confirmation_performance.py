"""Compact C responses retain audit/receipts, exact progress and old clients."""
import json
import uuid
import time
from app import confirmation_workflow as flow,db
from .test_confirmation_workflow import confirmation,claim,choose
from .test_review_workflow import task


def test_session_stats_do_not_materialize_geometry(confirmation,monkeypatch):
    def no_all_items(*a,**kw):raise AssertionError('session statistics decoded all changes')
    monkeypatch.setattr(flow,'changes',no_all_items)
    session=flow.get_session(confirmation,3)
    assert session['progress']['totalChanges']==2 and session['progress']['pending']==2
    assert session['resume']['firstPendingChangeId']


def test_delta_choice_and_undo_replay_exact_receipt_without_losing_other_decisions(confirmation):
    cid=confirmation;claim(cid);choose(cid,1,'B')
    first=flow.list_changes(cid,3)[0];key=str(uuid.uuid4());body={'choice':'A','expectedDecisionRevision':0}
    result=flow.write('decide',cid,3,key,body,first['changeId'],response_mode='delta')
    assert result['itemsScope']=='changed' and len(result['items'])==1
    assert result['items'][0]['decision']['choice']=='A'
    assert result['session']['progress']['keptA']==result['session']['progress']['adoptedB']==1
    assert result['nextPendingChangeId'] is None
    assert flow.write('decide',cid,3,key,body,first['changeId'],response_mode='delta')==result
    undo=flow.write('undo',cid,3,str(uuid.uuid4()),{'actionId':result['session']['undo']['actionId'],'expectedSessionRevision':result['session']['revision']},response_mode='delta')
    assert len(undo['items'])==1 and undo['items'][0]['decision'] is None
    assert undo['session']['progress']['adoptedB']==1
    assert flow.list_changes(cid,3)[1]['decision']['choice']=='B'
    with db.connect() as c:
        receipt=json.loads(c.execute('SELECT response_json FROM review_write_receipts WHERE request_key=?',(key,)).fetchone()[0])
    assert receipt==result


def test_delta_cursor_never_returns_all_geometry_and_legacy_receipt_can_replay(confirmation,monkeypatch):
    cid=confirmation;legacy=claim(cid);items=legacy['items']
    def no_all_items(*a,**kw):raise AssertionError('cursor decoded full list')
    monkeypatch.setattr(flow,'changes',no_all_items)
    result=flow.write('cursor',cid,3,str(uuid.uuid4()),{'expectedCursorRevision':0},items[-1]['changeId'],response_mode='delta')
    assert result['items']==[] and result['itemsScope']=='changed'
    assert result['session']['progress']['decided']==0
    assert result['nextPendingChangeId']==items[0]['changeId']


def test_delta_request_replays_pre_upgrade_full_receipt(confirmation):
    cid=confirmation;claim(cid);item=flow.list_changes(cid,3)[0];key=str(uuid.uuid4());body={'choice':'B','expectedDecisionRevision':0}
    legacy=flow.write('decide',cid,3,key,body,item['changeId'])
    assert len(legacy['items'])==2 and 'itemsScope' not in legacy
    assert flow.write('decide',cid,3,key,body,item['changeId'],response_mode='delta')==legacy


def test_five_thousand_changes_keep_cursor_receipt_small(task,caplog):
    from app import review_workflow as review
    from app.review_repository import upsert_media_revision,freeze_baseline,create_review_session
    with db.connect() as c:flow.migrate(c)
    media=upsert_media_revision('performance-video','b'*64,123,640,480,25,10)
    objects=[]
    for oid in range(1,501):
        x,y=20+(oid%25)*20,20+(oid//25)*20
        objects.append(dict(objectId=oid,bbox=[x,y,x+8,y+6],classKey='sperm'))
    frames=[dict(frameIndex=fi,coverage='objects',objects=objects) for fi in range(10)]
    a=freeze_baseline(media,'performance-video',1,frames)
    sid=create_review_session(a['id'])['id']
    review.write('claim',sid,2,str(uuid.uuid4()),{})
    for fi in range(10):
        patch=[dict(objectId=o['objectId'],bbox=[o['bbox'][0]+1,*o['bbox'][1:]]) for o in objects]
        review.write('submit',sid,2,str(uuid.uuid4()),{'expectedFrameRevision':0,'patch':patch},fi)
    cid=review.write('finish',sid,2,str(uuid.uuid4()),{'expectedSessionRevision':review.get_session(sid,2)['revision']})['confirmationSessionId']
    flow.write('claim',cid,3,str(uuid.uuid4()),{},response_mode='delta')
    started=time.perf_counter()
    full=flow.write('cursor',cid,3,str(uuid.uuid4()),{'expectedCursorRevision':0})
    full_ms=(time.perf_counter()-started)*1000
    started=time.perf_counter()
    compact=flow.write('cursor',cid,3,str(uuid.uuid4()),{'expectedCursorRevision':1},response_mode='delta')
    compact_ms=(time.perf_counter()-started)*1000
    assert len(full['items'])==5000 and compact['items']==[]
    assert compact['session']['progress']['totalChanges']==5000
    assert len(json.dumps(full))>100*len(json.dumps(compact))
    print(f'5000 changes: full={full_ms:.1f}ms/{len(json.dumps(full))} bytes delta={compact_ms:.1f}ms/{len(json.dumps(compact))} bytes')
