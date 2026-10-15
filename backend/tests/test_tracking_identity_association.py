"""Identity matching must keep a dimmer own subject through normal crossings."""
import cv2
import numpy as np

from app.services.tracking_evidence import MaskRegion, TrackingEvidenceProbe


def scene(*, connected=False):
    image = np.full((160,240,3),145,np.uint8)
    cv2.circle(image,(76,70),7,(65,)*3,-1)
    cv2.circle(image,(98,70),8,(30,)*3,-1)
    if connected:
        cv2.line(image,(81,70),(92,70),(70,)*3,3)
    cv2.line(image,(77,77),(42,114),(182,)*3,2)
    cv2.line(image,(99,77),(135,122),(182,)*3,2)
    return image


def masks():
    a=np.zeros((160,240),np.uint8); b=a.copy()
    cv2.circle(a,(76,70),9,1,-1);cv2.circle(a,(98,70),10,1,-1)
    cv2.line(a,(76,70),(42,114),1,6)
    cv2.circle(b,(98,70),10,1,-1);cv2.line(b,(98,70),(135,122),1,6)
    return {20:MaskRegion(a),26:MaskRegion(b)}


def probe():
    return TrackingEvidenceProbe(fps=30,frame_width=240,frame_height=160)


def test_joint_reference_does_not_choose_stronger_neighbor_already_owned_by_other_box():
    boxes={20:[38,59,109,120],26:[88,59,143,128]}
    p=probe();reference=p.prime(0,scene(),boxes)
    assert reference[20]['head']['status']=='matched'
    assert reference[20]['head']['point'][0]<85
    assert reference[26]['head']['point'][0]>90
    result=p.observe(1,scene(),boxes,masks())
    assert result[20]['head']['point'][0]<85
    assert result[20]['status']!='risk', 'own subject remains present; mixed mask alone is not lost identity'
    assert result[26]['status']!='risk'


def test_connected_optical_blob_never_replaces_reliable_own_anchor_with_midpoint():
    p=probe();box=[38,59,109,120]
    initial=scene();cv2.circle(initial,(98,70),9,(145,)*3,-1)
    p.prime(0,initial,{20:box})
    old=list(p._references[20].last_point)
    result=p.observe(1,scene(connected=True),{20:box},{20:masks()[20]})[20]
    assert result['head']['point'] is None or result['head']['point'][0]<85
    if result['head']['status']=='ambiguous':
        assert p._references[20].last_point==old
    separate=p.observe(2,scene(),{20:box},{20:masks()[20]})[20]
    assert separate['head']['point'][0]<85, 'splitting a connected blob must recover the original subject'


def test_identity_reference_export_never_exports_an_ambiguous_shared_point():
    p=probe();boxes={20:[38,59,109,120],26:[88,59,143,128]}
    p.prime(0,scene(),boxes)
    p.observe(1,scene(),boxes,masks())
    refs=p.export_references(1,boxes)
    assert refs[20]['point'][0]<85 and refs[26]['point'][0]>90
    assert refs[20]['originFrameIndex']==0
    assert refs[20]['frameIndex']==1


def test_two_overlapping_two_core_boxes_do_not_assign_identity_by_id_iteration_order():
    boxes={20:[60,55,112,126],26:[62,55,114,128]}
    for ordered in (boxes,dict(reversed(list(boxes.items())))):
        p=probe();refs=p.prime(0,scene(),ordered)
        assert all(ref['head']['status']=='ambiguous' for ref in refs.values())
        assert not p._references


def test_shared_single_core_duplicate_prompts_still_produce_merge_risk():
    image=scene();cv2.circle(image,(98,70),9,(145,)*3,-1)
    box=[38,59,87,120];region=masks()[20]
    p=probe();p.prime(0,image,{20:box,26:box})
    reports=p.observe(1,image,{20:box,26:box},{20:region,26:region})
    assert all(r['status']=='risk' and r['coverage']['status']=='merge' for r in reports.values())
    assert all('identityReference' not in r for r in reports.values())


def test_own_subject_missing_cannot_be_exempted_by_a_retained_neighbor():
    p=probe();boxes={20:[38,59,109,120],26:[88,59,143,128]};p.prime(0,scene(),boxes)
    current=scene();cv2.circle(current,(76,70),9,(145,)*3,-1)
    reports=p.observe(1,current,boxes,masks())
    assert reports[20]['status']=='risk'
    assert 'identityReference' not in reports[20]
