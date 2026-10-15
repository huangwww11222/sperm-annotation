"""Policy tests for verified native observations with weak identity evidence.

The numbers mirror audited box changes, not medical labels or model inference.
These explicit summaries test the consumer's decision boundary; the producer's
native-image/Mask checks have their own image-based tests.
"""
import copy

import cv2
import numpy as np
import pytest

from app.services.anomaly_detector import AnomalyDetector, AnomalyLevel, ManualBaseline
from app.services.tracking_evidence import MaskRegion, TrackingEvidenceProbe


SOURCE = ["isolated-native-video", 12345, 67890, 42]


def _box(width=100, height=60, center=(300, 300)):
    x, y = center
    return [x-width/2, y-height/2, x+width/2, y+height/2]


def _detector(boxes=None):
    boxes = boxes or {1: _box()}
    detector = AnomalyDetector(frame_width=2048, frame_height=1500, fps=44.187,
        all_object_ids=boxes, manual_baselines={
            oid: ManualBaseline(list(box), 0, f"object-{oid}") for oid, box in boxes.items()})
    detector.image_source_signature = list(SOURCE)
    detector.initialize_seed(0, boxes)
    return detector


def _unknown(oid, frame, box, **changes):
    summary = {"version": 2, "source": "native-frame-mask-v2", "sourceSignature": list(SOURCE),
        "observationVerified": True, "objectId": oid, "frameIndex": frame,
        "bbox": list(box), "maskArea": 1000, "status": "unknown",
        "geometryReferenceFrameIndex": None, "geometryReferenceBBox": None,
        "head": {"status": "unknown", "point": None, "previousPoint": None,
                 "previousFrameIndex": None, "referencePoint": None,
                 "referenceFrameIndex": None, "referenceBBox": None,
                 "associationStatus": "unknown", "radius": None, "confidence": None},
        "coverage": {"status": "unknown", "metrics": {}},
        "reasons": ["reference_missing"], "thresholds": {}}
    summary.update(changes)
    return summary


def _matched(detector, oid, frame, box, *, status="supported", coverage="no_clear_loss", lost=False):
    baseline = detector.states[oid].manual_baseline
    reference_point = [(baseline.bbox[0]+baseline.bbox[2])/2, (baseline.bbox[1]+baseline.bbox[3])/2]
    point = [(box[0]+box[2])/2, (box[1]+box[3])/2]
    result = _unknown(oid, frame, box, status=status,
        geometryReferenceFrameIndex=baseline.frame_index, geometryReferenceBBox=list(baseline.bbox))
    result["head"].update(status="lost" if lost else "matched", associationStatus="reliable",
        point=point, radius=5., confidence=.99, referencePoint=reference_point,
        referenceFrameIndex=baseline.frame_index, referenceBBox=list(baseline.bbox),
        previousPoint=point, previousFrameIndex=frame-1)
    result["coverage"]["status"] = coverage
    return result


def _push(detector, frame, boxes, evidences=None):
    return detector.push(frame, boxes, mask_areas={oid: 1000 for oid in boxes},
        identity_evidence=evidences if evidences is not None else {
            oid: _unknown(oid, frame, box) for oid, box in boxes.items()})


@pytest.mark.parametrize("baseline,current", [
    ([1215, 580, 1281, 653], [1187, 577, 1277, 626]),  # audited page 84: 2.018 aspect change
    (_box(), _box(32, 160)),  # one axis 2.67x solely through a turn
    (_box(48, 45), _box(25, 27)),  # faint body, complete reference unknown
])
def test_verified_unknown_shape_remains_a_warning_without_learning_a_new_reference(baseline, current):
    detector = _detector({1: baseline})
    original = copy.deepcopy(detector.states[1].manual_baseline)
    reports = [_push(detector, frame, {1: current}) for frame in range(1, 21)]
    assert not any(report.should_pause for report in reports)
    assert all(report.object_levels[1] == AnomalyLevel.WARNING for report in reports)
    assert all(report.frames[0].details["identity_evidence"]["status"] == "unknown" for report in reports)
    assert detector.states[1].manual_baseline == original
    assert not detector.normal_motion_samples


