"""Deletion scopes and human-feedback writes use isolated source files."""
import json
import logging
from pathlib import Path
import cv2
import numpy as np

import pytest
from fastapi.testclient import TestClient

from app import annotation_completion, annotation_state, main
from app.auth import sign_jwt
from .test_review_workflow import task


@pytest.fixture
def controls(tmp_path, monkeypatch, task):
    monkeypatch.setattr(main, "TRACK_DATA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "LEGACY_TRACK_DATA_DIR", tmp_path / "legacy")
    monkeypatch.setattr(main, "TASKS", {})
    root = main.TRACK_DATA_DIR / "control-video"
    root.mkdir(parents=True)
    writer = cv2.VideoWriter(str(root / "video.avi"), cv2.VideoWriter_fourcc(*"MJPG"), 30, (100, 100))
    for _ in range(4):
        writer.write(np.zeros((100, 100, 3), dtype=np.uint8))
    writer.release()
    (root / "media.json").write_text(json.dumps({"width": 100, "height": 100, "fps": 30, "frameCount": 4}))
    annotations = [{"id": "manual-0-7", "objectId": 7, "frameIndex": 0, "source": "manual", "name": "sperm7", "bbox": {"x": 10, "y": 10, "width": 20, "height": 20}}]
    state = {"updatedBy": 1, "revision": 0, "manualAnnotations": annotations, "manualBaselines": [], "deletedTrackingIds": [], "deletedFrameObjects": [], "deletedObjectIds": [], "pausedAnomalies": [{"object_id": 7}, {"object_id": 9}], "lastPausedContext": {"mediaId": "control-video", "frameIndex": 1}}
    (root / "workspace_state.json").write_text(json.dumps(state))
    rows = [{"frame_index": frame, "objects": [{"object_id": oid, "name": f"sperm{oid}", "bbox": [10, 10, 30, 30], "anomaly_level": "anomaly", "anomaly_reasons": ["adjacent_center_shift=45.0px normalized=1.4 sustained_motion HARD"], "anomaly_details": {"motion_normalized": 1.4}} for oid in (7, 9)]} for frame in (0, 1, 2)]
    (root / "tracker_results.json").write_text("\n".join(map(json.dumps, rows)))
    client = TestClient(main.app)
    client.headers["Authorization"] = "Bearer " + sign_jwt({"uid": 1})
    return client, root, state


def save(client, state, revision=0, key="write-1"):
    return client.put("/api/track/workspace/control-video", json={**state, "expectedRevision": revision}, headers={"Idempotency-Key": key})


def feedback(client, **body):
    key = body.pop("key", "feedback-1")
    return client.post("/api/track/feedback/control-video", json={"expectedRevision": 0, "objectId": 7, "frameIndex": 1, "decision": "normal", "calibrate": True, **body}, headers={"Idempotency-Key": key})


def test_preview_deduplicates_manual_override_and_counts_real_frames(controls):
    client, root, state = controls
    preview = client.get("/api/track/deletion-preview/control-video/7").json()
    assert (preview["totalCount"], preview["manualCount"], preview["aiCount"]) == (3, 1, 2)
    assert (preview["firstFrame"], preview["lastFrame"]) == (0, 2)


def test_single_frame_deletion_survives_reload_and_all_result_readers(controls):
    client, root, state = controls
    state["deletedFrameObjects"] = [{"objectId": 7, "frameIndex": 1}]
    assert save(client, state).status_code == 200
    restored = client.get("/api/track/workspace/control-video").json()
    assert restored["deletedFrameObjects"] == state["deletedFrameObjects"]
    assert "_writeReceipts" not in restored
    frames = client.get("/api/track/result/control-video").json()["frames"]
    assert [a["objectId"] for a in frames[1]["annotations"]] == [9]
    assert len(frames[0]["annotations"]) == len(frames[2]["annotations"]) == 2
    lines = client.get("/api/track/result-file/control-video").text.splitlines()
    assert [a["object_id"] for a in json.loads(lines[1])["objects"]] == [9]
    assert len(json.loads((root / "tracker_results.json").read_text().splitlines()[1])["objects"]) == 2


def test_whole_object_delete_undo_keeps_other_ids_and_raw_history(controls):
    client, root, state = controls
    original = (root / "tracker_results.json").read_bytes()
    state["deletedObjectIds"] = [7]
    assert save(client, state).json()["revision"] == 1
    frames = client.get("/api/track/result/control-video").json()["frames"]
    assert all([a["objectId"] for a in frame["annotations"]] == [9] for frame in frames)
    assert client.get("/api/track/deletion-preview/control-video/7").json()["totalCount"] == 0
    state["deletedObjectIds"] = []
    assert save(client, state, 1, "undo").status_code == 200
    assert client.get("/api/track/deletion-preview/control-video/7").json()["totalCount"] == 3
    assert (root / "tracker_results.json").read_bytes() == original


def test_workspace_replay_stale_version_and_same_key_different_body(controls):
    client, root, state = controls
    first = save(client, state)
    assert first.status_code == 200
    assert save(client, state).json() == first.json()
    assert save(client, state, 0, "stale").status_code == 409
    assert save(client, {**state, "currentFrame": 2}, 0).status_code == 409
    assert annotation_state.read_state(root)["revision"] == 1


def test_legacy_client_cannot_erase_new_deletion_or_feedback_fields(controls):
    client, root, state = controls
    assert feedback(client).status_code == 200
    state["deletedObjectIds"] = [7]
    assert save(client, state, 1).status_code == 200
    old = {"manualAnnotations": state["manualAnnotations"], "deletedTrackingIds": [], "normalMotionSamples": []}
    assert client.put("/api/track/workspace/control-video", json=old).status_code == 200
    restored = annotation_state.read_state(root)
    assert restored["deletedObjectIds"] == [7]
    assert restored["normalMotionSamples"] and restored["manualAnnotations"] == []
    assert client.put("/api/track/workspace/control-video", json={**old, "deletedObjectIds": []}).status_code == 428


def test_unversioned_request_cannot_add_new_deletion_rules(controls):
    client, root, state = controls
    assert client.put("/api/track/workspace/control-video", json={"deletedObjectIds": [7]}).status_code == 428
    assert annotation_state.read_state(root)["revision"] == 0


@pytest.mark.parametrize('legacy_id', ['7','ai-0-7'])
def test_unversioned_request_cannot_add_legacy_deletion_rules(controls, legacy_id):
    client, root, state = controls
    before = (root/'workspace_state.json').read_bytes()
    response = client.put('/api/track/workspace/control-video', json={'deletedTrackingIds':[legacy_id]})
    assert response.status_code == 428
    assert (root/'workspace_state.json').read_bytes() == before


def test_unversioned_stale_legacy_deletions_preserve_current_rules_without_adding(controls):
    client, root, state = controls
    state['deletedTrackingIds'] = ['7']
    assert save(client,state).status_code == 200
    # An old cached client may omit existing deletions or resend only a subset.
    assert client.put('/api/track/workspace/control-video',json={'deletedTrackingIds':[]}).status_code == 200
    assert annotation_state.read_state(root)['deletedTrackingIds'] == ['7']
    before = (root/'workspace_state.json').read_bytes()
    assert client.put('/api/track/workspace/control-video',json={'deletedTrackingIds':['7','9']}).status_code == 428
    assert (root/'workspace_state.json').read_bytes() == before
    current = annotation_state.read_state(root)
    assert save(client,{**current,'deletedTrackingIds':[]},current['revision'],'restore-versioned').status_code == 200
    assert annotation_state.read_state(root)['deletedTrackingIds'] == []


def test_normal_feedback_uses_server_metrics_and_clears_only_selected_issue(controls):
    client, root, state = controls
    result = feedback(client, motionNormalized=999).json()
    assert result["revision"] == 1
    assert result["normalMotionSamples"] == [{"objectId": 7, "frameIndex": 1, "reason": "motion", "decision": "normal", "calibrate": True, "features": {"motionNormalized": 1.4}}]
    assert result["pausedAnomalies"] == [{"object_id": 9}]
    assert result["lastPausedContext"]["frameIndex"] == 1
    replay = feedback(client, motionNormalized=999)
    assert replay.json() == result
    second = feedback(client, objectId=9, expectedRevision=1, key="second").json()
    assert second["pausedAnomalies"] == [] and second["lastPausedContext"] is None


def test_confirmation_once_and_reset_do_not_learn_or_delete_audit(controls):
    client, root, state = controls
    assert feedback(client, calibrate=False).json()["normalMotionSamples"] == []
    assert feedback(client, objectId=9, expectedRevision=1, key="learn").json()["normalMotionSamples"]
    reset = feedback(client, objectId=9, decision="reset", expectedRevision=2, key="reset").json()
    assert reset["normalMotionSamples"] == []
    assert [e["decision"] for e in reset["trackingFeedbackEvents"]] == ["normal", "normal", "reset"]


def test_corrected_feedback_requires_an_actual_geometry_change_and_never_calibrates(controls):
    client, root, state = controls
    assert feedback(client, decision="corrected").status_code == 409
    state["manualAnnotations"].append({**state["manualAnnotations"][0], "frameIndex": 1})
    assert save(client, state).status_code == 200
    assert feedback(client, decision="corrected", expectedRevision=1).status_code == 409
    state["manualAnnotations"][1]["bbox"] = {"x": 11, "y": 10, "width": 20, "height": 20}
    assert save(client, state, 1, "correct-box").status_code == 200
    result = feedback(client, decision="corrected", expectedRevision=2).json()
    assert result["normalMotionSamples"] == []
    assert result["trackingFeedbackEvents"][0]["decision"] == "corrected"


def test_tampered_workspace_cannot_inject_calibration(controls):
    client, root, state = controls
    state["normalMotionSamples"] = [{"features": {"motionNormalized": 999}}]
    state["trackingFeedbackEvents"] = [{"objectId": 7, "decision": "normal", "geometryReference": {"bbox": [0, 0, 999, 999]}}]
    assert save(client, state).status_code == 200
    assert not annotation_state.read_state(root).get("normalMotionSamples")
    assert not annotation_state.read_state(root).get("trackingFeedbackEvents")


def test_failed_atomic_replace_keeps_prior_state_and_same_request_retries(controls, monkeypatch, caplog):
    client, root, state = controls
    original = (root / "workspace_state.json").read_bytes()
    def fail(*args, **kwargs):
        raise PermissionError("injected replace failure")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail)
        with caplog.at_level(logging.ERROR, logger="review.annotation"):
            result = save(client, state)
    assert result.status_code == 500
    assert "annotation.workspace_save_failed" in caplog.text
    assert (root / "workspace_state.json").read_bytes() == original
    assert save(client, state).json()["revision"] == 1


