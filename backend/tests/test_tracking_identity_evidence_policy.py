"""Behavior contracts for corroborated tracking evidence, without model inference.

Evidence payloads below are explicit synthetic policy inputs, not proof that a
Mask circle is a sperm head. Raw-image/Mask extraction has separate tests. The
25/30-object scenarios exercise independent object decisions; no medical video,
real annotation file, model weight or business database is a fixture here.
"""

import copy
import math

import pytest

from app.services.anomaly_detector import AnomalyDetector, AnomalyLevel, ConfirmedGeometryReference, ManualBaseline


def _box(width=100, height=60, center=(260, 270)):
    x, y = center
    return [float(x - width / 2), float(y - height / 2),
            float(x + width / 2), float(y + height / 2)]


def _center(box):
    return [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]


def _detector(boxes=None, *, frame=0, confirmed=False, fps=44.187):
    boxes = boxes or {1: _box()}
    baselines = {
        oid: ManualBaseline(list(box), frame, f"object-{oid}",
                            "confirmed-normal" if confirmed else "manual",
                            1000 if confirmed else None)
        for oid, box in boxes.items()
    }
    detector = AnomalyDetector(frame_width=4096, frame_height=3000, fps=fps,
                               all_object_ids=boxes, manual_baselines=baselines)
    detector.initialize_seed(frame, boxes)
    return detector


def _evidence(oid, frame, box, *, status="supported", head_status="matched",
              coverage_status="no_clear_loss", point=None, previous_point=None,
              previous_frame=None, reference_frame=0, reference_point=None,
              mask_area=1000, reasons=None):
    """Explicit synthetic v2 identity and independent human geometry binding."""
    head = {"status": head_status, "confidence": .97, "associationStatus": "reliable",
            "referencePoint": list(reference_point if reference_point is not None else [260, 270]),
            "referenceFrameIndex": reference_frame,
            "referenceBBox": _box(center=reference_point if reference_point is not None else [260,270])}
    if head_status == "matched":
        head.update(point=list(point or _center(box)), radius=5.0)
    if previous_point is not None:
        head.update(previousPoint=list(previous_point),
                    previousFrameIndex=frame - 1 if previous_frame is None else previous_frame)
    return {
        "version": 2, "source": "native-frame-mask-v2", "objectId": oid,
        "geometryReferenceFrameIndex": reference_frame,
        "geometryReferenceBBox": _box(center=head["referencePoint"]),
        "frameIndex": frame, "bbox": list(box), "maskArea": mask_area,
        "status": status, "head": head,
        "coverage": {"status": coverage_status},
        "reasons": list(reasons or []), "thresholds": {},
    }


def _push(detector, frame, boxes, *, evidences=None, mask_areas=None, ignored=None):
    areas = mask_areas if mask_areas is not None else {oid: 1000 for oid in boxes}
    kwargs = {"mask_areas": areas}
    if ignored is not None:
        kwargs["ignored_object_ids"] = ignored
    if evidences is not None:
        for oid, evidence in evidences.items():
            baseline = detector.states.get(oid).manual_baseline if oid in detector.states else None
            if baseline and evidence.get("geometryReferenceFrameIndex") == baseline.frame_index:
                evidence["geometryReferenceBBox"] = list(baseline.bbox)
                if evidence["head"].get("referenceFrameIndex") == baseline.frame_index:
                    evidence["head"]["referenceBBox"] = list(baseline.bbox)
        kwargs["identity_evidence"] = evidences
    return detector.push(frame, boxes, **kwargs)


def _item(report, oid=1):
    return next(item for item in report.frames if item.object_id == oid)


def _supported(boxes, frame, *, previous=None, reference=None):
    return {oid: _evidence(oid, frame, box, previous_point=(previous or {}).get(oid, _center(box)),
                          reference_point=(reference or previous or {}).get(oid, _center(box)))
            for oid, box in boxes.items()}


@pytest.mark.parametrize("width,height", [(32, 160), (160, 32), (50, 120), (120, 40)])
def test_supported_tail_direction_changes_warn_without_pausing(width, height):
    detector = _detector()
    current = _box(width, height)
    before = copy.deepcopy(detector.states[1].manual_baseline)
    reports = [_push(detector, frame, {1: current},
                     evidences={1: _evidence(1, frame, current, previous_point=[260, 270])})
               for frame in (1, 2, 3, 4)]
    assert not any(report.should_pause for report in reports)
    assert all(report.object_levels[1] == AnomalyLevel.WARNING for report in reports)
    assert detector.states[1].manual_baseline == before
    assert not detector.normal_motion_samples


