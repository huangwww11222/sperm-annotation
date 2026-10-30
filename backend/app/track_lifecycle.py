"""Conservative boundary-exit protection; geometry alone never deletes a head.

Requires clipped outward motion, predicted departure, real image identity risk,
and persistent transfer to an already existing neighbour. Manual anchors and a
verified terminal identity take priority. Interior occlusions remain unresolved.
"""
import copy
import logging
from .review_evidence import valid_box

log=logging.getLogger('review.tracking')


def _outward(history, width, height):
    if len(history)<3:return False
    a,b,c=history[-3:]
    if not all(valid_box(v) for v in (a,b,c)):return False
    for axis,limit in ((0,width),(1,height)):
        sizes=[v[axis+2]-v[axis] for v in (a,b,c)]
        centres=[(v[axis]+v[axis+2])/2 for v in (a,b,c)]
        if sizes[-1]>.55*max(sizes[:-1]):continue
        delta=centres[-1]-centres[-2]
        margin=max(1.,.1*sizes[-1])
        if c[axis]<=1 and centres[0]>centres[1]>centres[2] and delta<-1 and centres[-1]+delta<=margin:return True
        if c[axis+2]>=limit-1 and centres[0]<centres[1]<centres[2] and delta>1 and centres[-1]+delta>=limit-margin:return True
    return False


def _captured(box, neighbour):
    x,y=(neighbour[0]+neighbour[2])/2,(neighbour[1]+neighbour[3])/2
    return box[0]<=x<=box[2] and box[1]<=y<=box[3]


def protect_boundary_exits(rows, *, width, height, manual=None, trajectory=None, log_changes=True):
    if not rows or width<=0 or height<=0:return rows
    frames={r['source_frame_index']:r for r in rows}
    if len(frames)!=len(rows) or set(frames)!=set(range(max(frames)+1)):return rows
    observations=trajectory or rows
    original={r['source_frame_index']:{o['object_id']:o['bbox'] for o in r['objects']} for r in observations}
    output={r['source_frame_index']:{o['object_id']:o['bbox'] for o in r['objects']} for r in rows}
    manual=manual or {};result=copy.deepcopy(rows);exited={};history={}
    for row in result:
        fi=row['source_frame_index'];evidence=row.setdefault('reviewEvidence',{})
        anchored=set((evidence.get('endpointCheck') or {}).get('matches',{}).values())
        existing=set((evidence.get('boundaryExit') or {}).get('exitedObjectIds',[]))
        exited.update({oid:fi for oid in existing})
        for obj in row['objects']:
            oid=obj['object_id']
            if oid in manual.get(fi,{}) or obj.get('source') in ('manual','manual_endpoint'):
                exited.pop(oid,None);history.pop(oid,None);continue
            if oid in exited or oid in anchored:continue
            past=history.get(oid,[])
            risk=any(f.get('objectId')==oid and f.get('kind')=='subject_identity' and
                     set(f.get('reasons',[])) & {'mask_head_missing','shared_optical_core'} for f in evidence.get('findings',[]))
            if not risk or not _outward([b for _,b in past[-3:]],width,height):continue
            if len(past)<3 or [i for i,_ in past[-3:]]!=[fi-3,fi-2,fi-1]:continue
            current=original[fi].get(oid)
            if current is None:continue
            # One-frame contact is not a confirmed transfer. The neighbour must
            # predate it, be distinct at the edge, and remain the captured head.
            for other,b in output[fi].items():
                if other==oid or other not in original.get(fi-1,{}) or other not in original.get(fi+1,{}):continue
                if _captured(past[-1][1],original[fi-1][other]):continue
                if not (_captured(current,b) or _captured(obj['bbox'],b)):continue
                following=original[fi+1].get(oid)
                if following is None or not (_captured(following,original[fi+1][other]) or _captured(output[fi+1].get(oid,following),output[fi+1].get(other,b))):continue
                exited[oid]=fi
                (log.warning if log_changes else log.debug)('tracking.boundary_exit object=%s frame=%s neighbour=%s evidence=clipped-outward-motion-and-image-identity-loss',oid,fi,other)
                break
        for oid,b in original[fi].items():
            history.setdefault(oid,[]).append((fi,b));history[oid]=history[oid][-3:]
        if exited:
            row['objects']=[o for o in row['objects'] if o['object_id'] not in exited]
            if 'anomalies' in row:row['anomalies']=[a for a in row['anomalies'] if a.get('object_id',a.get('objectId')) not in exited]
            archived=list((evidence.get('boundaryExit') or {}).get('findings',[]))
            archived.extend(f for f in evidence.get('findings',[]) if f.get('objectId') in exited)
            evidence['findings']=[f for f in evidence.get('findings',[]) if f.get('objectId') not in exited]
            evidence['boundaryExit']=dict(policy='boundary-exit-v1',exitedObjectIds=sorted(exited),findings=archived)
            if isinstance(evidence.get('jointCheck'),dict):
                evidence['jointCheck']['uncertainObjectIds']=[oid for oid in evidence['jointCheck'].get('uncertainObjectIds',[]) if oid not in exited]
            # The original findings remain archived, not active warnings for an
            # object that is no longer in the image. No tombstone/human approval.
    return result
