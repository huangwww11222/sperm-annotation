"""Earlier-frame branch restarts use disposable source files and real receipts."""
import json
import logging
from pathlib import Path

import pytest

from app import annotation_state, annotation_completion, db, main
from .test_annotation_controls import controls
from .test_review_workflow import task


@pytest.fixture
def restart_case(controls):
    client, directory, state = controls
    state.update(generationId="restart-generation", anomalyFrames=[{"frame_index": 0}, {"frame_index": 1}, {"frame_index": 2}])
    state["manualAnnotations"][0]["bbox"]["x"] = 12
    state["manualAnnotations"].append({**state["manualAnnotations"][0], "frameIndex": 2, "id": "manual-2-7"})
    state["manualBaselines"] = [{"objectId": 7, "frameIndex": 0, "bbox": [12, 10, 32, 30], "source": "manual"}]
    state["normalMotionSamples"] = [{"objectId": 9, "frameIndex": 0, "reason": "motion", "features": {"motionNormalized": 1.2}}]
    state["trackingFeedbackEvents"] = [{"objectId": 9, "frameIndex": 0, "decision": "normal"}]
    (directory / "workspace_state.json").write_text(json.dumps(state))
    (directory / "annotations_frame_000002.json").write_text('{"manual":"preserve"}')
    (directory / "seed_frame_000002.json").write_text('{"seed":"preserve"}')
    body = {"expectedRevision": 0, "expectedPausedFrame": 1, "startFrame": 0,
            "generationId": "restart-generation", "confirmDiscardFuture": True}
    return client, directory, state, body


def restart(case, body=None, key="restart-request"):
    return case[0].post("/api/track/restart-branch/control-video", json=body or case[3], headers={"Idempotency-Key": key})


def test_restart_cuts_only_future_ai_and_preserves_manual_and_feedback(restart_case, caplog):
    client, directory, original, body = restart_case
    caplog.set_level(logging.INFO)
    protected = {p.name: p.read_bytes() for p in directory.glob("*frame_*.json")}
    result = restart(restart_case)
    assert result.status_code == 200, result.text
    assert result.json()["revision"] == 1
    assert result.json()["removedRows"] == 2 and result.json()["keptRows"] == 1
    assert result.json()["cutoffFrame"] == 0
    current = annotation_state.read_state(directory)
    assert current["pausedAnomalies"] == [] and current["lastPausedContext"] is None
    assert current["anomalyFrames"] == [{"frame_index": 0}]
    for field in ("manualAnnotations", "manualBaselines", "normalMotionSamples", "trackingFeedbackEvents", "deletedObjectIds", "deletedFrameObjects", "generationId"):
        assert current[field] == original[field]
    assert {p.name: p.read_bytes() for p in directory.glob("*frame_*.json")} == protected
    assert [row["source_frame_index"] for row in annotation_state.read_rows(directory)] == [0]
    assert "tracking.branch_restarted" in caplog.text


def test_normal_rewind_cannot_skip_unresolved_pause(restart_case):
    client, directory, state, body = restart_case
    before = (directory / "tracker_results.json").read_bytes()
    response = client.post("/api/track/rewind", json={"mediaId": "control-video", "startFrame": 2, "generationId": state["generationId"]})
    assert response.status_code == 409
    assert (directory / "tracker_results.json").read_bytes() == before


def test_plain_workspace_put_cannot_erase_pause(restart_case):
    client, directory, state, body = restart_case
    payload = {**state, "pausedAnomalies": [], "lastPausedContext": None, "expectedRevision": 0}
    response = client.put("/api/track/workspace/control-video", json=payload, headers={"Idempotency-Key": "erase-pause"})
    assert response.status_code == 200, response.text
    current = annotation_state.read_state(directory)
    assert current["pausedAnomalies"] == state["pausedAnomalies"]
    assert current["lastPausedContext"] == state["lastPausedContext"]


