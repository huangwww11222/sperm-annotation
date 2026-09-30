"""Withdrawal and partial re-review transitions, concurrency and frozen history."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from app import db, review_workflow as review, confirmation_workflow as cflow
from app.review_repository import create_review_session, freeze_baseline
from .test_review_workflow import task, edit, moved, done, count
from .test_confirmation_workflow import confirmation as confirmed_task, claim, choose, write
from .test_review_completion import source, finish as submit_a, body
from .test_training_export import frozen
from .test_confirmation_workflow import confirmation


def pending_task(task):
    old = review.get_session(task, 1)
    with db.connect() as c:
        media = c.execute('SELECT media_revision_id FROM annotation_baselines WHERE id=?', (old['baselineId'],)).fetchone()[0]
        frames = [dict(frameIndex=r['frame_index'], coverage=r['coverage'], objects=json.loads(r['objects_json']))
                  for r in c.execute('SELECT * FROM baseline_frames WHERE baseline_id=? ORDER BY frame_index', (old['baselineId'],))]
    frames[0]['objects'][0]['name'] = 'withdrawal fixture'
    baseline = freeze_baseline(media, 'test-video', 1, frames)
    return create_review_session(baseline['id'])['id']


def withdraw(sid, uid=1, key=None, revision=None):
    return review.write('withdraw', sid, uid, key or uuid.uuid4().hex,
                        {'expectedSessionRevision': review.get_session(sid, uid)['revision'] if revision is None else revision})


def test_withdraw_preserves_a_and_working_data_and_replays(source):
    before = (source / 'workspace_state.json').read_bytes()
    result = submit_a(source)
    sid = result['session']['id']
    baseline = result['session']['baselineId']
    expected = review.get_session(sid, 1)['revision']
    assert review.get_session(sid, 1)['permissions']['canWithdraw']
    first = withdraw(sid, key='withdraw-test', revision=expected)
    assert first['session']['state'] == 'withdrawn'
    assert withdraw(sid, key='withdraw-test', revision=expected) == first
    assert count('review_withdrawals') == 1
    assert (source / 'workspace_state.json').read_bytes() == before
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM baseline_frames WHERE baseline_id=?', (baseline,)).fetchone()[0] == 3
    with pytest.raises(review.ReviewError):
        review.write('claim', sid, 2, 'claim-withdrawn', {})
    # Even identical geometry after withdrawal creates a new A/task.
    again = submit_a(source, body(source), key='resubmit')
    assert again['session']['id'] != sid
    assert again['session']['baselineId'] != baseline


def test_withdraw_only_author_before_claim_and_rejects_stale_or_reused_key(task):
    sid = pending_task(task)
    with pytest.raises(review.ReviewError) as exc:
        withdraw(sid, uid=2)
    assert exc.value.status == 403
    with pytest.raises(review.ReviewError) as exc:
        withdraw(sid, revision=99)
    assert exc.value.code == 'SESSION_REVISION_CONFLICT'
    review.write('claim', sid, 2, 'start', {})
    assert not review.get_session(sid, 1)['permissions']['canWithdraw']
    with pytest.raises(review.ReviewError) as exc:
        withdraw(sid)
    assert exc.value.code == 'REVIEW_ALREADY_STARTED'
    # Discarding the first draft must not reopen withdrawal.
    edit(sid, 'draft', moved()); edit(sid, 'discard')
    with pytest.raises(review.ReviewError):
        withdraw(sid)


def test_claim_and_withdraw_race_has_one_winner(task):
    sid = pending_task(task)
    def attempt(op):
        try:
            if op == 'withdraw': withdraw(sid, revision=0)
            else: review.write('claim', sid, 2, 'race-claim', {})
            return True
        except review.ReviewError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(attempt, ['withdraw', 'claim'])) == 1


def send_back(cid, index=0, uid=3, key=None, rev=None):
    s = cflow.get_session(cid, uid)
    item = cflow.list_changes(cid, uid)[index]
    body = {'expectedSessionRevision': s['revision'] if rev is None else rev,
            'changeId': item['changeId'], 'reason': 'A、B 均未覆盖目标头部，请重新检查。'}
    return cflow.write('return', cid, uid, key or uuid.uuid4().hex, body), body


def test_return_one_frame_preserves_other_choices_and_requires_resubmit(confirmed_task, task):
    cid = confirmed_task
    claim(cid); choose(cid, 0, 'A'); choose(cid, 1, 'B')
    old_context = cflow.get_frame(cid, 0, 3)
    old_a = review.get_frame(task, 0, 2)['baselineObjects']
    old_submission = review.get_frame(task, 0, 2)['lastSubmission']['id']
    first, request = send_back(cid, key='return-replay')
    assert cflow.write('return', cid, 3, 'return-replay', request) == first
    assert count('review_return_events') == 1
    assert first['session']['state'] == 'returned'
    assert not first['session']['permissions']['canEdit']
    assert review.get_session(task, 2)['progress']['submittedFrames'] == 2
    assert review.get_frame(task, 1, 2)['state'] == 'submitted'
    assert review.get_frame(task, 0, 2)['lastSubmission']['id'] == old_submission
    assert cflow.get_frame(cid, 0, 3) == old_context
    with pytest.raises(review.ReviewError): done(task)
    edit(task, 'draft', moved())
    assert review.get_frame(task, 0, 2)['state'] == 'unreviewed'
    edit(task, 'draft', moved(16)); edit(task, 'discard')
    assert review.get_frame(task, 0, 2)['state'] == 'unreviewed'
    edit(task, 'submit', moved(16))
    assert review.get_frame(task, 0, 2)['lastSubmission']['id'] != old_submission
    result = done(task)
    next_id = result['confirmationSessionId']
    assert next_id != cid
    new = cflow.get_session(next_id, 3)
    assert new['confirmerId'] == 3 and new['progress']['decided'] == 1
    items = cflow.list_changes(next_id, 3)
    assert items[0]['decision'] is None
    assert items[1]['decision']['choice'] == 'B'
    assert review.get_frame(task, 0, 2)['baselineObjects'] == old_a
    assert cflow.get_frame(cid, 0, 3) == old_context
    assert cflow.get_session(cid, 3)['returnedReview']['nextConfirmationId'] == next_id
    choose(next_id, 0, 'B')
    final = write(next_id, 'finish')['session']
    assert final['state'] == 'confirmed'
    assert cflow.final_data(final['finalVersionId'])['frames'][0]['objects'][0]['bbox'] == moved(16)[0]['bbox']


def test_return_unchanged_frame_cannot_reuse_its_previous_choice(confirmed_task, task):
    claim(confirmed_task); choose(confirmed_task, 0, 'A'); choose(confirmed_task, 1, 'B')
    send_back(confirmed_task)
    edit(task, 'submit', moved())
    next_id = done(task)['confirmationSessionId']
    assert cflow.list_changes(next_id, 3)[0]['decision'] is None
    assert cflow.list_changes(next_id, 3)[1]['decision']['choice'] == 'B'


def test_return_permission_versions_and_old_operations_are_blocked(confirmed_task):
    with pytest.raises(review.ReviewError): send_back(confirmed_task)
    claim(confirmed_task)
    with pytest.raises(review.ReviewError): send_back(confirmed_task, uid=2)
    with pytest.raises(review.ReviewError): send_back(confirmed_task, rev=0)
    send_back(confirmed_task)
    with pytest.raises(review.ReviewError): choose(confirmed_task, 1, 'B')
    with pytest.raises(review.ReviewError): write(confirmed_task, 'finish')
    with pytest.raises(review.ReviewError): write(confirmed_task, 'reopen')


def test_return_after_reopen_blocks_old_exports_through_next_review(frozen):
    from app import training_export as export
    cid = frozen['cid']
    write(cid, 'reopen')
    send_back(cid)
    with db.connect() as c, pytest.raises(review.ReviewError):
        export.eligible(c, [frozen['vid']])


def test_partial_rereview_export_audit_keeps_carried_decision_lineage(frozen, task):
    from app import training_export as export, quality_audit as audit
    cid = frozen['cid']
    write(cid, 'reopen')
    old_items = cflow.list_changes(cid, 3)
    source_event = old_items[1]['decision']['eventId']
    send_back(cid)
    # Preserve the exact old B patch on both non-returned and returned frames.
    patch = review.get_frame(task, 0, 2)['patch']
    edit(task, 'submit', patch)
    next_id = done(task)['confirmationSessionId']
    items = cflow.list_changes(next_id, 3)
    for i, item in enumerate(items):
        if item['decision'] is None: choose(next_id, i, 'B')
    new_final = write(next_id, 'finish')['session']['finalVersionId']
    job = export.create(3, 're-review-audit', {'finalVersionIds': [new_final], 'format': 'yolo', 'splitRatio': 0.8})
    with db.connect() as c:
        snapshot, checksum = audit.load(c, job['exportId'])
        tables = snapshot['tables']
        assert len(tables['review_return_events']) == 1
        carry = tables['confirmation_decision_carries'][0]
        assert carry['source_event_id'] == source_event
        assert {carry['event_id'], source_event} <= {r['id'] for r in tables['decision_events']}
        assert {cid, next_id} <= {r['id'] for r in tables['confirmation_sessions']}
        assert len(tables['review_versions']) == 2
        assert len(tables['review_version_frames']) == 6
        assert audit.sha(audit.packed(snapshot)) == checksum
