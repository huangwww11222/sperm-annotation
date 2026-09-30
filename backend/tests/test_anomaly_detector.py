"""Human feedback, motion continuity, and independent safety regressions."""

import math

import pytest

from app.services.anomaly_detector import AnomalyConfig, AnomalyDetector, AnomalyLevel


def _box(width: float = 40, height: float = 12, x: float = 100, y: float = 100) -> list[float]:
    return [x, y, x + width, y + height]


def _detector(bbox: list[float] | None = None, frame: int = 0) -> AnomalyDetector:
    detector = AnomalyDetector(frame_width=640, frame_height=432, fps=30, all_object_ids=[1])
    detector.set_manual_baseline(1, bbox or _box(), frame, "sperm1")
    detector.initialize_seed(frame, {1: bbox or _box()})
    return detector


def test_slow_shrink_eventually_pauses_against_fixed_manual_baseline() -> None:
    detector = _detector()
    reports = []
    for frame, width in enumerate([38, 35, 31, 28, 25], start=1):
        reports.append(detector.push(frame, {1: _box(width=width)}))
    assert not any(report.should_pause for report in reports[:-1])
    assert reports[-1].should_pause is True
    anomaly = next(frame for frame in reports[-1].frames if frame.object_id == 1)
    assert anomaly.details["manual_baseline_bbox"] == _box()
    assert anomaly.details["manual_baseline_frame"] == 0
    assert anomaly.details["width_ratio"] == 0.625


def test_ai_boxes_never_pollute_manual_baseline() -> None:
    detector = _detector()
    for frame, width in enumerate([38, 35, 32, 29], start=1):
        detector.push(frame, {1: _box(width=width)})
    baseline = detector.states[1].manual_baseline
    assert baseline is not None
    assert baseline.bbox == _box()
    assert baseline.frame_index == 0


def test_growth_pauses() -> None:
    report = _detector().push(1, {1: _box(width=64, height=16)})
    assert report.should_pause is True
    assert report.object_levels[1] == AnomalyLevel.ANOMALY
    anomaly = next(frame for frame in report.frames if frame.object_id == 1)
    assert anomaly.details["area_ratio"] > 1.8


def test_manual_correction_replaces_old_baseline() -> None:
    detector = _detector()
    corrected = _box(width=30, height=10, x=150)
    detector.set_manual_baseline(1, corrected, 8, "sperm1")
    detector.initialize_seed(8, {1: corrected})
    report = detector.push(9, {1: _box(width=29, height=10, x=153)})
    assert report.should_pause is False
    assert detector.states[1].manual_baseline.frame_index == 8
    assert detector.states[1].manual_baseline.bbox == corrected


def test_normal_size_jitter_does_not_pause() -> None:
    detector = _detector()
    for frame, (width, height) in enumerate([(39, 12), (42, 11), (37, 13), (41, 12)], start=1):
        assert detector.push(frame, {1: _box(width=width, height=height, x=100 + frame * 3)}).should_pause is False


def test_confirmed_geometry_requires_one_complete_shape_match() -> None:
    from app.services.anomaly_detector import ConfirmedGeometryReference

    manual = _box(width=40, height=20)
    confirmed = _box(width=20, height=40)
    detector = _detector(manual)
    detector.geometry_references = {1: ConfirmedGeometryReference(confirmed, 1)}
    detector.initialize_seed(1, {1: confirmed})
    # Width matches manual and height matches confirmation, but neither
    # complete human-approved shape is 40x40. Do not merge dimension ranges.
    report = detector.push(2, {1: _box(width=40, height=40)})
    assert report.should_pause
    assert report.frames[0].details["geometry_reference_source"] == "confirmed-normal"


@pytest.mark.parametrize("scenario", ["motion", "overlap", "loss"])
def test_geometry_confirmation_preserves_independent_checks(scenario) -> None:
    from app.services.anomaly_detector import ConfirmedGeometryReference

    manual = _box(width=40, height=20)
    confirmed = _box(width=44.4, height=11.8)
    detector = _detector(manual)
    detector.geometry_references = {1: ConfirmedGeometryReference(confirmed, 1)}
    seed = {1: confirmed}
    if scenario == "overlap":
        seed[2] = _box(x=200)
    detector.initialize_seed(1, seed)
    if scenario == "motion":
        current = {1: _box(width=44.4, height=11.8, x=400)}
        reason = "adjacent_center_shift="
    elif scenario == "overlap":
        current = {1: confirmed, 2: confirmed}
        reason = "bbox_overlap_with="
    else:
        current = {}
        reason = "unexpected_disappearance"
    report = detector.push(2, current)
    assert report.should_pause
    assert any(item.object_id == 1 and any(value.startswith(reason) for value in item.reasons) for item in report.frames)
    assert detector.states[1].manual_baseline.bbox == manual