def test_verified_natural_overlap_with_one_unknown_identity_never_builds_a_merge_streak():
    boxes = {17: [1195., 584., 1279., 639.], 18: [1196., 584., 1284., 640.]}
    detector = _detector({17: [1150., 584., 1234., 639.], 18: [1250., 584., 1338., 640.]})
    reports = []
    for frame in range(1, 41):
        evidence = {17: _matched(detector, 17, frame, boxes[17]),
                    18: _unknown(18, frame, boxes[18])}
        reports.append(_push(detector, frame, boxes, evidence))
    assert not any(report.should_pause for report in reports)
    pair = reports[-1].frames[0].details["overlap_pairs"][0]
    assert pair["level"] == "warning"
    assert pair["native_bbox_overlap_only"] is True
    assert pair["risk_streak"] == 0
    assert pair["independent_masks_supported"] is False
    assert reports[-1].frames[1].details["identity_evidence"]["status"] == "unknown"


@pytest.mark.parametrize("fault", ["missing_flag", "false_flag", "truthy_flag", "source", "unavailable_source",
    "old_frame", "bbox", "mask", "empty_mask", "version", "head", "stale_geometry", "bad_geometry_box",
    "unavailable_native_reason"])
def test_invalid_or_legacy_native_summary_retains_geometry_fallback(fault):
    detector = _detector()
    current = _box(55, 60)
    reports = []
    for frame in (1, 2, 3):
        evidence = _unknown(1, frame, current)
        if fault == "missing_flag": evidence.pop("observationVerified")
        elif fault == "false_flag": evidence["observationVerified"] = False
        elif fault == "truthy_flag": evidence["observationVerified"] = "true"
        elif fault == "source": evidence["sourceSignature"] = ["wrong", 12345, 67890, 42]
        elif fault == "unavailable_source": detector.image_source_signature = None
        elif fault == "old_frame": evidence["frameIndex"] = frame-1
        elif fault == "bbox": evidence["bbox"] = _box()
        elif fault == "mask": evidence["maskArea"] = None
        elif fault == "empty_mask": evidence["maskArea"] = 0
        elif fault == "version": evidence.update(version=1, source="native-frame-mask-v1")
        elif fault == "head": evidence["head"]["status"] = "pretend-normal"
        elif fault in {"stale_geometry", "bad_geometry_box"}:
            evidence = _matched(detector, 1, frame, current, status="unknown", coverage="unknown")
            if fault == "stale_geometry": evidence["geometryReferenceFrameIndex"] = frame
            else: evidence["geometryReferenceBBox"] = current
        elif fault == "unavailable_native_reason": evidence["reasons"] = ["native_image_unavailable"]
        reports.append(_push(detector, frame, {1: current}, {1: evidence}))
    assert [report.should_pause for report in reports] == [False, False, True]


@pytest.mark.parametrize("current", [_box(20, 10), _box(320, 200)])
def test_verified_unknown_cannot_exempt_clear_whole_box_collapse_or_explosion(current):
    detector = _detector()
    report = _push(detector, 1, {1: current})
    assert report.should_pause
    assert report.object_levels[1] == AnomalyLevel.ANOMALY


@pytest.mark.parametrize("kind", ["head_lost", "tail_loss", "merge", "foreign_core"])
def test_independent_subject_risk_still_pauses_in_naturally_overlapping_boxes(kind):
    boxes = {17: [1195., 584., 1279., 639.], 18: [1196., 584., 1284., 640.]}
    detector = _detector(boxes)
    reports = []
    for frame in (1, 2, 3):
        evidence = {17: _matched(detector, 17, frame, boxes[17]),
            18: _matched(detector, 18, frame, boxes[18], status="risk",
                coverage="unknown" if kind == "head_lost" else kind, lost=kind == "head_lost")}
        reports.append(_push(detector, frame, boxes, evidence))
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].object_levels[18] == AnomalyLevel.ANOMALY
    assert any(reason.startswith("image_") and reason.endswith(" HARD") for reason in reports[-1].frames[1].reasons)


