"""B review acceptance tests. Every test uses a disposable database."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import db, review_workflow as w
from app.auth import sign_jwt
from app.review_schema import apply_review_schema
from app.review_repository import (
    freeze_baseline,
    upsert_media_revision,
    create_review_session,
)
from app.review_workflow_routes import router


@pytest.fixture
def task(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "review.db")
    monkeypatch.delenv("REVIEW_ALLOW_SELF", raising=False)
    db.init_db()
    for name in ["A", "B", "C"]:
        db.create_user(name, "unused")
    with db.connect() as c:
        apply_review_schema(c)
        w.migrate(c)
    media = upsert_media_revision("test-video", "a" * 64, 1234, 640, 480, 30, 3)
    frames = [
        dict(
            frameIndex=i,
            coverage="objects" if i < 2 else "empty",
            objects=[dict(objectId=1, bbox=[10, 20, 30, 40], classKey="sperm")]
            if i < 2
            else [],
        )
        for i in range(3)
    ]
    a = freeze_baseline(media, "test-video", 1, frames)
    sid = create_review_session(a["id"])["id"]
    w.write("claim", sid, 2, "claim", {})
    return sid


def edit(sid, action, patch=None, fi=0, rev=None, key=None, uid=2):
    body = {
        "expectedFrameRevision": w.get_frame(sid, fi, uid)["frameRevision"]
        if rev is None
        else rev
    }
    if action != "discard":
        body["patch"] = patch or []
    return w.write(action, sid, uid, key or str(uuid.uuid4()), body, fi)


def moved(x=12.125):
    return [{"objectId": 1, "bbox": [x, 20, x + 20, 40]}]


def done(sid):
    return w.write(
        "finish",
        sid,
        2,
        str(uuid.uuid4()),
        {"expectedSessionRevision": w.get_session(sid, 2)["revision"]},
    )


def count(table):
    with db.connect() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_claim_and_browse_do_not_submit(task):
    s = w.get_session(task, 2)
    assert s["state"] == "pending" and s["progress"]["submittedFrames"] == 0
    assert w.get_frame(task, 2, 2)["baselineObjects"] == []
    assert w.get_session(task, 2)["progress"]["submittedFrames"] == 0
    w.write("cursor", task, 2, "cursor", {"expectedCursorRevision": 0}, 2)
    s = w.get_session(task, 2)
    assert s["resume"]["lastViewedFrameIndex"] == 2
    assert s["resume"]["firstUnsubmittedFrameIndex"] == 0
    assert s["revision"] == 1


def test_draft_roundtrip_and_discard_to_a(task):
    r = edit(task, "draft", moved())
    assert r["frame"]["state"] == "draft" and r["session"]["state"] == "pending"
    assert r["session"]["progress"]["submittedFrames"] == 0
    assert w.get_frame(task, 0, 2)["effectiveObjects"][0]["bbox"][0] == 12.125
    r = edit(task, "discard")
    assert r["frame"]["state"] == "unreviewed" and r["frame"]["lastSubmission"] is None
    assert r["frame"]["effectiveObjects"] == [
        {**o, "metrics": None} for o in r["frame"]["baselineObjects"]
    ]


def test_reedit_preserves_submission_and_discard_restores_b(task):
    first = edit(task, "submit", moved())
    previous = first["frame"]["lastSubmission"]["id"]
    draft = edit(task, "draft", [])  # Return to A is a real draft against committed B.
    assert draft["frame"]["hasDraft"] and draft["frame"]["patch"] == []
    assert draft["session"]["progress"]["submittedFrames"] == 0
    assert draft["frame"]["lastSubmission"]["id"] == previous
    r = edit(task, "discard")
    assert r["frame"]["state"] == "submitted" and r["frame"]["patch"] == moved()
    assert r["session"]["progress"]["modifiedBoxes"] == 1
    assert count("frame_submissions") == 1


def test_net_zero_does_not_invalidate_submission(task):
    first = edit(task, "submit", moved())
    r = edit(task, "draft", moved())
    assert r["frame"]["frameRevision"] == first["frame"]["frameRevision"]
    assert r["frame"]["state"] == "submitted" and not r["frame"]["hasDraft"]
    edit(task, "draft", moved(14))
    r = edit(task, "draft", moved())
    assert r["frame"]["state"] == "submitted"
    assert count("frame_submissions") == 1


def test_idempotent_replay_precedes_revision_check(task):
    first = edit(task, "submit", moved(), rev=0, key="retry-me")
    replay = edit(task, "submit", moved(), rev=0, key="retry-me")
    assert replay == first and count("frame_submissions") == 1
    with pytest.raises(w.ReviewError, match="重试标识"):
        edit(task, "submit", moved(15), rev=0, key="retry-me")
    with pytest.raises(w.ReviewError, match="其他窗口"):
        edit(task, "submit", moved(15), rev=0)


def test_concurrent_compare_and_swap(task):
    def attempt(x):
        try:
            return edit(task, "draft", moved(x), rev=0)
        except w.ReviewError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [14, 15]))
    assert sum(isinstance(x, dict) for x in results) == 1
    assert "FRAME_REVISION_CONFLICT" in results


def test_final_frame_does_not_freeze_and_explicit_finish_is_immutable(task):
    edit(task, "submit", moved(11))
    edit(task, "submit", moved(12.125))  # history must not duplicate C changes
    edit(task, "submit", [], fi=1)
    r = edit(task, "submit", [], fi=2)
    assert r["session"]["state"] == "in_progress"
    assert r["session"]["permissions"]["canComplete"] and count("review_versions") == 0
    final = done(task)
    assert final["session"]["state"] == "reviewed" and final["totalChanges"] == 1
    assert count("review_version_frames") == 3 and count("confirmation_sessions") == 1
    with db.connect() as c:
        change = c.execute("SELECT * FROM review_changes").fetchone()
        assert json.loads(change["after_bbox"]) == moved()[0]["bbox"]
        assert (
            change["frame_submission_id"]
            == w.get_frame(task, 0, 2)["lastSubmission"]["id"]
        )
        assert json.loads(
            c.execute(
                "SELECT objects_json FROM baseline_frames WHERE frame_index=0"
            ).fetchone()[0]
        )[0]["bbox"] == [10, 20, 30, 40]
    with pytest.raises(w.ReviewError, match="已完成"):
        edit(task, "draft", moved(13))
    assert w.get_frame(task, 0, 2)["effectiveObjects"][0]["bbox"] == moved()[0]["bbox"]


def test_empty_and_unchanged_frames_require_submission(task):
    with pytest.raises(w.ReviewError, match="未提交帧"):
        done(task)
    edit(task, "submit", fi=0)
    edit(task, "submit", fi=1)
    with pytest.raises(w.ReviewError, match="未提交帧"):
        done(task)
    edit(task, "submit", fi=2)
    assert done(task)["totalChanges"] == 0
    assert count("review_version_frames") == 3


@pytest.mark.parametrize(
    "table,action",
    [
        ("review_submission_payloads", "submit"),
        ("review_write_receipts", "submit"),
        ("review_changes", "finish"),
        ("confirmation_sessions", "finish"),
    ],
)
def test_transaction_rolls_back_on_storage_failure(task, table, action, caplog):
    if action == "finish":
        edit(task, "submit", moved())
        edit(task, "submit", fi=1)
        edit(task, "submit", fi=2)
    before = w.get_frame(task, 0, 2)
    with db.connect() as c:
        c.execute(
            f"CREATE TRIGGER fail_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END"
        )
    with pytest.raises(w.ReviewError, match="保存失败"):
        done(task) if action == "finish" else edit(task, "submit", moved())
    assert (
        "review.storage_failed" in caplog.text
        and "injected disk failure" in caplog.text
    )
    assert w.get_frame(task, 0, 2) == before
    assert count("review_versions") == 0 and count("confirmation_sessions") == 0
    with db.connect() as c:
        c.execute("DROP TRIGGER fail_write")
    if action == "submit":
        assert count("frame_submissions") == 0
        assert edit(task, "discard")["frame"]["state"] == "unreviewed"
    else:
        assert done(task)["totalChanges"] == 1


@pytest.mark.parametrize(
    "patch",
    [
        [{"objectId": 2, "bbox": [1, 2, 3, 4]}],
        moved() + moved(),
        [{"objectId": 1, "bbox": [-1, 0, 10, 10]}],
        [{"objectId": 1, "bbox": [0, 0, 0, 10]}],
        [{"objectId": 1, "bbox": [0, 0, 641, 10]}],
        [{"objectId": 1, "bbox": [0, 0, 10, 481]}],
    ],
)
def test_reject_new_duplicate_degenerate_or_outside_boxes(task, patch):
    with pytest.raises(w.ReviewError):
        edit(task, "draft", patch)
    assert w.get_frame(task, 0, 2)["frameRevision"] == 0


def test_task_assignee_is_still_protected_from_other_users_and_author(task):
    with pytest.raises(w.ReviewError) as e:
        edit(task, "draft", moved(), uid=3)
    assert e.value.status == 403
    with pytest.raises(w.ReviewError) as e:
        edit(task, "draft", moved(), uid=1)
    assert e.value.status == 403
    with pytest.raises(w.ReviewError) as e:
        w.write("claim", task, 1, "self", {})
    assert e.value.code == "SESSION_ALREADY_CLAIMED"


def test_legacy_incomplete_or_lost_snapshot_is_readonly(task):
    edit(task, "submit", moved())
    with db.connect() as c:
        c.execute(
            "UPDATE review_frames SET state='draft',submission_id=NULL WHERE session_id=? AND frame_index=0",
            (task,),
        )
    s = w.get_session(task, 2)
    assert s["readOnlyReason"] and not s["permissions"]["canEdit"]
    with db.connect() as c:
        c.execute("DELETE FROM baseline_frames WHERE frame_index=2")
    assert "未覆盖全部" in w.get_session(task, 2)["readOnlyReason"]


def test_metrics_are_pixel_iou_and_center_displacement():
    assert w.box_metrics([0, 0, 10, 10], [0, 0, 10, 10]) is None
    m = w.box_metrics([0, 0, 10, 10], [0, 0, 20, 10])
    assert m["iou"] == 0.5 and m["centerShiftPx"] == 5
    assert m["deltaWidth"] == 10 and m["changeType"] == "尺寸调整"
    assert w.box_metrics([0, 0, 10, 10], [20, 20, 30, 30])["iou"] == 0


def test_api_contract_auth_validation_and_error_id(task):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    url = f"/api/review/sessions/{task}/frames/0/draft"
    r = client.get("/api/review/sessions")
    assert r.status_code == 401 and r.json()["requestId"] == r.headers["X-Request-ID"]
    client.headers.update({"Authorization": "Bearer " + sign_jwt({"uid": 2})})
    body = {"patch": moved(), "expectedFrameRevision": 0}
    assert client.put(url, json=body).status_code == 428
    client.headers.update({"X-Review-Contract": "2", "Idempotency-Key": "strict"})
    for bad in [
        {**body, "addObjects": []},
        {**body, "patch": [{"objectId": 1, "bbox": [1, 2, 3]}]},
    ]:
        assert client.put(url, json=bad).status_code == 422
    r = client.put(
        url,
        content='{"expectedFrameRevision":0,"patch":[{"objectId":1,"bbox":[NaN,0,10,10]}]}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    r = client.put(url, json=body)
    assert r.status_code == 200 and r.json()["frame"]["hasDraft"]
    assert client.put(url, json=body).json() == r.json()


def test_existing_confirmation_api_reads_only_latest_net_changes(task):
    edit(task, "submit", moved(11))
    edit(task, "submit", moved(13))
    edit(task, "submit", fi=1)
    edit(task, "submit", fi=2)
    final = done(task)
    from app.review_routes import confirm_router, final_router
    from app.confirmation_workflow import migrate as migrate_confirmation
    with db.connect() as c:
        migrate_confirmation(c)

    app = FastAPI()
    app.include_router(confirm_router)
    app.include_router(final_router)
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer " + sign_jwt({"uid": 3})})
    assert client.get("/api/final-versions/").status_code == 200
    r = client.get(
        f"/api/confirmation/sessions/{final['confirmationSessionId']}/changes"
    )
    assert r.status_code == 200
    changes = r.json()["items"]
    assert len(changes) == 1 and changes[0]["afterBbox"] == moved(13)[0]["bbox"]
    assert changes[0]["beforeBbox"] == [10, 20, 30, 40]


def test_session_ensure_is_atomic_replay_and_validates_baseline(task):
    bid = w.get_session(task, 2)["baselineId"]
    a = w.ensure_session(bid, 2, "ensure")
    assert a["session"]["id"] == task
    assert w.ensure_session(bid, 2, "ensure") == a
    with pytest.raises(w.ReviewError) as e:
        w.ensure_session("missing", 2, "missing")
    assert e.value.status == 404


def test_existing_self_assignment_can_edit_without_configuration(task):
    with db.connect() as c:
        c.execute("UPDATE review_sessions SET reviewer_id=1 WHERE id=?", (task,))
    assert w.get_session(task, 1)["permissions"]["canEdit"]
    assert edit(task, "draft", moved(), uid=1)["frame"]["hasDraft"]


def test_author_can_claim_and_complete_own_review_via_api(task, monkeypatch):
    # A stale deployment flag must not reinstate the removed restriction.
    monkeypatch.setenv("REVIEW_ALLOW_SELF", "false")
    with db.connect() as c:
        c.execute("UPDATE review_sessions SET reviewer_id=NULL WHERE id=?", (task,))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client.headers.update(
        {"Authorization": "Bearer " + sign_jwt({"uid": 1}), "X-Review-Contract": "2"}
    )
    root = f"/api/review/sessions/{task}"
    assert client.get(root).json()["permissions"]["canClaim"]
    result = client.post(root + "/claim", headers={"Idempotency-Key": "self-claim"})
    assert result.status_code == 200
    assert result.json()["session"]["permissions"]["canEdit"]
    draft = client.put(
        root + "/frames/0/draft",
        json={"expectedFrameRevision": 0, "patch": moved()},
        headers={"Idempotency-Key": "self-draft"},
    )
    assert draft.status_code == 200 and draft.json()["frame"]["hasDraft"]
    for fi in range(3):
        f = client.get(root + f"/frames/{fi}").json()
        result = client.post(
            root + f"/frames/{fi}/submit",
            json={"expectedFrameRevision": f["frameRevision"], "patch": f["patch"]},
            headers={"Idempotency-Key": f"self-submit-{fi}"},
        )
        assert result.status_code == 200
    session = result.json()["session"]
    assert session["permissions"]["canComplete"]
    result = client.post(
        root + "/freeze",
        json={"expectedSessionRevision": session["revision"]},
        headers={"Idempotency-Key": "self-complete"},
    )
    assert result.status_code == 200
    assert result.json()["session"]["state"] == "reviewed"
    assert not result.json()["session"]["permissions"]["canEdit"]
    assert client.get(root + "/frames/0").json()["baselineObjects"][0]["bbox"] == [
        10,
        20,
        30,
        40,
    ]
