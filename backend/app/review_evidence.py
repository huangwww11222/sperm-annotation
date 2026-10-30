"""Head-localization evidence for post-tracking human review; never approvals.

Sparse tiled detector observations supplement per-frame tracking evidence. Neither
an absent detection nor agreement proves the presence/absence of a sperm head.
"""
import logging
import math
from .video_frames import open_capture_at

log = logging.getLogger('review.tracking')


def valid_box(box):
    return (isinstance(box, (list,tuple)) and len(box)==4
            and all(type(v) in (int,float) and math.isfinite(v) for v in box)
            and box[2]>box[0] and box[3]>box[1])


def compare_heads(boxes, candidates):
    if any(not valid_box(b) for b in boxes.values()):
        raise ValueError('追踪框结构无效，不能完成自动核验')
    if any(not isinstance(r,(list,tuple)) or len(r)!=5 or not valid_box(r[:4])
           or type(r[4]) not in (int,float) or not math.isfinite(r[4]) or not 0<=r[4]<=1 for r in candidates):
        raise ValueError('检测候选结构无效，不能完成自动核验')
    findings = []
    used = set()
    associations = {}
    for oid, box in boxes.items():
        cx, cy = (box[0]+box[2])/2, (box[1]+box[3])/2
        scale = max(3., math.hypot(box[2]-box[0], box[3]-box[1]))
        matches = []
        for index, row in enumerate(candidates):
            x, y = (row[0]+row[2])/2, (row[1]+row[3])/2
            d = math.hypot(x-cx,y-cy)
            if d <= scale:
                matches.append((d, index, row))
        if not matches:
            findings.append(dict(objectId=oid, kind='detector_unavailable_for_object', message='检测器未提供对应头部证据，请结合前后帧核对', bbox=box))
            continue
        matches.sort()
        distance, index, row = matches[0]
        used.add(index)
        if len(matches)==1: associations.setdefault(index,[]).append(oid)
        if distance > max(3., .35*scale):
            findings.append(dict(objectId=oid, kind='detector_mismatch', message='检测头部与追踪框位置不一致', bbox=box, candidateBox=row[:4], distancePx=round(distance,2)))
        intersection=max(0,min(box[2],row[2])-max(box[0],row[0]))*max(0,min(box[3],row[3])-max(box[1],row[1]))
        union=(box[2]-box[0])*(box[3]-box[1])+(row[2]-row[0])*(row[3]-row[1])-intersection
        if distance <= max(3., .35*scale) and intersection/max(union,1e-9)<.4:
            findings.append(dict(objectId=oid,kind='head_boundary',message='头部检测与追踪框边界差异较大，请核对框范围',bbox=box,candidateBox=row[:4]))
        # Multiple candidates / intersecting boxes are not proof of shared identity.
    for index, ids in associations.items():
        if len(ids)>1:
            for oid in ids:
                findings.append(dict(objectId=oid,kind='association_uncertain',message='多个对象仅对应一个检测候选，可能是遮挡或重复关联，请查看分合过程',bbox=boxes[oid],relatedObjects=ids,candidateBox=candidates[index][:4]))
    for index, row in enumerate(candidates):
        if index not in used and row[4] >= .5:
            findings.append(dict(objectId=None, kind='unlabelled_candidate', message='未关联头部候选：请判断是否精子或杂质；不会自动新增对象', bbox=row[:4], score=row[4]))
    return findings