def test_verified_unknown_old_overlap_rows_cannot_restore_a_bbox_only_hard_pause():
    # The source reports were published before a resume. Their old aggregate
    # overlap labels must not manufacture independent subject-loss evidence.
    boxes = {17: [1195., 584., 1279., 639.], 18: [1196., 584., 1284., 640.]}
    detector = _detector(boxes)
    rows = [{"source_frame_index": frame, "objects": [
        {"object_id": oid, "bbox": list(box), "mask_area": 1000,
         "anomaly_details": {"identity_evidence": _unknown(oid, frame, box),
            "overlap_pairs": [{"object_ids": [17, 18], "suspected_merge": True,
                "observation_started_frame": 1, "risk_streak": frame, "level": "anomaly"}]}}
        for oid, box in boxes.items()]} for frame in (1, 2, 3)]
    detector.prime_history((row["source_frame_index"], boxes) for row in rows[:2])
    detector.initialize_seed(3, boxes)
    detector.prime_observations(rows, 3, boxes)
    report = _push(detector, 4, boxes)
    assert not report.should_pause
    assert all(item.details["overlap_pairs"][0]["risk_streak"] == 0 for item in report.frames)


def test_verified_native_permission_is_not_sticky_after_a_decode_failure():
    detector = _detector()
    current = _box(55, 60)
    assert not _push(detector, 1, {1: current}).should_pause
    reports = [_push(detector, frame, {1: current}, {1: _unknown(
        1, frame, current, observationVerified=False, reasons=["native_image_unavailable"])})
        for frame in (2, 3, 4)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 2


def test_verified_unknown_unchanged_bbox_is_visible_without_becoming_a_loss_streak():
    detector = _detector()
    reports = [_push(detector, frame, {1: _box()}) for frame in range(1, 16)]
    assert not any(report.should_pause for report in reports)
    assert all(report.object_levels[1] == AnomalyLevel.WARNING for report in reports)
    assert all(report.frames[0].details['image_quality_reasons'] == ['reference_missing'] for report in reports)
    assert all(report.frames[0].details['identity_evidence']['status'] == 'unknown' for report in reports)
    assert detector.states[1].image_count == 0


def test_independently_matched_native_subject_recovers_from_unknown_warning():
    detector = _detector()
    pending = _push(detector, 1, {1: _box()})
    assert pending.object_levels[1] == AnomalyLevel.WARNING
    matched = _matched(detector, 1, 2, _box())
    recovered = _push(detector, 2, {1: _box()}, {1: matched})
    assert not recovered.should_pause
    assert recovered.object_levels[1] == AnomalyLevel.NORMAL
    assert recovered.frames[0].details['identity_evidence']['status'] == 'supported'
    assert 'image_quality_reasons' not in recovered.frames[0].details


def test_accepted_mask_collapse_is_independent_of_stable_native_bbox():
    detector = _detector()
    baseline = detector.states[1].manual_baseline
    detector.prime_manual_baselines({1: ManualBaseline(baseline.bbox, 0, mask_area=6000)})
    report = _push(detector, 1, {1: _box()})
    assert report.should_pause
    assert "confirmed_mask_area_ratio=0.167 HARD" in report.frames[0].reasons


def test_verified_native_summary_cannot_approve_an_absent_object():
    detector = _detector()
    report = _push(detector, 1, {}, {1: _unknown(1, 1, _box())})
    assert report.should_pause
    assert report.object_levels[1] == AnomalyLevel.DISAPPEARED


def test_verified_subject_jump_still_pauses_immediately():
    detector = _detector()
    moved = _box(center=(800, 300))
    evidence = _matched(detector, 1, 1, moved)
    evidence["head"]["previousPoint"] = [300, 300]
    report = _push(detector, 1, {1: moved}, {1: evidence})
    assert report.should_pause
    assert report.frames[0].details["motion_reference_kind"] == "head"


@pytest.mark.parametrize("count", [28, 40])
def test_many_normal_shape_changes_do_not_hide_one_independent_subject_loss(count):
    boxes = {oid: _box(center=(150+(oid-1)%6*300, 150+(oid-1)//6*180))
             for oid in range(1, count+1)}
    detector = _detector(boxes)
    reports = []
    for frame in (1, 2, 3):
        current = {oid: _box(32, 160, ((box[0]+box[2])/2, (box[1]+box[3])/2))
                   for oid, box in boxes.items()}
        evidences = {oid: _unknown(oid, frame, box) for oid, box in current.items()}
        evidences[count] = _matched(detector, count, frame, current[count], status="risk", lost=True, coverage="unknown")
        reports.append(_push(detector, frame, current, evidences))
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].object_levels[count] == AnomalyLevel.ANOMALY
    assert all(level == AnomalyLevel.WARNING for oid, level in reports[-1].object_levels.items() if oid != count)


def test_real_native_producer_unknown_can_warn_about_overlap_without_fabricating_identity():
    """Exercise actual native-image/Mask validation, not an asserted flag."""
    image = np.full((240, 320, 3), 145, dtype=np.uint8)
    cv2.circle(image, (100, 100), 8, (35, 35, 35), -1)
    cv2.circle(image, (120, 104), 8, (45, 45, 45), -1)
    boxes = {1: [88., 88., 140., 130.], 2: [89., 88., 141., 130.]}
    detector = AnomalyDetector(frame_width=320, frame_height=240,
        all_object_ids=boxes, manual_baselines={
            oid: ManualBaseline(list(box), 0) for oid, box in boxes.items()})
    detector.image_source_signature = list(SOURCE)
    detector.initialize_seed(0, boxes)
    probe = TrackingEvidenceProbe(fps=30, frame_width=320, frame_height=240)
    probe.source_signature = list(SOURCE)
    mask = np.zeros((42, 52), dtype=np.uint8)
    cv2.circle(mask, (12, 12), 8, 1, -1)
    cv2.circle(mask, (32, 16), 8, 1, -1)
    regions = {oid: MaskRegion(mask.copy(), (int(box[0]), int(box[1]))) for oid, box in boxes.items()}
    areas = {oid: int(mask.sum()) for oid in boxes}
    for frame in range(1, 16):
        evidences = probe.observe(frame, image, boxes, regions)
        assert all(item['observationVerified'] is True for item in evidences.values())
        assert all(item['status'] == 'unknown' and item['head']['point'] is None for item in evidences.values())
        report = detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences)
        assert not report.should_pause
        assert all(item.level == AnomalyLevel.WARNING for item in report.frames)


def _shared_core_scene():
    # The initial second manual rectangle really contains two candidate heads;
    # it cannot silently acquire the known object's identity on a later frame.
    initial_image = np.full((240, 320, 3), 145, np.uint8)
    for point in ((90, 100), (122, 100), (145, 100)):
        cv2.circle(initial_image, point, 5, (35, 35, 35), -1)
    manual = {1: [65., 75., 115., 125.], 2: [105., 75., 165., 125.]}
    boxes = {1: [65., 75., 115., 125.], 2: [66., 75., 116., 125.]}
    mask = np.zeros((240, 320), np.uint8)
    cv2.circle(mask, (90, 100), 7, 1, -1)
    probe = TrackingEvidenceProbe(fps=30, frame_width=320, frame_height=240)
    probe.source_signature = list(SOURCE)
    initial = probe.prime(0, initial_image, manual)
    assert initial[1]['head']['status'] == 'matched'
    assert initial[2]['reasons'] == ['reference_core_ambiguous']
    detector = AnomalyDetector(frame_width=320, frame_height=240,
        all_object_ids=manual, manual_baselines={
            oid: ManualBaseline(list(box), 0) for oid, box in manual.items()})
    detector.image_source_signature = list(SOURCE)
    detector.initialize_seed(0, manual)
    # Only one actual optical core is in the two later BBoxes, and both model
    # Masks have genuinely collapsed onto the same foreground pixels.
    image = np.full((240, 320, 3), 145, np.uint8)
    cv2.circle(image, (90, 100), 5, (35, 35, 35), -1)
    regions = {oid: MaskRegion(mask.copy()) for oid in boxes}
    areas = {oid: int(mask.sum()) for oid in boxes}
    return detector, probe, image, boxes, regions, areas


def test_real_native_single_core_shared_with_unreferenced_object_eventually_pauses():
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    reports = []
    for frame in range(1, 13):
        evidences = probe.observe(frame, image, boxes, regions)
        assert evidences[2]['status'] == 'unknown'
        assert evidences[2]['head']['point'] is None
        assert 'identityReference' not in evidences[2]
        reports.append(detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences))
    assert not any(report.should_pause for report in reports[:11])
    assert reports[-1].should_pause
    assert reports[-1].pause_frame_index == 1
    assert reports[-1].object_levels[1] == AnomalyLevel.WARNING
    assert reports[-1].object_levels[2] == AnomalyLevel.ANOMALY
    item = next(item for item in reports[-1].frames if item.object_id == 2)
    assert item.details['image_observation_category'] == 'shared_core_unresolved'
    assert 'image_shared_core_unresolved observations=12/12 HARD' in item.reasons
    assert item.details['identity_evidence']['status'] == 'unknown'


