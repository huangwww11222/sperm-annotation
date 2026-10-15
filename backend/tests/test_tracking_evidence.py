"""Native image evidence: rotation may be supported; clear loss never is."""
import json
import math

import cv2
import numpy as np
import pytest

from app.services.tracking_evidence import MaskRegion, TrackingEvidenceProbe


def _scene(head=(60, 100), angle=0, *, extra=None, bright=False, tail=True):
    background=80 if bright else 145
    image=np.full((240,320,3),background,np.uint8)
    mask=np.zeros((240,320),np.uint8)
    hx,hy=head
    cv2.circle(image,head,11,(background+28,)*3,-1)
    cv2.circle(image,head,8,((210 if bright else 38),)*3,-1)
    cv2.circle(mask,head,11,1,-1)
    if tail:
        end=(round(hx+75*math.cos(angle)),round(hy+75*math.sin(angle)))
        color=45 if bright else 180
        cv2.line(image,(round(hx+8*math.cos(angle)),round(hy+8*math.sin(angle))),end,(color,)*3,3)
        cv2.line(mask,head,end,1,8)
    if extra is not None:
        pos,color,radius=extra
        cv2.circle(image,pos,radius,(color,)*3,-1)
    ys,xs=np.nonzero(mask)
    box=[float(xs.min()),float(ys.min()),float(xs.max()+1),float(ys.max()+1)]
    return image,mask,box


def _region(mask):
    ys,xs=np.nonzero(mask)
    if not len(xs):return MaskRegion(mask,(0,0))
    x1,y1,x2,y2=int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)
    return MaskRegion(mask[y1:y2,x1:x2],(x1,y1))


def _probe():
    return TrackingEvidenceProbe(fps=30,frame_width=320,frame_height=240)


def test_requires_explicit_trusted_reference_and_never_learns_from_ai_seed():
    image,mask,box=_scene()
    probe=_probe()
    first=probe.observe(0,image,{1:box},{1:_region(mask)})[1]
    second=probe.observe(1,image,{1:box},{1:_region(mask)})[1]
    assert first['status']==second['status']=='unknown'
    assert 'reference_missing' in first['reasons']
    assert first['maskArea']==second['maskArea']==int(np.count_nonzero(mask))


@pytest.mark.parametrize('angle',[math.pi/4,math.pi/2,math.pi*3/4])
def test_head_and_visible_tail_rotation_supports_only_weak_geometry(angle):
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{7:box})
    rotated,newmask,newbox=_scene(head=(63,101),angle=angle)
    report=probe.observe(1,rotated,{7:newbox},{7:_region(newmask)})[7]
    assert report['status']=='supported'
    assert report['head']['status']=='matched'
    assert report['coverage']['status']=='no_clear_loss'
    assert report['objectId']==7 and report['frameIndex']==1
    assert report['bbox']==newbox
    assert report['maskArea']==int(np.count_nonzero(newmask))
    assert report['head']['referenceFrameIndex']==0


def test_head_stable_but_tail_cropped_is_risk():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    truncated=np.zeros_like(mask);cv2.circle(truncated,(60,100),11,1,-1)
    report=probe.observe(1,image,{1:[49,89,72,112]},{1:_region(truncated)})[1]
    assert report['status']=='risk'
    assert report['coverage']['status']=='tail_loss'
    assert report['head']['status']=='matched'


def test_full_bbox_with_head_only_mask_reports_segmentation_quality_warning():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    truncated=np.zeros_like(mask);cv2.circle(truncated,(60,100),11,1,-1)
    report=probe.observe(1,image,{1:box},{1:_region(truncated)})[1]
    assert report['status']=='warning'
    assert report['coverage']['status']=='local_mask_coverage'


