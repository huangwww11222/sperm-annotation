"""Unit tests for the sperm-tracking anomaly detector."""

from app.services.anomaly_detector import AnomalyConfig, AnomalyDetector, AnomalyLevel


def _bbox(area: float = 200.0, cx: float = 320.0, cy: float = 216.0, aspect: float = 2.5) -> list:
    h = (area / aspect) ** 0.5
    w = area / h
    x1, y1 = cx - w / 2, cy - h / 2
    return [x1, y1, x1 + w, y1 + h]


def test_normal_frames_no_anomaly() -> None:
    det = AnomalyDetector(frame_width=640, frame_height=432, fps=15)
    for i in range(12):
        det.push(i, {1: _bbox(area=200, cx=320 + (i % 3 - 1) * 2, cy=216)})
    assert not any(r.should_pause for r in det.reports)
    assert det.states[1].level == AnomalyLevel.NORMAL


def test_immediate_disappearance_away_from_edge() -> None:
    det = AnomalyDetector(frame_width=640, frame_height=432, fps=15, all_object_ids=[1])
    det.push(10, {1: _bbox(cx=320)})
    report = det.push(11, {})
    assert report.should_pause is True
    assert report.object_levels[1] == AnomalyLevel.DISAPPEARED
    assert report.frames[0].frame_index == 11
    assert report.frames[0].details["near_edge"] is False


def test_disappearance_within_10px_of_edge_is_normal() -> None:
    det = AnomalyDetector(frame_width=640, frame_height=432, fps=15, all_object_ids=[1])
    edge_bbox = [625.0, 200.0, 639.0, 214.0]
    det.push(10, {1: edge_bbox})
    for i in range(11, 20):
        report = det.push(i, {})
        assert report.should_pause is False
    assert det.states[1].level == AnomalyLevel.NORMAL
    assert det.states[1].is_dead is False


def test_width_growth_hard_pauses() -> None:
    det = AnomalyDetector(frame_width=640, frame_height=432, fps=15)
    for i in range(6):
        det.push(i, {1: _bbox(area=240, cx=320, cy=216)})
    report = det.push(6, {1: _bbox(area=480, cx=320, cy=216, aspect=5.0)})
    # Width/area growth should be recognized as HARD for a sperm bbox.
    assert report.should_pause is True
    assert report.object_levels[1] == AnomalyLevel.ANOMALY
    assert any("width_ratio" in reason or "area_ratio" in reason for reason in report.frames[0].reasons)


def test_height_growth_hard_pauses() -> None:
    det = AnomalyDetector(frame_width=640, frame_height=432, fps=15)
    for i in range(6):
        det.push(i, {1: _bbox(area=240, cx=320, cy=216)})
    # Same area order but much taller/narrower box triggers height/aspect growth.
    report = det.push(6, {1: _bbox(area=480, cx=320, cy=216, aspect=0.5)})
    assert report.should_pause is True
    assert report.object_levels[1] == AnomalyLevel.ANOMALY


def test_overlap_95_percent_pauses_both_objects() -> None:
    det = AnomalyDetector(frame_width=640, frame_height=432, fps=15)
    # Seed frame: intentional overlap is allowed while initializing tracking.
    det.push(0, {1: [100, 100, 120, 112], 2: [200, 100, 220, 112]})
    report = det.push(1, {1: [100, 100, 120, 112], 2: [101, 100, 121, 112]})
    assert report.should_pause is True
    assert report.object_levels[1] == AnomalyLevel.ANOMALY
    assert report.object_levels[2] == AnomalyLevel.ANOMALY
    assert any("bbox_overlap_with=2" in reason for frame in report.frames if frame.object_id == 1 for reason in frame.reasons)


def test_overlap_coverage_handles_containment() -> None:
    det = AnomalyDetector(frame_width=640, frame_height=432, fps=15)
    det.push(0, {1: [100, 100, 140, 120], 2: [250, 100, 290, 120]})
    # Box 2 lies entirely inside box 1; smaller-box coverage is 100%.
    report = det.push(1, {1: [100, 100, 140, 120], 2: [105, 105, 135, 115]})
    assert report.should_pause is True


def test_hard_anomaly_does_not_pollute_baseline() -> None:
    det = AnomalyDetector(frame_width=640, frame_height=432, fps=30)
    for i in range(6):
        det.push(i, {1: _bbox(area=200, cx=320, cy=216)})
    det.push(6, {1: _bbox(area=200, cx=380, cy=216)})
    history = det.states[1].history
    assert all(b[0] < 350 for b in history)


def test_config_defaults() -> None:
    cfg = AnomalyConfig()
    assert cfg.BASELINE_WINDOW == 5
    assert cfg.EDGE_EXIT_MARGIN_PX == 10.0
    assert cfg.AREA_GROW_HARD == 2.8
    assert cfg.WIDTH_GROW_HARD == 1.8
    assert cfg.HEIGHT_GROW_HARD == 1.8
    assert cfg.OVERLAP_HARD == 0.95