def test_short_shared_core_occlusion_clears_when_two_actual_heads_are_visible_again():
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    for frame in range(1, 12):
        evidences = probe.observe(frame, image, boxes, regions)
        assert not detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences).should_pause
    recovered = image.copy()
    cv2.circle(recovered, (105, 100), 5, (35, 35, 35), -1)
    larger = regions[1].data.copy()
    cv2.circle(larger, (105, 100), 7, 1, -1)
    two_regions = {oid: MaskRegion(larger.copy()) for oid in boxes}
    two_areas = {oid: int(larger.sum()) for oid in boxes}
    evidences = probe.observe(12, recovered, boxes, two_regions)
    assert 'singleCoreConflict' not in evidences[2]
    report = detector.push(12, boxes, mask_areas=two_areas, identity_evidence=evidences)
    assert not report.should_pause
    assert detector.states[2].image_count == 0
    # The first run was not converted into an approval, nor may it be counted
    # as a continuation of a new independent episode after the recovery frame.
    for frame in range(13, 25):
        evidences = probe.observe(frame, image, boxes, regions)
        report = detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences)
        assert report.should_pause is (frame == 24)
    assert report.pause_frame_index == 13


@pytest.mark.parametrize('fault', ['peer_flag', 'peer_source', 'peer_frame', 'peer_box', 'peer_unknown',
    'point', 'radius', 'iou', 'count', 'coverage', 'peer_coverage', 'one_sided_overlap', 'reason'])