def test_replay_after_new_results_never_truncates_again(restart_case):
    from app import tracking_restart as branch
    first = restart(restart_case)
    assert first.status_code == 200
    directory = restart_case[1]
    path = directory / "tracker_results.json"
    future = {"frame_index": 2, "objects": [{"object_id": 7, "bbox": [12, 10, 32, 30]}]}
    with path.open("a") as stream:
        stream.write(json.dumps(future) + "\n")
    published = path.read_bytes()
    assert restart(restart_case).json() == first.json()
    assert path.read_bytes() == published
    assert restart(restart_case, {**restart_case[3], "expectedRevision": 1}).status_code == 409


@pytest.mark.parametrize("field,value,expected", [("startFrame", -1, 422), ("startFrame", True, 422), ("startFrame", 0.5, 422), ("startFrame", 1, 422), ("expectedPausedFrame", 4, 422), ("expectedRevision", 9, 409), ("expectedPausedFrame", 2, 409), ("confirmDiscardFuture", False, 422), ("generationId", "old", 409)])
def test_invalid_or_stale_restart_preserves_both_files(restart_case, field, value, expected):
    directory = restart_case[1]
    before = {name: (directory / name).read_bytes() for name in ("workspace_state.json", "tracker_results.json")}
    response = restart(restart_case, {**restart_case[3], field: value})
    assert response.status_code == expected, response.text
    assert {name: (directory / name).read_bytes() for name in before} == before


@pytest.mark.parametrize("fail_name", ["tracker_results.json", "workspace_state.json"])
def test_each_file_publication_failure_rolls_back_then_original_request_recovers(restart_case, monkeypatch, fail_name):
    from app import tracking_restart as branch
    directory = restart_case[1]
    before = {name: (directory / name).read_bytes() for name in branch.FILES}
    publish = branch._publish_file
    def fail(staged, target):
        if target.name == fail_name:
            raise OSError("injected publication failure")
        publish(staged, target)
    monkeypatch.setattr(branch, "_publish_file", fail)
    response = restart(restart_case)
    assert response.status_code == 503
    assert {name: (directory / name).read_bytes() for name in branch.FILES} == before
    assert not list((directory.parent / branch.JOURNAL_ROOT).iterdir())
    monkeypatch.setattr(branch, "_publish_file", publish)
    assert restart(restart_case).json()["revision"] == 1


@pytest.mark.parametrize("reader", ["workspace", "results", "completion"])
def test_uncommitted_crash_journal_is_restored_before_any_source_read(restart_case, monkeypatch, reader):
    from app import tracking_restart as branch
    directory = restart_case[1]
    before = {name: (directory / name).read_bytes() for name in branch.FILES}
    publish, recover = branch._publish_file, branch.recover_directory
    monkeypatch.setattr(branch, "recover_directory", lambda _: None)
    def half_publish(staged, target):
        if target.name == "workspace_state.json":
            raise OSError("simulated process interruption before SQL commit")
        publish(staged, target)
    monkeypatch.setattr(branch, "_publish_file", half_publish)
    assert restart(restart_case).status_code == 503
    assert (directory / "tracker_results.json").read_bytes() != before["tracker_results.json"]
    monkeypatch.setattr(branch, "recover_directory", recover)
    if reader == "workspace":
        assert restart_case[0].get("/api/track/workspace/control-video").status_code == 200
    elif reader == "results":
        assert restart_case[0].get("/api/track/result/control-video").status_code == 200
    else:
        annotation_completion.source(directory, 1, main.source_media_info(directory))
    assert {name: (directory / name).read_bytes() for name in branch.FILES} == before


def test_committed_crash_cleanup_does_not_restore_over_later_edits(restart_case, monkeypatch):
    from app import tracking_restart as branch
    directory = restart_case[1]
    recover = branch.recover_directory
    monkeypatch.setattr(branch, "recover_directory", lambda _: None)
    assert restart(restart_case).status_code == 200
    current = json.loads((directory / "workspace_state.json").read_text())
    current.update(revision=5, currentFrame=2)
    (directory / "workspace_state.json").write_text(json.dumps(current))
    monkeypatch.setattr(branch, "recover_directory", recover)
    assert restart_case[0].get("/api/track/workspace/control-video").json()["revision"] == 5
    assert not list((directory.parent / branch.JOURNAL_ROOT).iterdir())
    assert restart(restart_case).json()["revision"] == 1
    assert annotation_state.read_state(directory)["revision"] == 5


