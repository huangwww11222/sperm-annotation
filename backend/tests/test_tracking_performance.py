"""Bounded 4K preparation and short publication locks; no model/GPU claim."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from types import SimpleNamespace
from pathlib import Path
import cv2
import numpy as np
import pytest

from app import main, tracker
from app.schemas import TrackRequest
from .test_annotation_controls import controls, save
from .test_review_workflow import task


def test_rewind_never_decodes_or_encodes_source_video(controls, monkeypatch):
    client, root, state = controls
    (root / tracker.OVERLAY_FILE_NAME).write_bytes(b'obsolete preview')
    renders = []
    monkeypatch.setattr(tracker, '_render_overlay_video', lambda *args: renders.append(args))
    info = tracker.rewind_tracking_results(root / tracker.RESULT_FILE_NAME, root / 'video.avi', 1)
    assert info['removedRows'] == 1
    assert info['overlayRegenerated'] is False
    assert renders == [], 'HTTP rewind must not transcode a full 4K video'
    assert not (root / tracker.OVERLAY_FILE_NAME).exists()


def test_slow_inference_does_not_block_workspace_reads_or_view_saves(controls, monkeypatch):
    client, root, state = controls
    entered, finish = Event(), Event()
    def inference(*args, **kwargs):
        entered.set()
        assert finish.wait(5), 'test inference was never released'
        return {'lastProcessedFrame': 0, 'processedFrames': 1}
    monkeypatch.setattr(main, 'track_video', inference)
    req = TrackRequest(mediaId='control-video', startFrame=0, annotations=[{'object_id': 7, 'bbox': [10, 10, 20, 20]}])
    with ThreadPoolExecutor(3) as pool:
        task = pool.submit(main._run_tracking_task, 'slow-test', req, root / 'video.avi', root / 'seed.json', root / tracker.RESULT_FILE_NAME)
        assert entered.wait(2)
        read = pool.submit(client.get, '/api/track/workspace/control-video')
        write = pool.submit(save, client, {**state, 'currentFrame': 1}, 0, 'view-save')
        try:
            assert read.result(timeout=.5).status_code == 200
            assert write.result(timeout=.5).status_code == 200
        finally:
            finish.set()
        task.result(timeout=2)


def test_model_busy_rejects_changes_to_manual_geometry_but_allows_view_state(controls, monkeypatch):
    client, root, state = controls
    monkeypatch.setattr(main, 'TASKS', {'test': {'status': 'running'}})
    changed = json.loads(json.dumps(state))
    changed['manualAnnotations'][0]['bbox']['width'] += 5
    response = save(client, changed)
    assert response.status_code == 409
    assert save(client, {**state, 'currentFrame': 1}, key='view-only').status_code == 200


def test_real_4k_segment_keeps_source_frame_numbers_and_pixel_boxes(tmp_path, monkeypatch, caplog):
    caplog.set_level('INFO', logger='review.tracking')
    video = tmp_path / '4k.avi'
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 25, (3840, 2160))
    assert writer.isOpened()
    for fi in range(12):
        frame = np.full((2160, 3840, 3), 30+fi*5, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    box = [3000., 1500., 3040., 1520.]
    seed = tmp_path / 'annotations_frame_000008.json'
    seed.write_text(json.dumps({'frame': {'frameIndex': 8}, 'annotations': [{'object_id': 42, 'bbox': box, 'source': 'manual'}]}))
    calls = []
    class Engine:
        model_id='fake';device='cpu';torch_dtype='float32'
        def make_tracker_session(self, frames):
            frames = list(frames)
            assert len(frames) == 3
            assert all(f.size == (3840,2160) for f in frames)
            assert np.asarray(frames[0]).mean() == pytest.approx(70,abs=2)
            return object()
        def add_manual_boxes(self, session, frame, objects):
            assert frame == 0 and objects[0]['bbox'] == box and objects[0]['object_id'] == 42
        def propagate_manual(self, session, max_frames, start_frame_idx):
            assert max_frames == 3 and start_frame_idx == 0
            for fi in [0,1,2]: yield SimpleNamespace(frame_idx=fi)
        def decode_tracker_output(self, session, output):
            return [SimpleNamespace(object_id=42,bbox=box,score=.9,mask_area=None,sam3_object_id=42)], None
    monkeypatch.setattr(tracker,'get_sam3_engine',lambda *a:Engine())
    monkeypatch.setattr(tracker,'_render_overlay_video',lambda *a:calls.append(a))
    progress=[]
    result=tracker.track_video(str(video),str(seed),str(tmp_path/'tracker_results.json'),3,start_frame=8,progress=lambda **v:progress.append(v))
    assert [r['frame_index'] for r in result['frames']] == [8,9,10]
    assert result['sourceFrameIndices'] == [8,9,10]
    assert result['frames'][-1]['objects'][0]['bbox'] == box
    assert result['lastProcessedFrame'] == 10 and not result['reachedVideoEnd']
    assert result['media']['width'] == 3840 and result['media']['height'] == 2160
    assert calls == []
    assert progress[-1]['stage'] == 'saving_results'
    assert 'frames=3 width=3840 height=2160 rgb_bytes=74649600' in caplog.text


def test_lost_tracking_start_response_replays_same_job_without_duplicate_inference(controls,monkeypatch):
    client,root,state=controls
    monkeypatch.setattr(main,'SAM3_ENABLED',True)
    submissions=[]
    class Executor:
        def submit(self,*args):submissions.append(args)
    monkeypatch.setattr(main,'TRACK_EXECUTOR',Executor())
    body={'mediaId':'control-video','startFrame':0,'annotations':[{'object_id':7,'bbox':[10,10,30,30]}]}
    headers={'Idempotency-Key':'same-tracking-request'}
    first=client.post('/api/track',json=body,headers=headers)
    again=client.post('/api/track',json=body,headers=headers)
    assert first.status_code==again.status_code==202
    assert first.json()==again.json() and len(submissions)==1
    different={**body,'startFrame':1}
    assert client.post('/api/track',json=different,headers=headers).status_code==409
    status=client.get('/api/track/status/'+first.json()['taskId']).json()
    assert 'requestHash' not in status and 'requestKey' not in status
    main.TASKS[first.json()['taskId']]['status']='success'
    reset=client.post('/api/review/media/control-video/reset-annotations',json={'expectedRevision':0,'confirmDiscard':True},headers={'X-Review-Contract':'2','Idempotency-Key':'reset-after-task'})
    assert reset.status_code==200
    assert client.post('/api/track',json=body,headers=headers).status_code==409


def test_result_publication_failure_keeps_previous_branch_and_cleans_temporary_file(tmp_path,monkeypatch):
    result=tmp_path/'tracker_results.json'
    original=json.dumps({'frame_index':0,'source_frame_index':0,'objects':[]})+'\n'
    result.write_text(original)
    replace=Path.replace
    def fail(path,destination):
        if Path(destination)==result:raise OSError('injected publication failure')
        return replace(path,destination)
    monkeypatch.setattr(Path,'replace',fail)
    with pytest.raises(OSError,match='injected'):
        tracker._merge_rows(result,[{'frame_index':1,'source_frame_index':1,'objects':[]}])
    assert result.read_text()==original
    assert not list(tmp_path.glob('*.tmp'))


def test_preview_is_lazy_cached_and_never_holds_source_lock_during_render(controls,monkeypatch):
    from app import tracking_preview
    client,root,state=controls
    renders=[]
    def render(video,output,meta,rows):
        # The separate HTTP save must complete while rendering is in flight.
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(save,client,{**state,'currentFrame':2},0,'preview-view').result(timeout=.5).status_code==200
        output.write_bytes(b'preview')
        renders.append(True)
    monkeypatch.setattr(tracking_preview,'_render_overlay_video',render)
    assert client.get('/api/track/overlay/control-video').status_code==200
    assert client.get('/api/track/overlay/control-video').status_code==200
    assert len(renders)==1


def test_preview_rejects_changed_sources_and_cannot_publish_stale_file(controls,monkeypatch):
    from app import tracking_preview
    client,root,state=controls
    def render(video,output,meta,rows):
        assert save(client,{**state,'deletedObjectIds':[7]},key='changed').status_code==200
        output.write_bytes(b'stale preview')
    monkeypatch.setattr(tracking_preview,'_render_overlay_video',render)
    assert client.get('/api/track/overlay/control-video').status_code==409
    assert not (root / tracker.OVERLAY_FILE_NAME).exists()
    assert not list((root/'.frame_cache').glob('overlay-*'))


def test_model_diagnostic_returns_serializable_device_and_correct_dtype(controls,monkeypatch):
    client,root,state=controls
    monkeypatch.setattr(main,'get_tracker_engine',lambda:SimpleNamespace(model=None,processor=None,model_id='local-model',device='cuda:0',torch_dtype='bfloat16'))
    response=client.get('/api/track/sam3/health')
    assert response.status_code==200
    assert response.json()['device']=='cuda:0' and response.json()['dtype']=='bfloat16'
