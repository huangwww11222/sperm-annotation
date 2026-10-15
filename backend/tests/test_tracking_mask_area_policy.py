"""Historical Mask area is a clue, not independent proof of a lost subject.

Inputs are synthetic policy summaries, strictly bound to current source/frame/
bbox/Mask and an explicit human reference. They do not assert model accuracy.
Native image extraction is covered separately by the evidence producer tests.
"""
from __future__ import annotations

import copy
import logging

import pytest

from app.services.anomaly_detector import (
    AnomalyDetector, AnomalyLevel, ConfirmedGeometryReference, ManualBaseline,
)


SOURCE = ["isolated-mask-area-video", 1245, 67890, 19]
REFERENCE_AREA = 3459
CURRENT_AREA = 802


def _box(oid=1):
    x, y = 180 + (oid - 1) % 10 * 160, 180 + (oid - 1) // 10 * 160
    return [float(x), float(y), float(x + 70), float(y + 85)]


def _detector(count=1, *, confirmation=False):
    boxes = {oid: _box(oid) for oid in range(1, count + 1)}
    detector = AnomalyDetector(frame_width=2048, frame_height=1500, fps=44.187,
        all_object_ids=boxes, manual_baselines={oid: ManualBaseline(box, 0,
            f"object-{oid}", mask_area=REFERENCE_AREA) for oid, box in boxes.items()},
        geometry_references={oid: ConfirmedGeometryReference(box, 5, REFERENCE_AREA,
            f"normal-{oid}") for oid, box in boxes.items()} if confirmation else None)
    detector.image_source_signature = list(SOURCE)
    detector.initialize_seed(5 if confirmation else 0, boxes)
    return detector, boxes


def _evidence(oid, frame, box, *, reference_frame=0, area=CURRENT_AREA,
              status="supported", coverage="no_clear_loss", association="reliable"):
    point = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
    return {"version": 2, "source": "native-frame-mask-v2", "sourceSignature": list(SOURCE),
        "observationVerified": True, "objectId": oid, "frameIndex": frame,
        "bbox": list(box), "maskArea": area, "status": status,
        "geometryReferenceFrameIndex": reference_frame, "geometryReferenceBBox": list(box),
        "head": {"status": "matched", "associationStatus": association, "point": point,
            "radius": 5., "confidence": .99, "previousPoint": point,
            "previousFrameIndex": frame - 1, "referencePoint": point,
            "referenceFrameIndex": reference_frame, "referenceBBox": list(box),
            "metrics": {"bboxHeadCoverage": 1., "maskCoverage": 1.}},
        "coverage": {"status": coverage, "metrics": {}}, "reasons": [], "thresholds": {}}


def _push(detector, frame, boxes, *, evidence=None, area=CURRENT_AREA, reference_frame=0):
    evidence = evidence if evidence is not None else {
        oid: _evidence(oid, frame, box, reference_frame=reference_frame, area=area)
        for oid, box in boxes.items()}
    return detector.push(frame, boxes, mask_areas={oid: area for oid in boxes},
                         identity_evidence=evidence)


@pytest.mark.parametrize("status,coverage", [("supported", "no_clear_loss"),
    ("warning", "local_mask_coverage"), ("warning", "mask_contamination"),
    ("warning", "subject_present_uncertain"), ("uncertain", "subject_present_uncertain")])
def test_historical_area_drop_with_reliably_present_subject_stays_a_warning(status, coverage, caplog):
    detector, boxes = _detector()
    original = copy.deepcopy(detector.states[1].manual_baseline)
    caplog.set_level(logging.INFO, logger="review.tracking")
    # Explicit uncertain image evidence retains its separate bounded 12-frame
    # policy. This test isolates the area gate before that independent limit.
    frames = range(1, 4) if status == "uncertain" else range(1, 16)
    reports = [_push(detector, frame, boxes, evidence={1: _evidence(1, frame, boxes[1],
        status=status, coverage=coverage)}) for frame in frames]
    assert not any(report.should_pause for report in reports)
    assert all(report.object_levels[1] == AnomalyLevel.WARNING for report in reports)
    for report in reports:
        item = report.frames[0]
        assert "confirmed_mask_area_ratio=0.232 SOFT" in item.reasons
        assert item.details["mask_area_decision"] == "reliable_current_subject_warning"
        assert item.details["mask_area_ratio"] == pytest.approx(CURRENT_AREA / REFERENCE_AREA)
        assert item.details["identity_evidence"]["status"] == status
        assert not item.details["geometry_extreme"]
        assert not item.details["geometry_risk"]
    assert detector.states[1].manual_baseline == original
    assert detector.normal_motion_samples == {}
    assert "tracking.mask_area_candidate" in caplog.text
    assert "decision=reliable_current_subject_warning" in caplog.text
    assert len([record for record in caplog.records if record.message.startswith("tracking.mask_area_candidate ")]) == 1