def test_busy_and_submitted_sources_reject_without_truncation(restart_case, monkeypatch):
    from app.review_repository import create_review_session, freeze_baseline, upsert_media_revision
    directory = restart_case[1]
    original = (directory / "tracker_results.json").read_bytes()
    monkeypatch.setattr(main, "TASKS", {"busy": {"status": "running"}})
    assert restart(restart_case).status_code == 409
    monkeypatch.setattr(main, "TASKS", {})
    mid = upsert_media_revision("control-video", "c" * 64, 10, 100, 100, 30, 4)
    baseline = freeze_baseline(mid, "control-video", 1, [dict(frameIndex=i, coverage="empty", objects=[]) for i in range(4)])
    create_review_session(baseline["id"])
    assert restart(restart_case).status_code == 409
    assert (directory / "tracker_results.json").read_bytes() == original


@pytest.mark.parametrize("fault", ["no_seed", "no_result", "corrupt_result", "fake_pause"])
def test_restart_requires_real_pause_and_valid_seed(restart_case, fault):
    directory = restart_case[1]
    state = restart_case[2]
    if fault == "no_seed":
        state["deletedFrameObjects"] = [{"objectId": oid, "frameIndex": 0} for oid in (7, 9)]
        (directory / "workspace_state.json").write_text(json.dumps(state))
    elif fault == "no_result":
        (directory / "tracker_results.json").unlink()
    elif fault == "corrupt_result":
        (directory / "tracker_results.json").write_text("bad json")
    else:
        rows = [json.loads(line) for line in (directory / "tracker_results.json").read_text().splitlines()]
        for obj in rows[1]["objects"]:
            obj["anomaly_level"] = "normal"
        (directory / "tracker_results.json").write_text("\n".join(map(json.dumps, rows)))
    before = (directory / "workspace_state.json").read_bytes()
    assert restart(restart_case).status_code in {409, 422, 500}
    assert (directory / "workspace_state.json").read_bytes() == before


def test_deletion_can_clear_pause_and_undo_restores_server_candidates(restart_case):
    client, directory, state, _ = restart_case
    delete = {**state, "expectedRevision": 0, "deletedObjectIds": [7, 9], "pausedAnomalies": [], "lastPausedContext": None}
    assert client.put("/api/track/workspace/control-video", json=delete, headers={"Idempotency-Key": "delete-paused"}).status_code == 200
    assert annotation_state.read_state(directory)["pausedAnomalies"] == []
    undo = {**state, "expectedRevision": 1, "deletedObjectIds": []}
    assert client.put("/api/track/workspace/control-video", json=undo, headers={"Idempotency-Key": "undo-paused"}).status_code == 200
    assert annotation_state.read_state(directory)["pausedAnomalies"] == state["pausedAnomalies"]
    assert client.post("/api/track/rewind", json={"mediaId": "control-video", "startFrame": 0, "generationId": state["generationId"]}).status_code == 409


def test_real_task_pause_is_persisted_without_incrementing_editor_revision(restart_case):
    client, directory, state, _ = restart_case
    annotation_state.publish_tracking_pause(directory, "control-video", {"frame_index": 2, "reasons": [{"object_id": 7, "display_name": "测试对象", "type": "size_shrink", "reasons": ["manual_height_ratio=0.59 HARD"]}]})
    restored = client.get("/api/track/workspace/control-video").json()
    assert restored["revision"] == 0
    assert restored["lastPausedContext"]["frameIndex"] == 2
    item = restored["pausedAnomalies"][0]
    assert item["objectId"] == 7 and item["displayName"] == "测试对象" and item["acceptsGeometry"]
    assert item["title"] and item["rawReasons"] and item["summary"]


def test_restart_requires_authentication_and_idempotency_key(restart_case):
    client = restart_case[0]
    assert client.post("/api/track/restart-branch/control-video", json=restart_case[3]).status_code == 428
    client.headers.pop("Authorization")
    assert restart(restart_case).status_code == 401