def test_completion_filters_manual_and_tracking_and_keeps_deleted_only_frame_known(controls):
    client, root, state = controls
    state["manualAnnotations"].append({**state["manualAnnotations"][0], "frameIndex": 3})
    assert save(client, state).status_code == 200
    state["deletedObjectIds"] = [7]
    state["manualAnnotations"] = []
    assert save(client, state, 1, "delete-all").status_code == 200
    _, frames = annotation_completion.source(root, 1, {"width": 100, "height": 100, "frameCount": 4})
    assert set(frames) == {0, 1, 2, 3} and frames[3] == {}
    assert all(7 not in objects for objects in frames.values())


def test_seed_and_direct_tracking_reject_tombstoned_object(controls, monkeypatch):
    client, root, state = controls
    state["deletedObjectIds"] = [7]
    assert save(client, state).status_code == 200
    monkeypatch.setattr(main, "SAM3_ENABLED", True)
    seed = {"mediaId": "control-video", "frameIndex": 1, "annotations": [{"object_id": 7, "bbox": [10, 10, 30, 30]}]}
    assert client.post("/api/track/annotations", json=seed).status_code == 409
    assert client.post("/api/track", json={**seed, "startFrame": 1}).status_code == 409


def test_corrupt_tracking_is_not_an_empty_successful_deletion_preview(controls):
    client, root, state = controls
    (root / "tracker_results.json").write_text("invalid json")
    assert client.get("/api/track/deletion-preview/control-video/7").status_code == 500
    assert client.get("/api/track/result/control-video").status_code == 500


