"""Local benchmark on isolated copies; outputs timings, not inference claims."""
import argparse, hashlib, json, resource, shutil, sys, time
from pathlib import Path
import cv2

p=argparse.ArgumentParser()
p.add_argument('--source-dir',type=Path,required=True)
p.add_argument('--phase',choices=['before','after'],default='after')
p.add_argument('--processor',action='store_true')
p.add_argument('--pipeline',action='store_true',help='Real decoder/processor and simulated inference, never GPU acceptance')
p.add_argument('--video',default='')
p.add_argument('--count',type=int,default=8)
args=p.parse_args()
from app.config import TRACK_DATA_DIR,DB_FILE,DATA_DIR,MODEL_ID
assert all('work' in path.resolve().parts for path in [TRACK_DATA_DIR,DB_FILE,DATA_DIR]),'Only isolated work/ storage is permitted'
from app import main, tracker
assert args.source_dir.is_dir()
assert args.phase!='before' or args.processor,'Before mode only measures the previous whole-window preprocessing'
source=args.source_dir
results=[]
for original in sorted(source.glob('*.mp4')):
    if args.video and original.name!=args.video: continue
    directory=TRACK_DATA_DIR/('real-'+original.stem)
    directory.mkdir(parents=True,exist_ok=True)
    video=directory/original.name
    if not video.exists(): shutil.copy2(original,video)
    meta=tracker._probe_video(video)
    (directory/'media.json').write_text(json.dumps({'videoName':video.name,**meta}))
    if args.processor or args.pipeline:
        from app.services.sam3_engine import Sam3Engine,read_video
        from transformers import Sam3TrackerVideoProcessor
        processor=Sam3TrackerVideoProcessor.from_pretrained(MODEL_ID,local_files_only=True)
        engine=Sam3Engine(MODEL_ID,'cpu','bfloat16')
        engine.tracker_processor=processor;engine.tracker_model=object()
        t=time.perf_counter()
        if args.pipeline:
            from types import SimpleNamespace
            from app.services.sam3_engine import TrackDetection
            start=meta['frameCount']//2
            box=[round(x,3) for x in [meta['width']*.4,meta['height']*.4,meta['width']*.41,meta['height']*.405]]
            seed=directory/('annotations_frame_'+str(start).zfill(6)+'.json')
            seed.write_text(json.dumps({'frame':{'frameIndex':start},'annotations':[{'object_id':42,'bbox':box,'source':'manual'}]}))
            engine.propagate_manual=lambda session,max_frames,start_frame_idx:(SimpleNamespace(frame_idx=fi) for fi in range(max_frames))
            engine.decode_tracker_output=lambda session,output:([TrackDetection(object_id=42,bbox=box,score=.9)],{})
            original_get=tracker.get_sam3_engine
            tracker.get_sam3_engine=lambda *a:engine
            try: tracked=tracker.track_video(str(video),str(seed),str(directory/tracker.RESULT_FILE_NAME),args.count,start_frame=start)
            finally: tracker.get_sam3_engine=original_get
            expected=list(range(start,min(start+args.count,meta['frameCount'])))
            assert tracked['sourceFrameIndices']==expected
            assert tracked['lastProcessedFrame']==expected[-1]
            for frame in tracked['frames']:
                if frame['source_frame_index']>=start:
                    assert frame['objects'][0]['object_id']==42 and frame['objects'][0]['bbox']==box
            row={'video':original.name,'mode':'real_source_simulated_inference','sourceIndices':expected,'seconds':time.perf_counter()-t,'sourceFps':tracked['sourceFps'],'lastFrame':tracked['lastProcessedFrame'],'size':[meta['width'],meta['height']]}
            results.append(row);print(json.dumps(row,ensure_ascii=False),flush=True);continue
        elif args.phase=='before':
            frames,info=read_video(video,max_frames=args.count)
            session=processor.init_video_session(video=frames,inference_device='cpu',processing_device='cpu',inference_state_device='cpu',video_storage_device='cpu',dtype=engine.torch_dtype,max_vision_features_cache_size=1)
        else:
            from app.services.sam3_engine import SourceVideoWindow
            with SourceVideoWindow(video,max_frames=args.count) as window:
                session=engine.make_tracker_session(window)
                info=window.meta
        row={'video':original.name,'phase':args.phase,'count':args.count,'seconds':time.perf_counter()-t,
             'peakRssMiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024),
             'storageMiB':sum(frame.numel()*frame.element_size() for frame in session.processed_frames.values())/1024**2,'sourceSize':[int(session.video_width),int(session.video_height)],
             'inputHashes':[hashlib.sha256(frame.contiguous().view(__import__('torch').uint8).numpy().tobytes()).hexdigest() for frame in session.processed_frames.values()]}
    else:
        cache=directory/'.frame_cache'
        if cache.exists(): shutil.rmtree(cache)
        cold=[];warm=[];hashes=[]
        for timings in [cold,warm]:
            for fi in range(12):
                t=time.perf_counter();r=main.video_frame(directory.name,fi,{'uid':3});timings.append((time.perf_counter()-t)*1000)
                data=r.body if hasattr(r,'body') else Path(r.path).read_bytes()
                if timings is cold:hashes.append(hashlib.sha256(data).hexdigest())
        probe=cv2.VideoCapture(str(video));references=[]
        for fi in range(12):
            ok,frame=probe.read();assert ok
            ok,jpeg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,95]);assert ok
            references.append(hashlib.sha256(jpeg.tobytes()).hexdigest())
        probe.release();assert hashes==references,'source frame identity changed'
        # Rewind a populated branch from the middle, preserving raw inputs.
        rows=[{'frame_index':fi,'source_frame_index':fi,'objects':[]} for fi in range(meta['frameCount'])]
        result=directory/tracker.RESULT_FILE_NAME;result.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        t=time.perf_counter();info=tracker.rewind_tracking_results(result,video,meta['frameCount']//2);rewind_ms=(time.perf_counter()-t)*1000
        row={'video':original.name,'phase':args.phase,'meta':meta,'coldFrameMs':cold,'warmFrameMs':warm,'frameHashes':hashes,
             'coldMeanMs':sum(cold)/len(cold),'warmMeanMs':sum(warm)/len(warm),'rewindMs':rewind_ms,'rewind':info}
    results.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
Path('work/performance-'+args.phase+('-pipeline' if args.pipeline else '-processor-'+Path(args.video).stem+'-'+str(args.count) if args.processor else '-frames')+'.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
