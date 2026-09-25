"""Regression tests for fixed manual-baseline anomaly detection."""

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
