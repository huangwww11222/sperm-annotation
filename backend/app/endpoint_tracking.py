"""Independent human endpoint tracking. Evidence only; never replaces labels.

Tail IDs have their own namespace. Association needs a unique trajectory across
multiple frames, not equal IDs, endpoint proximity or a single overlapping box.
"""
import copy
import hashlib
import json
import logging
import math
from .review_evidence import valid_box

log = logging.getLogger('review.tracking')


def validate_endpoints(workspace, head, tail, width, height, count):
    if count < 2 or not head:
        raise ValueError('首尾双向追踪需要至少两帧，以及完整人工首帧标注')
    for frame, supplied in ((0, head), (count-1, tail)):
        saved = {}
        for o in workspace.get('manualAnnotations', []):
            if o.get('source') != 'manual' or o.get('frameIndex', 0) != frame:continue
            b=o.get('bbox');oid=o.get('objectId')
            if not b:continue
            if oid in saved:raise ValueError('已保存端点存在重复对象，请重新读取工作区')
            saved[oid]=[b['x']*width/100,b['y']*height/100,(b['x']+b['width'])*width/100,(b['y']+b['height'])*height/100]
        actual={}
        for o in supplied:
            oid=o.get('object_id',o.get('objectId'));b=o.get('bbox')
            if type(oid) is not int or oid<=0 or oid in actual or o.get('source')!='manual' or not valid_box(b):
                raise ValueError('首尾端点必须是人工框，且身份唯一、坐标有效')
            if o.get('frameIndex',frame)!=frame or b[0]<0 or b[1]<0 or b[2]>width or b[3]>height:
                raise ValueError('首尾端点帧号或坐标越界')
            actual[oid]=b
        if actual.keys()!=saved.keys() or any(max(abs(a-b) for a,b in zip(actual[k],saved[k]))>.501 for k in actual):
            raise ValueError('首尾标注与已保存工作区不一致，请保存并重新启动')


def iou(a,b):
    inter=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return inter/max(1e-9,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter)


def _evidence(row):
    return row.setdefault('reviewEvidence',dict(state='not_sampled',findings=[],backwardCheck='not_run'))