@pytest.mark.parametrize("fault", ["unknown", "missing", "old_version", "missing_flag", "source",
    "source_unavailable", "frame", "bbox", "mask_binding", "empty", "bad_metrics",
    "lost_head", "ambiguous_head", "temporary_occlusion", "tail_loss", "merge", "foreign_core"])
def test_uncertain_missing_or_positive_loss_evidence_keeps_extreme_mask_fallback(fault):
    detector, boxes = _detector()
    evidence = _evidence(1, 1, boxes[1])
    area = CURRENT_AREA
    if fault == "missing": evidence = None
    elif fault == "old_version": evidence.update(version=1, source="native-frame-mask-v1")
    elif fault == "missing_flag": evidence.pop("observationVerified")
    elif fault == "source": evidence["sourceSignature"] = ["another", 1245, 67890, 19]
    elif fault == "source_unavailable": detector.image_source_signature = None
    elif fault == "frame": evidence["frameIndex"] = 0
    elif fault == "bbox": evidence["bbox"][0] += 1
    elif fault == "mask_binding": evidence["maskArea"] += 1
    elif fault == "empty": area = evidence["maskArea"] = 0
    elif fault == "bad_metrics": evidence["coverage"]["metrics"] = "broken"
    elif fault == "temporary_occlusion":
        evidence.update(status="uncertain")
        evidence["head"]["associationStatus"] = "temporary-occlusion"
        evidence["coverage"]["status"] = "subject_present_uncertain"
    elif fault in {"lost_head", "ambiguous_head"}:
        evidence["status"] = "risk"
        evidence["head"]["status"] = "lost" if fault == "lost_head" else "ambiguous"
        evidence["coverage"]["status"] = "unknown"
    elif fault in {"tail_loss", "merge", "foreign_core"}:
        evidence["status"] = "risk"
        evidence["coverage"]["status"] = fault
    elif fault == "unknown":
        evidence.update(status="unknown", geometryReferenceFrameIndex=None, geometryReferenceBBox=None)
        evidence["head"].update(status="unknown", point=None, previousPoint=None,
            previousFrameIndex=None, referencePoint=None, referenceFrameIndex=None,
            referenceBBox=None, associationStatus="unknown", radius=None, confidence=None)
        evidence["coverage"]["status"] = "unknown"
        evidence["reasons"] = ["reference_missing"]
    report = _push(detector, 1, boxes, evidence={} if evidence is None else {1: evidence}, area=area)
    assert report.should_pause
    assert report.frames[0].details["mask_area_decision"] == "extreme_mask_loss_fallback"
    assert any(reason.startswith("confirmed_mask_area_ratio=") and reason.endswith(" HARD")
               for reason in report.frames[0].reasons)
    if fault == "unknown":
        assert report.frames[0].details["identity_evidence"]["status"] == "unknown"
        assert detector.states[1].image_count == 0


def test_after_human_normal_confirmation_a_new_real_loss_is_not_exempt():
    detector, boxes = _detector(confirmation=True)
    warning = _push(detector, 6, boxes, reference_frame=5)
    assert not warning.should_pause
    evidence = _evidence(1, 7, boxes[1], reference_frame=5, status="risk", coverage="tail_loss")
    lost = _push(detector, 7, boxes, evidence={1: evidence})
    assert lost.should_pause
    assert "confirmed_mask_area_ratio=0.232 HARD" in lost.frames[0].reasons
    assert lost.frames[0].details["identity_evidence"]["coverage"]["status"] == "tail_loss"


def test_mask_area_warning_does_not_erase_independent_bounded_uncertain_observation():
    detector, boxes = _detector()
    reports = [_push(detector, frame, boxes, evidence={1: _evidence(1, frame, boxes[1],
        status="uncertain", coverage="subject_present_uncertain")}) for frame in range(1, 13)]
    assert not any(report.should_pause for report in reports[:-1])
    assert reports[-1].should_pause
    assert reports[-1].frames[0].details["risk_type"] == "image"
    assert reports[-1].frames[0].details["mask_area_decision"] == "reliable_current_subject_warning"
    assert any(reason.startswith("image_subject_present_uncertain observations=12/12")
               and reason.endswith(" HARD") for reason in reports[-1].frames[0].reasons)


@pytest.mark.parametrize("kind", ["head", "tail", "merge"])
def test_without_extreme_area_independent_subject_loss_still_uses_three_observations(kind):
    detector, boxes = _detector()
    reports = []
    for frame in (1, 2, 3):
        evidence = _evidence(1, frame, boxes[1], area=1500, status="risk",
            coverage="unknown" if kind == "head" else "tail_loss" if kind == "tail" else "merge")
        if kind == "head": evidence["head"]["status"] = "lost"
        reports.append(_push(detector, frame, boxes, evidence={1: evidence}, area=1500))
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 1


