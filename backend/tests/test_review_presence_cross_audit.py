"""Independent read/receipt probes across the revised B/C presence workflow."""
import json
import uuid

import pytest

from app import db, confirmation_workflow as confirm, review_workflow as review
from .test_review_workflow import task, edit, done, moved
from .test_confirmation_workflow import claim, choose, write


def presence(task):
    with db.connect() as c:
        confirm.migrate(c)
    allocation = review.write('add_object', task, 2, 'audit-allocation', {'expectedSessionRevision': review.get_session(task, 2)['revision']})
    obj = allocation['object']
    added = {'objectId': obj['objectId'], 'op': 'add', 'classKey': obj['classKey'], 'bbox': [80, 80, 100, 100]}
    edit(task, 'submit', [{'objectId': 1, 'op': 'delete'}, added], fi=0)
    edit(task, 'submit', fi=1); edit(task, 'submit', fi=2)
    return done(task)['confirmationSessionId']


def test_contract_two_cannot_decide_absence_but_can_replay_original_receipt(task):
    cid = presence(task)
    claim(cid)
    first = confirm.list_changes(cid, 3)[0]
    body = {'choice': 'B', 'expectedDecisionRevision': first['decisionRevision']}
    with pytest.raises(review.ReviewError) as err:
        confirm.write('decide', cid, 3, 'two-reject', body, first['changeId'], client_contract='2')
    assert err.value.status == 428
    saved = confirm.write('decide', cid, 3, 'three-save', body, first['changeId'], client_contract='3')
    assert confirm.write('decide', cid, 3, 'three-save', body, first['changeId'], client_contract='2') == saved
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM decision_events WHERE confirmation_id=?', (cid,)).fetchone()[0] == 1


def test_latest_review_snapshot_read_is_not_ambiguous_on_equal_timestamps(task):
    with db.connect() as c:
        confirm.migrate(c)
    edit(task, 'submit', moved(12), fi=0)
    edit(task, 'submit', fi=1); edit(task, 'submit', fi=2)
    first = done(task)
    cid = first['confirmationSessionId']; claim(cid)
    change = confirm.list_changes(cid, 3)[0]
    write(cid, 'return', {'expectedSessionRevision': confirm.get_session(cid, 3)['revision'], 'changeId': change['changeId'], 'reason': '需要重新调整位置'})
    edit(task, 'submit', moved(22), fi=0)
    latest = done(task)
    # SQLite timestamps are second precision. Fast tests and repeated explicit
    # finishing may legitimately share one timestamp; insertion order is clear.
    with db.connect() as c:
        c.execute("UPDATE review_versions SET created_at='2026-10-21 00:00:00' WHERE session_id=?", (task,))
    current = review.get_frame(task, 0, 2)
    assert current['effectiveObjects'][0]['bbox'] == moved(22)[0]['bbox']
    assert review.get_session(task, 2)['completedReviewVersionId'] == latest['reviewVersionId']