def test_single_core_diagnosis_needs_a_matching_trusted_current_partner(fault):
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    for frame in range(1, 16):
        evidences = probe.observe(frame, image, boxes, regions)
        assert 'singleCoreConflict' in evidences[2]
        value = evidences[2]['singleCoreConflict']
        if fault == 'peer_flag': evidences[1]['observationVerified'] = False
        elif fault == 'peer_source': evidences[1]['sourceSignature'] = ['wrong', 1, 2, 3]
        elif fault == 'peer_frame': evidences[1]['frameIndex'] = frame-1
        elif fault == 'peer_box': evidences[1]['bbox'] = [65., 75., 116., 125.]
        elif fault == 'peer_unknown': evidences[1].update(status='unknown', head={'status': 'unknown'})
        elif fault == 'point': value['point'] = [110., 100.]
        elif fault == 'radius': value['radius'] = 100.
        elif fault == 'iou': value['maskIoU'] = .89
        elif fault == 'count': value['visibleCoreCount'] = True
        elif fault == 'coverage': value['maskHeadCoverage'] = .79
        elif fault == 'peer_coverage': value['partnerMaskHeadCoverage'] = .79
        elif fault == 'one_sided_overlap': evidences[1]['coverage']['metrics']['maskOverlap'].pop('2')
        elif fault == 'reason': value['reason'] = 'client_says_normal'
        report = detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences)
        item = next(item for item in report.frames if item.object_id == 2)
        assert item.details.get('image_observation_category') != 'shared_core_unresolved'
        if fault not in {'peer_flag', 'peer_source', 'peer_frame', 'peer_box', 'peer_unknown'}:
            assert item.level != AnomalyLevel.ANOMALY


def test_shared_core_observation_survives_restart_without_reclassifying_unknown_identity():
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    rows = []
    for frame in range(1, 9):
        evidences = probe.observe(frame, image, boxes, regions)
        report = detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences)
        assert not report.should_pause
        rows.append({'source_frame_index': frame, 'objects': [
            {'object_id': item.object_id, 'bbox': list(boxes[item.object_id]),
             'mask_area': areas[item.object_id], 'anomaly_details': copy.deepcopy(item.details)}
            for item in report.frames]})
    restored, _, _, _, _, _ = _shared_core_scene()
    restored.prime_history((row['source_frame_index'], boxes) for row in rows[:-1])
    restored.initialize_seed(8, boxes)
    restored.prime_observations(rows, 8, boxes)
    assert restored.states[2].image_count == 8
    for frame in range(9, 13):
        evidences = probe.observe(frame, image, boxes, regions)
        report = restored.push(frame, boxes, mask_areas=areas, identity_evidence=evidences)
        assert report.should_pause is (frame == 12)
    assert report.pause_frame_index == 1
    assert report.frames[1].details['identity_evidence']['head']['point'] is None


