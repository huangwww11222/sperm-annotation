"""Production resume/publication with recorded synthetic native observations.

Only model/window IO is replaced. Native image/Mask analysis, detector restore,
source binding and publication execute real code; this is not GPU acceptance.
"""
import copy
import json

import pytest

from app import tracker
from app.video_frames import source_signature
from .test_tracking_adaptive_feedback import FakeEngine, FakeWindow
from .test_tracking_natural_motion_policy import _shared_core_scene


@pytest.mark.parametrize('resume_frame', [8, 11])
def test_native_resume_restores_shared_core_before_new_frames(tmp_path, monkeypatch, resume_frame):
    detector, probe, image, boxes, regions, areas = _shared_core_scene()
    video = tmp_path / 'video.avi'
    video.write_bytes(b'isolated-window-model-fixture')
    signature = source_signature(video)
    detector.image_source_signature = signature
    probe.source_signature = signature
    manual = {oid: list(state.manual_baseline.bbox) for oid, state in detector.states.items()}
    initial = tmp_path / 'annotations_frame_000000.json'
    initial.write_text(json.dumps({'frame': {'frameIndex': 0}, 'annotations': [
        {'object_id': oid, 'bbox': box, 'source': 'manual'} for oid, box in manual.items()]}))
    history = []
    for frame in range(1, resume_frame + 1):
        evidence = probe.observe(frame, image, boxes, regions)
        report = detector.push(frame, boxes, mask_areas=areas, identity_evidence=evidence)
        assert not report.should_pause
        history.append({'frame_index': frame, 'source_frame_index': frame, 'objects': [
            {'object_id': item.object_id, 'bbox': boxes[item.object_id],
             'mask_area': areas[item.object_id], 'anomaly_level': item.level.value,
             'anomaly_reasons': item.reasons, 'anomaly_details': copy.deepcopy(item.details)}
            for item in report.frames]})
    output = tmp_path / 'tracker_results.json'
    output.write_text(''.join(json.dumps(row) + '\n' for row in history))
    seed = tmp_path / f'seed_frame_{resume_frame:06d}.json'
    seed.write_text(json.dumps({'frame': {'frameIndex': resume_frame}, 'annotations': [
        {'object_id': oid, 'bbox': box, 'source': 'ai'} for oid, box in boxes.items()]}))

    class NativeEngine(FakeEngine):
        def decode_tracker_output_regions(self, session, output):
            detections, _ = self.decode_tracker_output(session, output)
            for detection in detections:
                detection.mask_area = areas[detection.object_id]
            return detections, regions

    class NativeImages:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def observe(self, frame, current_boxes, masks):
            return probe.observe(frame, image, current_boxes, masks)

    engine = NativeEngine({frame: boxes for frame in range(resume_frame, 13)}, source_start=resume_frame)
    meta = {'width': 320, 'height': 240, 'fps': 30, 'frameCount': 20}
    monkeypatch.setattr(tracker, '_probe_video', lambda *args: meta)
    monkeypatch.setattr(tracker, 'SourceVideoWindow', lambda *args, **kwargs: FakeWindow({
        **meta, 'source_frame_indices': list(range(resume_frame, 13))}))
    monkeypatch.setattr(tracker, 'get_sam3_engine', lambda *args: engine)
    monkeypatch.setattr('app.services.tracking_image_evidence.NativeImageEvidence', NativeImages)
    result = tracker.track_video(str(video), str(seed), str(output), 13-resume_frame,
                                 start_frame=resume_frame)

    assert result['anomaly_paused'] is not None, 'resume must retain prior native single-core observations'
    assert result['analyzedThroughFrame'] == 12
    assert result['lastProcessedFrame'] == 1
    item = next(item for item in result['anomaly_paused']['reasons'] if item['object_id'] == 2)
    assert item['details']['decision_frame_index'] == 12
    assert item['details']['identity_evidence']['status'] == 'unknown'
    assert item['details']['identity_evidence']['head']['point'] is None
    assert 'identityReference' not in item['details']['identity_evidence']
    # Publication returns the first risk rather than retaining unconfirmed
    # future frames used only to reach the persistent decision.
    published = [json.loads(line) for line in output.read_text().splitlines()]
    assert max(row['source_frame_index'] for row in published) == 1
