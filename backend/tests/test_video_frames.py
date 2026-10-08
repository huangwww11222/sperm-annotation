"""Exact identity, bounded cache and atomic publication; no source mutations."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import cv2
import numpy as np
import pytest
from app.video_frames import ExactFrames, FrameReadError
from app.services.sam3_engine import read_video


def video(path, value=20):
    cap=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),25,(96,64))
    assert cap.isOpened()
    for fi in range(15): cap.write(np.full((64,96,3),value+fi*8,dtype=np.uint8))
    cap.release()
    return path


def pixel(data):
    return float(cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR).mean())


def test_sequential_and_random_frames_keep_identity_and_reuse_decoder(tmp_path,monkeypatch):
    path=video(tmp_path/'video.avi');service=ExactFrames();opens=[];actual=cv2.VideoCapture
    def capture(*a): opens.append(a);return actual(*a)
    monkeypatch.setattr(cv2,'VideoCapture',capture)
    try:
        for fi in [0,1,2,4,7,5,14,0]: assert pixel(service.read(path,fi))==pytest.approx(20+fi*8,abs=2)
        assert len(opens)==2 # 0..7 sequential; one backward seek to 5; cache hits thereafter.
    finally: service.close()


def test_concurrent_same_frame_has_one_decode_and_complete_identical_jpeg(tmp_path,monkeypatch):
    path=video(tmp_path/'video.avi');service=ExactFrames();encodes=[];actual=cv2.imencode
    def encode(*a): encodes.append(True);return actual(*a)
    monkeypatch.setattr(cv2,'imencode',encode)
    try:
        with ThreadPoolExecutor(8) as pool: results=list(pool.map(lambda _:service.read(path,9),range(8)))
        assert len(set(results))==1 and len(encodes)==1
        assert not list((tmp_path/'.frame_cache').glob('*.tmp'))
    finally: service.close()


def test_cache_size_and_decoder_count_are_bounded(tmp_path):
    service=ExactFrames(max_decoders=1,max_entries=2,max_bytes=1500)
    try:
        for folder in ['a','b']:
            directory=tmp_path/folder;directory.mkdir();path=video(directory/'source.avi')
            for fi in range(6): service.read(path,fi)
            cached=list((directory/'.frame_cache').glob('frame_*.jpg'))
            assert len(cached)<=2 and sum(p.stat().st_size for p in cached)<=1500
        assert len(service.decoders)==1
    finally: service.close()


def test_replaced_source_cannot_return_old_cached_pixels(tmp_path):
    path=video(tmp_path/'source.avi');service=ExactFrames()
    try:
        assert pixel(service.read(path,3))==pytest.approx(44,abs=2)
        replacement=video(tmp_path/'new.avi',100);replacement.replace(path)
        assert pixel(service.read(path,3))==pytest.approx(124,abs=2)
    finally: service.close()


def test_failed_cache_publication_does_not_leave_partial_image(tmp_path,monkeypatch):
    path=video(tmp_path/'source.avi');service=ExactFrames();actual=Path.replace
    def fail(self,dest):
        if str(dest).endswith('.jpg'): raise OSError('injected disk failure')
        return actual(self,dest)
    monkeypatch.setattr(Path,'replace',fail)
    try:
        with pytest.raises(OSError,match='injected'):service.read(path,3)
        assert not list((tmp_path/'.frame_cache').glob('frame_*.jpg'))
        assert not list((tmp_path/'.frame_cache').glob('*.tmp'))
    finally: service.close()


def test_failed_seek_falls_back_to_exact_sequential_frames(tmp_path,monkeypatch):
    path=video(tmp_path/'source.avi');actual=cv2.VideoCapture
    class NoSeek:
        def __init__(self,*args):self.cap=actual(*args)
        def set(self,*args):return False
        def __getattr__(self,name):return getattr(self.cap,name)
    monkeypatch.setattr(cv2,'VideoCapture',NoSeek)
    frames,meta=read_video(path,max_frames=3,start_frame=8)
    assert meta['source_frame_indices']==[8,9,10]
    assert float(np.asarray(frames[0]).mean())==pytest.approx(84,abs=2)
    service=ExactFrames()
    try:
        assert pixel(service.read(path,8))==pytest.approx(84,abs=2)
        with pytest.raises(FrameReadError):service.read(path,16)
    finally:service.close()


def test_chunked_real_processor_matches_full_video_preprocessing(tmp_path):
    transformers=pytest.importorskip('transformers')
    from app.services.sam3_engine import Sam3Engine,SourceVideoWindow
    import torch
    processor=transformers.Sam3TrackerVideoProcessor(
        transformers.Sam3ImageProcessor(size={'height':32,'width':32}),
        transformers.Sam2VideoVideoProcessor(size={'height':32,'width':32}))
    path=video(tmp_path/'source.avi');frames,meta=read_video(path,max_frames=5,start_frame=3)
    original=processor.init_video_session(video=frames,inference_device='cpu',processing_device='cpu',inference_state_device='cpu',video_storage_device='cpu',dtype=torch.bfloat16)
    engine=Sam3Engine('fake','cpu','bfloat16');engine.tracker_processor=processor;engine.tracker_model=object()
    with SourceVideoWindow(path,max_frames=5,start_frame=3) as window: bounded=engine.make_tracker_session(window)
    assert all(torch.equal(original.get_frame(fi),bounded.get_frame(fi)) for fi in range(5))
    assert (bounded.video_height,bounded.video_width)==(64,96)
    assert window.meta['source_frame_indices']==[3,4,5,6,7]
    assert len(bounded.processed_frames)==5


def test_per_object_mask_geometry_matches_full_batch_without_retaining_masks():
    from types import SimpleNamespace
    transformers=pytest.importorskip('transformers')
    from app.services.sam3_engine import Sam3Engine
    import torch
    processor=transformers.Sam3TrackerVideoProcessor(
        transformers.Sam3ImageProcessor(size={'height':32,'width':32}),
        transformers.Sam2VideoVideoProcessor(size={'height':32,'width':32}))
    masks=torch.full((3,1,16,16),-2.)
    masks[0,0,2:8,3:10]=3.;masks[2]=2.
    session=SimpleNamespace(video_height=64,video_width=96,obj_ids=[7,17,50])
    output=SimpleNamespace(pred_masks=masks,object_score_logits=torch.tensor([0.,1.,2.]))
    reference=processor.post_process_masks([masks],original_sizes=[[64,96]],binarize=True)[0].numpy()
    engine=Sam3Engine('fake','cpu','float32');engine.tracker_processor=processor;engine.tracker_model=object()
    detections,retained=engine.decode_tracker_output(session,output)
    assert retained=={} and [d.object_id for d in detections]==[7,50]
    for detection,index in zip(detections,[0,2]):
        ys,xs=np.where(reference[index,0]>.5)
        assert detection.bbox==[xs.min(),ys.min(),xs.max()+1,ys.max()+1]
        assert detection.mask_area==int(reference[index].sum())
    _,retained=engine.decode_tracker_output(session,output,keep_masks=True)
    assert all(np.array_equal(retained[oid],reference[i,0]) for oid,i in [(7,0),(50,2)])


def test_model_load_uses_inference_dtype_from_start_and_is_cached(monkeypatch):
    from types import SimpleNamespace
    transformers=pytest.importorskip('transformers')
    from app.services.sam3_engine import Sam3Engine
    import torch
    calls=[]
    class Model:
        @classmethod
        def from_pretrained(cls,model_id,**kwargs):calls.append(('load',kwargs['dtype']));return cls()
        def to(self,device,dtype):calls.append(('to',dtype));return self
        def eval(self):return self
    monkeypatch.setattr(transformers,'Sam3TrackerVideoModel',Model)
    monkeypatch.setattr(transformers,'Sam3TrackerVideoProcessor',SimpleNamespace(from_pretrained=lambda *a,**kw:object()))
    engine=Sam3Engine('fake','cpu','bfloat16');engine.load_tracker();engine.load_tracker()
    assert calls==[('load',torch.bfloat16),('to',torch.bfloat16)]
