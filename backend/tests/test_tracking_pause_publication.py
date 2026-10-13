"""Whole tracking/publication behavior with deterministic model geometry."""
import json
import pytest

from .test_tracking_adaptive_feedback import _geometry_segment, _confirmed_shape_workspace


def test_moderate_shape_observes_three_frames_but_publishes_first_risk(tmp_path, monkeypatch, caplog):
    caplog.set_level('INFO', logger='review.tracking')
    manual = [100, 100, 140, 120]
    risky = [100, 100, 140, 112.8]
    other = [100, 250, 140, 270]
    result = _geometry_segment(tmp_path, monkeypatch, 0, manual,
                               {frame: {7: risky, 12: other} for frame in range(1, 5)})
    pause = result['anomaly_paused']
    assert pause['frame_index'] == 1
    item = next(item for item in pause['reasons'] if item['object_id'] == 7)
    assert item['details']['decision_frame_index'] == 3
    assert item['currentBox'] == risky
    assert item['type'] == 'size_shrink'
    assert result['lastProcessedFrame'] == 1
    assert result['processedFrames'] == 2
    rows = [json.loads(line) for line in (tmp_path/'tracker_results.json').read_text().splitlines()]
    assert [row['source_frame_index'] for row in rows] == [0, 1]
    assert 'tracking.pause_window' in caplog.text


def test_transient_moderate_shape_recovers_without_pause(tmp_path, monkeypatch):
    manual = [100, 100, 140, 120]
    risky = [100, 100, 140, 112.8]
    other = [100, 250, 140, 270]
    result = _geometry_segment(tmp_path, monkeypatch, 0, manual,
                               {1: {7: risky, 12: other}, 2: {7: manual, 12: other}})
    assert result['anomaly_paused'] is None
    assert result['lastProcessedFrame'] == 2


def test_moderate_observation_survives_tracking_chunk_boundary(tmp_path, monkeypatch):
    manual = [100, 100, 140, 120]
    risky = [100, 100, 140, 112.8]
    other = [100, 250, 140, 270]
    first = _geometry_segment(tmp_path, monkeypatch, 0, manual,
                              {1: {7: risky, 12: other}, 2: {7: risky, 12: other}})
    assert first['anomaly_paused'] is None
    second = _geometry_segment(tmp_path, monkeypatch, 2, risky,
                               {3: {7: risky, 12: other}, 4: {7: risky, 12: other}})
    assert second['anomaly_paused']['frame_index'] == 1
    assert second['lastProcessedFrame'] == 1
    assert [row['source_frame_index'] for row in second['frames']] == [0, 1]


def test_overlap_does_not_exempt_extreme_shrink_of_another_object(tmp_path, monkeypatch):
    manual = [100, 100, 140, 120]
    # The second object is contained in the first with low IoU; this is an
    # explicitly supplied seed, not a model collision of separate objects.
    other = [110, 105, 120, 110]
    result = _geometry_segment(tmp_path, monkeypatch, 0, manual,
                               {frame: {7: manual, 12: other} for frame in range(1, 7)})
    # _geometry_segment supplies object 12's fixed seed at 100,200,140,220,
    # so this verifies low-IoU containment with its independent extreme shrink
    # still detected rather than a blanket exemption for overlap.
    assert result['anomaly_paused'] is not None
    assert 12 in {item['object_id'] for item in result['anomaly_paused']['reasons']}


def test_missing_just_confirmed_reference_blocks_instead_of_using_old_manual_box(tmp_path, monkeypatch, caplog):
    caplog.set_level('INFO', logger='review.tracking')
    _, accepted, other = _confirmed_shape_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr('app.tracker._load_confirmed_geometry_references', lambda *args: {})
    with pytest.raises(ValueError, match='确认参考未正确载入'):
        _geometry_segment(tmp_path, monkeypatch, 1, accepted, {2: {7: accepted, 12: other}})
    assert 'tracking.confirmed_reference_invalid' in caplog.text
    rows = [json.loads(line) for line in (tmp_path/'tracker_results.json').read_text().splitlines()]
    assert [row['source_frame_index'] for row in rows] == [0, 1]


def test_soft_containment_does_not_mislabel_extreme_growth_as_overlap(tmp_path, monkeypatch):
    manual = [100, 100, 140, 120]
    huge = [100, 100, 210, 120]
    other = [110, 100, 150, 120]
    result = _geometry_segment(tmp_path, monkeypatch, 0, manual, {1: {7: huge, 12: other}})
    pause = result['anomaly_paused']['reasons'][0]
    assert pause['object_id'] == 7
    assert pause['type'] == 'size_growth'
    assert any(reason.startswith('bbox_overlap_with=') and reason.endswith(' SOFT') for reason in pause['reasons'])
    assert any(reason.startswith('manual_width_ratio=') and reason.endswith(' HARD') for reason in pause['reasons'])