def verify_endpoints(video,engine,rows,tail,window_size=16,progress=None,reverse_observations=None,joint_mode=False):
    """Reverse entire source timeline in overlapping bounded windows.

    Window boundaries use reverse AI continuation, never new human anchors.
    Failure leaves forward geometry intact and explicitly marks unchecked data.
    """
    from .services.sam3_engine import SourceVideoWindow
    frames={r['source_frame_index']:r for r in rows}
    end=max(frames);first=min(frames)
    signature=hashlib.sha256(json.dumps(dict(head=frames[first]['objects'],tail=tail,first=first,last=end,
                                           model=str(getattr(engine,'model_id','unknown')),policy='joint-endpoints-v3' if joint_mode else 'human-endpoints-v1'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    report=dict(version=1,state='running',anchorKind='human-terminal',anchorFrame=end,anchorHash=signature,
                firstFrame=first,lastFrame=end,matches={},unmatchedTailIds=[],checkedFrames=0,
                policy='joint-endpoints-v3' if joint_mode else 'human-endpoints-v1',iouThreshold=.5)
    reverse={};current=end;prompts=copy.deepcopy(tail)
    try:
        if first!=0 or set(frames)!=set(range(end+1)):
            raise ValueError('双向核验缺少完整正向帧，不能默认通过')
        if not tail:
            report['state']='insufficient'
        else:
            if window_size<2:raise ValueError('双向核验窗口至少两帧')
            reverse[end]={int(o['object_id']):list(o['bbox']) for o in tail}
            while current>first and prompts:
                start=max(first,current-window_size+1)
                if progress:progress(stage='checking',message=(f'正在处理视频：第 {start+1}–{current+1} 帧' if joint_mode else f'人工尾帧反向核验：第 {current+1} → {start+1} 帧'))
                # Bounded image list is released immediately after processor input.
                with SourceVideoWindow(video,start_frame=start,max_frames=current-start+1) as source:
                    images=list(source)
                if len(images)!=current-start+1:raise ValueError('反向核验原始帧读取不完整')
                images.reverse()
                session=engine.make_tracker_session(images);del images
                engine.add_manual_boxes(session,0,prompts)
                iterator=engine.propagate_manual(session,max_frames=current-start+1,start_frame_idx=0)
                observed=set()
                try:
                    for output in iterator:
                        index=output.frame_idx
                        if type(index) is not int or not 0<=index<=current-start or index in observed:
                            raise ValueError('反向输出帧号无效或重复')
                        observed.add(index)
                        detections,_=engine.decode_tracker_output(session,output)
                        boxes={int(d.object_id):list(d.bbox) for d in detections}
                        if len(boxes)!=len(detections) or any(not valid_box(b) for b in boxes.values()) or not boxes.keys()<=set(o['object_id'] for o in prompts):
                            raise ValueError('反向输出身份或框结构无效')
                        # Human anchor wins over model output on the endpoint itself.
                        if current-index!=end:reverse[current-index]=boxes
                    if observed!=set(range(current-start+1)):
                        raise ValueError('反向核验未返回完整帧，不能默认通过')
                finally:
                    if hasattr(iterator,'close'):iterator.close()
                del session
                log.info('tracking.endpoint_reverse_chunk start=%s end=%s objects=%s anchor=%s',start,current,len(prompts),signature)
                prompts=[dict(object_id=k,bbox=b) for k,b in reverse[start].items()]
                current=start
            # Every tail track can end as it leaves the image backwards. The
            # earlier range remains explicitly without an endpoint counterpart.
            scores={};counts={};overlaps={}
            for fi,boxes in reverse.items():
                for tid,b in boxes.items():
                    for o in frames[fi]['objects']:
                        key=(tid,o['object_id']);value=iou(b,o['bbox']);scores[key]=scores.get(key,0)+value;counts[key]=counts.get(key,0)+1
                        if joint_mode:overlaps.setdefault(key,[]).append(value)
            averages={k:s/max(1,counts[k]) for k,s in scores.items() if counts[k]>=min(3,end-first+1)}
            if joint_mode:
                # A long drifting interval must not erase a strong, unique
                # human-endpoint correspondence. Still require multi-frame
                # support and an actual endpoint match, never equal IDs.
                anchored={}
                for (tid,fid),mean in averages.items():
                    anchor=max((iou(reverse.get(fi,{}).get(tid),o['bbox']) for fi in (first,end)
                                if tid in reverse.get(fi,{}) for o in frames[fi]['objects'] if o['object_id']==fid),default=0.)
                    top=sorted(overlaps[(tid,fid)],reverse=True)[:min(3,end-first+1)]
                    if anchor>=.5 and min(top)>=.5:anchored[(tid,fid)]=.25*mean+.75*sum(top)/len(top)
                averages=anchored
            for tid in reverse[end]:
                ranked=sorted(((s,fid) for (t,fid),s in averages.items() if t==tid),reverse=True)
                if not ranked or ranked[0][0]<.5 or (len(ranked)>1 and ranked[0][0]-ranked[1][0]<.15):continue
                score,fid=ranked[0]
                competing=sorted((s,t) for (t,f),s in averages.items() if f==fid and t!=tid)
                if competing and score-competing[-1][0]<.15:continue
                report['matches'][str(tid)]=fid
            if joint_mode:report['matches']=extend_matches(rows,reverse,report['matches'])
            report.update(state='checked',checkedFrames=len(reverse),unmatchedTailIds=[t for t in reverse[end] if str(t) not in report['matches']])
            if reverse_observations is not None:reverse_observations.update(reverse)
    except Exception as exc:
        report.update(state='failed',error=str(exc))
        log.exception('tracking.endpoint_reverse_failed anchor=%s',signature)
    matched={fid:int(tid) for tid,fid in report['matches'].items()}
    for fi,row in frames.items():
        evidence=_evidence(row)
        evidence['endpointCheck']=dict(report,observed=fi in reverse)
        if fi==end:evidence['endpointCheck']['terminalAnnotations']=copy.deepcopy(tail)
        if report['state']=='failed':
            evidence['findings'].append(dict(objectId=None,kind='check_failed',message='人工尾帧反向核验失败，请检查此片段',error=report['error']))
            continue
        boxes=reverse.get(fi,{})
        for o in row['objects']:
            oid=o['object_id'];tid=matched.get(oid);other=boxes.get(tid)
            if other is None:
                evidence['findings'].append(dict(objectId=oid,kind='endpoint_identity_uncertain',message='此对象未获人工尾帧轨迹对应；可能已出画或跟丢，请核对片段',bbox=o['bbox']))
            elif iou(o['bbox'],other)<.5:
                evidence['findings'].append(dict(objectId=oid,kind='endpoint_disagreement',message='人工首尾独立追踪位置不一致，请核对头部和身份',bbox=o['bbox'],reverseBox=other,tailObjectId=tid,cycleIou=iou(o['bbox'],other)))
        for tid,b in boxes.items():
            if str(tid) not in report['matches']:
                evidence['findings'].append(dict(objectId=None,tailObjectId=tid,kind='endpoint_identity_uncertain',message='尾帧对象未能唯一对应正向轨迹，请核对漏标、入画或身份',bbox=b))
    log.info('tracking.endpoint_verified state=%s anchor=%s matched=%s unmatched=%s checked=%s',report['state'],signature,report['matches'],report['unmatchedTailIds'],report['checkedFrames'])
    return report


def terminal_aliases(row):
    """Keep independent tail annotation IDs in raw workspace; canonical IDs in A.

    A mapping is evidence of temporal correspondence, never a geometry approval.
    Human edits keep their stable tail identity and override AI at that endpoint.
    """
    check=(row.get('reviewEvidence') or {}).get('endpointCheck') or {}
    if check.get('state')!='checked' or 'terminalAnnotations' not in check:return {}
    return {int(t):int(f) for t,f in {**check.get('matches',{}),**check.get('unmatchedAliases',{})}.items()}


def _head_alignment(a, b):
    """Position evidence independent of bounding-box padding; never area alone."""
    if not valid_box(a) or not valid_box(b):return None
    widths=[a[2]-a[0],a[3]-a[1],b[2]-b[0],b[3]-b[1]]
    ratio=((a[2]-a[0])*(a[3]-a[1]))/((b[2]-b[0])*(b[3]-b[1]))
    if not .15<=ratio<=6.7:return None
    d=math.hypot((a[0]+a[2]-b[0]-b[2])/2,(a[1]+a[3]-b[1]-b[3])/2)/max(2.,min(widths))
    return d if d<=.45 else None


def extend_matches(rows, reverse, matches):
    """Recover padding-induced misses with unique contiguous trajectory support.

    Existing verified pairs are reserved. One frame or a crossing is insufficient;
    competing heads/tails remain unresolved rather than being swallowed by area.
    """
    result=dict(matches);frames={r['source_frame_index']:r for r in rows}
    first,end=min(frames),max(frames);used=set(result.values());candidates={}
    tids=set(reverse.get(end,{}))-set(map(int,result))
    for tid in tids:
        observations={}
        for fi,boxes in reverse.items():
            if fi not in frames or tid not in boxes:continue
            for obj in frames[fi]['objects']:
                fid=obj['object_id']
                if fid in used:continue
                d=_head_alignment(obj['bbox'],boxes[tid])
                if d is not None:observations.setdefault(fid,[]).append((fi,d))
        for fid,items in observations.items():
            items=sorted(items);run=longest=0;previous=None
            for fi,_ in items:
                run=run+1 if previous is not None and fi==previous+1 else 1
                longest=max(longest,run);previous=fi
            required=min(3,end-first+1)
            # A real human endpoint and neighbouring frames must support the pair.
            if longest<required or not any(fi in (first,end) for fi,_ in items):continue
            best=sorted(d for _,d in items)[:required]
            candidates[(tid,fid)]=sum(best)/len(best)+.1*(1-len(items)/max(1,len(reverse)))
    for tid in sorted(tids):
        ranked=sorted((score,fid) for (t,fid),score in candidates.items() if t==tid)
        if not ranked or (len(ranked)>1 and ranked[1][0]-ranked[0][0]<.2):continue
        score,fid=ranked[0]
        competing=[s for (t,f),s in candidates.items() if f==fid and t!=tid]
        if competing and min(competing)-score<.2:continue
        if fid in result.values():continue
        result[str(tid)]=fid
        log.info('tracking.endpoint_association_recovered tail=%s object=%s evidence=contiguous-head-centres',tid,fid)
    return result


def project_endpoints(rows):
    """Pure, shared read projection for saved joint runs and newly generated rows.

    Source files and human originals remain untouched. The confirmed complete
    human tail defines endpoint presence/geometry, including unresolved identities.
    UI, raw result and dataset source must all use the same projection.
    """
    terminal=next((r for r in rows if 'terminalAnnotations' in ((r.get('reviewEvidence') or {}).get('endpointCheck') or {})),None)
    if terminal is None:return rows
    check=terminal['reviewEvidence']['endpointCheck']
    if check.get('policy') not in ('joint-endpoints-v2','joint-endpoints-v3'):return rows
    if check.get('state')!='checked' and not (check.get('state')=='insufficient' and check.get('terminalAnnotations')==[]):return rows
    tail=check['terminalAnnotations'];end=check['anchorFrame']
    if terminal['source_frame_index']!=end or not isinstance(tail,list):raise ValueError('人工尾帧证据无效')
    originals={}
    for obj in tail:
        oid=obj.get('object_id');box=obj.get('bbox')
        if type(oid) is not int or oid<=0 or oid in originals or not valid_box(box):raise ValueError('人工尾帧身份或坐标无效')
        originals[oid]=obj
    reverse={end:{oid:list(o['bbox']) for oid,o in originals.items()}}
    # Saved unmatched observations are real reverse boxes, not inferred motion.
    for row in rows:
        fi=row['source_frame_index'];boxes=reverse.setdefault(fi,{})
        for finding in (row.get('reviewEvidence') or {}).get('findings',[]):
            tid=finding.get('tailObjectId')
            if fi!=end and tid in originals and finding.get('kind')=='endpoint_identity_uncertain' and valid_box(finding.get('bbox')):
                boxes[tid]=finding['bbox']
    matches=extend_matches(rows,reverse,check.get('matches',{}))
    aliases=dict(matches);unmatched_aliases={};assigned=set(matches.values())
    reserved={o['object_id'] for row in rows for o in row['objects']}|set(originals)|assigned
    for tid in originals:
        if str(tid) in matches:continue
        canonical=check.get('unmatchedAliases',{}).get(str(tid),tid)
        if canonical in assigned:
            canonical=max(reserved,default=0)+1;reserved.add(canonical)
        assigned.add(canonical);aliases[str(tid)]=canonical
        if canonical!=tid:unmatched_aliases[str(tid)]=canonical
    result=copy.deepcopy(rows)
    for row in result:
        evidence=row.setdefault('reviewEvidence',{})
        updated=evidence.get('endpointCheck')
        if updated is not None:
            updated.update(matches=matches,unmatchedTailIds=[tid for tid in originals if str(tid) not in matches],unmatchedAliases=unmatched_aliases,associationPolicy='head-centre-v1')
        if row['source_frame_index']==end:
            row['objects']=[dict(object_id=aliases[str(tid)],bbox=list(o['bbox']),name=o.get('name','rare sperm'),source='manual_endpoint',score=None,mask_area=None) for tid,o in originals.items()]
    return result