def test_future_confirmation_is_not_applied_to_earlier_predictions() -> None:
    from app.services.anomaly_detector import ConfirmedGeometryReference

    detector = _detector(_box(width=40, height=20))
    confirmed = _box(width=44.4, height=11.8)
    detector.geometry_references = {1: ConfirmedGeometryReference(confirmed, 8)}
    assert detector.push(1, {1: confirmed}).should_pause


def test_normal_motion_is_not_compared_with_manual_center() -> None:
    detector = _detector()
    for frame in range(1, 15):
        report = detector.push(frame, {1: _box(x=100 + frame * 10)})
        assert report.should_pause is False
    assert detector.states[1].motion_history[-1][0] == 240


def test_disappearance_away_from_edge_pauses() -> None:
    report = _detector().push(1, {})
    assert report.should_pause is True
    assert report.object_levels[1] == AnomalyLevel.DISAPPEARED


def test_exit_at_edge_is_not_anomaly() -> None:
    edge = _box(width=10, height=10, x=629)
    detector = _detector(edge)
    assert detector.push(1, {}).should_pause is False


def test_shrink_at_left_side_is_treated_as_normal_exit() -> None:
    detector = _detector(_box(x=25))
    report = detector.push(1, {1: _box(width=20, x=5)})
    frame = next(item for item in report.frames if item.object_id == 1)
    assert report.should_pause is False
    assert frame.details["near_side_edge"] is True
    assert frame.details["side_exit_shrink_ignored"] is True
    assert not any(reason.startswith("manual_") for reason in frame.reasons)


def test_shrink_at_right_side_is_treated_as_normal_exit() -> None:
    detector = _detector(_box(x=575))
    report = detector.push(1, {1: _box(width=20, x=620)})
    assert report.should_pause is False
    frame = next(item for item in report.frames if item.object_id == 1)
    assert frame.details["side_exit_shrink_ignored"] is True


def test_same_shrink_away_from_side_still_pauses() -> None:
    report = _detector().push(1, {1: _box(width=20, x=110)})
    assert report.should_pause is True
    frame = next(item for item in report.frames if item.object_id == 1)
    assert frame.details["side_exit_shrink_ignored"] is False


def test_shrink_at_top_edge_is_not_exempted_as_side_exit() -> None:
    detector = _detector(_box(y=5))
    report = detector.push(1, {1: _box(width=20, x=110, y=5)})
    assert report.should_pause is True
    frame = next(item for item in report.frames if item.object_id == 1)
    assert frame.details["near_side_edge"] is False


def test_growth_at_side_still_pauses() -> None:
    detector = _detector(_box(x=590))
    report = detector.push(1, {1: _box(width=70, x=570)})
    assert report.should_pause is True
    frame = next(item for item in report.frames if item.object_id == 1)
    assert any("manual_width_ratio" in reason and "HARD" in reason for reason in frame.reasons)


def test_overlap_pauses_both_real_objects() -> None:
    detector = AnomalyDetector(frame_width=640, frame_height=432, fps=30)
    detector.set_manual_baseline(1, _box(x=100), 0, "sperm1")
    detector.set_manual_baseline(2, _box(x=200), 0, "sperm2")
    detector.initialize_seed(0, {1: _box(x=100), 2: _box(x=200)})
    report = detector.push(1, {1: _box(x=120), 2: _box(x=121)})
    assert report.should_pause is True
    assert report.object_levels[1] == AnomalyLevel.ANOMALY
    assert report.object_levels[2] == AnomalyLevel.ANOMALY