def test_tracking_queued_allows_ui_state_but_rejects_new_deletion(controls, monkeypatch):
    client, root, state = controls
    monkeypatch.setattr(main, "TASKS", {"queued-test": {"status": "queued"}})
    assert save(client, {**state, "currentFrame": 2}).status_code == 200
    state["deletedObjectIds"] = [7]
    assert save(client, state, 1, "delete-while-queued").status_code == 409
    assert annotation_state.read_state(root)["deletedObjectIds"] == []


def test_unresolved_pause_blocks_rewind_before_truncating_results(controls, monkeypatch):
    client, root, state = controls
    monkeypatch.setattr(main, "SAM3_ENABLED", True)
    before = (root / "tracker_results.json").read_bytes()
    response = client.post("/api/track/rewind", json={"mediaId": "control-video", "startFrame": 1})
    assert response.status_code == 409 and "逐个明确确认" in response.json()["detail"]
    assert (root / "tracker_results.json").read_bytes() == before


def test_deleting_paused_object_removes_it_from_continuation_gate(controls, monkeypatch):
    client, root, state = controls
    state["deletedObjectIds"] = [7, 9]
    assert save(client, state).status_code == 200
    main._require_pause_resolved(annotation_state.read_state(root), 1)


def test_feedback_cannot_accept_missing_deleted_or_non_anomalous_objects(controls):
    client, root, state = controls
    assert feedback(client, objectId=42).status_code == 409
    assert feedback(client, frameIndex=99).status_code == 409
    state["deletedFrameObjects"] = [{"objectId": 7, "frameIndex": 1}]
    assert save(client, state).status_code == 200
    assert feedback(client, expectedRevision=1).status_code == 422


