"""A transient advisory cannot be promoted by a different persistent cause."""
import copy

from app.services.anomaly_detector import AnomalyDetector, ManualBaseline
from app.tracking_pause_policy import select_pause_evidence


def test_persistent_head_loss_does_not_promote_an_already_recovered_shape_warning():
    box = [100., 100., 200., 200.]
    detector = AnomalyDetector(frame_width=500, frame_height=500, fps=30,
                               all_object_ids={1}, manual_baselines={1: ManualBaseline(box, 0)})
    detector.initialize_seed(0, {1: box})
    rows = []
    for frame, current in [(1, [100., 118., 200., 182.]), (2, box), (3, box)]:
        evidence = {'version': 1, 'source': 'native-frame-mask-v1', 'objectId': 1,
                    'frameIndex': frame, 'bbox': current, 'maskArea': 1000, 'status': 'risk',
                    'head': {'status': 'lost', 'confidence': .99, 'radius': 5., 'point': [150., 150.],
                             'referenceFrameIndex': 0, 'referencePoint': [150., 150.]},
                    'coverage': {'status': 'unknown'}, 'reasons': [], 'thresholds': {}}
        report = detector.push(frame, {1: current}, mask_areas={1: 1000}, identity_evidence={1: evidence})
        item = report.frames[0]
        rows.append({'source_frame_index': frame, 'objects': [{'object_id': 1, 'bbox': current,
                     'mask_area': 1000, 'anomaly_reasons': list(item.reasons),
                     'anomaly_details': copy.deepcopy(item.details)}]})
    assert report.should_pause
    first, selected, _ = select_pause_evidence(report, rows, 3)
    assert first == 1
    assert any(reason.startswith('image_head_lost') and reason.endswith(' HARD') for reason in selected[0].reasons)
    assert any(reason.startswith('manual_height_ratio') and reason.endswith(' SOFT') for reason in selected[0].reasons)
    assert not any(reason.startswith('manual_height_ratio') and reason.endswith(' HARD') for reason in selected[0].reasons)
    assert selected[0].details['identity_evidence']['frameIndex'] == 1


def test_persistent_geometry_keeps_its_cause_when_image_categories_do_not_persist():
    baseline, current = [100., 100., 200., 200.], [100., 118., 200., 182.]
    detector = AnomalyDetector(frame_width=500, frame_height=500, fps=30,
                               all_object_ids={1}, manual_baselines={1: ManualBaseline(baseline, 0)})
    detector.initialize_seed(0, {1: baseline})
    rows = []
    for frame, (head, coverage) in enumerate([('lost', 'unknown'), ('matched', 'tail_loss'),
                                             ('matched', 'foreign_core')], start=1):
        evidence = {'version': 1, 'source': 'native-frame-mask-v1', 'objectId': 1,
                    'frameIndex': frame, 'bbox': current, 'maskArea': 1000, 'status': 'risk',
                    'head': {'status': head, 'confidence': .99, 'radius': 5., 'point': [150., 150.],
                             'referenceFrameIndex': 0, 'referencePoint': [150., 150.]},
                    'coverage': {'status': coverage}, 'reasons': [], 'thresholds': {}}
        report = detector.push(frame, {1: current}, mask_areas={1: 1000}, identity_evidence={1: evidence})
        item = report.frames[0]
        rows.append({'source_frame_index': frame, 'objects': [{'object_id': 1, 'bbox': current,
                     'mask_area': 1000, 'anomaly_reasons': list(item.reasons),
                     'anomaly_details': copy.deepcopy(item.details)}]})
    first, selected, _ = select_pause_evidence(report, rows, 3)
    assert first == 1 and selected[0].details['risk_type'] == 'geometry'
    assert any(reason.startswith('manual_height_ratio') and reason.endswith(' HARD') for reason in selected[0].reasons)
    assert not any(reason.startswith('image_') and reason.endswith(' HARD') for reason in selected[0].reasons)