def test_review_range_uses_configured_lookback() -> None:
    detector = _detector()
    anomaly = next(frame for frame in detector.push(10, {1: _box(width=20)}).frames if frame.object_id == 1)
    assert anomaly.details["review_lookback_frames"] == AnomalyConfig().REVIEW_LOOKBACK_FRAMES
    assert anomaly.details["review_start_frame"] == 5
    assert anomaly.details["review_end_frame"] == 9


def test_config_defaults_are_tunable_and_bidirectional() -> None:
    config = AnomalyConfig()
    assert config.MANUAL_AREA_RATIO_MIN_HARD < 1 < config.MANUAL_AREA_RATIO_MAX_HARD
    assert config.MANUAL_WIDTH_RATIO_MIN_HARD < 1 < config.MANUAL_WIDTH_RATIO_MAX_HARD
    assert config.MANUAL_HEIGHT_RATIO_MIN_HARD < 1 < config.MANUAL_HEIGHT_RATIO_MAX_HARD
    assert config.SIDE_EXIT_MARGIN_PX == 10
    assert config.REVIEW_LOOKBACK_FRAMES == 5


def _sample(value: float = 1.1, object_id: int = 1, **changes) -> dict:
    return {"objectId": object_id, "frameIndex": 3, "reason": "motion", "decision": "normal", "calibrate": True, "features": {"motionNormalized": value}, **changes}


def test_one_large_movement_warns_before_persistent_movement_pauses() -> None:
    detector = _detector()
    reports = [detector.push(frame, {1: _box(x=100 + frame * 45)}) for frame in range(1, 4)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[0].object_levels[1] == AnomalyLevel.WARNING
    assert reports[-1].frames[0].details["motion_streak"] == 3


def test_returning_jitter_does_not_build_a_drift_streak() -> None:
    detector = _detector()
    for frame in range(1, 20):
        report = detector.push(frame, {1: _box(x=145 if frame % 2 else 100)})
        assert not report.should_pause
    assert report.frames[0].details["motion_oscillation"] is True


def test_confirmed_similar_motion_continues_without_repeated_pauses() -> None:
    detector = _detector()
    detector.prime_normal_feedback([_sample()])
    for frame in range(1, 9):
        report = detector.push(frame, {1: _box(x=100 + frame * 45)})
        assert not report.should_pause
        assert report.frames[0].details["normal_sample_matched"] is True
    assert report.frames[0].details["motion_pause_threshold"] == pytest.approx(1.32)


def test_new_motion_outside_confirmed_sample_still_pauses() -> None:
    detector = _detector()
    detector.prime_normal_feedback([_sample()])
    reports = [detector.push(frame, {1: _box(x=100 + frame * 70)}) for frame in range(1, 4)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].frames[0].details["normal_sample_matched"] is False


def test_human_can_add_larger_sample_without_fixed_pixel_cap() -> None:
    detector = _detector()
    detector.prime_normal_feedback([_sample(), _sample(2.1)])
    for frame in range(1, 4):
        assert not detector.push(frame, {1: _box(x=100 + frame * 85)}).should_pause
    assert detector.normal_motion_samples[1] == [1.1, 2.1]


def test_extreme_jump_remains_immediate_after_feedback() -> None:
    detector = _detector()
    detector.prime_normal_feedback([_sample()])
    report = detector.push(1, {1: _box(x=300)})
    assert report.should_pause
    assert report.frames[0].details["motion_reason"] == "extreme_jump"


@pytest.mark.parametrize("fps,gap,scale", [(15, 1, 1), (30, 1, 1), (60, 1, 1), (30, 3, 1), (30, 1, 2)])
def test_motion_decision_is_normalized_for_fps_gaps_and_resolution(fps, gap, scale) -> None:
    detector = AnomalyDetector(frame_width=4000, frame_height=2000, fps=fps, all_object_ids=[1])
    initial = _box(width=40 * scale, height=12 * scale, x=100 * scale, y=100 * scale)
    detector.set_manual_baseline(1, initial, 0)
    detector.initialize_seed(0, {1: initial})
    shift = 45 * scale * gap * 30 / fps
    reports = []
    for step in range(1, 4):
        reports.append(detector.push(step * gap, {1: _box(width=40 * scale, height=12 * scale, x=100 * scale + step * shift, y=100 * scale)}))
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].frames[0].details["motion_normalized"] == pytest.approx(45 / math.hypot(40, 12))