def test_legacy_stable_frame_id_is_applied_to_manual_and_ai(controls):
    client, root, state = controls
    state["deletedTrackingIds"] = ["ai-0-7"]
    assert save(client, state).status_code == 200
    _, frames = annotation_completion.source(root, 1, {"width": 100, "height": 100, "frameCount": 4})
    assert 7 not in frames[0] and 7 in frames[1]


def test_deleting_one_paused_object_then_confirming_other_clears_remaining_blocker(controls):
    client, root, state = controls
    # Mimic an older pause panel that retained presentation data for a removed box.
    state["pausedAnomalies"] = [{"objectId": 7, "title": "A"}, {"objectId": 9, "title": "B"}]
    state["deletedFrameObjects"] = [{"objectId": 7, "frameIndex": 1}]
    assert save(client, state).status_code == 200
    result = feedback(client, objectId=9, expectedRevision=1).json()
    assert result["pausedAnomalies"] == [] and result["lastPausedContext"] is None
    main._require_pause_resolved(annotation_state.read_state(root), 1)


def test_deletion_in_another_frame_does_not_hide_current_pause(controls):
    client, root, state = controls
    state["deletedFrameObjects"] = [{"objectId": 7, "frameIndex": 0}]
    assert save(client, state).status_code == 200
    result = feedback(client, objectId=9, expectedRevision=1).json()
    assert result["pausedAnomalies"] == [{"object_id": 7}]
    assert result["lastPausedContext"]["frameIndex"] == 1


def test_resolved_anomaly_cannot_be_confirmed_again_with_new_intent(controls):
    client, root, state = controls
    assert feedback(client).status_code == 200
    repeated = feedback(client, expectedRevision=1, key="new-feedback-key")
    assert repeated.status_code == 409 and "当前待确认暂停" in repeated.json()["detail"]
    restored = annotation_state.read_state(root)
    assert restored["revision"] == 1 and len(restored["trackingFeedbackEvents"]) == 1


def test_old_warning_or_different_paused_frame_cannot_calibrate_current_motion(controls):
    client, root, state = controls
    assert feedback(client, frameIndex=0).status_code == 409
    state["pausedAnomalies"] = []
    state["lastPausedContext"] = None
    assert save(client, state).status_code == 200
    assert feedback(client, expectedRevision=1).status_code == 409
    assert annotation_state.read_state(root).get("normalMotionSamples", []) == []


def test_resolved_legacy_pause_item_is_not_a_pending_anomaly(controls):
    client, root, state = controls
    state["pausedAnomalies"] = [{"objectId": 7, "resolved": True}]
    assert save(client, state).status_code == 200
    assert feedback(client, expectedRevision=1).status_code == 409