def test_subject_presence_does_not_override_whole_box_diagonal_extreme():
    detector, boxes = _detector()
    baseline = boxes[1]
    box = [baseline[0] - 60, baseline[1] - 60, baseline[2] + 60, baseline[3] + 60]
    evidence = _evidence(1, 1, box)
    evidence["geometryReferenceBBox"] = list(baseline)
    evidence["head"]["referenceBBox"] = list(baseline)
    evidence["head"]["referencePoint"] = [(baseline[0] + baseline[2])/2, (baseline[1] + baseline[3])/2]
    evidence["head"]["point"] = list(evidence["head"]["referencePoint"])
    evidence["head"]["previousPoint"] = list(evidence["head"]["point"])
    report = _push(detector, 1, {1: box}, evidence={1: evidence})
    assert report.should_pause
    assert any(reason.startswith("manual_diagonal_ratio=") and reason.endswith(" HARD")
               for reason in report.frames[0].reasons)
    assert "confirmed_mask_area_ratio=0.232 SOFT" in report.frames[0].reasons


@pytest.mark.parametrize("count", [30, 60])
def test_many_area_warnings_do_not_hide_one_new_true_loss(count):
    detector, boxes = _detector(count)
    for frame in (1, 2):
        report = _push(detector, frame, boxes)
        assert not report.should_pause
        assert all(level == AnomalyLevel.WARNING for level in report.object_levels.values())
    evidence = {oid: _evidence(oid, 3, box) for oid, box in boxes.items()}
    evidence[count].update(status="risk")
    evidence[count]["head"]["status"] = "lost"
    evidence[count]["coverage"]["status"] = "unknown"
    report = _push(detector, 3, boxes, evidence=evidence)
    assert report.should_pause
    assert report.object_levels[count] == AnomalyLevel.ANOMALY
    assert all(level == AnomalyLevel.WARNING for oid, level in report.object_levels.items() if oid != count)


@pytest.mark.parametrize("fault", [False, True])
def test_history_prime_and_fresh_push_use_same_mask_area_gate(fault, caplog):
    detector, boxes = _detector()
    caplog.set_level(logging.INFO, logger="review.tracking")
    rows = []
    for frame in (1, 2):
        evidence = _evidence(1, frame, boxes[1])
        if fault: evidence["sourceSignature"][0] = "wrong-source"
        rows.append({"source_frame_index": frame, "objects": [{"object_id": 1,
            "bbox": list(boxes[1]), "mask_area": CURRENT_AREA,
            "anomaly_details": {"identity_evidence": evidence}}]})
    detector.prime_history((row["source_frame_index"], boxes) for row in rows)
    detector.initialize_seed(2, boxes)
    detector.prime_observations(rows, 2, boxes)
    assert "tracking.mask_area_candidate " not in caplog.text
    evidence = _evidence(1, 3, boxes[1])
    if fault: evidence["sourceSignature"][0] = "wrong-source"
    report = _push(detector, 3, boxes, evidence={1: evidence})
    assert report.should_pause is fault
    assert detector.states[1].geometry_count == 0
    if not fault:
        assert report.frames[0].details["mask_area_decision"] == "reliable_current_subject_warning"
    assert len([record for record in caplog.records if record.message.startswith("tracking.mask_area_candidate ")]) == 1


def test_mask_area_logging_records_only_decision_changes_and_recovery(caplog):
    detector, boxes = _detector()
    caplog.set_level(logging.INFO, logger="review.tracking")
    assert not _push(detector, 1, boxes).should_pause
    assert not _push(detector, 2, boxes).should_pause
    assert not _push(detector, 3, boxes, area=1500).should_pause
    assert not _push(detector, 4, boxes, area=1500).should_pause
    assert not _push(detector, 5, boxes).should_pause
    assert _push(detector, 6, boxes, evidence={}).should_pause
    records = [record.message for record in caplog.records if record.message.startswith("tracking.mask_area_candidate")]
    assert len(records) == 4
    assert "frame=1 " in records[0] and "decision=reliable_current_subject_warning" in records[0]
    assert "candidate_cleared" in records[1] and "frame=3 " in records[1]
    assert "frame=5 " in records[2] and "previous=None" in records[2]
    assert "frame=6 " in records[3] and "decision=extreme_mask_loss_fallback" in records[3]
    assert "previous=reliable_current_subject_warning" in records[3]


def test_current_native_support_is_not_sticky_after_the_next_read_fails():
    detector, boxes = _detector()
    assert not _push(detector, 1, boxes).should_pause
    failed = _push(detector, 2, boxes, evidence={})
    assert failed.should_pause
    assert failed.frames[0].details["mask_area_decision"] == "extreme_mask_loss_fallback"


def test_disappearance_still_pauses_after_a_supported_area_warning():
    detector, boxes = _detector()
    assert not _push(detector, 1, boxes).should_pause
    report = _push(detector, 2, {}, evidence={})
    assert report.should_pause
    assert report.object_levels[1] == AnomalyLevel.DISAPPEARED
