"""Image risk approval and movement calibration keep their server provenance."""
import json

import pytest

from app import annotation_state, tracker
from .test_annotation_controls import controls, task, feedback
from .test_tracking_confirmation_policy import _approval, _save


@pytest.mark.parametrize('scope', ['object', 'frame'])
def test_head_movement_calibration_is_separate_and_survives_reload(controls, scope):
    client, root, _ = controls
    rows = [json.loads(line) for line in (root / 'tracker_results.json').read_text().splitlines()]
    for row in rows:
        for obj in row['objects']:
            obj['anomaly_details']['motion_reference_kind'] = 'head'
    (root / 'tracker_results.json').write_text('\n'.join(map(json.dumps, rows)))
    response = feedback(client, scope=scope)
    assert response.status_code == 200, response.text
    samples = response.json()['normalMotionSamples']
    assert samples and all(s['features'] == {'motionNormalized': 1.4, 'referenceKind': 'head'} for s in samples)
    assert annotation_state.read_state(root)['normalMotionSamples'] == samples
    assert feedback(client, scope=scope).json() == response.json()


def test_image_risk_normal_approval_restores_a_trusted_reference_without_manual_rewrite(tmp_path):
    original, body, box = _approval(tmp_path, reasons=['image_foreign_core HARD'], mask_area=217)
    response = _save(tmp_path, body)
    event = response['trackingFeedbackEvents'][-1]
    assert event['geometryReference']['bbox'] == box
    assert event['geometryReference']['maskArea'] == 217
    assert response['normalMotionSamples'] == []
    assert annotation_state.read_state(tmp_path)['manualAnnotations'] == original['manualAnnotations']
    refs = tracker._load_confirmed_geometry_references(annotation_state.read_state(tmp_path), 5, {})
    assert refs[7][-1].frame_index == 5 and refs[7][-1].bbox == box
