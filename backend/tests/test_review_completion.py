import json
import pytest
from app import annotation_completion as a, db, review_workflow as w
from .test_review_workflow import task

INFO = {"width": 640, "height": 480, "fps": 30.0, "frameCount": 3}


@pytest.fixture
def source(tmp_path, task):
    (tmp_path / "video.avi").write_bytes(b"fixture-video-content")
    (tmp_path / "workspace_state.json").write_text(
        json.dumps(
            {
                "updatedBy": 1,
                "manualAnnotations": [
                    {
                        "source": "manual",
                        "objectId": 1,
                        "frameIndex": 0,
                        "name": "sperm 1",
                        "bbox": {"x": 10, "y": 10, "width": 10, "height": 10},
                    }
                ],
                "deletedTrackingIds": ["ai-0-2"],
            }
        )
    )
    (tmp_path / "tracker_results.json").write_text(
        json.dumps(
            {
                "source_frame_index": 0,
                "objects": [
                    {"object_id": 1, "bbox": [2, 3, 4, 5]},
                    {"object_id": 2, "bbox": [8, 9, 10, 11]},
                ],
            }
        )
        + "\n"
        + json.dumps({"source_frame_index": 1, "objects": []})
    )
    return tmp_path


def body(source, **patch):
    return {
        "expectedSourceRevision": a.preview(source, 1, INFO)["sourceRevision"],
        "confirmComplete": True,
        "explicitEmptyFrameRanges": [{"start": 2, "end": 2}],
        **patch,
    }


def finish(source, payload=None, key="send"):
    return a.complete(
        source,
        source / "video.avi",
        "complete-media",
        1,
        key,
        payload or body(source),
        INFO,
    )


def test_source_merge_uses_manual_override_deletions_and_unknown(source):
    p = a.preview(source, 1, INFO)
    assert (
        p["objectFrames"] == 1
        and p["emptyFrames"] == 1
        and p["unknownFrameRanges"] == [{"start": 2, "end": 2}]
    )
    _, frames = a.source(source, 1, INFO)
    assert list(frames[0]) == [1]
    assert (
        frames[0][1]["bbox"] == [64, 48, 128, 96]
        and frames[0][1]["classKey"] == "sperm"
    )
    with pytest.raises(w.ReviewError) as e:
        a.preview(source, 2, INFO)
    assert e.value.status == 403


def test_completion_requires_full_coverage_and_explicit_confirmation(source):
    with pytest.raises(w.ReviewError, match="未知帧"):
        finish(source, body(source, explicitEmptyFrameRanges=[]))
    with pytest.raises(w.ReviewError, match="已有对象"):
        finish(source, body(source, explicitEmptyFrameRanges=[{"start": 0, "end": 2}]))
    with pytest.raises(w.ReviewError, match="请确认"):
        finish(source, body(source, confirmComplete=False))
    with pytest.raises(w.ReviewError, match="范围超出"):
        finish(source, body(source, explicitEmptyFrameRanges=[{"start": 2, "end": 3}]))


def test_completion_atomic_idempotent_and_independent_from_later_workspace(source):
    b = body(source)
    r = finish(source, b)
    assert (
        r["session"]["state"] == "pending"
        and r["session"]["permissions"]["canClaim"] is True
    )
    sid = r["session"]["id"]
    assert w.get_session(sid, 2)["permissions"]["canClaim"]
    (source / "workspace_state.json").write_text("{}")
    assert finish(source, b) == r
    assert w.get_frame(sid, 0, 2)["baselineObjects"][0]["bbox"] == [64, 48, 128, 96]
    assert len(w.get_session(sid, 2)["resume"]["draftFrameIndexes"]) == 0
    with db.connect() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM baseline_frames WHERE baseline_id=?",
                (r["session"]["baselineId"],),
            ).fetchone()[0]
            == 3
        )


def test_new_key_same_snapshot_reuses_session(source):
    assert (
        finish(source)["session"]["id"]
        == finish(source, key="another")["session"]["id"]
    )


def test_source_revision_changes_on_manual_edits_but_not_view_state(source):
    b = body(source)
    f = source / "workspace_state.json"
    data = json.loads(f.read_text())
    data["currentFrame"] = 1
    data["updatedAt"] = "later"
    f.write_text(json.dumps(data))
    assert a.preview(source, 1, INFO)["sourceRevision"] == b["expectedSourceRevision"]
    data["manualAnnotations"][0]["bbox"]["x"] = 11
    f.write_text(json.dumps(data))
    with pytest.raises(w.ReviewError, match="来源已变化"):
        finish(source, b)


def test_source_rejects_duplicate_identity_and_invalid_json(source):
    f = source / "workspace_state.json"
    data = json.loads(f.read_text())
    data["manualAnnotations"] *= 2
    f.write_text(json.dumps(data))
    with pytest.raises(w.ReviewError, match="重复"):
        a.preview(source, 1, INFO)
    f.write_text("not json")
    with pytest.raises(w.ReviewError, match="先在标注页保存"):
        a.preview(source, 1, INFO)


def test_completion_rolls_back_on_frame_failure(source):
    with db.connect() as c:
        before = c.execute("SELECT COUNT(*) FROM annotation_baselines").fetchone()[0]
        c.execute(
            "CREATE TRIGGER fail_a BEFORE INSERT ON baseline_frames BEGIN SELECT RAISE(ABORT,'failure'); END"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        finish(source)
    with db.connect() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM annotation_baselines").fetchone()[0]
            == before
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM review_write_receipts WHERE request_key='send'"
            ).fetchone()[0]
            == 0
        )


def test_media_referenced_by_a_cannot_be_deleted(task, monkeypatch, tmp_path):
    from app import main
    from fastapi import HTTPException

    monkeypatch.setattr(main, "media_dir", lambda _: tmp_path)
    with pytest.raises(HTTPException) as e:
        main.delete_media("test-video", {"uid": 1})
    assert e.value.status_code == 409 and tmp_path.exists()
