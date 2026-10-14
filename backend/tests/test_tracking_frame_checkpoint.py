"""Explicit whole-frame approval is atomic and never rewrites provenance."""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import annotation_state, tracker
from app.services.anomaly_detector import AnomalyDetector, ManualBaseline
from .test_tracking_confirmation_policy import _approval, _save
from .test_annotation_controls import controls, task, feedback, save


def setup_checkpoint(root):
    state, body, box = _approval(root)
    state['manualBaselines'] = [{'objectId': oid, 'frameIndex': 0, 'source': 'manual',
                                'bbox': [100, 100, 144.4, 140]} for oid in (7, 9, 12)]
    (root / 'workspace_state.json').write_text(json.dumps(state))
    (root / 'media.json').write_text(json.dumps({'width': 200, 'height': 200, 'frameCount': 20}))
    return state, {**body, 'scope': 'frame'}, box


def test_frame_confirmation_includes_unflagged_objects_and_replays_atomically(tmp_path):
    state, body, box = setup_checkpoint(tmp_path)
    original = (tmp_path / 'tracker_results.json').read_bytes()
    result = _save(tmp_path, body)
    events = result['trackingFeedbackEvents']
    assert [e['objectId'] for e in events] == [7, 9, 12]
    assert all(e['scope'] == 'frame' and e['geometryReference']['checkpoint'] for e in events)
    assert len({e['checkpointId'] for e in events}) == 1
    assert result['pausedAnomalies'] == [] and result['lastPausedContext'] is None
    assert _save(tmp_path, body) == result
    restored = annotation_state.read_state(tmp_path)
    assert restored['revision'] == 1
    assert restored['manualBaselines'] == state['manualBaselines']
    assert restored['manualAnnotations'] == state['manualAnnotations']
    assert (tmp_path / 'tracker_results.json').read_bytes() == original


def test_restart_loads_checkpoint_not_old_manual_or_future_confirmation(tmp_path):
    _, body, box = setup_checkpoint(tmp_path)
    _save(tmp_path, body)
    seed = tmp_path / 'annotations_frame_5.json'
    before = tracker._load_latest_manual_baselines(seed, 4)
    after = tracker._load_latest_manual_baselines(seed, 5)
    assert before[7].frame_index == 0
    assert after[7].frame_index == 5 and after[7].bbox == box
    assert after[9].frame_index == 5 and after[12].frame_index == 5
    detector = AnomalyDetector(manual_baselines={7: after[7]}, frame_width=200, frame_height=200)
    detector.initialize_seed(5, {7: box})
    # Returning to the much taller old manual shape is no longer normal merely
    # because it matches the original drawing before this checkpoint.
    reports = [detector.push(fi, {7: [100, 100, 144.4, 140]}) for fi in (6, 7, 8)]
    assert any(report.should_pause for report in reports)
    assert reports[0].frames[0].details['geometry_reference_source'] == 'confirmed-normal'


def test_saved_manual_override_and_new_object_are_part_of_checkpoint(tmp_path):
    state, body, _ = setup_checkpoint(tmp_path)
    manual = [{'objectId': oid, 'source': 'manual', 'frameIndex': 5, 'name': f'sperm{oid}',
               'bbox': {'x': 10, 'y': 10, 'width': 10, 'height': 20}} for oid in (7, 22)]
    state['manualAnnotations'] = manual
    (tmp_path / 'workspace_state.json').write_text(json.dumps(state))
    result = _save(tmp_path, body)
    refs = {e['objectId']: e['geometryReference'] for e in result['trackingFeedbackEvents']}
    assert set(refs) == {7, 9, 12, 22}
    assert refs[7]['bbox'] == refs[22]['bbox'] == [20, 20, 40, 60]
    assert annotation_state.read_state(tmp_path)['manualAnnotations'] == manual


@pytest.mark.parametrize('fault', ['missing', 'invalid', 'wrong-frame', 'stale'])
def test_invalid_whole_frame_never_partially_confirms(tmp_path, fault):
    state, body, _ = setup_checkpoint(tmp_path)
    if fault in ('missing', 'invalid'):
        state['pausedAnomalies'].append({'objectId': 99})
        if fault == 'invalid':
            row = json.loads((tmp_path / 'tracker_results.json').read_text())
            row['objects'].append({'object_id': 99, 'bbox': [0, 0, 0, 0]})
            (tmp_path / 'tracker_results.json').write_text(json.dumps(row))
    if fault == 'wrong-frame':
        body['frameIndex'] = 4
    if fault == 'stale':
        body['expectedRevision'] = 8
    (tmp_path / 'workspace_state.json').write_text(json.dumps(state))
    before = (tmp_path / 'workspace_state.json').read_bytes()
    with pytest.raises(HTTPException):
        _save(tmp_path, body)
    assert (tmp_path / 'workspace_state.json').read_bytes() == before


