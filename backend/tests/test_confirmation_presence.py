"""Presence changes stay auditable while only chosen real boxes enter F/labels.

These tests arrange immutable B fixtures directly so the confirmation/export
contract is checked independently of the review editor implementation.
"""

import csv
import io
import json
import uuid
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import confirmation_workflow as confirm
from app import audit_export, db, main, quality_audit, statistics_export
from app import training_export as export
from app import review_workflow as review
from app.auth import sign_jwt
from app.confirmation_workflow_routes import router
from app.review_repository import compute_file_sha256

from .test_confirmation_workflow import claim, choose, write
from .test_review_workflow import done, edit, task
from .test_training_export import video_file


@pytest.fixture
def presence(task):
    with db.connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS review_object_catalog(session_id TEXT NOT NULL,object_id INTEGER NOT NULL,class_key TEXT NOT NULL,origin TEXT NOT NULL,actor_id INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(session_id,object_id))")
        confirm.migrate(c)
    for fi in range(3):
        edit(task, "submit", fi=fi)
    result = done(task)
    cid, rv = result["confirmationSessionId"], result["reviewVersionId"]
    bid = confirm.get_session(cid, 3)["baselineId"]
    added = {"objectId": 2, "annotationId": f"{bid}:f0:o2", "classKey": "sperm", "bbox": [45, 50, 65, 70]}
    # frame 0: delete A #1 + add B #2. Frame 1 stays unchanged; frame 2 empty.
    with db.connect() as c:
        c.execute("UPDATE review_version_frames SET objects_json=? WHERE version_id=? AND frame_index=0", (json.dumps([added]), rv))
        for oid, before, after in [(1, [10, 20, 30, 40], None), (2, None, added["bbox"])]:
            c.execute("""INSERT INTO review_changes(id,review_version_id,frame_index,object_id,annotation_id,before_bbox,after_bbox,reviewer_id)
                VALUES (?,?,?,?,?,?,?,?)""", (f"presence_{oid}", rv, 0, oid, f"{bid}:f0:o{oid}", json.dumps(before), json.dumps(after), 2))
        c.execute("UPDATE confirmation_sessions SET total_changes=2 WHERE id=?", (cid,))
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_object_catalog'").fetchone():
            c.execute("INSERT OR IGNORE INTO review_object_catalog(session_id,object_id,class_key,origin,actor_id,created_at) VALUES (?,?,?,?,?,?)", (task, 2, "sperm", "review", 2, "2026-10-21T00:00:00Z"))
    return {"cid": cid, "rv": rv, "bid": bid, "sid": task, "added": added}


def resolve(presence, deleted_choice="B", added_choice="B"):
    cid = presence["cid"]
    claim(cid)
    choose(cid, 0, deleted_choice)
    choose(cid, 1, added_choice)
    return write(cid, "finish")["session"]["finalVersionId"]


def test_presence_api_has_null_sides_and_no_fake_geometry_metrics(presence):
    deleted, added = confirm.list_changes(presence["cid"], 3)
    assert deleted["changeKind"] == "delete" and deleted["afterBbox"] is None
    assert added["changeKind"] == "add" and added["beforeBbox"] is None
    for item in [deleted, added]:
        assert item["metrics"]["changeType"] in ("补标", "删除")
        assert all(item["metrics"][key] is None for key in ("iou", "centerShiftPx", "deltaCenterX", "deltaCenterY", "deltaWidth", "deltaHeight"))


def test_old_confirmation_page_cannot_accept_absence_but_committed_replay_survives(presence):
    cid = presence['cid']
    claim(cid)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    auth = {'Authorization': 'Bearer ' + sign_jwt({'uid': 3}), 'X-Review-Contract': '2', 'Idempotency-Key': 'presence-decision'}
    url = f'/api/confirmation/sessions/{cid}/changes/presence_1/decision'
    body = {'choice': 'B', 'expectedDecisionRevision': 0}
    old = client.put(url, json=body, headers=auth)
    assert old.status_code == 428 and old.json()['code'] == 'CONTRACT_UPGRADE_REQUIRED'
    saved = client.put(url, json=body, headers={**auth, 'X-Review-Contract': '3'})
    assert saved.status_code == 200
    assert client.put(url, json=body, headers=auth).json() == saved.json()
    choose(cid, 1, 'A')
    version = {'expectedSessionRevision': confirm.get_session(cid, 3)['revision']}
    rejected = client.post(f'/api/confirmation/sessions/{cid}/finalize', json=version, headers={**auth, 'Idempotency-Key': 'presence-finish'})
    assert rejected.status_code == 428
    assert client.post(f'/api/confirmation/sessions/{cid}/finalize', json=version, headers={**auth, 'X-Review-Contract': '3', 'Idempotency-Key': 'presence-finish'}).status_code == 200


