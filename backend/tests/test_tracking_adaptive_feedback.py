"""Tracking orchestration with a fake inference engine, never a GPU claim."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.tracker import track_video


class FakeEngine:
    model_id = "test-model"
    device = "cpu"
    torch_dtype = "float32"

    def __init__(self, boxes):
        self.boxes = boxes
        self.seed_ids = []

    def make_tracker_session(self, frames):
        return object()

    def add_manual_boxes(self, session, frame, objects):
        self.seed_ids = [obj["object_id"] for obj in objects]

    def propagate_manual(self, session, max_frames, start_frame_idx):
        for frame in sorted(self.boxes):
            yield SimpleNamespace(frame_idx=frame)

    def decode_tracker_output(self, session, output):
        return [SimpleNamespace(object_id=oid, bbox=box, score=0.9, mask_area=None, sam3_object_id=oid) for oid, box in self.boxes[output.frame_idx].items()], None


def _run(tmp_path, monkeypatch, *, workspace=None, samples=None, boxes=None):
    seed = tmp_path / "seed_frame_000000.json"
    seed.write_text(json.dumps({"frame": {"frameIndex": 0}, "annotations": [
        {"object_id": 7, "name": "sperm7", "bbox": [100, 100, 140, 112], "source": "manual"},
        {"object_id": 12, "name": "sperm12", "bbox": [100, 200, 140, 212], "source": "manual"},
    ]}))
    if workspace:
        (tmp_path / "workspace_state.json").write_text(json.dumps(workspace))
    engine = FakeEngine(boxes or {frame: {7: [100 + 45 * frame, 100, 140 + 45 * frame, 112], 12: [100, 200, 140, 212]} for frame in range(1, 7)})
    meta = {"width": 1000, "height": 500, "fps": 30, "frameCount": 8, "source_frame_indices": list(range(8))}
    monkeypatch.setattr("app.tracker._probe_video", lambda path: meta)
    monkeypatch.setattr("app.tracker.read_video", lambda *a, **kw: ([object()] * 8, meta))
    monkeypatch.setattr("app.tracker.get_sam3_engine", lambda *a: engine)
    monkeypatch.setattr("app.tracker._render_overlay_video", lambda *a, **kw: None)
    result = track_video(str(tmp_path / "video.avi"), str(seed), str(tmp_path / "tracker_results.json"), 8, normal_feedback=samples)
    return result, engine


def _sample():
    return {"objectId": 7, "frameIndex": 3, "reason": "motion", "decision": "normal", "calibrate": True, "features": {"motionNormalized": 1.1}}


def test_tracker_restores_human_sample_and_coalesces_similar_warnings(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch, workspace={"normalMotionSamples": [_sample()]})
    assert result["anomaly_paused"] is None
    assert result["lastProcessedFrame"] == 6
    assert result["warningSummary"] == [{"objectId": 7, "name": "sperm7", "reason": "motion", "count": 6, "firstFrame": 1, "lastFrame": 6, "calibrated": True}]
    assert result["frames"][-1]["objects"][0]["anomaly_details"]["feedback_sample_count"] == 1


def test_tracker_no_sample_stops_on_third_motion_frame_and_exposes_metrics(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch)
    assert result["lastProcessedFrame"] == 3
    reason = result["anomaly_paused"]["reasons"][0]
    assert reason["object_id"] == 7
    assert reason["type"] == "tracking_motion"
    assert reason["metrics"]["motionStreak"] == 3
    assert reason["metrics"]["centerShift"] == 45
    assert reason["metrics"]["feedbackSampleCount"] == 0


def test_explicit_empty_sample_list_overrides_old_persisted_feedback(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch, workspace={"normalMotionSamples": [_sample()]}, samples=[])
    assert result["anomaly_paused"]["frame_index"] == 3


def test_global_delete_prevents_seed_and_stray_model_outputs_reintroducing_id(tmp_path, monkeypatch):
    result, engine = _run(tmp_path, monkeypatch, workspace={"deletedObjectIds": [7]})
    assert engine.seed_ids == [12]
    assert all([obj["object_id"] for obj in row["objects"]] == [12] for row in result["frames"])
    assert result["anomaly_paused"] is None


def test_single_frame_delete_keeps_future_identity_and_raw_box_for_undo(tmp_path, monkeypatch):
    result, engine = _run(tmp_path, monkeypatch, workspace={"deletedFrameObjects": [{"objectId": 7, "frameIndex": 2}]}, samples=[_sample()])
    assert engine.seed_ids == [7, 12]
    assert [obj["object_id"] for obj in result["frames"][2]["objects"]] == [12]
    assert [obj["object_id"] for obj in result["frames"][3]["objects"]] == [7, 12]
    raw = [json.loads(line) for line in (tmp_path / "tracker_results.json").read_text().splitlines()]
    assert [obj["object_id"] for obj in raw[2]["objects"]] == [7, 12]
    assert result["warningSummary"][0]["count"] == 5


def test_deleted_frame_disappearance_does_not_disable_future_disappearance_checks(tmp_path, monkeypatch):
    boxes = {1: {12: [100, 200, 140, 212]}, 2: {12: [100, 200, 140, 212]}}
    result, _ = _run(tmp_path, monkeypatch, workspace={"deletedFrameObjects": [{"objectId": 7, "frameIndex": 1}]}, boxes=boxes)
    assert result["lastProcessedFrame"] == 2
    assert result["anomaly_paused"]["reasons"][0]["type"] == "disappearance"


def test_all_deleted_seeds_fail_before_loading_model(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="已删除对象"):
        _run(tmp_path, monkeypatch, workspace={"deletedObjectIds": [7, 12]})


def test_overlay_applies_same_global_and_single_frame_deletion_rules(tmp_path, monkeypatch):
    import numpy as np
    from app.tracker import _render_overlay_video

    (tmp_path / "workspace_state.json").write_text(json.dumps({"deletedObjectIds": [12], "deletedFrameObjects": [{"objectId": 7, "frameIndex": 1}]}))
    class Capture:
        remaining = 3

        def isOpened(self):
            return True

        def read(self):
            self.remaining -= 1
            return (True, np.zeros((48, 64, 3), dtype=np.uint8)) if self.remaining >= 0 else (False, None)

        def release(self):
            pass

    class Writer:
        def write(self, frame):
            pass

        def release(self):
            pass

    seen = []
    def draw(image, objects, title):
        seen.append([obj["object_id"] for obj in objects])
        return np.asarray(image)
    monkeypatch.setattr("app.tracker.cv2.VideoCapture", lambda path: Capture())
    monkeypatch.setattr("app.tracker.open_video_writer", lambda *args: Writer())
    monkeypatch.setattr("app.tracker.draw_frame", draw)
    rows = [{"source_frame_index": frame, "objects": [{"object_id": oid, "bbox": [10, 10, 20, 20]} for oid in [7, 12]]} for frame in range(3)]
    _render_overlay_video(tmp_path / "video.avi", tmp_path / "overlay.mp4", {"width": 64, "height": 48, "fps": 30}, rows)
    assert seen == [[7], [], [7]]


def _geometry_segment(tmp_path, monkeypatch, seed_frame, seed_box, boxes):
    """Real tracker orchestration with a fresh fake model session per resume."""
    seed = tmp_path / f"annotations_frame_{seed_frame:06d}.json"
    seed.write_text(json.dumps({"frame": {"frameIndex": seed_frame}, "annotations": [
        {"object_id": 7, "name": "sperm7", "bbox": seed_box, "source": "manual" if seed_frame == 0 else "ai"},
        {"object_id": 12, "name": "sperm12", "bbox": [100, 200, 140, 220], "source": "manual" if seed_frame == 0 else "ai"},
    ]}))
    engine = FakeEngine(boxes)
    meta = {"width": 696, "height": 512, "fps": 4, "frameCount": 8, "source_frame_indices": list(range(8))}
    monkeypatch.setattr("app.tracker._probe_video", lambda path: meta)
    monkeypatch.setattr("app.tracker.read_video", lambda *a, **kw: ([object()] * 8, meta))
    monkeypatch.setattr("app.tracker.get_sam3_engine", lambda *a: engine)
    monkeypatch.setattr("app.tracker._render_overlay_video", lambda *a, **kw: None)
    return track_video(str(tmp_path / "video.avi"), str(seed), str(tmp_path / "tracker_results.json"), 8, start_frame=seed_frame)


def _confirmed_shape_workspace(tmp_path, monkeypatch):
    from app import annotation_state

    manual = [100, 100, 140, 120]
    accepted = [100, 100, 144.4, 111.8]
    other = [100, 200, 140, 220]
    first = _geometry_segment(tmp_path, monkeypatch, 0, manual, {1: {7: accepted, 12: other}})
    assert first["anomaly_paused"]["frame_index"] == 1
    reasons = first["anomaly_paused"]["reasons"]
    assert reasons[0]["type"] == "size_shrink"
    workspace = {"manualBaselines": [{"objectId": 7, "frameIndex": 0, "bbox": manual, "source": "manual"}],
                 "lastPausedContext": {"frameIndex": 1}, "pausedAnomalies": reasons}
    (tmp_path / "workspace_state.json").write_text(json.dumps(workspace))
    payload = {"expectedRevision": 0, "objectId": 7, "frameIndex": 1, "decision": "normal", "calibrate": False}
    response = annotation_state.write_state(tmp_path, "test-shape", 1, "accept-shape", payload, "feedback",
                                           lambda previous: annotation_state.feedback_state(previous, tmp_path, payload, 1))
    assert response["normalMotionSamples"] == []
    assert response["pausedAnomalies"] == []
    assert response["trackingFeedbackEvents"][-1]["geometryReference"]["bbox"] == accepted
    return manual, accepted, other


def test_normal_shape_confirmation_resumes_after_restart_without_redrawing(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="review.tracking")
    manual, accepted, other = _confirmed_shape_workspace(tmp_path, monkeypatch)
    # Alternating both human-approved shapes must not force a redraw or teach
    # any additional AI-derived reference. A fresh detector simulates restart.
    boxes = {frame: {7: accepted if frame % 2 else manual, 12: other} for frame in range(2, 7)}
    result = _geometry_segment(tmp_path, monkeypatch, 1, accepted, boxes)
    assert result["anomaly_paused"] is None
    assert result["lastProcessedFrame"] == 6
    accepted_row = next(row for row in result["frames"] if row["frame_index"] == 3)
    details = accepted_row["objects"][0]["anomaly_details"]
    assert details["manual_baseline_bbox"] == manual
    assert details["manual_baseline_frame"] == 0
    assert details["geometry_reference_bbox"] == accepted
    assert details["geometry_reference_source"] == "confirmed-normal"
    assert details["geometry_reference_frame"] == 1
    assert details["feedback_sample_count"] == 0
    assert "geometry_references={7: 1}" in caplog.text
    assert 'geometry_reference_frame' in caplog.text


def test_shape_confirmation_does_not_hide_further_shrink_or_other_objects(tmp_path, monkeypatch):
    _, accepted, other = _confirmed_shape_workspace(tmp_path, monkeypatch)
    shrink = [100, 100, 120, 105]
    result = _geometry_segment(tmp_path, monkeypatch, 1, accepted, {2: {7: shrink, 12: other}})
    pause = result["anomaly_paused"]["reasons"][0]
    assert pause["object_id"] == 7
    assert pause["type"] == "size_shrink"
    assert pause["geometryReferenceSource"] == "confirmed-normal"
    assert pause["geometryReferenceFrame"] == 1
    result = _geometry_segment(tmp_path, monkeypatch, 1, accepted,
                               {2: {7: accepted, 12: [100, 200, 120, 210]}})
    assert [item["object_id"] for item in result["anomaly_paused"]["reasons"]] == [12]


def test_new_manual_reference_supersedes_accepted_shape(tmp_path, monkeypatch):
    manual, accepted, other = _confirmed_shape_workspace(tmp_path, monkeypatch)
    state_path = tmp_path / "workspace_state.json"
    workspace = json.loads(state_path.read_text())
    workspace["manualBaselines"] = [{"objectId": 7, "frameIndex": 1, "bbox": manual, "source": "manual"}]
    state_path.write_text(json.dumps(workspace))
    result = _geometry_segment(tmp_path, monkeypatch, 1, accepted, {2: {7: accepted, 12: other}})
    pause = result["anomaly_paused"]["reasons"][0]
    assert pause["manualBaselineFrame"] == 1
    assert pause["geometryReferenceSource"] == "manual"


def test_geometry_restore_respects_frame_reset_correction_and_deletion():
    from app.services.anomaly_detector import ManualBaseline
    from app.tracker import _load_confirmed_geometry_references

    event = {"objectId": 7, "frameIndex": 2, "decision": "normal", "geometryReference": {
        "objectId": 7, "frameIndex": 2, "bbox": [100, 100, 144, 112], "source": "confirmed-normal"}}
    workspace = {"trackingFeedbackEvents": [event]}
    manual = {7: ManualBaseline([100, 100, 140, 120], 0)}
    assert not _load_confirmed_geometry_references(workspace, 1, manual)
    assert _load_confirmed_geometry_references(workspace, 2, manual)[7].frame_index == 2
    for tombstone in ({"deletedObjectIds": [7]}, {"deletedFrameObjects": [{"objectId": 7, "frameIndex": 2}]}):
        assert not _load_confirmed_geometry_references({**workspace, **tombstone}, 3, manual)
    for decision in ("reset", "corrected"):
        revised = {"trackingFeedbackEvents": [event, {"objectId": 7, "frameIndex": 3, "decision": decision}]}
        assert not _load_confirmed_geometry_references(revised, 3, manual)
    future_correction = {"trackingFeedbackEvents": [event, {"objectId": 7, "frameIndex": 4, "decision": "corrected"}]}
    assert 7 in _load_confirmed_geometry_references(future_correction, 3, manual)
    future_reset = {"trackingFeedbackEvents": [event, {"objectId": 7, "frameIndex": 4, "decision": "reset"}]}
    assert not _load_confirmed_geometry_references(future_reset, 3, manual)


@pytest.mark.parametrize("bbox", ([100, 100, float("nan"), 112], [100, 100, 99, 112], [100, 100, 144], None))
def test_invalid_geometry_reference_is_not_restored(bbox):
    from app.tracker import _load_confirmed_geometry_references

    event = {"objectId": 7, "frameIndex": 2, "decision": "normal", "geometryReference": {
        "objectId": 7, "frameIndex": 2, "bbox": bbox, "source": "confirmed-normal"}}
    assert not _load_confirmed_geometry_references({"trackingFeedbackEvents": [event]}, 2, {})

@pytest.mark.parametrize('rule', ({'deletedObjectIds': [7]}, {'deletedFrameObjects': [{'objectId': 7, 'frameIndex': 1}]}))
def test_resume_does_not_resurrect_deleted_seed_identity_from_retained_history(tmp_path, monkeypatch, caplog, rule):
    caplog.set_level('INFO', logger='review.tracking')
    other = [100, 200, 140, 220]
    (tmp_path / 'tracker_results.json').write_text(json.dumps({
        'frame_index': 0, 'source_frame_index': 0,
        'objects': [{'object_id': 7, 'bbox': [100, 100, 140, 120]}, {'object_id': 12, 'bbox': other}]
    }) + '\n')
    (tmp_path / 'workspace_state.json').write_text(json.dumps(rule))
    result = _geometry_segment(tmp_path, monkeypatch, 1, [100, 100, 140, 120],
                               {fi: {12: other} for fi in range(2, 7)})
    assert result['anomaly_paused'] is None
    assert result['lastProcessedFrame'] == 6
    assert 'active_ids=[12]' in caplog.text
    # Undo data remains intact; new seed/output includes only requested IDs.
    raw = [json.loads(line) for line in (tmp_path / 'tracker_results.json').read_text().splitlines()]
    assert raw[0]['objects'][0]['object_id'] == 7
    assert all([obj['object_id'] for obj in row['objects']] == [12] for row in raw[1:])