def test_point_only_seed_cannot_discard_future_results(restart_case):
    client, directory, state, _ = restart_case
    state["manualAnnotations"] = [{"objectId": 3, "source": "manual", "frameIndex": 0, "point": {"x": 10, "y": 10}}]
    state["deletedFrameObjects"] = [{"objectId": oid, "frameIndex": 0} for oid in (7, 9)]
    (directory / "workspace_state.json").write_text(json.dumps(state))
    before = (directory / "tracker_results.json").read_bytes()
    assert restart(restart_case).status_code == 422
    assert (directory / "tracker_results.json").read_bytes() == before


def test_pause_publication_keeps_legacy_manual_seed_restore(restart_case):
    client, directory, _, _ = restart_case
    (directory / "workspace_state.json").unlink()
    for path in directory.glob("annotations_frame_*.json"):
        path.unlink()
    manual = {"media": {"width": 100, "height": 100}, "frame": {"frameIndex": 0}, "annotations": [{"object_id": 7, "frameIndex": 0, "source": "manual", "bbox": [10, 10, 30, 30]}]}
    (directory / "annotations_frame_000000.json").write_text(json.dumps(manual))
    annotation_state.publish_tracking_pause(directory, "control-video", None)
    restored = client.get("/api/track/workspace/control-video").json()
    assert len(restored["manualAnnotations"]) == len(restored["manualBaselines"]) == 1
    assert restored["manualAnnotations"][0]["objectId"] == 7


def test_result_published_but_pause_write_fails_restores_real_pause_on_read(restart_case, monkeypatch):
    from app import tracker
    client, directory, state, _ = restart_case
    state.update(pausedAnomalies=[], lastPausedContext=None)
    (directory / "workspace_state.json").write_text(json.dumps(state))
    rows = [{"frame_index": 2, "source_frame_index": 2, "objects": [{"object_id": 7, "bbox": [10, 10, 30, 30], "anomaly_level": "anomaly"}]}]
    pause = {"frame_index": 2, "reasons": [{"object_id": 7, "type": "tracking_motion", "reasons": ["motion_jump HARD"]}]}
    publish = annotation_state.publish_tracking_pause
    monkeypatch.setattr(annotation_state, "publish_tracking_pause", lambda *a: (_ for _ in ()).throw(OSError("pause publication failed")))
    with pytest.raises(Exception):
        tracker._merge_rows(directory / "tracker_results.json", rows, keep_before_source_frame=2, pause=pause)
    assert (directory / annotation_state.PAUSE_PENDING_FILE).is_file()
    monkeypatch.setattr(annotation_state, "publish_tracking_pause", publish)
    restored = client.get("/api/track/workspace/control-video").json()
    assert restored["lastPausedContext"]["frameIndex"] == 2
    assert restored["pausedAnomalies"][0]["objectId"] == 7
    assert client.post("/api/track/rewind", json={"mediaId": "control-video", "startFrame": 0, "generationId": state["generationId"]}).status_code == 409
    assert not (directory / annotation_state.PAUSE_PENDING_FILE).exists()