class ReviewEvidence:
    def __init__(self, video, first_frame=0):
        self.video = video
        self.first_frame = first_frame
        self.failed = None

    def observe(self, frame, boxes, identity=None):
        findings = []
        for oid, item in (identity or {}).items():
            if item.get('status') == 'risk' and not all('tail' in reason for reason in item.get('reasons', []) or ['unknown']):
                findings.append(dict(objectId=int(oid), kind='subject_identity', message='头部位置或身份证据存在疑点，请查看前后帧', bbox=boxes.get(int(oid)), reasons=item.get('reasons', [])))
        result = dict(version=1, state='not_sampled', findings=findings, backwardCheck='not_run')
        if frame != self.first_frame and frame % 5:
            return result
        if self.failed:
            return dict(result, state='failed', error=self.failed)
        reader = None
        try:
            from .head_click import detect_frame
            reader = open_capture_at(self.video, frame)
            ok, image = reader.read()
            if not ok:
                raise RuntimeError('原始帧读取失败')
            candidates = detect_frame(image)
            findings.extend(compare_heads(boxes, candidates))
            return dict(result, state='checked', findings=findings, detectorCandidateCount=len(candidates))
        except Exception as exc:
            self.failed = str(exc)
            log.exception('tracking.review_evidence_failed frame=%s', frame)
            findings.append(dict(objectId=None, kind='check_failed', message='头部检测未完成，需人工检查完整画面', error=str(exc)))
            return dict(result, state='failed', findings=findings, error=str(exc))
        finally:
            if reader is not None: reader.release()


def backward_checks(video, engine, rows, limit=2, window=8):
    """Bounded cycle check on doubtful clips. Agreement is never an approval."""
    from .services.sam3_engine import SourceVideoWindow
    candidates=[r for r in rows if any(f['kind'] in ('subject_identity','detector_mismatch','association_uncertain','tracking_lost','head_boundary') for f in (r.get('reviewEvidence') or {}).get('findings',[]))]
    processed=[]
    by_frame={r['source_frame_index']:r for r in rows}
    for row in candidates:
        end=row['source_frame_index'];start=max(min(by_frame),end-window+1)
        if end-start<2 or any(abs(end-old)<window for old in processed): continue
        if len(processed)>=limit: break
        processed.append(end)
        evidence=row['reviewEvidence'];evidence['backwardCheck']='running'
        iterator=None
        try:
            if not row['objects']: raise ValueError('片段末帧无可用对象，不能进行反向核验')
            with SourceVideoWindow(video, start_frame=start,max_frames=end-start+1) as frames:
                images=list(frames)
            reverse_session=engine.make_tracker_session(list(reversed(images)))
            del images
            engine.add_manual_boxes(reverse_session,0,row['objects'])
            iterator=engine.propagate_manual(reverse_session,max_frames=end-start+1,start_frame_idx=0)
            compared=0; observed=set()
            for output in iterator:
                index=int(output.frame_idx)
                if index<0 or index>end-start:raise ValueError('反向结果帧号越界')
                target=end-index
                if target==end: continue
                if target in observed:raise ValueError('反向结果帧号重复')
                observed.add(target)
                original=by_frame.get(target)
                if original is None: continue
                predicted,_=engine.decode_tracker_output(reverse_session,output)
                reverse={int(d.object_id):d.bbox for d in predicted}
                if len(reverse)!=len(predicted) or any(not valid_box(b) for b in reverse.values()):raise ValueError('反向结果包含重复身份或无效框')
                for obj in original['objects']:
                    oid=obj['object_id'];a=obj['bbox'];b=reverse.get(oid)
                    compared+=1
                    inter=0 if b is None else max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
                    union=(a[2]-a[0])*(a[3]-a[1])+(0 if b is None else (b[2]-b[0])*(b[3]-b[1]))-inter
                    if b is None or inter/max(union,1e-9)<.5:
                        e=original.setdefault('reviewEvidence',dict(state='not_sampled',findings=[],backwardCheck='not_run'))
                        e['findings'].append(dict(objectId=oid,kind='backward_disagreement',message='短片段正反向定位不一致，请核对头部与身份',bbox=a,reverseBox=b,cycleIou=inter/max(union,1e-9)))
            if observed!=set(range(start,end)) or not compared: raise ValueError('反向核验未返回完整可比较帧')
            evidence.update(backwardCheck='checked',backwardRange=[start,end],backwardComparisons=compared)
            log.info('tracking.backward_checked start=%s end=%s comparisons=%s',start,end,compared)
        except Exception as exc:
            evidence.update(backwardCheck='failed',backwardError=str(exc))
            evidence['findings'].append(dict(objectId=None,kind='check_failed',message='反向核验未完成，请人工检查此片段'))
            log.exception('tracking.backward_failed start=%s end=%s',start,end)
        finally:
            if iterator is not None and hasattr(iterator,'close'): iterator.close()