@pytest.mark.parametrize("sample", [_sample(object_id=2), _sample(reason="shape"), _sample(decision="corrected"), _sample(calibrate=False), _sample(float("nan")), _sample(float("inf")), _sample(-2), {"objectId": 1, "reason": "motion"}])
def test_unrelated_invalid_or_once_only_feedback_never_relaxes_motion(sample) -> None:
    detector = _detector()
    detector.prime_normal_feedback([sample])
    for frame in range(1, 4):
        report = detector.push(frame, {1: _box(x=100 + frame * 45)})
    assert report.should_pause
    assert report.frames[0].details["feedback_sample_count"] == 0


def test_normal_sample_does_not_disable_size_or_missing_checks() -> None:
    detector = _detector()
    detector.prime_normal_feedback([_sample(3)])
    assert detector.push(1, {1: _box(width=70)}).should_pause
    other = _detector()
    other.prime_normal_feedback([_sample(3)])
    assert other.push(1, {}).should_pause


def test_normal_sample_does_not_disable_identity_overlap_check() -> None:
    detector = _detector()
    detector.set_manual_baseline(2, _box(x=200), 0)
    detector.initialize_seed(0, {1: _box(), 2: _box(x=200)})
    detector.prime_normal_feedback([_sample(3), _sample(3, object_id=2)])
    report = detector.push(1, {1: _box(x=150), 2: _box(x=151)})
    assert report.should_pause
    assert all(level == AnomalyLevel.ANOMALY for level in report.object_levels.values())


def test_reset_feedback_restores_initial_range_and_ai_history_cannot_expand_it() -> None:
    detector = _detector()
    detector.prime_normal_feedback([_sample(2)])
    for frame in range(1, 4):
        assert not detector.push(frame, {1: _box(x=100 + frame * 45)}).should_pause
    detector.prime_normal_feedback([])
    for frame in range(4, 7):
        report = detector.push(frame, {1: _box(x=100 + frame * 45)})
    assert report.should_pause
    assert report.frames[0].details["motion_pause_threshold"] == 1


def test_clean_frame_resets_suspected_drift_streak_and_history_is_bounded() -> None:
    detector = _detector()
    for frame in range(1, 30):
        report = detector.push(frame, {1: _box(x=100 + frame * 3)})
        assert not report.should_pause
    assert len(detector.states[1].motion_history) == detector.config.MOTION_HISTORY_FRAMES
    assert len(detector.states[1].motion_frame_indices) == detector.config.MOTION_HISTORY_FRAMES
    assert detector.push(30, {1: _box(x=232)}).frames[0].details["motion_streak"] == 1
    assert detector.push(31, {1: _box(x=235)}).frames[0].details["motion_streak"] == 0


def test_pause_log_explains_threshold_and_human_sample_count(caplog) -> None:
    import logging

    detector = _detector()
    detector.prime_normal_feedback([_sample()])
    with caplog.at_level(logging.INFO, logger="review.tracking"):
        assert detector.push(1, {1: _box(x=300)}).should_pause
    assert "tracking.anomaly_pause frame=1" in caplog.text
    assert "'samples': 1" in caplog.text
    assert "'threshold': 1.32" in caplog.text


def test_soft_overlap_cannot_downgrade_independent_hard_size_anomaly() -> None:
    detector = _detector()
    second = _box(width=60, height=16, x=200)
    detector.set_manual_baseline(2, second, 0)
    detector.initialize_seed(0, {1: _box(), 2: second})
    report = detector.push(1, {1: _box(width=64, height=16), 2: _box(width=60, height=16, x=110)})
    assert report.should_pause
    assert report.object_levels[1] == AnomalyLevel.ANOMALY
    assert detector.states[1].level == AnomalyLevel.ANOMALY


def test_missing_manual_baseline_still_uses_fixed_seed_motion_scale() -> None:
    detector = AnomalyDetector(all_object_ids=[1])
    detector.initialize_seed(0, {1: _box()})
    first = detector.push(1, {1: _box(width=45, height=15, x=105)})
    second = detector.push(2, {1: _box(width=50, height=20, x=110)})
    assert first.frames[0].details["object_scale"] == second.frames[0].details["object_scale"] == pytest.approx(math.hypot(40, 12))