@pytest.mark.parametrize("legacy_manual", [False, True])
def test_first_workspace_pause_read_retries_publication_and_preserves_legacy_manual(controls, monkeypatch, legacy_manual):
    from fastapi import HTTPException
    from app import tracker
    client, directory, _ = controls
    (directory / "workspace_state.json").unlink()
    (directory / "tracker_results.json").unlink()
    if legacy_manual:
        manual = {"media": {"width": 100, "height": 100}, "frame": {"frameIndex": 0}, "annotations": [{"object_id": 7, "frameIndex": 0, "source": "manual", "bbox": [10, 10, 30, 30]}]}
        (directory / "annotations_frame_000000.json").write_text(json.dumps(manual))
    rows = [{"frame_index": 1, "source_frame_index": 1, "objects": [{"object_id": 7, "bbox": [10, 10, 30, 30], "anomaly_level": "anomaly"}]}]
    pause = {"frame_index": 1, "reasons": [{"object_id": 7, "type": "tracking_motion", "reasons": ["motion_jump HARD"]}]}
    publish = annotation_state.publish_tracking_pause
    monkeypatch.setattr(annotation_state, "publish_tracking_pause", lambda *a: (_ for _ in ()).throw(OSError("first workspace publication failed")))
    with pytest.raises(HTTPException) as failed:
        tracker._merge_rows(directory / "tracker_results.json", rows, pause=pause)
    assert failed.value.status_code == 503
    marker = directory / annotation_state.PAUSE_PENDING_FILE
    assert marker.is_file() and not (directory / "workspace_state.json").exists()
    assert client.get("/api/track/workspace/control-video").status_code == 503
    assert marker.is_file() and not (directory / "workspace_state.json").exists()
    monkeypatch.setattr(annotation_state, "publish_tracking_pause", publish)
    response = client.get("/api/track/workspace/control-video")
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["exists"] and restored["revision"] == 0
    assert restored["lastPausedContext"]["frameIndex"] == 1
    assert restored["pausedAnomalies"][0]["objectId"] == 7
    assert len(restored.get("manualAnnotations", [])) == int(legacy_manual)
    assert len(restored.get("manualBaselines", [])) == int(legacy_manual)
    if legacy_manual:
        assert restored["manualAnnotations"][0]["objectId"] == 7
        assert restored["manualAnnotations"][0]["bbox"] == {"x": 10, "y": 10, "width": 20, "height": 20}
    assert not marker.exists()


def test_unpublished_pause_intent_does_not_accept_or_erase_current_pause(restart_case):
    client, directory, state, _ = restart_case
    annotation_state.prepare_pause_publication(directory, [{"source_frame_index": 9, "frame_index": 9, "objects": []}], None)
    restored = client.get("/api/track/workspace/control-video").json()
    assert restored["pausedAnomalies"] == state["pausedAnomalies"]
    assert not (directory / annotation_state.PAUSE_PENDING_FILE).exists()


@pytest.mark.parametrize("failure", ["workspace_fsync", "directory_fsync"])
def test_pause_sync_failure_keeps_intent_until_read_recovers(restart_case, monkeypatch, failure):
    from fastapi import HTTPException
    from app import tracker, tracking_restart as branch
    client, directory, state, _ = restart_case
    rows = annotation_state.read_rows(directory)
    tracker._write_jsonl(directory / "tracker_results.json", rows)
    state.update(pausedAnomalies=[], lastPausedContext=None)
    (directory / "workspace_state.json").write_text(json.dumps(state))
    pause = {"frame_index": 1, "reasons": [{"object_id": 7, "type": "tracking_motion", "reasons": ["motion_jump HARD"]}]}
    annotation_state.prepare_pause_publication(directory, rows, pause)
    marker = directory / annotation_state.PAUSE_PENDING_FILE
    if failure == "workspace_fsync":
        owner, name = annotation_state.os, "fsync"
    else:
        owner, name = branch, "_sync_directory"
    original_sync = getattr(owner, name)
    monkeypatch.setattr(owner, name, lambda *a: (_ for _ in ()).throw(OSError("injected durability failure")))
    with pytest.raises(HTTPException) as failed:
        annotation_state.recover_pause_publication(directory)
    assert failed.value.status_code == 503
    assert marker.is_file()
    monkeypatch.setattr(owner, name, original_sync)
    restored = client.get("/api/track/workspace/control-video").json()
    assert restored["revision"] == 0
    assert restored["lastPausedContext"]["frameIndex"] == 1
    assert restored["pausedAnomalies"][0]["objectId"] == 7
    assert not marker.exists()
    assert client.post("/api/track/rewind", json={"mediaId": "control-video", "startFrame": 0, "generationId": state["generationId"]}).status_code == 409