def test_presence_finalize_storage_failure_preserves_choices_and_same_key_retry(presence, caplog):
    cid = presence['cid']
    claim(cid)
    choose(cid, 0, 'B')
    choose(cid, 1, 'A')
    body = {'expectedSessionRevision': confirm.get_session(cid, 3)['revision']}
    with db.connect() as c:
        c.execute("CREATE TRIGGER inject_presence_failure BEFORE INSERT ON confirmation_final_records BEGIN SELECT RAISE(ABORT,'presence disk failure'); END")
    with pytest.raises(review.ReviewError) as error:
        confirm.write('finish', cid, 3, 'presence-retry', body)
    assert error.value.code == 'STORAGE_UNAVAILABLE'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM final_versions').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM decision_heads').fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM review_write_receipts WHERE request_key='presence-retry'").fetchone()[0] == 0
        c.execute('DROP TRIGGER inject_presence_failure')
    committed = confirm.write('finish', cid, 3, 'presence-retry', body)
    assert confirm.write('finish', cid, 3, 'presence-retry', body) == committed
    assert len(confirm.final_data(committed['session']['finalVersionId'])['decisionRecords']) == 2
    assert 'confirmation.storage_failed' in caplog.text and 'confirmation.finalized' in caplog.text


def test_legacy_geometry_task_without_explicit_class_keeps_default(task):
    with db.connect() as c:
        confirm.migrate(c)
    for fi in range(3):
        edit(task, 'submit', fi=fi)
    cid = done(task)['confirmationSessionId']
    session = confirm.get_session(cid, 3)
    with db.connect() as c:
        for table, field, identity in [('baseline_frames', 'baseline_id', session['baselineId']), ('review_version_frames', 'version_id', session['reviewVersionId'])]:
            for row in c.execute(f'SELECT frame_index,objects_json FROM {table} WHERE {field}=?', (identity,)).fetchall():
                objects = json.loads(row['objects_json'])
                for obj in objects:
                    obj.pop('classKey', None)
                c.execute(f'UPDATE {table} SET objects_json=? WHERE {field}=? AND frame_index=?', (json.dumps(objects), identity, row['frame_index']))
    claim(cid)
    vid = write(cid, 'finish')['session']['finalVersionId']
    assert confirm.final_data(vid)['frames'][0]['objects'][0]['classKey'] == 'sperm'


@pytest.mark.parametrize("deleted_choice,added_choice,expected", [("A", "A", [1]), ("A", "B", [1, 2]), ("B", "A", []), ("B", "B", [2])])
def test_presence_choices_build_only_real_objects_and_keep_absence_evidence(presence, deleted_choice, added_choice, expected):
    vid = resolve(presence, deleted_choice, added_choice)
    frozen = confirm.final_data(vid)
    assert [o["objectId"] for o in frozen["frames"][0]["objects"]] == expected
    assert [o["objectId"] for o in frozen["frames"][1]["objects"]] == [1]
    assert frozen["frames"][2]["objects"] == []
    records = frozen["decisionRecords"]
    assert len(records) == 2 and {r["objectId"] for r in records} == {1, 2}
    assert [r["decision"]["choice"] for r in records] == [deleted_choice, added_choice]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM final_frame_objects WHERE final_version_id=? AND frame_index=0", (vid,)).fetchone()[0] == len(expected)
        assert json.loads(c.execute("SELECT objects_json FROM baseline_frames WHERE baseline_id=? AND frame_index=0", (presence["bid"],)).fetchone()[0])[0]["bbox"] == [10, 20, 30, 40]