def test_near_head_phase_fringe_is_not_a_major_tail_loss():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    halo=image.copy()
    cv2.line(halo,(41,90),(41,110),(180,)*3,3)
    cv2.line(halo,(41,90),(50,94),(180,)*3,3)
    report=probe.observe(1,halo,{1:box},{1:_region(mask)})[1]
    assert report['status']=='supported'
    assert report['coverage']['status']=='no_clear_loss'


def test_thick_background_lobe_is_not_assumed_to_be_another_head():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    enlarged=mask.copy();cv2.circle(enlarged,(118,101),22,1,-1)
    ys,xs=np.nonzero(enlarged);newbox=[float(xs.min()),float(ys.min()),float(xs.max()+1),float(ys.max()+1)]
    report=probe.observe(1,image,{1:newbox},{1:_region(enlarged)})[1]
    assert report['status']=='supported'
    assert report['coverage']['status']=='no_clear_loss'
    assert report['coverage']['metrics']['thicknessToHeadDistance']>20


def test_mask_focus_on_other_core_warns_when_own_subject_remains_in_bbox():
    reference,mask,box=_scene()
    probe=_probe();probe.prime(0,reference,{16:box})
    image,_,_=_scene(extra=((124,103),112,5))
    focused=mask.copy();cv2.circle(focused,(124,103),17,1,-1)
    ys,xs=np.nonzero(focused);newbox=[float(xs.min()),float(ys.min()),float(xs.max()+1),float(ys.max()+1)]
    report=probe.observe(1,image,{16:newbox},{16:_region(focused)})[16]
    assert report['status']=='warning'
    assert report['head']['status']=='matched'
    assert report['coverage']['status']=='mask_contamination'
    assert 'own_subject_retained_with_foreign_mask_focus' in report['reasons']


def test_baseline_gray_core_is_ambiguous_context_not_new_risk_every_frame():
    image,mask,box=_scene(extra=((124,103),112,5))
    probe=_probe();probe.prime(0,image,{16:box})
    focused=mask.copy();cv2.circle(focused,(124,103),17,1,-1)
    ys,xs=np.nonzero(focused);newbox=[float(xs.min()),float(ys.min()),float(xs.max()+1),float(ys.max()+1)]
    for frame in (1,2,3):
        report=probe.observe(frame,image,{16:newbox},{16:_region(focused)})[16]
        assert report['status']=='uncertain'
        assert report['coverage']['status']=='subject_present_uncertain'
        assert report['coverage']['metrics']['baselineContextMatched'] is True
        assert 'accepted_core_context_ambiguous' in report['reasons']


def test_existing_gray_context_never_exempts_clear_tail_loss():
    image,mask,box=_scene(extra=((124,103),112,5))
    probe=_probe();probe.prime(0,image,{16:box})
    focused=np.zeros_like(mask);cv2.circle(focused,(60,100),11,1,-1)
    report=probe.observe(1,image,{16:[49,89,72,112]},{16:_region(focused)})[16]
    assert report['status']=='risk'
    assert report['coverage']['status']=='tail_loss'


def test_two_ids_cannot_be_supported_from_the_same_optical_core():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box,2:box})
    reports=probe.observe(1,image,{1:box,2:box},{1:_region(mask),2:_region(mask)})
    assert all(r['status']=='risk' and r['coverage']['status']=='merge' for r in reports.values())
    assert all('shared_optical_core' in r['reasons'] for r in reports.values())


def test_overlapping_rectangles_need_real_mask_iou_and_distinct_cores_for_independence():
    first,mask1,box1=_scene(head=(60,80),angle=math.pi/4)
    second,mask2,box2=_scene(head=(220,120),angle=math.pi*3/4)
    reference=np.minimum(first,second)
    # Preserve bright line pixels as well as both dark cores.
    reference[mask2>0]=second[mask2>0]
    probe=_probe();probe.prime(0,reference,{1:box1,2:box2})
    first,mask1,box1=_scene(head=(90,80),angle=math.pi/4)
    second,mask2,box2=_scene(head=(200,120),angle=math.pi*3/4)
    current=first.copy();current[mask2>0]=second[mask2>0]
    reports=probe.observe(1,current,{1:box1,2:box2},{1:_region(mask1),2:_region(mask2)})
    assert reports[1]['status']==reports[2]['status']=='supported'
    assert reports[1]['coverage']['metrics']['maskOverlap']['2']<.2
    assert 2 in reports[1]['coverage']['metrics']['independentObjectIds']