def test_applied_pause_intent_cleanup_retry_never_reopens_confirmed_pause(restart_case, monkeypatch):
    from app import tracker
    client, directory, state, _ = restart_case
    rows = annotation_state.read_rows(directory)
    pause = {"frame_index": 1, "reasons": [{"object_id": oid, "type": "tracking_motion", "reasons": ["motion_jump HARD"]} for oid in (7, 9)]}
    original_unlink = Path.unlink
    def pending_cleanup_fails(path, *args, **kwargs):
        if path.name == annotation_state.PAUSE_PENDING_FILE:
            raise OSError("injected pending cleanup failure")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", pending_cleanup_fails)
    tracker._merge_rows(directory / "tracker_results.json", rows, pause=pause)
    assert (directory / annotation_state.PAUSE_PENDING_FILE).is_file()
    for revision, oid in enumerate((7, 9)):
        response = client.post("/api/track/feedback/control-video", json={"expectedRevision": revision, "objectId": oid, "frameIndex": 1, "decision": "normal", "calibrate": False}, headers={"Idempotency-Key": f"confirm-{oid}"})
        assert response.status_code == 200, response.text
    assert annotation_state.read_state(directory)["pausedAnomalies"] == []
    monkeypatch.setattr(Path, "unlink", original_unlink)
    restored = client.get("/api/track/workspace/control-video").json()
    assert restored["pausedAnomalies"] == [] and restored["lastPausedContext"] is None
    assert restored["revision"] == 2
    assert not (directory / annotation_state.PAUSE_PENDING_FILE).exists()


def test_discarded_pause_cannot_return_when_deletion_is_undone(restart_case):
    client, directory, state, _ = restart_case
    deleted = {**state, "deletedObjectIds": [7, 9], "expectedRevision": 0}
    assert client.put("/api/track/workspace/control-video", json=deleted, headers={"Idempotency-Key": "delete-for-rewind"}).status_code == 200
    assert client.post("/api/track/rewind", json={"mediaId": "control-video", "startFrame": 0, "generationId": state["generationId"]}).status_code == 200
    assert client.put("/api/track/workspace/control-video", json={**state, "expectedRevision": 1}, headers={"Idempotency-Key": "undo-after-failed-run"}).status_code == 200
    restored = annotation_state.read_state(directory)
    assert restored["pausedAnomalies"] == [] and restored["lastPausedContext"] is None


def test_receipt_insert_failure_restores_both_files(restart_case, monkeypatch):
    from app import tracking_restart as branch
    directory = restart_case[1]
    before = {name: (directory / name).read_bytes() for name in branch.FILES}
    connect = branch.connect
    class Connection:
        def __init__(self): self.raw = connect()
        def __enter__(self): self.raw.__enter__(); return self
        def __exit__(self, *args): return self.raw.__exit__(*args)
        def close(self): self.raw.close()
        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO review_write_receipts"):
                raise OSError("injected receipt insert failure")
            return self.raw.execute(sql, *args)
        def commit(self): self.raw.commit()
    monkeypatch.setattr(branch, "connect", Connection)
    assert restart(restart_case).status_code == 503
    assert {name: (directory / name).read_bytes() for name in branch.FILES} == before
    monkeypatch.setattr(branch, "connect", connect)
    assert restart(restart_case).json()["revision"] == 1


def test_partial_rollback_directory_cleanup_can_be_retried(restart_case, monkeypatch):
    from app import tracking_restart as branch
    directory = restart_case[1]
    before = {name: (directory / name).read_bytes() for name in branch.FILES}
    publish, remove = branch._publish_file, branch.shutil.rmtree
    def fail(staged, target):
        if target.name == "workspace_state.json":
            raise OSError("injected publication failure")
        publish(staged, target)
    def partially_remove(folder, *args, **kwargs):
        (Path(folder) / "tracker_results.json.old").unlink(missing_ok=True)
        raise OSError("injected partial directory cleanup")
    monkeypatch.setattr(branch, "_publish_file", fail)
    monkeypatch.setattr(branch.shutil, "rmtree", partially_remove)
    assert restart(restart_case).status_code == 503
    assert {name: (directory / name).read_bytes() for name in branch.FILES} == before
    monkeypatch.setattr(branch.shutil, "rmtree", remove)
    assert restart_case[0].get("/api/track/workspace/control-video").status_code == 200
    assert not list((directory.parent / branch.JOURNAL_ROOT).iterdir())