@pytest.mark.parametrize("corruption", ["unlisted_add", "duplicate_b", "wrong_class", "wrong_annotation", "wrong_side", "both_absent"])
def test_frozen_presence_corruption_is_rejected_without_partial_final(presence, corruption):
    with db.connect() as c:
        objects = [presence["added"]]
        if corruption == "unlisted_add":
            objects.append({**presence["added"], "objectId": 3, "annotationId": f"{presence['bid']}:f0:o3"})
        elif corruption == "duplicate_b":
            objects.append(presence["added"])
        elif corruption == "wrong_class":
            # Existing object identity must not acquire another class in B.
            objects.append({"objectId": 1, "classKey": "other", "annotationId": f"{presence['bid']}:f0:o1", "bbox": [10, 20, 30, 40]})
        elif corruption == "wrong_annotation":
            c.execute("UPDATE review_changes SET annotation_id='unrelated' WHERE object_id=2")
        elif corruption == "wrong_side":
            c.execute("UPDATE review_changes SET before_bbox='[1,2,3,4]' WHERE object_id=2")
        else:
            c.execute("UPDATE review_changes SET before_bbox='null',after_bbox='null' WHERE object_id=2")
        c.execute("UPDATE review_version_frames SET objects_json=? WHERE version_id=? AND frame_index=0", (json.dumps(objects), presence["rv"]))
    with pytest.raises(review.ReviewError) as error:
        resolve(presence)
    assert error.value.code in ('INVALID_FROZEN_VERSION', 'INVALID_CHANGES')
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM final_versions").fetchone()[0] == 0