def test_explicit_checkpoint_followed_by_aspect_flip_does_not_pause_again():
    detector = _detector(frame=8, confirmed=True)
    flipped = _box(32, 160)
    baseline = detector.states[1].manual_baseline
    for frame in (9, 10, 11):
        report = _push(detector, frame, {1: flipped},
                       evidences={1: _evidence(1, frame, flipped, previous_point=[260, 270], reference_frame=8)})
        assert not report.should_pause
        assert report.object_levels[1] == AnomalyLevel.WARNING
    assert detector.states[1].manual_baseline == baseline
    assert detector.states[1].manual_baseline.frame_index == 8
    assert detector.states[1].manual_baseline.source == "confirmed-normal"


@pytest.mark.parametrize("count", [25, 30])
def test_many_objects_taking_turns_rotating_do_not_accumulate_a_global_pause(count):
    boxes = {oid: _box(center=(260 + ((oid - 1) % 6) * 450,
                                      270 + ((oid - 1) // 6) * 450))
             for oid in range(1, count + 1)}
    detector = _detector(boxes)
    baseline = {oid: copy.deepcopy(state.manual_baseline) for oid, state in detector.states.items()}
    for frame in range(1, count * 3 + 1):
        changing = 1 + (frame - 1) // 3
        current = {oid: _box(32, 160, _center(box)) if oid == changing else list(box)
                   for oid, box in boxes.items()}
        report = _push(detector, frame, current,
                       evidences=_supported(current, frame, previous={oid: _center(box) for oid, box in boxes.items()}))
        assert not report.should_pause, f"false interruption with {count} objects, frame {frame}"
        assert report.object_levels[changing] == AnomalyLevel.WARNING
    assert {oid: state.manual_baseline for oid, state in detector.states.items()} == baseline


@pytest.mark.parametrize("head_status,coverage_status", [
    ("lost", "unknown"), ("ambiguous", "no_clear_loss"),
    ("matched", "tail_loss"), ("matched", "foreign_core"), ("matched", "merge"),
])
def test_independent_identity_or_coverage_risk_pauses_even_when_bbox_is_unchanged(head_status, coverage_status):
    detector = _detector()
    current = _box()
    reports = [_push(detector, frame, {1: current}, evidences={1: _evidence(
        1, frame, current, status="risk", head_status=head_status,
        coverage_status=coverage_status, previous_point=[260, 270],
        reasons=[f"identity_or_coverage:{head_status}:{coverage_status}"])})
        for frame in (1, 2, 3)]
    assert any(report.should_pause for report in reports), "head/core or tail evidence must be independent of bbox ratios"
    paused = next(report for report in reports if report.should_pause)
    assert paused.object_levels[1] == AnomalyLevel.ANOMALY
    assert _item(paused).reasons


def test_real_object_16_like_foreign_core_evidence_cannot_be_cleared_by_head_presence():
    # Anonymized numeric geometry reproduces the audited 83x36 vs 75x69
    # direction change. The independent optical/core metrics are synthetic
    # inputs expressing a suspicious extra core; they are not clinical truth.
    detector = _detector({16: _box(75, 69)})
    current = _box(83, 36)
    reports = []
    for frame in (1, 2, 3):
        evidence = _evidence(16, frame, current, status="risk", coverage_status="foreign_core",
                             point=[226, 281], previous_point=[226, 281], reference_point=[226, 281],
                             reasons=["foreign_optical_core_in_mask"])
        evidence["coverage"].update(maxCircleToExpectedHeadPx=56.72, maxCircleToForeignCorePx=4.0)
        reports.append(_push(detector, frame, {16: current}, evidences={16: evidence}))
    assert any(report.should_pause for report in reports)
    assert next(report for report in reports if report.should_pause).object_levels[16] == AnomalyLevel.ANOMALY


@pytest.mark.parametrize("count", [25, 30])
def test_one_real_loss_among_supported_rotating_objects_still_pauses(count):
    boxes = {oid: _box(center=(260 + ((oid - 1) % 6) * 450,
                                      270 + ((oid - 1) // 6) * 450))
             for oid in range(1, count + 1)}
    detector = _detector(boxes)
    reports = []
    for frame in (1, 2, 3):
        current = {oid: _box(32, 160, _center(box)) for oid, box in boxes.items()}
        evidence = _supported(current, frame)
        evidence[count] = _evidence(count, frame, current[count], status="risk",
                                    head_status="lost", coverage_status="unknown",
                                    previous_point=_center(boxes[count]), reference_point=_center(boxes[count]),
                                    reasons=["tracked_subject_missing"])
        reports.append(_push(detector, frame, current, evidences=evidence))
    assert any(report.should_pause for report in reports)
    paused = next(report for report in reports if report.should_pause)
    assert paused.object_levels[count] == AnomalyLevel.ANOMALY
    assert all(level != AnomalyLevel.ANOMALY for oid, level in paused.object_levels.items() if oid != count)


@pytest.mark.parametrize("width,height", [(20, 25), (310, 100)])
def test_supported_subject_does_not_exempt_catastrophic_bbox_area(width, height):
    detector = _detector()
    current = _box(width, height)
    report = _push(detector, 1, {1: current},
                   evidences={1: _evidence(1, 1, current, previous_point=[260, 270])})
    assert report.should_pause
    assert report.object_levels[1] == AnomalyLevel.ANOMALY


def test_supported_head_does_not_exempt_catastrophic_confirmed_mask_loss():
    detector = _detector(frame=8, confirmed=True)
    current = _box()
    report = _push(detector, 9, {1: current}, mask_areas={1: 240},
                   evidences={1: _evidence(1, 9, current, previous_point=[260, 270], reference_frame=8, mask_area=240)})
    assert report.should_pause
    assert any(reason.startswith("confirmed_mask_area_ratio=") for reason in _item(report).reasons)


@pytest.mark.parametrize("unknown_reason", ["low_contrast", "image_unavailable", "mask_unavailable",
                                           "unverified_distance_circle_only", "tail_coverage_unknown"])
def test_unknown_evidence_retains_bbox_fallback_instead_of_approving_normality(unknown_reason):
    detector = _detector()
    current = _box(55, 60)
    mask_area = None if unknown_reason == "mask_unavailable" else 1000
    reports = [_push(detector, frame, {1: current}, mask_areas={} if mask_area is None else {1: mask_area},
                     evidences={1: _evidence(
        1, frame, current, status="unknown", head_status="unknown", coverage_status="unknown",
        mask_area=mask_area, reasons=[unknown_reason])}) for frame in (1, 2, 3)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].object_levels[1] == AnomalyLevel.ANOMALY


@pytest.mark.parametrize("binding", ["old_frame", "future_frame", "other_object", "other_bbox", "other_mask_area",
                                    "version", "boolean_version", "boolean_frame", "nonfinite_point",
                                    "nonfinite_radius", "invalid_previous_frame", "point_outside_bbox"])
def test_wrong_or_invalid_evidence_binding_cannot_suppress_bbox_protection(binding):
    detector = _detector()
    current = _box(55, 60)
    reports = []
    for frame in (1, 2, 3):
        evidence = _evidence(1, frame, current, previous_point=[260, 270])
        if binding == "old_frame": evidence["frameIndex"] = frame - 1
        elif binding == "future_frame": evidence["frameIndex"] = frame + 1
        elif binding == "other_object": evidence["objectId"] = 2
        elif binding == "other_bbox": evidence["bbox"] = _box()
        elif binding == "other_mask_area": evidence["maskArea"] = 2000
        elif binding == "version": evidence["version"] = 3
        elif binding == "boolean_version": evidence["version"] = True
        elif binding == "boolean_frame": evidence["frameIndex"] = True
        elif binding == "nonfinite_point": evidence["head"]["point"] = [math.nan, 270]
        elif binding == "nonfinite_radius": evidence["head"]["radius"] = math.inf
        elif binding == "invalid_previous_frame": evidence["head"]["previousFrameIndex"] = frame + 1
        elif binding == "point_outside_bbox":
            evidence["head"]["point"] = [3000, 2500]
            evidence["head"]["previousPoint"] = [3000, 2500]
        reports.append(_push(detector, frame, {1: current}, evidences={1: evidence}))
    assert [report.should_pause for report in reports] == [False, False, True]


def test_plain_bbox_only_call_keeps_existing_slow_shrink_protection():
    detector = _detector()
    current = _box(55, 60)
    reports = [_push(detector, frame, {1: current}) for frame in (1, 2, 3)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 1


def test_absent_object_cannot_be_rescued_by_stale_supported_evidence():
    detector = _detector()
    report = _push(detector, 1, {}, evidences={1: _evidence(1, 1, _box(), previous_point=[260, 270])})
    assert report.should_pause
    assert report.object_levels[1] == AnomalyLevel.DISAPPEARED


def test_single_frame_ignored_loss_does_not_disable_next_frame_disappearance():
    detector = _detector()
    first = _push(detector, 1, {}, evidences={1: _evidence(
        1, 1, _box(), status="risk", head_status="lost", coverage_status="unknown")}, ignored=[1])
    assert not first.should_pause
    next_frame = _push(detector, 2, {}, evidences={})
    assert next_frame.should_pause
    assert next_frame.object_levels[1] == AnomalyLevel.DISAPPEARED


def test_subject_evidence_does_not_turn_natural_edge_departure_into_error():
    edge = _box(10, 10, center=(4090, 270))
    detector = _detector({1: edge})
    report = _push(detector, 1, {}, evidences={})
    assert not report.should_pause


def test_verified_subject_large_jump_remains_an_immediate_pause():
    detector = _detector()
    moved = _box(center=(710, 270))
    report = _push(detector, 1, {1: moved}, evidences={1: _evidence(
        1, 1, moved, point=[710, 270], previous_point=[260, 270], previous_frame=0)})
    assert report.should_pause
    assert report.object_levels[1] == AnomalyLevel.ANOMALY


def test_bbox_center_change_at_high_fps_does_not_override_a_supported_stable_subject():
    detector = _detector(fps=120)
    # The foreground extends towards the upper/right tail endpoint while its
    # verified rigid subject remains fixed. Area remains below the separate
    # catastrophic limit. At 120 FPS the bbox-center delta alone exceeds the
    # historical extreme-motion threshold, so geometry-only suppression fails.
    current = _box(130, 175, center=(315, 350))
    report = _push(detector, 1, {1: current}, evidences={1: _evidence(
        1, 1, current, point=[260, 270], previous_point=[260, 270], previous_frame=0)})
    assert not report.should_pause
    assert report.object_levels[1] == AnomalyLevel.WARNING


def test_supported_subject_does_not_exempt_new_sustained_bbox_merge():
    original = {1: _box(center=(260, 270)), 2: _box(center=(710, 270))}
    detector = _detector(original)
    # Separate boxes at the first observation avoid the historical first-report
    # pair guard. Then both IDs predict one region: the new merge stays unsafe.
    _push(detector, 1, original, evidences=_supported(original, 1))
    merged = {1: _box(center=(485, 270)), 2: _box(center=(486, 270))}
    reports = [_push(detector, frame, merged, evidences=_supported(
        merged, frame, previous={oid: _center(box) for oid, box in (original if frame == 2 else merged).items()},
        reference={oid: _center(box) for oid, box in original.items()}))
        for frame in (2, 3, 4)]
    assert any(report.should_pause for report in reports)
    paused = next(report for report in reports if report.should_pause)
    assert all(paused.object_levels[oid] == AnomalyLevel.ANOMALY for oid in (1, 2))


def test_supported_frame_clears_pending_bbox_streak_without_accepting_ai_baseline():
    detector = _detector()
    pending = _box(55, 60)
    assert not _push(detector, 1, {1: pending}).should_pause
    assert not _push(detector, 2, {1: pending}).should_pause
    baseline = copy.deepcopy(detector.states[1].manual_baseline)
    report = _push(detector, 3, {1: pending},
                   evidences={1: _evidence(1, 3, pending, previous_point=[260, 270])})
    assert not report.should_pause
    assert report.object_levels[1] == AnomalyLevel.WARNING
    assert detector.states[1].manual_baseline == baseline
    # Evidence loss resumes observation from an uncertain frame, not a stale
    # hard count accumulated before the independently supported observation.
    after = [_push(detector, frame, {1: pending}, evidences={}) for frame in (4, 5, 6)]
    assert [item.should_pause for item in after] == [False, False, True]
    assert after[-1].pause_frame_index == 4


def _pair_fixture():
    original = {1: [200., 240., 260., 300.], 2: [270., 240., 330., 300.]}
    merged_boxes = {1: [200., 240., 330., 300.], 2: [200., 240., 330., 300.]}
    points = {1: [230., 270.], 2: [300., 270.]}
    detector = _detector(original)
    _push(detector, 1, original, evidences=_supported(original, 1))
    return detector, merged_boxes, points


@pytest.mark.parametrize("mode", ["independent", "missing", "one_sided", "nan", "collision", "boundary", "high_other"])
def test_overlapping_bbox_requires_two_real_small_mask_ious_and_distinct_subjects(mode):
    detector, boxes, points = _pair_fixture()
    reports = []
    for frame in (2, 3, 4):
        evidence = {}
        for oid in (1, 2):
            point = [265., 270.] if mode == "collision" else points[oid]
            item = _evidence(oid, frame, boxes[oid], point=point,
                             previous_point=points[oid] if frame == 2 else point, reference_point=points[oid])
            if mode != "missing" and not (mode == "one_sided" and oid == 2):
                overlap = .20 if mode == "boundary" else .21 if mode == "high_other" and oid == 2 else .08
                if mode == "nan" and oid == 2:
                    overlap = math.nan
                item["coverage"]["metrics"] = {"maskOverlap": {str(3 - oid): overlap}}
            evidence[oid] = item
        reports.append(_push(detector, frame, boxes, evidences=evidence))
    if mode == "independent":
        assert not any(report.should_pause for report in reports)
        assert all(all(level == AnomalyLevel.WARNING for level in report.object_levels.values()) for report in reports)
        assert all(_item(report, 1).details["overlap_pairs"][0]["independent_masks_supported"] for report in reports)
    else:
        assert [report.should_pause for report in reports] == [False, False, True]
        assert all(reports[-1].object_levels[oid] == AnomalyLevel.ANOMALY for oid in (1, 2))


def _saved_row(frame, box, evidence, *, details=None):
    return {"source_frame_index": frame, "objects": [{
        "object_id": 1, "bbox": list(box), "mask_area": evidence["maskArea"],
        "anomaly_details": {**(details or {}), "identity_evidence": evidence},
    }]}


def test_restored_supported_output_does_not_rebuild_a_bbox_only_false_streak():
    current = _box(55, 60)
    rows = [_saved_row(frame, current, _evidence(1, frame, current, previous_point=[260, 270]))
            for frame in (1, 2)]
    detector = _detector()
    detector.prime_history((row["source_frame_index"], {1: current}) for row in rows[:1])
    detector.initialize_seed(2, {1: current})
    detector.prime_observations(rows, 2, {1: current})
    reports = [_push(detector, frame, {1: current}, evidences={}) for frame in (3, 4, 5)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 3
    assert detector.states[1].manual_baseline.frame_index == 0


def test_independent_image_risk_survives_chunk_restart_and_keeps_first_frame():
    current = _box()
    first = _detector()
    rows = []
    for frame in (1, 2):
        evidence = _evidence(1, frame, current, status="risk", coverage_status="tail_loss",
                             previous_point=[260, 270], reasons=["visible_tail_outside_mask"])
        report = _push(first, frame, {1: current}, evidences={1: evidence})
        assert not report.should_pause
        rows.append(_saved_row(frame, current, evidence, details=_item(report).details))
    restored = _detector()
    restored.prime_history((row["source_frame_index"], {1: current}) for row in rows[:1])
    restored.initialize_seed(2, {1: current})
    restored.prime_observations(rows, 2, {1: current})
    evidence = _evidence(1, 3, current, status="risk", coverage_status="tail_loss",
                         previous_point=[260, 270], reasons=["visible_tail_outside_mask"])
    report = _push(restored, 3, {1: current}, evidences={1: evidence})
    assert report.should_pause
    assert report.pause_frame_index == 1
    assert _item(report).details["risk_type"] == "image"
    assert _item(report).details["image_observation_count"] == 3
    assert _item(report).details["identity_evidence"]["frameIndex"] == 3


def test_explicit_checkpoint_ends_the_previous_image_risk_streak():
    current = _box()
    rows = [_saved_row(frame, current, _evidence(
        1, frame, current, status="risk", coverage_status="tail_loss", previous_point=[260, 270]))
        for frame in (1, 2)]
    restored = _detector(frame=2, confirmed=True)
    restored.prime_observations(rows, 2, {1: current})
    reports = [_push(restored, frame, {1: current}, evidences={1: _evidence(
        1, frame, current, status="risk", coverage_status="tail_loss", previous_point=[260, 270], reference_frame=2)})
        for frame in (3, 4, 5)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 3


def test_repeated_read_of_one_risky_image_does_not_count_as_three_frames():
    detector = _detector()
    current = _box()
    for _ in range(3):
        report = _push(detector, 1, {1: current}, evidences={1: _evidence(
            1, 1, current, status="risk", coverage_status="foreign_core", previous_point=[260, 270])})
        assert not report.should_pause
        assert _item(report).details["image_observation_count"] == 1
    for frame in (2, 3):
        report = _push(detector, frame, {1: current}, evidences={1: _evidence(
            1, frame, current, status="risk", coverage_status="foreign_core", previous_point=[260, 270])})
    assert report.should_pause
    assert report.pause_frame_index == 1


@pytest.mark.parametrize("change", ["category", "frame_gap", "unknown"])
def test_image_risk_persistence_does_not_mix_categories_gaps_or_uncertainty(change):
    detector = _detector()
    current = _box()
    _push(detector, 1, {1: current}, evidences={1: _evidence(
        1, 1, current, status="risk", coverage_status="foreign_core", previous_point=[260, 270])})
    if change == "unknown":
        _push(detector, 2, {1: current}, evidences={1: _evidence(
            1, 2, current, status="unknown", head_status="unknown", coverage_status="unknown")})
    frames = (3, 4, 5) if change in {"frame_gap", "unknown"} else (2, 3, 4)
    coverage = "tail_loss" if change == "category" else "foreign_core"
    reports = [_push(detector, frame, {1: current}, evidences={1: _evidence(
        1, frame, current, status="risk", coverage_status=coverage, previous_point=[260, 270])}) for frame in frames]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == frames[0]


@pytest.mark.parametrize("sample_kind,measurement_kind,expected_pause", [
    ("bbox", "bbox", False), ("bbox", "head", True),
    ("head", "head", False), ("head", "bbox", True),
])
def test_explicit_head_and_bbox_motion_samples_cannot_relax_each_other(sample_kind, measurement_kind, expected_pause):
    detector = _detector()
    detector.prime_normal_feedback([{
        "objectId": 1, "frameIndex": 0, "reason": "motion", "decision": "normal", "calibrate": True,
        "features": {"motionNormalized": 6.0, "referenceKind": sample_kind},
    }])
    moved = _box(center=(710, 270))
    evidence = {1: _evidence(1, 1, moved, point=[710, 270], previous_point=[260, 270], previous_frame=0)}
    report = _push(detector, 1, {1: moved}, evidences=evidence if measurement_kind == "head" else None)
    assert report.should_pause is expected_pause
    assert _item(report).details["motion_reference_kind"] == measurement_kind
    assert _item(report).details["feedback_sample_count"] == int(sample_kind == measurement_kind)


@pytest.mark.parametrize("missing", ["referenceFrameIndex", "referencePoint", "both"])
def test_supported_summary_without_a_human_optical_reference_keeps_bbox_fallback(missing):
    detector = _detector()
    current = _box(55, 60)
    reports = []
    for frame in (1, 2, 3):
        evidence = _evidence(1, frame, current, previous_point=[260, 270])
        for field in ("referenceFrameIndex", "referencePoint"):
            if missing in {field, "both"}:
                evidence["head"].pop(field)
        reports.append(_push(detector, frame, {1: current}, evidences={1: evidence}))
    assert [report.should_pause for report in reports] == [False, False, True]


@pytest.mark.parametrize("human_kind", ["manual", "confirmed_geometry"])
def test_old_optical_reference_cannot_override_new_human_geometry(human_kind):
    detector = _detector()
    if human_kind == "manual":
        detector.set_manual_baseline(1, _box(), 8)
    else:
        detector.geometry_references = {1: ConfirmedGeometryReference(_box(), 8, mask_area=1000)}
    detector.initialize_seed(8, {1: _box()})
    current = _box(55, 60)
    reports = [_push(detector, frame, {1: current}, evidences={1: _evidence(
        1, frame, current, previous_point=[260, 270], reference_frame=0)}) for frame in (9, 10, 11)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 9


def test_saved_supported_summaries_before_new_human_reference_do_not_restore_an_exemption():
    current = _box(55, 60)
    rows = [_saved_row(frame, current, _evidence(
        1, frame, current, previous_point=[260, 270], reference_frame=0)) for frame in (9, 10)]
    restored = _detector(frame=8)
    restored.prime_history((row["source_frame_index"], {1: current}) for row in rows[:1])
    restored.initialize_seed(10, {1: current})
    restored.prime_observations(rows, 10, {1: current})
    report = _push(restored, 11, {1: current}, evidences={})
    assert report.should_pause
    assert report.pause_frame_index == 9


@pytest.mark.parametrize("head_status,coverage_status", [
    ("lost", "unknown"), ("ambiguous", "unknown"), ("matched", "foreign_core"),
])
def test_old_image_risk_reference_cannot_restart_a_risk_after_human_checkpoint(head_status, coverage_status):
    detector = _detector(frame=8, confirmed=True)
    current = _box()
    reports = [_push(detector, frame, {1: current}, evidences={1: _evidence(
        1, frame, current, status="risk", head_status=head_status, coverage_status=coverage_status,
        previous_point=[260, 270], reference_frame=0)}) for frame in (9, 10, 11)]
    assert not any(report.should_pause for report in reports)
    assert all(report.object_levels[1] == AnomalyLevel.NORMAL for report in reports)
    assert detector.states[1].image_count == 0


def test_old_optical_motion_cannot_replace_current_human_motion_anchor():
    current = _box(center=(710, 270))
    detector = _detector({1: current}, frame=8)
    report = _push(detector, 9, {1: current}, evidences={1: _evidence(
        1, 9, current, point=[710, 270], previous_point=[260, 270],
        previous_frame=8, reference_frame=0, reference_point=[260, 270])})
    assert not report.should_pause
    assert _item(report).details["motion_reference_kind"] == "bbox"
    assert _item(report).details["center_shift"] == 0


def test_optical_reference_point_must_belong_to_the_actual_human_box():
    detector = _detector({1: _box(60, 60)})
    current = _box()
    reports = [_push(detector, frame, {1: current}, evidences={1: _evidence(
        1, frame, current, point=[260, 270], previous_point=[260, 270],
        reference_frame=0, reference_point=[220, 270])}) for frame in (1, 2, 3)]
    assert [report.should_pause for report in reports] == [False, False, True]


def test_legacy_supported_peak_does_not_silently_grant_new_geometry_exemption():
    detector=_detector();current=_box(55,60)
    reports=[]
    for frame in (1,2,3):
        evidence=_evidence(1,frame,current,previous_point=[260,270])
        evidence.update(version=1,source='native-frame-mask-v1')
        reports.append(detector.push(frame,{1:current},mask_areas={1:1000},identity_evidence={1:evidence}))
    assert [r.should_pause for r in reports]==[False,False,True]


def test_temporary_optical_occlusion_freezes_bbox_fallback_but_cannot_continue_indefinitely():
    detector=_detector();current=_box(32,160)
    reports=[]
    for frame in range(1,13):
        evidence=_evidence(1,frame,current,status='uncertain',head_status='ambiguous',coverage_status='subject_present_uncertain',point=[260,270])
        evidence['head'].update(point=[260,270],radius=5.,associationStatus='temporary-occlusion')
        reports.append(_push(detector,frame,{1:current},evidences={1:evidence}))
    assert not any(r.should_pause for r in reports[:11])
    assert reports[-1].should_pause
    assert _item(reports[-1]).details['image_observation_required']==12
    assert _item(reports[-1]).details['geometry_observation_count']==0


@pytest.mark.parametrize('fault',['geometry_frame','geometry_box','origin_point','source'])
def test_v2_independent_binding_cannot_waive_protection_when_malformed(fault):
    detector=_detector();detector.image_source_signature=['source',123,456,789];current=_box(55,60)
    reports=[]
    for frame in (1,2,3):
        evidence=_evidence(1,frame,current,previous_point=[260,270]);evidence['sourceSignature']=list(detector.image_source_signature)
        if fault=='geometry_frame':evidence['geometryReferenceFrameIndex']=frame
        elif fault=='geometry_box':evidence['geometryReferenceBBox']=current
        elif fault=='origin_point':evidence['head']['referencePoint']=[3500,2500]
        elif fault=='source':evidence['sourceSignature']=['other',123,456,789]
        reports.append(detector.push(frame,{1:current},mask_areas={1:1000},identity_evidence={1:evidence}))
    assert [r.should_pause for r in reports]==[False,False,True]