def test_checkpoint_excludes_deleted_objects_and_clears_pause_candidates(tmp_path):
    state, body, _ = setup_checkpoint(tmp_path)
    state.update(deletedObjectIds=[9], deletedFrameObjects=[{'objectId': 12, 'frameIndex': 5}],
                 _pauseCandidates=[{'objectId': 7}, {'objectId': 9}], _pauseContext={'frameIndex': 5})
    (tmp_path / 'workspace_state.json').write_text(json.dumps(state))
    result = _save(tmp_path, body)
    assert [e['objectId'] for e in result['trackingFeedbackEvents']] == [7]
    restored = annotation_state.read_state(tmp_path)
    assert restored['_pauseCandidates'] == [] and restored['_pauseContext'] is None


def test_checkpoint_publish_failure_retry_and_new_manual_supersedes(tmp_path, monkeypatch):
    state, body, _ = setup_checkpoint(tmp_path)
    before = (tmp_path / 'workspace_state.json').read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', lambda *args: (_ for _ in ()).throw(OSError('disk failure')))
        with pytest.raises(HTTPException):
            _save(tmp_path, body)
    assert (tmp_path / 'workspace_state.json').read_bytes() == before
    _save(tmp_path, body)
    restored = annotation_state.read_state(tmp_path)
    restored['manualBaselines'][0].update(frameIndex=6, bbox=[50, 50, 70, 70])
    (tmp_path / 'workspace_state.json').write_text(json.dumps(restored))
    assert tracker._load_latest_manual_baselines(tmp_path / 'seed.json', 6)[7] == ManualBaseline([50, 50, 70, 70], 6)


def test_frame_api_replay_protected_fields_and_reset(controls):
    client, root, original = controls
    response = feedback(client, scope='frame')
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result['trackingFeedbackEvents']) == 2 and len(result['normalMotionSamples']) == 2
    assert result['pausedAnomalies'] == []
    assert feedback(client, scope='frame').json() == result
    assert feedback(client, scope='object').status_code == 409
    assert feedback(client, scope='frame', expectedRevision=1, key='repeat-new-key').status_code == 409
    current = client.get('/api/track/workspace/control-video').json()
    assert save(client, {**current, 'trackingFeedbackEvents': []}, 1, 'ordinary-save').status_code == 200
    assert len(annotation_state.read_state(root)['trackingFeedbackEvents']) == 2
    assert feedback(client, decision='reset', expectedRevision=2, key='reset').status_code == 200
    loaded = tracker._load_latest_manual_baselines(root / 'seed.json', 1)
    assert 7 not in loaded and loaded[9].frame_index == 1


def test_checkpoint_small_jitter_motion_and_later_shrink_still_checked(tmp_path):
    _, body, box = setup_checkpoint(tmp_path)
    _save(tmp_path, body)
    baseline = tracker._load_latest_manual_baselines(tmp_path / 'seed.json', 5)[7]
    detector = AnomalyDetector(manual_baselines={7: baseline}, frame_width=1000, frame_height=1000)
    detector.initialize_seed(5, {7: box})
    for fi in range(6, 15):
        dx = (fi - 5) * 4
        moved = [box[0]+dx, box[1], box[2]+dx, box[3]+.1]
        assert not detector.push(fi, {7: moved}).should_pause
    assert detector._state(7).manual_baseline == baseline
    small = [moved[0], moved[1], moved[2], moved[1]+2]
    assert detector.push(15, {7: small}).should_pause


@pytest.mark.parametrize('change', ['bbox', 'frameIndex'])
def test_corrupted_checkpoint_does_not_silently_restore_old_baseline(tmp_path, change):
    _, body, _ = setup_checkpoint(tmp_path)
    _save(tmp_path, body)
    stored = annotation_state.read_state(tmp_path)
    stored['trackingFeedbackEvents'][0]['geometryReference'][change] = [0, 0, 0, 0] if change == 'bbox' else 3
    (tmp_path / 'workspace_state.json').write_text(json.dumps(stored))
    with pytest.raises(ValueError, match='人工检查点'):
        tracker._load_latest_manual_baselines(tmp_path / 'seed.json', 5)
    stored['trackingFeedbackEvents'].append({'objectId': 7, 'decision': 'reset'})
    (tmp_path / 'workspace_state.json').write_text(json.dumps(stored))
    assert tracker._load_latest_manual_baselines(tmp_path / 'seed.json', 5)[7].frame_index == 0