@pytest.mark.parametrize("added_choice", ["A", "B"])
def test_training_zip_has_real_empty_frames_and_full_presence_provenance(presence, tmp_path, monkeypatch, added_choice):
    monkeypatch.setattr(export, "DATASET_EXPORT_DIR", tmp_path / "datasets")
    monkeypatch.setattr(main, "TRACK_DATA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "LEGACY_TRACK_DATA_DIR", tmp_path / "legacy")
    monkeypatch.setattr(export, "enqueue", lambda _: None)
    video = video_file(main.TRACK_DATA_DIR / "test-video")
    with db.connect() as c:
        export.migrate(c)
        c.execute("UPDATE media_revisions SET sha256=?,file_size=?", (compute_file_sha256(video), video.stat().st_size))
    vid = resolve(presence, "B", added_choice)
    job = export.create(3, uuid.uuid4().hex, {"finalVersionIds": [vid], "format": "both", "splitRatio": 0.8})
    export.run(job["exportId"])
    state = export.status(job["exportId"], 3)
    assert state["state"] == "ready", state
    with zipfile.ZipFile(export.download(job["exportId"], 3)) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest['requiredFeatures'] == ['review_presence_v1']
        sample = next(s for s in manifest["samples"] if s["frameIndex"] == 0)
        assert len(z.read(sample["label"]).splitlines()) == int(added_choice == "B")
        evidence = json.loads(z.read(f"provenance/{vid}.json"))
        assert len(evidence["decisionRecords"]) == 2
        assert evidence["decisionRecords"][0]["afterBbox"] is None
    with db.connect() as c:
        audit, _ = quality_audit.load(c, job["exportId"])
        assert audit['requiredFeatures'] == ['review_presence_v1']
        assert audit['tables']['review_object_catalog']
        assert len(audit["tables"]["review_changes"]) == 2
        assert len(json.loads(audit["tables"]["confirmation_final_records"][0]["decision_snapshot_json"])) == 2
    path = statistics_export.export_statistics_zip(db.DB_FILE, tmp_path / "statistics", source_system_id="presence-test")
    with zipfile.ZipFile(path) as z:
        assert json.loads(z.read('manifest.json'))['requiredFeatures'] == ['review_presence_v1']
        assert 'review_object_catalog.csv' in z.namelist()
        assert 'review_annotation_returns.csv' in z.namelist()
        rows = list(csv.DictReader(io.StringIO(z.read("review_changes.csv").decode("utf-8-sig"))))
        assert len(rows) == 2 and any(r["before_bbox"] == "null" for r in rows)
        records = [json.loads(line) for line in z.read("confirmation_final_records.jsonl").decode().splitlines()]
        assert len(records[0]["decision_snapshot_json"]) == 2
    with audit_export.connect_readonly(db.DB_FILE) as c:
        audit_export.bundle(c, audit_export.jobs(c), tmp_path / 'audit-presence.zip')
    with zipfile.ZipFile(tmp_path / 'audit-presence.zip') as z:
        assert json.loads(z.read('manifest.json'))['requiredFeatures'] == ['review_presence_v1']


@pytest.mark.parametrize('restore_returned_frame', [False, True])
def test_real_review_presence_roundtrip_retains_other_frame_choice(task, restore_returned_frame):
    with db.connect() as c:
        confirm.migrate(c)
    allocation = review.write('add_object', task, 2, 'allocate-real-add', {'expectedSessionRevision': review.get_session(task, 2)['revision']})
    obj = allocation['object']
    added = {'op': 'add', 'objectId': obj['objectId'], 'classKey': obj['classKey'], 'bbox': [60, 70, 90, 100]}
    patch = [{'op': 'delete', 'objectId': 1}, added]
    edit(task, 'submit', patch, fi=0)
    edit(task, 'submit', [added], fi=1)
    edit(task, 'submit', fi=2)
    cid = done(task)['confirmationSessionId']
    claim(cid)
    choose(cid, 0, 'B')
    choose(cid, 1, 'B')
    prior = choose(cid, 2, 'A')['items'][2]['decision']['eventId']
    change = confirm.list_changes(cid, 3)[0]
    write(cid, 'return', {'expectedSessionRevision': confirm.get_session(cid, 3)['revision'], 'changeId': change['changeId'], 'reason': '补标位置需要再次检查'})
    assert confirm.get_session(cid, 3)['state'] == 'returned'
    assert review.get_frame(task, 0, 2)['state'] == 'unreviewed'
    edit(task, 'submit', [] if restore_returned_frame else patch, fi=0)
    next_cid = done(task)['confirmationSessionId']
    items = confirm.list_changes(next_cid, 3)
    carried = next(x for x in items if x['frameIndex'] == 1)
    assert carried['changeKind'] == 'add' and carried['decision']['choice'] == 'A'
    with db.connect() as c:
        assert c.execute('SELECT source_event_id FROM confirmation_decision_carries WHERE event_id=?', (carried['decision']['eventId'],)).fetchone()[0] == prior
    for i, item in enumerate(items):
        if item['frameIndex'] == 0:
            assert item['decision'] is None
            choose(next_cid, i, 'B')
    assert confirm.get_session(next_cid, 3)['state'] == 'in_progress'
    vid = write(next_cid, 'finish')['session']['finalVersionId']
    final = confirm.final_data(vid)
    assert len(final['decisionRecords']) == (1 if restore_returned_frame else 3)
    assert [o['objectId'] for o in final['frames'][0]['objects']] == ([1] if restore_returned_frame else [2])
    assert [o['objectId'] for o in final['frames'][1]['objects']] == [1]
    assert final['frames'][2]['objects'] == []


def test_returned_confirmation_reports_when_reviewer_returns_entire_video_to_annotation(task):
    with db.connect() as c:
        confirm.migrate(c)
    edit(task, 'submit', [{'objectId': 1, 'bbox': [12, 20, 32, 40]}], fi=0)
    edit(task, 'submit', fi=1)
    edit(task, 'submit', fi=2)
    cid = done(task)['confirmationSessionId']
    claim(cid)
    item = confirm.list_changes(cid, 3)[0]
    write(cid, 'return', {'expectedSessionRevision': confirm.get_session(cid, 3)['revision'], 'changeId': item['changeId'], 'reason': '此帧需重新检查'})
    before = confirm.get_session(cid, 3)
    assert before['state'] == 'returned' and before['returnedReview']['nextConfirmationId'] is None
    with db.connect() as c:
        immutable_before = [tuple(r) for r in c.execute('SELECT version_id,frame_index,objects_json FROM review_version_frames ORDER BY version_id,frame_index')]
    reason = '整段多处漏标，需要回人工标注重新追踪'
    review.write('return_annotation', task, 2, 'return-whole-video-after-c', {'expectedSessionRevision': review.get_session(task, 2)['revision'], 'reason': reason})
    after = confirm.get_session(cid, 3)
    assert after['state'] == 'returned'
    assert after['returnedReview']['nextConfirmationId'] is None
    assert after['returnedReview'].get('returnedToAnnotation') is True
    assert after['returnedReview']['reviewState'] == 'returned_to_annotation'
    assert after['returnedReview']['annotationReturnReason'] == reason
    assert not any(after['permissions'][key] for key in ('canClaim', 'canEdit', 'canComplete', 'canReopen', 'canExport', 'canReturn'))
    with pytest.raises(review.ReviewError):
        write(cid, 'finish')
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT version_id,frame_index,objects_json FROM review_version_frames ORDER BY version_id,frame_index')] == immutable_before
        assert c.execute('SELECT COUNT(*) FROM review_return_events').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM review_annotation_returns').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM final_versions').fetchone()[0] == 0