def shape_pause(root):
    """The screenshot case: same accepted shape would fail every resumed frame."""
    rows = annotation_state.read_rows(root)
    obj = rows[1]["objects"][0]
    obj.update(bbox=[10, 10, 32.2, 21.8],
               anomaly_reasons=["manual_height_ratio=0.590 HARD", "manual_aspect_change=1.881 HARD"],
               anomaly_details={"current_bbox": [10, 10, 32.2, 21.8], "manual_baseline_frame": 0})
    (root / "tracker_results.json").write_text("\n".join(map(json.dumps, rows)))
    return obj["bbox"]


def test_normal_size_confirmation_saves_server_reference_without_redrawing(controls, caplog):
    client, root, state = controls
    expected_box = shape_pause(root)
    with caplog.at_level(logging.INFO, logger="review.annotation"):
        response = feedback(client, calibrate=False, geometryReference={"bbox": [0, 0, 999, 999]})
    assert response.status_code == 200
    body = response.json()
    event = body["trackingFeedbackEvents"][-1]
    assert event["geometryReference"] == {"objectId": 7, "frameIndex": 1, "bbox": expected_box, "source": "confirmed-normal"}
    assert body["normalMotionSamples"] == []
    stored = annotation_state.read_state(root)
    assert stored["manualAnnotations"] == state["manualAnnotations"]
    assert stored["manualBaselines"] == state["manualBaselines"]
    assert body["pausedAnomalies"] == [{"object_id": 9}]
    assert "geometry_reference_frame=1" in caplog.text
    assert feedback(client, calibrate=False, geometryReference={"bbox": [0, 0, 999, 999]}).json() == body
    # Stale clients cannot overwrite this server-owned audit/reference.
    assert client.put("/api/track/workspace/control-video", json={"trackingFeedbackEvents": [], "manualAnnotations": state["manualAnnotations"]}).status_code == 200
    assert annotation_state.read_state(root)["trackingFeedbackEvents"][-1] == event


def test_size_confirmation_atomic_failure_keeps_pause_and_retry_creates_one_reference(controls, monkeypatch):
    client, root, state = controls
    shape_pause(root)
    original = (root / "workspace_state.json").read_bytes()
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("injected geometry feedback replacement failure")
        patch.setattr(Path, "replace", fail)
        assert feedback(client).status_code == 500
    assert (root / "workspace_state.json").read_bytes() == original
    assert feedback(client).status_code == 200
    assert feedback(client).status_code == 200
    events = annotation_state.read_state(root)["trackingFeedbackEvents"]
    assert len(events) == 1 and "geometryReference" in events[0]


@pytest.mark.parametrize("box", [None, [1, 2, 3], [1, 2, 1, 10], [1, 2, "invalid", 10]])
def test_size_confirmation_rejects_invalid_server_geometry(controls, box):
    client, root, state = controls
    shape_pause(root)
    rows = annotation_state.read_rows(root)
    rows[1]["objects"][0]["bbox"] = box
    (root / "tracker_results.json").write_text("\n".join(map(json.dumps, rows)))
    assert feedback(client).status_code == 422
    assert annotation_state.read_state(root)["revision"] == 0


def test_normal_motion_feedback_does_not_rebase_shape(controls):
    client, root, state = controls
    assert "geometryReference" not in feedback(client).json()["trackingFeedbackEvents"][-1]


def test_combined_shape_and_motion_confirmation_records_independent_evidence(controls):
    client, root, state = controls
    shape_pause(root)
    rows = annotation_state.read_rows(root)
    obj = rows[1]["objects"][0]
    obj["anomaly_reasons"].append("adjacent_center_shift=45 normalized=1.4 sustained_motion HARD")
    obj["anomaly_details"]["motion_normalized"] = 1.4
    (root / "tracker_results.json").write_text("\n".join(map(json.dumps, rows)))
    result = feedback(client, calibrate=True).json()
    event = result["trackingFeedbackEvents"][-1]
    assert event["sample"]["features"]["motionNormalized"] == 1.4
    assert event["geometryReference"]["bbox"] == obj["bbox"]
    assert result["normalMotionSamples"][0] == event["sample"]