def test_two_strong_cores_inside_mask_warn_if_reliable_own_subject_is_retained():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    merged=image.copy();cv2.circle(merged,(116,100),8,(38,)*3,-1)
    multimask=mask.copy();cv2.circle(multimask,(116,100),11,1,-1)
    report=probe.observe(1,merged,{1:box},{1:_region(multimask)})[1]
    assert report['status']=='warning'
    assert report['coverage']['status']=='mask_contamination'


def test_ambiguous_first_reference_remains_unknown():
    image,mask,box=_scene(extra=((116,100),38,8))
    probe=_probe();ref=probe.prime(0,image,{1:box})[1]
    report=probe.observe(1,image,{1:box},{1:_region(mask)})[1]
    assert ref['head']['status']=='ambiguous'
    assert report['status']=='unknown'


def test_background_box_cannot_be_supported_by_visible_head_elsewhere():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    shift=np.zeros_like(mask);cv2.rectangle(shift,(180,85),(240,125),1,-1)
    report=probe.observe(1,image,{1:[180,85,241,126]},{1:_region(shift)})[1]
    assert report['status']=='risk'
    assert report['head']['status']=='lost'


@pytest.mark.parametrize('failure',['image','mask','mask_shape','image_shape','mask_empty'])
def test_unavailable_or_invalid_input_never_produces_supported(failure):
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    frame=None if failure=='image' else image[:200] if failure=='image_shape' else image
    regions=None if failure=='mask' else {1:MaskRegion(mask[:,:,None])} if failure=='mask_shape' else {1:MaskRegion(np.zeros_like(mask))} if failure=='mask_empty' else {1:_region(mask)}
    report=probe.observe(1,frame,{1:box},regions)[1]
    assert report['status']!='supported'
    if failure in ('image','image_shape'):
        assert report['maskArea']==int(np.count_nonzero(mask))
    json.dumps(report,allow_nan=False)


def test_low_contrast_reference_has_no_permission_to_relax_geometry():
    image=np.full((240,320,3),145,np.uint8)
    probe=_probe();probe.prime(0,image,{1:[40,80,140,130]})
    mask=np.ones((50,100),np.uint8)
    report=probe.observe(1,image,{1:[40,80,140,130]},{1:MaskRegion(mask,(40,80))})[1]
    assert report['status']=='unknown'


def test_failed_new_reference_cannot_keep_old_geometry_permission():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    blank=np.full_like(image,145)
    probe.prime(4,blank,{1:box})
    report=probe.observe(5,image,{1:box},{1:_region(mask)})[1]
    assert report['status']=='unknown'
    assert 'reference_missing' in report['reasons']


def test_reference_never_moves_to_ai_output_and_object_ids_are_independent():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{41:box})
    image2,mask2,box2=_scene(head=(63,102),angle=math.pi/4)
    report=probe.observe(1,image2,{99:box2,41:box2},{41:_region(mask2),99:_region(mask2)})
    assert report[41]['status']=='supported'
    assert report[99]['status']=='unknown'
    assert report[41]['head']['referenceFrameIndex']==0
    assert report[41]['head']['referencePoint'][0]<61


def test_old_frames_cannot_use_future_optical_reference():
    image,mask,box=_scene()
    probe=_probe();probe.prime(10,image,{1:box})
    report=probe.observe(9,image,{1:box},{1:_region(mask)})[1]
    assert report['status']=='unknown'
    assert 'reference_in_future' in report['reasons']


