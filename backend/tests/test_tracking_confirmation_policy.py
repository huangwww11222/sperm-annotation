"""Human approval snapshots are server facts, not an object-wide exemption.

Uses real workspace publication/receipts and fresh detector loaders. No model
weights, hospital files or default business storage are touched.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import annotation_state, tracker
from app.services.anomaly_detector import ManualBaseline


def _approval(directory: Path, *, reasons=None, details=None, mask_area=None):
    directory.mkdir(exist_ok=True)
    box = [100.0, 100.0, 144.4, 111.8]
    objects = [
        {"object_id": 7, "bbox": box, "mask_area": mask_area,
         "anomaly_level": "anomaly", "anomaly_reasons": reasons or ["manual_height_ratio=0.590 HARD"],
         "anomaly_details": details or {}},
        {"object_id": 9, "bbox": [102.0, 100.0, 140.0, 111.8]},
        {"object_id": 12, "bbox": [101.0, 100.0, 144.0, 111.8]},
    ]
    (directory / "tracker_results.json").write_text(json.dumps({"source_frame_index": 5, "objects": objects}))
    state = {"revision": 0, "manualAnnotations": [], "manualBaselines": [],
             "pausedAnomalies": [{"objectId": 7}], "lastPausedContext": {"frameIndex": 5}}
    (directory / "workspace_state.json").write_text(json.dumps(state))
    body = {"expectedRevision": 0, "objectId": 7, "frameIndex": 5, "decision": "normal", "calibrate": False,
            # Neither of these untrusted client fields may determine snapshots.
            "geometryReference": {"bbox": [0, 0, 999, 999]},
            "overlapReferences": [{"objectIds": [7, 99]}]}
    return state, body, box


def _save(directory: Path, body: dict, key="accept"):
    return annotation_state.write_state(directory, directory.name, 1, key, body, "feedback",
                                        lambda state: annotation_state.feedback_state(state, directory, body, 1))


def _geometry_event(frame, height, oid=7):
    return {"id": f"event-{frame}", "objectId": oid, "frameIndex": frame, "decision": "normal",
            "geometryReference": {"objectId": oid, "frameIndex": frame, "bbox": [100, 100, 140, 100 + height],
                                  "maskArea": height * 12, "source": "confirmed-normal"}}


def _overlap_event(frame, a=7, b=9):
    return {"id": f"overlap-{frame}-{a}-{b}", "objectId": a, "frameIndex": frame, "decision": "normal",
            "overlapReferences": [{"objectIds": [a, b], "frameIndex": frame,
                                   "bboxes": [[100, 100, 140, 120], [102, 100, 142, 120]],
                                   "source": "confirmed-normal"}]}


def _overlap_loader():
    loader = getattr(tracker, "_load_confirmed_overlap_references", None)
    assert callable(loader), "confirmed overlap pairs must be restored by actual tracker startup"
    return loader


def test_confirmation_captures_actual_mask_and_receipt_reference(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="review.annotation")
    _, body, box = _approval(tmp_path, mask_area=217)
    response = _save(tmp_path, body)
    event = response["trackingFeedbackEvents"][-1]
    assert event["geometryReference"] == {"objectId": 7, "frameIndex": 5, "bbox": box,
                                          "maskArea": 217, "source": "confirmed-normal", "checkpoint": True}
    assert response["confirmedReference"]["eventId"] == event["id"]
    assert response["confirmedReference"]["geometryReference"] == event["geometryReference"]
    assert _save(tmp_path, body) == response
    assert annotation_state.read_state(tmp_path)["manualAnnotations"] == []
    assert "geometry_reference_frame=5" in caplog.text
    assert "overlap_pairs=[]" in caplog.text


@pytest.mark.parametrize("mask", [None, 0, -2, True, "217"])
def test_invalid_mask_does_not_create_a_reference_measurement(tmp_path, mask):
    _, body, _ = _approval(tmp_path, mask_area=mask)
    event = _save(tmp_path, body)["trackingFeedbackEvents"][-1]
    assert "maskArea" not in event["geometryReference"]


def test_latest_three_complete_approved_shapes_survive_reload_without_ai_learning():
    events = [_geometry_event(frame, height) for frame, height in [(1, 20), (2, 12), (3, 28), (4, 8)]]
    workspace = {"trackingFeedbackEvents": events}
    refs = tracker._load_confirmed_geometry_references(copy.deepcopy(workspace), 4, {})[7]
    assert len(refs) == 3
    assert [ref.frame_index for ref in refs] == [2, 3, 4]
    assert [ref.bbox for ref in refs] == [event["geometryReference"]["bbox"] for event in events[1:]]
    assert [ref.mask_area for ref in refs] == [144, 336, 96]
    assert [ref.event_id for ref in refs] == [event["id"] for event in events[1:]]
    assert workspace["trackingFeedbackEvents"] == events


def test_geometry_temporal_manual_deletion_correction_and_reset_boundaries():
    events = [_geometry_event(1, 20), _geometry_event(3, 12), _geometry_event(7, 8)]
    workspace = {"trackingFeedbackEvents": events}
    restore = tracker._load_confirmed_geometry_references
    assert [r.frame_index for r in restore(workspace, 3, {})[7]] == [1, 3]
    assert [r.frame_index for r in restore(workspace, 3, {7: ManualBaseline([100, 100, 140, 120], 1)})[7]] == [3]
    assert not restore(workspace, 3, {7: ManualBaseline([100, 100, 140, 120], 3)})
    for tombstone in [{"deletedObjectIds": [7]}, {"deletedFrameObjects": [{"objectId": 7, "frameIndex": 3}]}]:
        assert not restore({**workspace, **tombstone}, 3, {})
    correction = {"objectId": 7, "frameIndex": 3, "decision": "corrected"}
    assert not restore({"trackingFeedbackEvents": [events[0], correction]}, 3, {})
    assert [r.frame_index for r in restore({"trackingFeedbackEvents": [events[0], correction]}, 2, {})[7]] == [1]
    # Reset is intentionally the existing object-wide action, including when
    # replaying an older branch. It never deletes the immutable audit events.
    assert not restore({"trackingFeedbackEvents": [*events, {"objectId": 7, "decision": "reset"}]}, 3, {})


@pytest.mark.parametrize("decision", ["reset", "corrected"])
def test_retired_corrupt_confirmation_does_not_block_default_recovery(decision):
    from app.tracking_pause_policy import require_confirmed_geometry
    corrupt = _geometry_event(2, 20)
    corrupt["geometryReference"]["bbox"] = [100, 100, 99, 120]
    workspace = {"trackingFeedbackEvents": [corrupt, {"objectId": 7, "frameIndex": 3, "decision": decision}]}
    original = copy.deepcopy(workspace)
    refs = tracker._load_confirmed_geometry_references(workspace, 3, {})
    assert refs == {}
    require_confirmed_geometry(workspace, 3, {7}, {}, refs)
    assert workspace == original


def test_corrupt_approval_outside_last_three_does_not_block_current_shapes():
    from app.tracking_pause_policy import require_confirmed_geometry
    corrupt = _geometry_event(1, 20)
    corrupt["geometryReference"]["bbox"] = [100, 100, 99, 120]
    workspace = {"trackingFeedbackEvents": [corrupt, *[_geometry_event(frame, 20) for frame in (2, 3, 4)]]}
    refs = tracker._load_confirmed_geometry_references(workspace, 4, {})
    require_confirmed_geometry(workspace, 4, {7}, {}, refs)
    assert [ref.frame_index for ref in refs[7]] == [2, 3, 4]


def test_active_corrupt_confirmation_remains_an_error():
    from app.tracking_pause_policy import require_confirmed_geometry
    corrupt = _geometry_event(2, 20)
    corrupt["geometryReference"]["bbox"] = [100, 100, 99, 120]
    workspace = {"trackingFeedbackEvents": [corrupt]}
    refs = tracker._load_confirmed_geometry_references(workspace, 3, {})
    with pytest.raises(ValueError, match="尺寸参考损坏"):
        require_confirmed_geometry(workspace, 3, {7}, {}, refs)


def test_frame_overlap_captures_every_actual_pair_not_last_other_id(tmp_path):
    _, body, _ = _approval(tmp_path,
                          reasons=["bbox_overlap_with=9 coverage=0.951 HARD", "bbox_overlap_with=12 coverage=0.990 HARD"],
                          details={"other_object_id": 12})
    body = {**body, 'scope': 'frame'}
    result = _save(tmp_path, body)
    references = result["trackingFeedbackEvents"][0].get("overlapReferences")
    assert [ref["objectIds"] for ref in references] == [[7, 9], [7, 12]]
    assert all(ref["frameIndex"] == 5 and ref["source"] == "confirmed-normal" for ref in references)
    actual = json.loads((tmp_path / "tracker_results.json").read_text())["objects"]
    boxes = {item["object_id"]: item["bbox"] for item in actual}
    assert [ref["bboxes"] for ref in references] == [[boxes[oid] for oid in ref["objectIds"]] for ref in references]
    assert result["confirmedReference"]["overlapReferences"] == result['trackingFeedbackEvents'][-1]['overlapReferences']
    assert _save(tmp_path, body) == result
    loaded = _overlap_loader()(annotation_state.read_state(tmp_path), 5, {})
    assert set(loaded) == {(7, 9), (7, 12), (9, 12)}
    assert loaded[(7, 9)].event_id == result["trackingFeedbackEvents"][1]["id"]


def test_structured_overlap_pairs_are_used_and_unrelated_pairs_rejected(tmp_path):
    _, body, _ = _approval(tmp_path, reasons=["bbox_overlap_with=9 coverage=0.980 HARD"],
                          details={"overlap_pairs": [{"object_ids": [7, 9]}, {"object_ids": [7, 12]}, {"object_ids": [9, 12]}]})
    refs = _save(tmp_path, {**body, 'scope': 'frame'})["trackingFeedbackEvents"][0]["overlapReferences"]
    assert [ref["objectIds"] for ref in refs] == [[7, 9], [7, 12]]


def test_overlap_history_multiple_pairs_frame_and_manual_boundaries():
    events = [_overlap_event(2, 7, 9), _overlap_event(4, 7, 12)]
    load = _overlap_loader()
    workspace = {"trackingFeedbackEvents": events}
    assert set(load(workspace, 4, {})) == {(7, 9), (7, 12)}
    assert set(load(workspace, 3, {})) == {(7, 9)}
    assert set(load(workspace, 4, {9: ManualBaseline([100, 100, 140, 120], 2)})) == {(7, 12)}
    assert not load(workspace, 4, {7: ManualBaseline([100, 100, 140, 120], 4)})
    assert set(load({**workspace, "deletedObjectIds": [9]}, 4, {})) == {(7, 12)}
    for decision in ["corrected", "reset"]:
        revised = {"trackingFeedbackEvents": [*events, {"objectId": 9, "frameIndex": 4, "decision": decision}]}
        assert set(load(revised, 4, {})) == {(7, 12)}


def test_unknown_overlap_peer_fails_without_changing_receipt_or_pause(tmp_path):
    _, body, _ = _approval(tmp_path, reasons=["bbox_overlap_with=99 coverage=0.980 HARD"])
    before = (tmp_path / "workspace_state.json").read_bytes()
    with pytest.raises(HTTPException) as exc:
        _save(tmp_path, body)
    assert exc.value.status_code == 422
    assert (tmp_path / "workspace_state.json").read_bytes() == before


def test_atomic_failure_preserves_approval_request_and_retry_is_exact(tmp_path, monkeypatch):
    _, body, _ = _approval(tmp_path, mask_area=217)
    before = (tmp_path / "workspace_state.json").read_bytes()
    original = Path.replace
    def reject(path, target):
        if Path(target) == tmp_path / "workspace_state.json":
            raise OSError("injected approval publication failure")
        return original(path, target)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", reject)
        with pytest.raises(HTTPException) as exc:
            _save(tmp_path, body)
        assert exc.value.status_code == 500
    assert (tmp_path / "workspace_state.json").read_bytes() == before
    response = _save(tmp_path, body)
    assert response["revision"] == 1
    assert len(response["trackingFeedbackEvents"]) == 1
    assert _save(tmp_path, body) == response


def test_approval_receipt_replay_after_reset_restores_exact_historical_summary(tmp_path):
    _, body, _ = _approval(tmp_path, mask_area=217)
    first = _save(tmp_path, body)
    reset = {"expectedRevision": 1, "objectId": 7, "decision": "reset", "calibrate": False}
    second = _save(tmp_path, reset, "reset")
    assert second["revision"] == 2
    assert "confirmedReference" not in second
    assert _save(tmp_path, body) == first
    stored = annotation_state.read_state(tmp_path)
    assert len(stored["trackingFeedbackEvents"]) == 2
    assert not tracker._load_confirmed_geometry_references(stored, 5, {})


def test_unrelated_last_peer_does_not_create_implicit_overlap_approval(tmp_path):
    _, body, _ = _approval(tmp_path, details={"other_object_id": 12})
    event = _save(tmp_path, body)["trackingFeedbackEvents"][-1]
    assert "overlapReferences" not in event


def test_normal_mask_only_confirmation_captures_same_complete_geometry(tmp_path):
    _, body, box = _approval(tmp_path, reasons=["confirmed_mask_area_ratio=0.500 HARD"], mask_area=120)
    event = _save(tmp_path, body)["trackingFeedbackEvents"][-1]
    assert event["geometryReference"]["bbox"] == box
    assert event["geometryReference"]["maskArea"] == 120
    references = tracker._load_confirmed_geometry_references(annotation_state.read_state(tmp_path), 5, {})
    assert references[7][0].mask_area == 120


def test_frame_deleted_pair_approval_is_filtered_without_hiding_other_pair():
    workspace = {"trackingFeedbackEvents": [_overlap_event(2), _overlap_event(3, 7, 12)],
                 "deletedFrameObjects": [{"objectId": 9, "frameIndex": 2}]}
    assert set(_overlap_loader()(workspace, 4, {})) == {(7, 12)}
    workspace["deletedFrameObjects"] = [{"objectId": 12, "frameIndex": 4}]
    assert set(_overlap_loader()(workspace, 4, {})) == {(7, 9)}


@pytest.mark.parametrize("broken", [
    {"objectIds": [7, 99]}, {"objectIds": [7, 7]}, {"source": "ai"},
    {"frameIndex": 99}, {"bboxes": [[100, 100, 140, 120], [100, 100, 90, 120]]},
])
def test_legacy_or_malformed_pair_reference_never_exempts_new_peer(broken):
    event = _overlap_event(2)
    event["overlapReferences"][0].update(broken)
    # An unknown valid integer peer is a stored identity. It can be restored
    # only if explicitly tied to the event, never extrapolated to another pair.
    references = _overlap_loader()({"trackingFeedbackEvents": [event]}, 3, {})
    if broken == {"objectIds": [7, 99]}:
        assert set(references) == {(7, 99)}
        assert (7, 9) not in references
    else:
        assert not references


def test_server_pause_diagnostics_survive_save_feedback_and_refresh(tmp_path):
    _approval(tmp_path)
    reason = {"object_id": 7, "type": "size_shrink", "reasons": ["manual_height_ratio=0.641 HARD"],
              "details": {"geometry_streak": 3, "risk_start_frame": 3, "decision_frame_index": 5}}
    annotation_state.publish_tracking_pause(tmp_path, tmp_path.name, {"frame_index": 3, "reasons": [reason]})
    restored = annotation_state.public_state(annotation_state.read_state(tmp_path))
    assert restored["pausedAnomalies"][0]["diagnostic"] == reason
    assert restored["lastPausedContext"]["frameIndex"] == 3
    payload = {"pausedAnomalies": [{"objectId": 7, "title": "local display", "diagnostic": {"tampered": True}}]}
    clean = dict(restored)
    annotation_state.preserve_pause(restored, clean, payload)
    assert clean["pausedAnomalies"][0]["diagnostic"] == reason


def test_observation_starting_at_previous_window_last_frame_uses_its_real_evidence(tmp_path, monkeypatch):
    from .test_tracking_adaptive_feedback import _geometry_segment

    manual = [100, 100, 140, 120]
    moderate = [100, 100, 144.4, 111.8]
    other = [100, 200, 140, 220]
    initial = _geometry_segment(tmp_path, monkeypatch, 0, manual, {1: {7: moderate, 12: other}})
    assert initial["anomaly_paused"] is None
    # Only ONE pending observation exists when the first window ends. The
    # new seed coincides with that pending row and must not overwrite evidence.
    resumed = _geometry_segment(tmp_path, monkeypatch, 1, moderate,
                                {2: {7: moderate, 12: other}, 3: {7: moderate, 12: other}})
    assert resumed["anomaly_paused"]["frame_index"] == 1
    reason = next(item for item in resumed["anomaly_paused"]["reasons"] if item["object_id"] == 7)
    assert reason["type"] == "size_shrink"
    assert reason["details"]["geometry_observation_count"] == 3
    assert reason["details"]["decision_frame_index"] == 3
    assert resumed["lastProcessedFrame"] == 1
    assert resumed["analyzedThroughFrame"] == 3


def test_deleted_single_frame_confirmation_does_not_block_active_future_object():
    from app.tracking_pause_policy import require_confirmed_geometry

    workspace = {"trackingFeedbackEvents": [_geometry_event(2, 12)],
                 "deletedFrameObjects": [{"objectId": 7, "frameIndex": 2}]}
    manual = {7: ManualBaseline([100, 100, 140, 120], 0)}
    restored = tracker._load_confirmed_geometry_references(workspace, 3, manual)
    assert not restored
    # Deleting only the acceptance frame does not delete future tracking
    # identity. The agreement guard must apply the exact same deletion rules.
    require_confirmed_geometry(workspace, 3, {7}, manual, restored)


def test_earlier_persistent_risk_never_labels_later_extreme_object_on_old_frame():
    from app.services.anomaly_detector import AnomalyDetector, AnomalyLevel
    from app.tracking_pause_policy import select_pause_evidence

    normal = {7: [100, 100, 140, 120], 12: [200, 100, 240, 120]}
    subject = AnomalyDetector(frame_width=1000, frame_height=600, fps=4)
    for oid, bbox in normal.items():
        subject.set_manual_baseline(oid, bbox, 0)
    subject.initialize_seed(10, normal)
    rows = []
    for frame in [11, 12, 13]:
        boxes = {7: [100, 100, 140, 112], 12: normal[12] if frame < 13 else [200, 100, 212, 104]}
        report = subject.push(frame, boxes)
        facts = {item.object_id: item for item in report.frames}
        rows.append({"source_frame_index": frame, "objects": [
            {"object_id": oid, "bbox": bbox, "anomaly_level": facts[oid].level.value,
             "anomaly_reasons": list(facts[oid].reasons), "anomaly_details": copy.deepcopy(facts[oid].details)}
            for oid, bbox in boxes.items()]})
    assert report.should_pause and report.pause_frame_index == 11
    assert report.object_levels[12] == AnomalyLevel.ANOMALY
    first, selected, retained = select_pause_evidence(report, rows, 13)
    assert first == 11
    assert [item.object_id for item in selected] == [7]
    assert all(item["source_frame_index"] <= 11 for item in retained)
    assert retained[-1]["objects"][1]["bbox"] == normal[12]
    assert not any(item["object_id"] == 12 for item in retained[-1]["anomalies"])
    assert selected[0].details["current_bbox"] == [100, 100, 140, 112]


@pytest.mark.parametrize("mask", [None, 0])
def test_mask_absence_is_not_interpreted_as_explicit_empty_model_mask(mask):
    from app.services.anomaly_detector import AnomalyDetector, ConfirmedGeometryReference

    bbox = [100, 100, 140, 120]
    subject = AnomalyDetector(geometry_references={7: [ConfirmedGeometryReference(bbox, 2, mask_area=200)]})
    subject.set_manual_baseline(7, bbox, 0)
    subject.initialize_seed(3, {7: bbox})
    report = subject.push(4, {7: bbox}, mask_areas={} if mask is None else {7: mask})
    assert report.should_pause is (mask == 0)


def test_multiple_overlap_pairs_include_only_real_current_approvals():
    from app.services.anomaly_detector import AnomalyDetector, OverlapReference

    boxes = {7: [100, 100, 140, 120], 9: [101, 100, 141, 120], 12: [300, 100, 340, 120]}
    approved = {7: boxes[7], 9: boxes[9]}
    subject = AnomalyDetector(frame_width=1000, frame_height=600, fps=4,
                             overlap_references={(7, 9): OverlapReference((7, 9), approved, 2)})
    for oid, bbox in boxes.items():
        subject.set_manual_baseline(oid, bbox, 0)
    subject.initialize_seed(3, boxes)
    predictions = {**boxes, 12: [102, 100, 142, 120]}
    reports = [subject.push(frame, predictions) for frame in [4, 5, 6]]
    assert [report.should_pause for report in reports] == [False, False, True]
    pairs = next(item for item in reports[-1].frames if item.object_id == 7).details["overlap_pairs"]
    assert next(pair for pair in pairs if pair["object_ids"] == [7, 9])["normal_reference_matched"]
    assert not next(pair for pair in pairs if pair["object_ids"] == [7, 12])["normal_reference_matched"]


def test_normal_overlap_confirmation_supersedes_earlier_separation_on_restart():
    from app.services.anomaly_detector import AnomalyDetector, OverlapReference

    separate = {7: [100, 100, 140, 120], 9: [200, 100, 240, 120]}
    overlap = {7: [150, 100, 190, 120], 9: [150.5, 100, 190.5, 120]}
    initial = AnomalyDetector(frame_width=1000, frame_height=600, fps=4)
    for oid, bbox in separate.items():
        initial.set_manual_baseline(oid, bbox, 0)
    initial.initialize_seed(0, separate)
    rows = [{"source_frame_index": 0, "objects": [{"object_id": oid, "bbox": bbox} for oid, bbox in separate.items()]}]
    for frame in [1, 2, 3]:
        report = initial.push(frame, overlap)
        facts = {item.object_id: item for item in report.frames}
        rows.append({"source_frame_index": frame, "objects": [
            {"object_id": oid, "bbox": bbox, "anomaly_level": facts[oid].level.value,
             "anomaly_reasons": list(facts[oid].reasons), "anomaly_details": copy.deepcopy(facts[oid].details)}
            for oid, bbox in overlap.items()]})
    assert report.should_pause and report.pause_frame_index == 1
    # The application publishes through first risk frame 1; the user approves
    # this pair there. Earlier frame 0 separation is historical, not a reason
    # to ignore a later explicit normal acceptance during startup replay.
    restored = AnomalyDetector(frame_width=1000, frame_height=600, fps=4,
                               overlap_references={(7, 9): OverlapReference((7, 9), overlap, 1)})
    for oid, bbox in separate.items():
        restored.set_manual_baseline(oid, bbox, 0)
    restored.initialize_seed(1, overlap)
    restored.prime_observations(rows[:2], 1, overlap)
    reports = [restored.push(frame, overlap) for frame in [2, 3, 4]]
    assert not any(report.should_pause for report in reports)
    pairs = reports[0].frames[0].details["overlap_pairs"]
    assert pairs[0]["normal_reference_matched"]
    assert pairs[0]["reference_source"] == "confirmed-normal"


def test_separation_after_normal_overlap_confirmation_rechecks_new_convergence():
    from app.services.anomaly_detector import AnomalyDetector, OverlapReference

    overlap = {7: [150, 100, 190, 120], 9: [150.5, 100, 190.5, 120]}
    separate = {7: overlap[7], 9: [250, 100, 290, 120]}
    subject = AnomalyDetector(frame_width=1000, frame_height=600, fps=4,
                             overlap_references={(7, 9): OverlapReference((7, 9), overlap, 1)})
    for oid, bbox in overlap.items():
        subject.set_manual_baseline(oid, bbox, 0)
    subject.initialize_seed(1, overlap)
    assert not subject.push(2, separate).should_pause
    reports = [subject.push(frame, overlap) for frame in [3, 4, 5]]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 3


def test_new_manual_seed_does_not_inherit_preceding_pair_observations():
    from app.services.anomaly_detector import AnomalyDetector

    separate = {7: [100, 100, 140, 120], 9: [200, 100, 240, 120]}
    overlap = {7: [150, 100, 190, 120], 9: [150.5, 100, 190.5, 120]}
    initial = AnomalyDetector(frame_width=1000, frame_height=600, fps=4)
    for oid, bbox in separate.items():
        initial.set_manual_baseline(oid, bbox, 0)
    initial.initialize_seed(0, separate)
    rows = [{"source_frame_index": 0, "objects": [{"object_id": oid, "bbox": bbox} for oid, bbox in separate.items()]}]
    for frame in [1, 2]:
        report = initial.push(frame, overlap)
        facts = {item.object_id: item for item in report.frames}
        rows.append({"source_frame_index": frame, "objects": [
            {"object_id": oid, "bbox": bbox, "anomaly_level": facts[oid].level.value,
             "anomaly_reasons": list(facts[oid].reasons), "anomaly_details": copy.deepcopy(facts[oid].details)}
            for oid, bbox in overlap.items()]})
    corrected = {**overlap, 7: [150.3, 100, 190.3, 120]}
    restored = AnomalyDetector(frame_width=1000, frame_height=600, fps=4)
    restored.set_manual_baseline(7, corrected[7], 2)
    restored.set_manual_baseline(9, separate[9], 0)
    restored.initialize_seed(2, corrected)
    restored.prime_observations(rows, 2, corrected)
    reports = [restored.push(frame, corrected) for frame in [3, 4, 5]]
    assert not any(report.should_pause for report in reports)
