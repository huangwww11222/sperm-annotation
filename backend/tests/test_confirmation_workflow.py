"""C acceptance and failure injection tests. Only disposable SQLite files."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from app import confirmation_workflow as cflow
from app import db
from app import review_workflow as review
from app.auth import sign_jwt
from app.confirmation_workflow_routes import final_router, router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .test_review_workflow import count, done, edit, moved
from .test_review_workflow import task as task


@pytest.fixture
def confirmation(task):
    with db.connect() as c:
        cflow.migrate(c)
    edit(task, "submit", moved())
    edit(task, "submit", moved(13.333), fi=1)
    edit(task, "submit", fi=2)
    cid = done(task)["confirmationSessionId"]
    return cid


def write(cid, action, body=None, change=None, key=None, uid=3):
    if action in ("undo", "finish", "reopen"):
        body = {
            "expectedSessionRevision": cflow.get_session(cid, uid)["revision"],
            **(body or {}),
        }
    return cflow.write(action, cid, uid, key or str(uuid.uuid4()), body or {}, change)


def claim(cid, uid=3):
    return write(cid, "claim", uid=uid)


def choose(cid, i, choice, uid=3, rev=None, key=None):
    item = cflow.list_changes(cid, uid)[i]
    return write(
        cid,
        "decide",
        {
            "choice": choice,
            "expectedDecisionRevision": item["decisionRevision"]
            if rev is None
            else rev,
        },
        item["changeId"],
        key,
        uid,
    )


def finish(cid):
    claim(cid)
    choose(cid, 0, "A")
    choose(cid, 1, "B")
    return write(cid, "finish")


def test_browse_claim_and_cursor_never_make_decisions(confirmation):
    cid = confirmation
    assert cflow.get_session(cid, 3)["permissions"]["canClaim"]
    s = claim(cid)["session"]
    assert s["state"] == "pending"
    item = cflow.list_changes(cid, 3)[1]
    r = write(cid, "cursor", {"expectedCursorRevision": 0}, item["changeId"])
    assert r["session"]["revision"] == s["revision"]
    assert r["session"]["resume"]["lastViewedChangeId"] == item["changeId"]
    assert r["session"]["resume"]["firstPendingChangeId"] != item["changeId"]
    assert r["session"]["progress"]["decided"] == 0


def test_read_context_uses_frozen_b_and_real_dimensions(confirmation, task):
    ctx = cflow.get_frame(confirmation, 0, 3)
    assert ctx["baselineObjects"][0]["bbox"] == [10, 20, 30, 40]
    assert ctx["reviewObjects"][0]["bbox"] == moved()[0]["bbox"]
    with db.connect() as c:
        c.execute(
            "UPDATE review_frames SET patch_json='[]' WHERE session_id=?", (task,)
        )
    assert cflow.get_frame(confirmation, 0, 3) == ctx
    s = cflow.get_session(confirmation, 3)
    assert s["media"]["width"] == 640 and s["media"]["height"] == 480


def test_choice_saves_immediately_and_undo_survives_reload(confirmation):
    cid = confirmation
    claim(cid)
    first = choose(cid, 0, "B")
    assert first["session"]["progress"]["decided"] == 1
    assert first["session"]["state"] == "in_progress"
    choose(cid, 0, "A")
    s = cflow.get_session(cid, 3)
    r = write(cid, "undo", {"actionId": s["undo"]["actionId"]})
    assert r["items"][0]["decision"]["choice"] == "B"
    r = write(cid, "undo", {"actionId": r["session"]["undo"]["actionId"]})
    assert r["items"][0]["decision"] is None
    assert (
        r["session"]["progress"]["decided"] == 0
        and not r["session"]["permissions"]["canUndo"]
    )
    assert count("decision_events") == 4


def test_same_choice_is_noop_and_duplicate_request_replays(confirmation):
    cid = confirmation
    claim(cid)
    r = choose(cid, 0, "A", rev=0, key="original")
    assert choose(cid, 0, "A", rev=0, key="original") == r
    assert count("decision_events") == 1
    r2 = choose(cid, 0, "A")
    assert r2["session"]["revision"] == r["session"]["revision"]
    assert r2["items"][0]["decisionRevision"] == r["items"][0]["decisionRevision"]
    with pytest.raises(review.ReviewError) as e:
        choose(cid, 0, "B", rev=0, key="original")
    assert e.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_last_choice_requires_explicit_video_completion(confirmation):
    cid = confirmation
    claim(cid)
    choose(cid, 0, "A")
    r = choose(cid, 1, "B")
    assert (
        r["session"]["permissions"]["canComplete"]
        and r["session"]["state"] == "in_progress"
    )
    assert count("final_versions") == 0


def test_complete_all_frames_precision_classes_and_original_versions(confirmation):
    cid = confirmation
    # Source classes are immutable; arrange non-default classes in matching A/B fixtures.
    with db.connect() as c:
        for table, col, key in [
            ("baseline_frames", "baseline_id", cflow.get_session(cid, 3)["baselineId"]),
            (
                "review_version_frames",
                "version_id",
                cflow.get_session(cid, 3)["reviewVersionId"],
            ),
        ]:
            r = c.execute(
                f"SELECT objects_json FROM {table} WHERE {col}=? AND frame_index=1",
                (key,),
            ).fetchone()
            objects = json.loads(r[0])
            objects[0]["classKey"] = "rare sperm"
            c.execute(
                f"UPDATE {table} SET objects_json=? WHERE {col}=? AND frame_index=1",
                (json.dumps(objects), key),
            )
    result = finish(cid)
    vid = result["session"]["finalVersionId"]
    f = cflow.final_data(vid)
    assert len(f["frames"]) == 3 and f["frames"][2]["objects"] == []
    assert f["frames"][0]["objects"][0]["bbox"] == [10, 20, 30, 40]
    o = f["frames"][1]["objects"][0]
    assert o["bbox"] == moved(13.333)[0]["bbox"] and o["classKey"] == "rare sperm"
    assert (
        o["choice"] == "B" and o["decisionEventId"] and o["resolution"] == "adopted_b"
    )
    with pytest.raises(review.ReviewError):
        choose(cid, 0, "B")
    assert not result["session"]["permissions"]["canEdit"]


def test_reopen_keeps_old_final_and_makes_new_version(confirmation):
    cid = confirmation
    r = finish(cid)
    old_id = r["session"]["finalVersionId"]
    old = cflow.final_data(old_id)
    r = write(cid, "reopen")
    assert r["session"]["state"] == "in_progress"
    assert (
        r["items"][0]["decision"]["choice"] == "A"
        and not r["session"]["permissions"]["canExport"]
    )
    choose(cid, 0, "B")
    new = write(cid, "finish")
    vid = new["session"]["finalVersionId"]
    assert vid != old_id and cflow.final_data(old_id) == old
    f = cflow.final_data(vid)
    assert f["previousFinalVersionId"] == old_id
    assert f["frames"][0]["objects"][0]["bbox"] == moved()[0]["bbox"]
    assert count("confirmation_reopen_events") == 1


def test_partial_and_stale_completion_rejected(confirmation):
    cid = confirmation
    claim(cid)
    with pytest.raises(review.ReviewError) as e:
        write(cid, "finish")
    assert e.value.code == "UNDECIDED_CHANGES"
    before = cflow.get_session(cid, 3)["revision"]
    choose(cid, 0, "B")
    choose(cid, 1, "B")
    with pytest.raises(review.ReviewError) as e:
        cflow.write("finish", cid, 3, "stale", {"expectedSessionRevision": before})
    assert e.value.code == "CONFIRMATION_REVISION_CONFLICT"
    assert count("final_versions") == 0


def test_concurrent_choice_and_conflicting_undo(confirmation):
    cid = confirmation
    claim(cid)

    def attempt(choice):
        try:
            return choose(cid, 0, choice, rev=0)
        except review.ReviewError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        res = list(pool.map(attempt, ["A", "B"]))
    assert (
        sum(isinstance(x, dict) for x in res) == 1
        and "DECISION_REVISION_CONFLICT" in res
    )
    old = cflow.get_session(cid, 3)
    choose(cid, 1, "A")
    with pytest.raises(review.ReviewError):
        write(cid, "undo", {"actionId": old["undo"]["actionId"]})


@pytest.mark.parametrize(
    "table,action",
    [
        ("decision_heads", "decide"),
        ("confirmation_actions", "decide"),
        ("review_write_receipts", "decide"),
        ("final_version_frames", "finish"),
        ("final_frame_objects", "finish"),
        ("confirmation_final_records", "finish"),
        ("confirmation_reopen_events", "reopen"),
    ],
)
def test_atomic_rollback_and_retry_on_write_failure(
    confirmation, table, action, caplog
):
    cid = confirmation
    claim(cid)
    if action in ("finish", "reopen"):
        choose(cid, 0, "A")
        choose(cid, 1, "B")
    if action == "reopen":
        write(cid, "finish")
    before = cflow.get_session(cid, 3)
    events = count("decision_events")
    finals = count("final_versions")
    with db.connect() as c:
        c.execute(
            f"CREATE TRIGGER inject_failure BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected write failure'); END"
        )
    with pytest.raises(review.ReviewError) as e:
        choose(cid, 0, "B", key="retry") if action == "decide" else write(
            cid, action, key="retry"
        )
    assert e.value.status == 503 and "confirmation.storage_failed" in caplog.text
    assert (
        cflow.get_session(cid, 3) == before
        and count("decision_events") == events
        and count("final_versions") == finals
    )
    with db.connect() as c:
        c.execute("DROP TRIGGER inject_failure")
    choose(cid, 0, "B", key="retry") if action == "decide" else write(
        cid, action, key="retry"
    )


def test_zero_changes_requires_explicit_completion_and_preserves_empty_frame(task):
    with db.connect() as c:
        cflow.migrate(c)
    for fi in range(3):
        edit(task, "submit", fi=fi)
    cid = done(task)["confirmationSessionId"]
    s = claim(cid)["session"]
    assert (
        s["progress"]["totalChanges"] == 0
        and s["permissions"]["canComplete"]
        and s["state"] == "pending"
    )
    r = write(cid, "finish")
    f = cflow.final_data(r["session"]["finalVersionId"])
    assert len(f["frames"]) == 3 and f["frames"][2]["objects"] == []
    assert f["frames"][0]["objects"][0]["resolution"] == "unchanged"


def test_user_can_confirm_own_work_but_not_take_another_users_task(confirmation):
    cid = confirmation
    claim(cid, uid=1)
    choose(cid, 0, "A", uid=1)
    with pytest.raises(review.ReviewError) as e:
        claim(cid, uid=2)
    assert e.value.code == "CONFIRMATION_ALREADY_CLAIMED"
    with pytest.raises(review.ReviewError) as e:
        choose(cid, 1, "B", uid=2)
    assert e.value.status == 403


def test_invalid_legacy_snapshot_is_readonly(confirmation):
    with db.connect() as c:
        c.execute("DELETE FROM review_version_frames WHERE frame_index=2")
    s = cflow.get_session(confirmation, 3)
    assert s["readOnlyReason"] and not s["permissions"]["canClaim"]
    with pytest.raises(review.ReviewError):
        claim(confirmation)


def test_api_auth_contract_strict_choices_foreign_change_and_export(confirmation):
    cid = confirmation
    app = FastAPI()
    app.include_router(router)
    app.include_router(final_router)
    client = TestClient(app)
    url = f"/api/confirmation/sessions/{cid}"
    assert client.get(url).status_code == 401
    client.headers.update({"Authorization": "Bearer " + sign_jwt({"uid": 3})})
    assert client.post(url + "/claim").status_code == 428
    client.headers.update({"X-Review-Contract": "2", "Idempotency-Key": "claim-api"})
    assert client.post(url + "/claim").status_code == 200
    change = cflow.list_changes(cid, 3)[0]["changeId"]
    endpoint = url + f"/changes/{change}/decision"
    assert (
        client.put(
            endpoint, json={"choice": "C", "expectedDecisionRevision": 0}
        ).status_code
        == 422
    )
    assert (
        client.put(
            endpoint,
            json={"choice": "A", "expectedDecisionRevision": 0, "bbox": [0, 0, 10, 10]},
        ).status_code
        == 422
    )
    client.headers["Idempotency-Key"] = "foreign"
    assert (
        client.put(
            url + "/changes/missing/decision",
            json={"choice": "A", "expectedDecisionRevision": 0},
        ).status_code
        == 404
    )
    choose(cid, 0, "A")
    choose(cid, 1, "B")
    r = write(cid, "finish")
    dl = client.get(
        "/api/final-versions/" + r["session"]["finalVersionId"] + "/download"
    )
    assert (
        dl.status_code == 200
        and len(dl.json()["frames"]) == 3
        and "attachment" in dl.headers["Content-Disposition"]
    )
    assert dl.headers["X-Request-ID"]


@pytest.mark.parametrize("action", ["undo", "finish", "reopen", "cursor"])
def test_retry_replays_committed_response_for_every_action(confirmation, action):
    cid = confirmation
    claim(cid)
    choose(cid, 0, "B")
    choose(cid, 1, "A")
    if action == "reopen":
        write(cid, "finish")
    s = cflow.get_session(cid, 3)
    body = {"expectedSessionRevision": s["revision"]}
    target = None
    if action == "undo":
        body["actionId"] = s["undo"]["actionId"]
    if action == "cursor":
        target = cflow.list_changes(cid, 3)[1]["changeId"]
        body = {
            "expectedCursorRevision": s["resume"]["cursorRevision"],
            "changeId": target,
        }
    r = cflow.write(action, cid, 3, "lost-response", body, target)
    after = cflow.get_session(cid, 3)
    assert cflow.write(action, cid, 3, "lost-response", body, target) == r
    assert cflow.get_session(cid, 3) == after


def test_undo_rollback_restores_head_action_and_receipt(confirmation, caplog):
    cid = confirmation
    claim(cid)
    choose(cid, 0, "B")
    s = cflow.get_session(cid, 3)
    items = cflow.list_changes(cid, 3)
    events = count("decision_events")
    body = {"expectedSessionRevision": s["revision"], "actionId": s["undo"]["actionId"]}
    with db.connect() as c:
        c.execute(
            "CREATE TRIGGER undo_failure BEFORE UPDATE ON confirmation_actions BEGIN SELECT RAISE(ABORT,'injected undo failure'); END"
        )
    with pytest.raises(review.ReviewError) as e:
        cflow.write("undo", cid, 3, "undo-retry", body)
    assert e.value.status == 503 and "confirmation.storage_failed" in caplog.text
    assert (
        cflow.get_session(cid, 3) == s
        and cflow.list_changes(cid, 3) == items
        and count("decision_events") == events
    )
    with db.connect() as c:
        c.execute("DROP TRIGGER undo_failure")
    r = cflow.write("undo", cid, 3, "undo-retry", body)
    assert (
        r["session"]["progress"]["decided"] == 0
        and not r["session"]["permissions"]["canUndo"]
    )


def test_change_kind_follows_center_movement_not_top_left_corner():
    assert (
        cflow.change_metrics([10, 10, 20, 20], [8, 8, 22, 22])["changeType"]
        == "尺寸调整"
    )
    assert (
        cflow.change_metrics([10, 10, 20, 20], [12, 12, 22, 22])["changeType"]
        == "位置调整"
    )
    assert (
        cflow.change_metrics([10, 10, 20, 20], [10, 10, 22, 22])["changeType"]
        == "位置 + 尺寸调整"
    )
