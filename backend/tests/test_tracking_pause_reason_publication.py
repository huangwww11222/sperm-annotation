"""Pause presentation must describe the actual hard evidence, not soft ratios.

Model outputs are synthetic. The real tracker selects/publishes the injected
detector report; no model, user video or business workspace is modified.
"""
import copy
import json
import logging

import pytest

from app import tracker
from app import annotation_state
from app.services.anomaly_detector import AnomalyFrame, AnomalyLevel, AnomalyReport
from .test_tracking_adaptive_feedback import _run


@pytest.mark.parametrize('saved_type', ['coverage_loss', 'size_growth'])
def test_unknown_historical_hard_reason_keeps_its_saved_category(saved_type):
    from app.tracking_pause_policy import classify_pause_reason
    assert classify_pause_reason(['historical_custom_rule HARD'], fallback=saved_type) == saved_type
    assert classify_pause_reason(['adjacent_center_shift=90px normalized=3.0 HARD'], fallback=saved_type) == 'tracking_motion'


@pytest.mark.parametrize(('reasons', 'details', 'level', 'expected'), [
    (['manual_diagonal_ratio=2.011 HARD'], {'diagonal_ratio': 2.011}, 'anomaly', 'size_growth'),
    (['manual_diagonal_ratio=0.330 HARD'], {'diagonal_ratio': .33}, 'anomaly', 'size_shrink'),
    (['manual_area_ratio=0.300 SOFT', 'manual_diagonal_ratio=2.011 HARD'],
     {'area_ratio': .3, 'diagonal_ratio': 2.011}, 'anomaly', 'size_growth'),
    (['manual_area_ratio=0.500 SOFT', 'adjacent_center_shift=90px normalized=3.0 HARD'],
     {'area_ratio': .5}, 'anomaly', 'tracking_motion'),
    (['manual_diagonal_ratio=2.011 SOFT', 'image_head_lost observations=3/3 HARD'],
     {'diagonal_ratio': 2.011}, 'anomaly', 'identity_loss'),
    (['manual_diagonal_ratio=2.011 HARD', 'image_tail_loss observations=3/3 HARD'],
     {'diagonal_ratio': 2.011}, 'anomaly', 'coverage_loss'),
    (['manual_diagonal_ratio=2.011 HARD', 'bbox_overlap_with=12 coverage=1 HARD'],
     {'diagonal_ratio': 2.011}, 'anomaly', 'overlap'),
    (['manual_diagonal_ratio=2.011 HARD', 'image_head_lost observations=3/3 HARD'],
     {'diagonal_ratio': 2.011}, 'disappeared', 'disappearance'),
    (['manual_area_ratio=2.100 HARD'], {'area_ratio': 2.1}, 'anomaly', 'size_growth'),
    (['manual_height_ratio=0.300 HARD'], {'height_ratio': .3}, 'anomaly', 'size_shrink'),
    (['manual_aspect_change=2.000 HARD'], {'aspect_ratio_change': 2}, 'anomaly', 'shape_change'),
])
def test_real_tracker_publishes_actual_hard_pause_type(tmp_path, monkeypatch, caplog, reasons, details, level, expected):
    caplog.set_level(logging.INFO, logger='review.tracking')
    original_reasons = list(reasons)
    original_details = copy.deepcopy(details)

    def diagnosed(self, frame, boxes, **kwargs):
        assert frame == 1
        diagnostic = {**copy.deepcopy(details), 'current_bbox': boxes[7], 'pause_started_frame': frame}
        anomaly = AnomalyFrame(frame, 7, AnomalyLevel(level), list(reasons), diagnostic)
        return AnomalyReport(frame, {7: anomaly.level, 12: AnomalyLevel.NORMAL}, [anomaly], True, frame)

    monkeypatch.setattr(tracker.AnomalyDetector, 'push', diagnosed)
    result, _ = _run(tmp_path, monkeypatch, boxes={1: {
        7: [100, 100, 140, 112], 12: [100, 200, 140, 212],
    }})
    item = result['anomaly_paused']['reasons'][0]
    assert item['type'] == expected
    assert item['reasons'] == original_reasons
    assert reasons == original_reasons and details == original_details
    assert result['lastProcessedFrame'] == 1
    assert 'tracking.pause_window' in caplog.text
    assert f'type={expected}' in caplog.text


@pytest.mark.parametrize(('saved_type', 'reasons', 'geometry', 'title'), [
    ('size_growth', ['manual_diagonal_ratio=2.011 HARD'], True, '扩大'),
    ('tracking_motion', ['manual_diagonal_ratio=2.011 HARD'], True, '扩大'),
    ('tracking_motion', ['manual_diagonal_ratio=0.330 HARD'], True, '缩小'),
    ('tracking_motion', ['adjacent_center_shift=90px normalized=3.0 HARD'], False, '运动'),
])
def test_pause_publication_reconstructs_current_or_legacy_reason_without_accepting_it(
        tmp_path, saved_type, reasons, geometry, title):
    # An existing workspace avoids the unrelated legacy import path and lets
    # the test prove that presentation never changes manual geometry or scope.
    original = {'revision': 9, 'manualAnnotations': [], 'normalMotionSamples': [],
                'trackingFeedbackEvents': [], 'pausedAnomalies': []}
    (tmp_path / 'workspace_state.json').write_text(json.dumps(original))
    diagnostic = {'object_id': 7, 'name': 'sperm7', 'type': saved_type, 'reasons': reasons,
                  'details': {'diagonal_ratio': .33 if '0.330' in reasons[0] else 2.011}}
    annotation_state.publish_tracking_pause(tmp_path, 'isolated-video', {'frame_index': 110, 'reasons': [diagnostic]})
    state = annotation_state.read_state(tmp_path, recover_pause=False)
    notice = state['pausedAnomalies'][0]
    assert notice['acceptsGeometry'] is geometry
    assert title in notice['title']
    assert notice['diagnostic'] == diagnostic, 'saved evidence must remain unmodified'
    assert state['lastPausedContext']['frameIndex'] == 110
    assert state['revision'] == 9
    assert state['trackingFeedbackEvents'] == [] and state['normalMotionSamples'] == []
    assert state['manualAnnotations'] == []
