"""Review add/delete and return-to-annotation contract, isolated SQLite only."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, review_workflow as w, media_reimport
from app.auth import sign_jwt
from .test_review_workflow import task, edit, done, count
from .test_review_completion import source, finish as send_a, body as send_body


def allocate(sid, key=None, **changes):
    payload = {'expectedSessionRevision': w.get_session(sid, 2)['revision'], **changes}
    return w.write('add_object', sid, 2, key or uuid.uuid4().hex, payload)


def send_back(sid, uid=2, key=None, **changes):
    payload = {'expectedSessionRevision': w.get_session(sid, uid)['revision'],
               'reason': '整段追踪身份有误，请重新标注。', **changes}
    return w.write('return_annotation', sid, uid, key or uuid.uuid4().hex, payload)


def test_delete_is_only_current_frame_preserves_a_and_discard(task):
    original = w.get_frame(task, 0, 2)['baselineObjects']
    saved = edit(task, 'draft', [{'objectId': 1, 'op': 'delete'}])
    assert saved['frame']['effectiveObjects'] == []
    assert saved['frame']['baselineObjects'] == original
    assert saved['frame']['metrics']['deletedBoxes'] == 1
    assert saved['frame']['metrics']['totalBoxes'] == 0
    assert w.get_frame(task, 1, 2)['effectiveObjects'][0]['objectId'] == 1
    restored = edit(task, 'discard')['frame']['effectiveObjects']
    assert [{k: v for k, v in o.items() if k not in ('metrics', 'changeKind')} for o in restored] == original


def test_add_known_object_on_empty_frame_and_freeze_union(task):
    # The identity exists in the video, although not in this particular frame.
    edit(task, 'submit', [{'objectId': 1, 'op': 'delete'}])
    edit(task, 'submit', fi=1)
    add = {'objectId': 1, 'op': 'add', 'bbox': [90, 80, 120, 100], 'classKey': 'sperm'}
    edit(task, 'submit', [add], fi=2)
    result = done(task)
    assert result['totalChanges'] == 2
    with db.connect() as c:
        changes = c.execute('SELECT * FROM review_changes WHERE review_version_id=? ORDER BY frame_index', (result['reviewVersionId'],)).fetchall()
        assert json.loads(changes[0]['before_bbox']) == [10, 20, 30, 40]
        assert json.loads(changes[0]['after_bbox']) is None
        assert json.loads(changes[1]['before_bbox']) is None
        assert json.loads(changes[1]['after_bbox']) == add['bbox']
        frozen = c.execute('SELECT * FROM review_version_frames WHERE version_id=? ORDER BY frame_index', (result['reviewVersionId'],)).fetchall()
        assert json.loads(frozen[0]['objects_json']) == []
        assert json.loads(frozen[2]['objects_json'])[0]['classKey'] == 'sperm'


def test_allocated_identity_is_stable_replayable_and_never_reused(task):
    revision = w.get_session(task, 2)['revision']
    first = allocate(task, key='allocation-first')
    assert first['object'] == {'objectId': 2, 'classKey': 'sperm', 'origin': 'review'}
    assert w.write('add_object', task, 2, 'allocation-first', {'expectedSessionRevision': revision}) == first
    edit(task, 'draft', [{'objectId': 2, 'op': 'add', 'bbox': [50, 50, 70, 70], 'classKey': 'sperm'}])
    edit(task, 'discard')
    second = allocate(task)
    assert second['object']['objectId'] == 3
    assert [x['objectId'] for x in w.get_objects(task, 2)['items']] == [1, 2, 3]
    assert w.get_session(task, 2)['progress']['submittedFrames'] == 0


def test_add_remove_net_zero_and_restore_after_submitted_delete(task):
    oid = allocate(task)['object']['objectId']
    add = {'objectId': oid, 'op': 'add', 'bbox': [50, 50, 70, 70], 'classKey': 'sperm'}
    edit(task, 'draft', [add])
    assert edit(task, 'draft', [])['frame']['state'] == 'unreviewed'
    deleted = [{'objectId': 1, 'op': 'delete'}]
    edit(task, 'submit', deleted)
    edit(task, 'draft', [])
    assert w.get_session(task, 2)['progress']['submittedFrames'] == 0
    restored = edit(task, 'discard')['frame']
    assert restored['state'] == 'submitted' and restored['effectiveObjects'] == []
    assert restored['patch'] == deleted


@pytest.mark.parametrize('patch', [
    [{'objectId': 1, 'op': 'add', 'bbox': [1, 1, 10, 10], 'classKey': 'sperm'}],
    [{'objectId': 900, 'op': 'add', 'bbox': [1, 1, 10, 10], 'classKey': 'sperm'}],
    [{'objectId': 1, 'op': 'update', 'bbox': [1, 1, 10, 10], 'classKey': 'other'}],
    [{'objectId': 1, 'op': 'delete'}, {'objectId': 1, 'bbox': [1, 1, 10, 10]}],
    [{'objectId': 1, 'op': 'erase'}],
    [{'objectId': 1, 'op': 'delete', 'bbox': [1, 1, 10, 10]}],
])
def test_invalid_or_cross_identity_operations_do_not_write(task, patch):
    before = w.get_frame(task, 0, 2)
    with pytest.raises(w.ReviewError):
        edit(task, 'draft', patch)
    assert w.get_frame(task, 0, 2) == before


def test_new_object_allocation_failure_rolls_back_and_original_key_recovers(task, caplog):
    before = w.get_session(task, 2)
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON review_write_receipts BEGIN SELECT RAISE(ABORT,'injected allocation failure'); END")
    with pytest.raises(w.ReviewError, match='保存失败'):
        allocate(task, key='allocation-rollback')
    assert w.get_session(task, 2) == before
    assert 'review.storage_failed' in caplog.text
    with db.connect() as c:
        c.execute('DROP TRIGGER fail_receipt')
    assert allocate(task, key='allocation-rollback')['object']['objectId'] == 2


def test_return_annotation_retains_history_unlocks_source_and_resubmits_new_a(source):
    sent = send_a(source)
    sid = sent['session']['id']
    w.write('claim', sid, 2, 'return-claim', {})
    edit(sid, 'submit', [{'objectId': 1, 'op': 'delete'}])
    workspace = (source / 'workspace_state.json').read_bytes()
    old = w.get_frame(sid, 0, 2)
    revision = w.get_session(sid, 2)['revision']
    request = {'expectedSessionRevision': revision, 'reason': '整段追踪身份有误，请重新标注。'}
    result = w.write('return_annotation', sid, 2, 'return-original', request)
    assert w.write('return_annotation', sid, 2, 'return-original', request) == result
    assert result['session']['state'] == 'returned_to_annotation'
    assert result['session']['returnAnnotation']['reason'] == request['reason']
    assert not result['session']['permissions']['canEdit']
    assert (source / 'workspace_state.json').read_bytes() == workspace
    assert w.get_frame(sid, 0, 2)['patch'] == old['patch']
    with db.connect() as c:
        assert media_reimport.overwrite_permission(c, 'complete-media')[0]
        assert c.execute('SELECT COUNT(*) FROM review_annotation_returns WHERE session_id=?', (sid,)).fetchone()[0] == 1
    again = send_a(source, send_body(source), key='return-resend')
    assert again['session']['baselineId'] != sent['session']['baselineId']
    assert again['session']['id'] != sid
    with pytest.raises(w.ReviewError):
        edit(sid, 'draft', [])


def test_return_permission_reason_revision_and_finish_conflicts(task):
    assert w.get_session(task, 2)['permissions']['canReturnAnnotation']
    assert not w.get_session(task, 1)['permissions']['canReturnAnnotation']
    for kwargs in ({'uid': 1}, {'reason': '  '}, {'expectedSessionRevision': 999}):
        with pytest.raises(w.ReviewError):
            send_back(task, **kwargs)
    for fi in range(3): edit(task, 'submit', fi=fi)
    done(task)
    with pytest.raises(w.ReviewError): send_back(task)


def test_changed_source_cannot_create_parallel_review_until_returned(source):
    first = send_a(source)
    sid = first['session']['id']
    w.write('claim', sid, 2, 'claim-source-guard', {})
    path = source / 'workspace_state.json'
    data = json.loads(path.read_text())
    data['manualAnnotations'][0]['bbox']['x'] += 1
    path.write_text(json.dumps(data))
    with pytest.raises(w.ReviewError) as err:
        send_a(source, send_body(source), key='parallel-review')
    assert err.value.code == 'MEDIA_ALREADY_SUBMITTED'
    send_back(sid)
    assert send_a(source, send_body(source), key='new-after-return')['session']['id'] != sid


def test_old_contract_cannot_erase_structural_draft_or_committed_payload(task):
    # Replay is still earlier than new compatibility restrictions.
    geometric = {'expectedFrameRevision': 0, 'patch': [{'objectId': 1, 'bbox': [11, 20, 31, 40]}]}
    old_success = w.write('draft', task, 2, 'old-geometric', geometric, 0, contract='2')
    edit(task, 'submit', [{'objectId': 1, 'op': 'delete'}])
    assert w.write('draft', task, 2, 'old-geometric', geometric, 0, contract='2') == old_success
    for op in ['draft', 'submit', 'discard']:
        payload = {'expectedFrameRevision': w.get_frame(task, 0, 2)['frameRevision']}
        if op != 'discard': payload['patch'] = []
        with pytest.raises(w.ReviewError) as err:
            w.write(op, task, 2, uuid.uuid4().hex, payload, 0, contract='2')
        assert err.value.status == 428
    edit(task, 'submit', fi=1); edit(task, 'submit', fi=2)
    with pytest.raises(w.ReviewError) as err:
        w.write('finish', task, 2, uuid.uuid4().hex, {'expectedSessionRevision': w.get_session(task, 2)['revision']}, contract='2')
    assert err.value.status == 428


def test_return_failure_rolls_back_state_and_original_key_retries(task, caplog):
    before = w.get_session(task, 2)
    payload = {'expectedSessionRevision': before['revision'], 'reason': '身份需整段检查'}
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_return_receipt BEFORE INSERT ON review_write_receipts BEGIN SELECT RAISE(ABORT,'injected return failure'); END")
    with pytest.raises(w.ReviewError) as err:
        w.write('return_annotation', task, 2, 'return-retry', payload)
    assert err.value.status == 503 and 'review.storage_failed' in caplog.text
    assert w.get_session(task, 2) == before
    assert count('review_annotation_returns') == 0
    with db.connect() as c: c.execute('DROP TRIGGER fail_return_receipt')
    result = w.write('return_annotation', task, 2, 'return-retry', payload)
    assert result['session']['state'] == 'returned_to_annotation'
    assert w.write('return_annotation', task, 2, 'return-retry', payload) == result


def test_return_and_frame_submission_race_is_serialized(task):
    revision = w.get_session(task, 2)['revision']
    def act(action):
        try:
            if action == 'return': return send_back(task, expectedSessionRevision=revision)
            return edit(task, 'submit', [{'objectId': 1, 'op': 'delete'}], rev=0)
        except w.ReviewError: return None
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(act, ['return', 'submit']))
    assert sum(result is not None for result in outcomes) == 1
    state = w.get_session(task, 2)
    assert (state['state'] == 'returned_to_annotation') != (state['progress']['submittedFrames'] == 1)


def test_returned_task_status_is_history_not_tracking_blocker(source):
    sent = send_a(source); sid = sent['session']['id']
    w.write('claim', sid, 2, 'claim-history', {})
    send_back(sid)
    status = w.submission_status('complete-media', 1)
    assert status['items'] == []
    assert status['history'][0]['id'] == sid
    assert status['history'][0]['returnAnnotation']['actorId'] == 2
    assert not status['history'][0]['permissions']['canWithdraw']


def test_new_identity_inherits_real_manual_category_instead_of_splitting_yolo_class(source):
    path = source / 'workspace_state.json'
    workspace = json.loads(path.read_text())
    workspace['manualAnnotations'][0]['name'] = 'rare sperm 1'
    path.write_text(json.dumps(workspace))
    sent = send_a(source); sid = sent['session']['id']
    w.write('claim', sid, 2, 'category-inherit-claim', {})
    allocated = allocate(sid)['object']
    assert allocated['classKey'] == w.get_frame(sid, 0, 2)['baselineObjects'][0]['classKey'] == 'rare sperm'


def test_empty_baseline_uses_same_default_category_as_manual_annotation(task):
    with db.connect() as c:
        c.execute("UPDATE baseline_frames SET coverage='empty',objects_json='[]' WHERE baseline_id=?", (w.get_session(task, 2)['baselineId'],))
    assert allocate(task)['object']['classKey'] == 'rare sperm'
    with pytest.raises(w.ReviewError) as err:
        allocate(task, classKey='unregistered-category')
    assert err.value.code == 'INVALID_CLASS'


def test_multiple_categories_require_explicit_existing_category_and_replay_exact_choice(task):
    with db.connect() as c:
        bid = w.get_session(task, 2)['baselineId']
        c.execute("UPDATE baseline_frames SET coverage='objects',objects_json=? WHERE baseline_id=? AND frame_index=2", (json.dumps([{'objectId': 8, 'classKey': 'second-class', 'bbox': [20, 20, 40, 40]}]), bid))
    with pytest.raises(w.ReviewError) as err:
        allocate(task)
    assert err.value.code == 'CLASS_REQUIRED'
    before = w.get_session(task, 2)['revision']
    allocated = allocate(task, key='explicit-class', classKey='second-class')
    assert allocated['object'] == {'objectId': 9, 'classKey': 'second-class', 'origin': 'review'}
    assert w.write('add_object', task, 2, 'explicit-class', {'expectedSessionRevision': before, 'classKey': 'second-class'}) == allocated
    with pytest.raises(w.ReviewError): allocate(task, classKey='another-new-class')


def test_completed_review_withdrawal_guidance_matches_allowed_return_path(task):
    for fi in range(3): edit(task, 'submit', fi=fi)
    done(task)
    for uid in [1, 2]:
        session = w.get_session(task, uid)
        assert not session['permissions']['canWithdraw']
        assert not session['permissions']['canReturnAnnotation']
        assert '对比确认' in session['withdrawalBlockedReason']
        assert '本帧重审' in session['withdrawalBlockedReason']
        assert '退回重新标注' not in session['withdrawalBlockedReason']