def test_bright_optical_core_is_supported_without_assuming_dark_microscopy():
    image,mask,box=_scene(bright=True)
    probe=_probe();probe.prime(0,image,{1:box})
    report=probe.observe(1,image,{1:box},{1:_region(mask)})[1]
    assert report['status']=='supported'
    assert report['head']['metrics']['polarity']=='bright'


def test_invalid_bbox_is_json_safe_and_never_supported():
    image,mask,box=_scene()
    probe=_probe();probe.prime(0,image,{1:box})
    report=probe.observe(1,image,{1:[float('nan'),0,30,20]},{1:_region(mask)})[1]
    assert report['status']=='unknown'
    json.dumps(report,allow_nan=False)


@pytest.mark.parametrize('neighbor,tail_cropped,expected_pause', [(False,True,True),(True,True,True),(True,False,False)])
def test_neighbor_mask_contamination_cannot_override_clear_own_structure_outside_bbox(neighbor,tail_cropped,expected_pause):
    """Known synthetic tail geometry, not a claim about clinical sperm truth.

    In the cropped pair, the BBox and Mask are identical. Adding only a distinct
    neighboring optical core must not suppress the same major outside loss.
    The complete-tail case keeps harmless contamination as a warning.
    """
    from app.services.anomaly_detector import AnomalyDetector, ManualBaseline
    reference,fullmask,fullbox=_scene()
    image=reference.copy()
    mask=np.zeros_like(fullmask) if tail_cropped else fullmask.copy()
    if tail_cropped:
        cv2.circle(mask,(60,100),11,1,-1)
    cv2.circle(mask,(83,80),11,1,-1)
    if neighbor:
        cv2.circle(image,(83,80),8,(38,)*3,-1)
    ys,xs=np.nonzero(mask);box=[float(xs.min()),float(ys.min()),float(xs.max()+1),float(ys.max()+1)]
    probe=_probe();probe.prime(0,reference,{7:fullbox})
    detector=AnomalyDetector(frame_width=320,frame_height=240,fps=30,all_object_ids=[7],manual_baselines={7:ManualBaseline(fullbox,0)})
    detector.initialize_seed(0,{7:fullbox})
    decisions=[]
    for frame in(1,2,3):
        evidence=probe.observe(frame,image,{7:box},{7:_region(mask)})[7]
        report=detector.push(frame,{7:box},mask_areas={7:int(mask.sum())},identity_evidence={7:evidence})
        decisions.append(report.should_pause)
    assert any(decisions)==expected_pause, 'clear own major loss must remain actionable even when a neighbor is also segmented'
    if tail_cropped:
        assert evidence['coverage']['status']=='tail_loss'
        assert all(item['branch']['outsidePixels']>150 for item in evidence['coverage']['metrics']['tailScales'])
    else:
        assert evidence['status']=='warning' and evidence['coverage']['status']=='mask_contamination'


def test_tail_continuation_past_a_neighbor_head_is_not_attributed_to_our_object():
    reference,mask,box=_scene()
    image=reference.copy()
    # Our trusted full tail is unchanged. An added neighbor and bright
    # continuation past its optical core create an apparent connected ridge
    # outside our rectangle. It must stop supplying own-object loss evidence.
    cv2.line(image,(68,100),(150,100),(180,)*3,3)
    cv2.circle(image,(115,100),8,(38,)*3,-1)
    mixed=mask.copy();cv2.circle(mixed,(115,100),11,1,-1)
    probe=_probe();probe.prime(0,reference,{7:box})
    for frame in(1,2,3):
        report=probe.observe(frame,image,{7:box},{7:_region(mixed)})[7]
        assert report['status']=='warning' and report['coverage']['status']=='mask_contamination'
        metrics=report['coverage']['metrics']
        assert metrics['tailAssociationAmbiguous'] is True
        assert all(scale['branch']['outsidePixels']==0 for scale in metrics['tailScales'])