def test_single_core_observation_gap_starts_a_new_episode():
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    for frame in range(1, 7):
        evidences = probe.observe(frame, image, boxes, regions)
        assert not detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences).should_pause
    for frame in range(8, 20):
        evidences = probe.observe(frame, image, boxes, regions)
        report = detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences)
        assert report.should_pause is (frame == 19)
    assert report.pause_frame_index == 8


def test_single_core_observation_does_not_accumulate_across_different_trusted_partners():
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    for frame in range(1, 7):
        evidences = probe.observe(frame, image, boxes, regions)
        assert not detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidences).should_pause
    # Explicit synthetic policy input for a different server-bound partner;
    # the read/core measurements come from the actual producer above. The
    # alternate ID is not a claim that this physical frame has a third sperm.
    detector.prime_manual_baselines({3: ManualBaseline(list(boxes[1]), 0)})
    current = {2: boxes[2], 3: boxes[1]}
    for frame in range(7, 19):
        measured = probe.observe(frame, image, boxes, regions)
        peer = copy.deepcopy(measured[1])
        peer['objectId'] = 3
        unknown = copy.deepcopy(measured[2])
        unknown['singleCoreConflict']['partnerObjectId'] = 3
        unknown['coverage']['metrics']['maskOverlap'] = {'3': 1.0}
        report = detector.push(frame, current, mask_areas={2: areas[2], 3: areas[1]},
            identity_evidence={2: unknown, 3: peer}, ignored_object_ids=[1])
        assert report.should_pause is (frame == 18)
    assert report.pause_frame_index == 7
    item = next(item for item in report.frames if item.object_id == 2)
    assert item.details['image_observation_partner_object_id'] == 3


@pytest.mark.parametrize('location', ['peer_head', 'unknown_coverage', 'peer_coverage'])
@pytest.mark.parametrize('phase', ['push', 'prime'])
def test_malformed_optional_metrics_are_rejected_without_crashing_and_then_recover(location, phase, caplog):
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    evidences = probe.observe(1, image, boxes, regions)
    if location == 'peer_head': evidences[1]['head']['metrics'] = 1
    elif location == 'unknown_coverage': evidences[2]['coverage']['metrics'] = 1
    else: evidences[1]['coverage']['metrics'] = 1
    if phase == 'push':
        report = detector.push(1, boxes, mask_areas=areas, identity_evidence=evidences)
        assert all(item.details.get('image_observation_category') != 'shared_core_unresolved' for item in report.frames)
    else:
        row = {'source_frame_index': 1, 'objects': [
            {'object_id': oid, 'bbox': box, 'mask_area': areas[oid],
             'anomaly_details': {'identity_evidence': evidences[oid]}}
            for oid, box in boxes.items()]}
        detector.initialize_seed(1, boxes)
        detector.prime_observations([row], 1, boxes)
    assert 'invalid_evidence_metrics' in caplog.text
    # A later valid current observation starts independently after rejection;
    # the malformed metadata must neither poison nor silently approve identity.
    for frame in range(2, 14):
        healthy = probe.observe(frame, image, boxes, regions)
        report = detector.push(frame, boxes, mask_areas=areas, identity_evidence=healthy)
        assert report.should_pause is (frame == 13)
    assert report.pause_frame_index == 2


def test_legacy_summaries_may_omit_optional_metrics_without_rejection(caplog):
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    evidences = probe.observe(1, image, boxes, regions)
    for item in evidences.values():
        item['head'].pop('metrics', None)
        item['coverage'].pop('metrics', None)
        item.pop('singleCoreConflict', None)
    report = detector.push(1, boxes, mask_areas=areas, identity_evidence=evidences)
    assert not report.should_pause
    assert not any('identity_evidence_error' in item.details for item in report.frames)
    assert 'invalid_evidence_metrics' not in caplog.text
