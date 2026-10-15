"""Real tracking publication/pause policy; image/model outputs are simulated."""
import json

import pytest

from .test_tracking_adaptive_feedback import FakeEngine, _geometry_segment


@pytest.fixture(autouse=True)
def simulated_source(tmp_path):
    (tmp_path / "video.avi").write_bytes(b"simulated-engine-source")


def setup(monkeypatch, *, status='supported', coverage='no_clear_loss', fault=None):
    from app.services import tracking_image_evidence
    seen = {'closed': False, 'observed': []}

    class Images:
        def __init__(self, video, **options):
            seen['references'] = options['manual_baselines']; seen['video'] = video
        def __enter__(self): return self
        def __exit__(self, *args): seen['closed'] = True
        def observe(self, frame, boxes, masks):
            seen['observed'].append(frame)
            if fault == frame:
                raise RuntimeError('injected probe fault')
            return {oid: {'version': 2, 'source': 'native-frame-mask-v2', 'objectId': oid,
                          'frameIndex': frame, 'bbox': box, 'maskArea': 1000,
                          'sourceSignature': __import__('app.video_frames', fromlist=['source_signature']).source_signature(seen['video']),
                          'geometryReferenceFrameIndex': seen['references'][oid].frame_index,
                          'geometryReferenceBBox': seen['references'][oid].bbox,
                          'status': status if oid == 7 else 'supported',
                          'head': {'status': 'matched', 'associationStatus': 'reliable', 'confidence': .99, 'radius': 5,
                                   'point': [(box[0]+box[2])/2, (box[1]+box[3])/2],
                                   'referenceFrameIndex': seen['references'][oid].frame_index,
                                   'referenceBBox': seen['references'][oid].bbox,
                                   'referencePoint': [(seen['references'][oid].bbox[0]+seen['references'][oid].bbox[2])/2,
                                                      (seen['references'][oid].bbox[1]+seen['references'][oid].bbox[3])/2]},
                          'coverage': {'status': coverage if oid == 7 else 'no_clear_loss'},
                          'reasons': [], 'thresholds': {}} for oid, box in boxes.items()}

    def decode(self, session, output):
        detections, _ = self.decode_tracker_output(session, output)
        for detection in detections:
            detection.mask_area = 1000
        return detections, {d.object_id: object() for d in detections}

    monkeypatch.setattr(tracking_image_evidence, 'NativeImageEvidence', Images)
    monkeypatch.setattr(FakeEngine, 'decode_tracker_output_regions', decode, raising=False)
    return seen


def test_supported_native_evidence_survives_tracker_and_publication(tmp_path, monkeypatch, caplog):
    caplog.set_level('INFO', logger='review.tracking')
    seen = setup(monkeypatch)
    manual, rotated, other = [100, 100, 140, 120], [113, 78, 127, 142], [100, 200, 140, 220]
    result = _geometry_segment(tmp_path, monkeypatch, 0, manual,
                               {frame: {7: rotated, 12: other} for frame in range(5)})
    assert seen['closed'] and seen['observed'] == list(range(5))
    assert seen['references'][7].source == 'manual'
    assert result['anomaly_paused'] is None and result['lastProcessedFrame'] == 4
    detail = result['frames'][1]['objects'][0]['anomaly_details']
    assert detail['identity_evidence']['status'] == 'supported'
    assert detail['geometry_reference_bbox'] == manual
    assert 'tracking.image_evidence' in caplog.text


def test_independent_tail_risk_returns_first_image_and_keeps_identity_cause(tmp_path, monkeypatch):
    seen = setup(monkeypatch, status='risk', coverage='tail_loss')
    manual, other = [100, 100, 140, 120], [100, 200, 140, 220]
    result = _geometry_segment(tmp_path, monkeypatch, 0, manual,
                               {frame: {7: manual, 12: other} for frame in range(5)})
    assert seen['closed']
    item = result['anomaly_paused']['reasons'][0]
    assert item['type'] == 'coverage_loss'
    assert item['details']['identity_evidence']['frameIndex'] == result['lastProcessedFrame'] == 1
    assert item['details']['decision_frame_index'] == result['analyzedThroughFrame'] == 3
    assert item['details']['image_observation_count'] == 3
    assert any(r.startswith('image_tail_loss') and r.endswith(' HARD') for r in item['reasons'])
    assert [row['source_frame_index'] for row in result['frames']] == [0, 1]


def test_unknown_image_keeps_existing_geometry_protection(tmp_path, monkeypatch):
    seen = setup(monkeypatch, status='unknown', coverage='unknown')
    manual, risky, other = [100, 100, 140, 120], [100, 100, 140, 112.8], [100, 200, 140, 220]
    result = _geometry_segment(tmp_path, monkeypatch, 0, manual,
                               {frame: {7: risky, 12: other} for frame in range(5)})
    assert seen['closed']
    assert result['anomaly_paused']['reasons'][0]['type'] == 'size_shrink'
    assert result['lastProcessedFrame'] == 1


def test_probe_exception_releases_reader_and_never_publishes_partial_success(tmp_path, monkeypatch):
    seen = setup(monkeypatch, fault=2)
    original = json.dumps({'source_frame_index': 0, 'objects': []})
    (tmp_path / 'tracker_results.json').write_text(original)
    manual, other = [100, 100, 140, 120], [100, 200, 140, 220]
    with pytest.raises(RuntimeError, match='probe fault'):
        _geometry_segment(tmp_path, monkeypatch, 0, manual,
                          {frame: {7: manual, 12: other} for frame in range(5)})
    assert seen['closed']
    assert (tmp_path / 'tracker_results.json').read_text() == original
