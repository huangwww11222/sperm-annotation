"""Reliable optical identity survives human shape approval and scoped invalidation."""
import copy
import json

import cv2
import numpy as np

from app.services.anomaly_detector import ManualBaseline
from app.services.tracking_evidence import MaskRegion, TrackingEvidenceProbe
from app.services.tracking_image_evidence import NativeImageEvidence, derive_identity_references
from app.tracker import _load_identity_references, _load_latest_manual_baselines
from app.video_frames import source_signature


def video(path):
    writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),30,(160,120));assert writer.isOpened()
    for frame in range(4):
        image=np.full((120,160,3),145,np.uint8)
        cv2.circle(image,(45+frame,55),8,(38,)*3,-1)
        writer.write(image)
    writer.release();return path


def test_reliable_reference_origin_is_independent_of_latest_geometry_checkpoint(tmp_path):
    path=video(tmp_path/'input.avi');manual=[32.,42.,118.,82.]
    with NativeImageEvidence(path,fps=30,width=160,height=120,manual_baselines={7:ManualBaseline(manual,0)},identity_baselines={7:ManualBaseline(manual,0)},seed_frame=0) as native:
        mask=np.zeros((120,160),np.uint8);cv2.circle(mask,(46,55),11,1,-1);cv2.line(mask,(46,55),(110,70),1,8)
        evidence=native.observe(1,{7:manual},{7:MaskRegion(mask)})[7]
        reference=evidence['identityReference']
    derived=derive_identity_references(path,fps=30,width=160,height=120,frame_index=1,boxes={7:manual},prior_references={7:reference})
    assert derived[7]['originFrameIndex']==0 and derived[7]['frameIndex']==1
    checkpoint=ManualBaseline(manual,1,source='confirmed-normal')
    with NativeImageEvidence(path,fps=30,width=160,height=120,manual_baselines={7:checkpoint},identity_baselines={7:ManualBaseline(manual,0)},identity_references=derived,seed_frame=1) as native:
        evidence=native.observe(2,{7:manual},{7:MaskRegion(mask)})[7]
    assert evidence['head']['referenceFrameIndex']==0
    assert evidence['geometryReferenceFrameIndex']==1
    assert evidence['geometryReferenceBBox']==manual
    assert evidence['identityReference']['originFrameIndex']==0


def reference(frame=5):
    return {'version':2,'source':'native-frame-mask-v2','objectId':7,'status':'reliable',
            'frameIndex':frame,'originFrameIndex':0,'generationId':'current','objectEpoch':0}


def test_identity_loader_does_not_misread_confirmed_normal_geometry_as_new_manual():
    workspace={'generationId':'current','trackingFeedbackEvents':[{'objectId':7,'frameIndex':5,'decision':'normal','identityReference':reference()}]}
    refs=_load_identity_references(workspace,5,{7:ManualBaseline([1,1,30,30],5,source='confirmed-normal')})
    assert refs[7]['originFrameIndex']==0
    assert _load_identity_references(workspace,7,{7:ManualBaseline([1,1,30,30],6)})=={}


def test_identity_loader_retains_current_validation_even_if_appearance_origin_precedes_manual():
    workspace={'generationId':'current','trackingFeedbackEvents':[{'objectId':7,'frameIndex':5,'decision':'normal','identityReference':reference()}]}
    assert 7 in _load_identity_references(workspace,5,{7:ManualBaseline([1,1,30,30],4)})


def test_identity_loader_rejects_future_reset_generation_and_deleted_undo_epochs():
    workspace={'generationId':'current','trackingFeedbackEvents':[{'objectId':7,'frameIndex':5,'decision':'normal','identityReference':reference()}]}
    assert _load_identity_references(workspace,4,{})=={}
    changed=copy.deepcopy(workspace);changed['generationId']='replacement'
    assert _load_identity_references(changed,5,{})=={}
    changed=copy.deepcopy(workspace);changed['_overlapReferenceEpochs']={'7':1}
    assert _load_identity_references(changed,5,{})=={}
    changed=copy.deepcopy(workspace);changed['trackingFeedbackEvents'].append({'objectId':7,'frameIndex':5,'decision':'reset'})
    assert _load_identity_references(changed,5,{})=={}


def test_object_scoped_checkpoint_restores_latest_geometry_but_not_as_manual_identity(tmp_path):
    manual=[10.,10.,40.,40.];accepted=[9.,7.,47.,51.]
    seed=tmp_path/'seed_frame_000005.json';seed.write_text(json.dumps({'frame':{'frameIndex':0},'annotations':[{'source':'manual','object_id':7,'bbox':manual}]}))
    state={'trackingFeedbackEvents':[{'objectId':7,'frameIndex':5,'scope':'object','decision':'normal','geometryReference':{'objectId':7,'frameIndex':5,'source':'confirmed-normal','checkpoint':True,'bbox':accepted}}]}
    (tmp_path/'workspace_state.json').write_text(json.dumps(state))
    shape=_load_latest_manual_baselines(seed,5);identity=_load_latest_manual_baselines(seed,5,include_checkpoints=False)
    assert shape[7].source=='confirmed-normal' and shape[7].bbox==accepted
    assert identity[7].source=='manual' and identity[7].frame_index==0 and identity[7].bbox==manual


def test_discarded_future_identity_does_not_revive_after_new_branch_reaches_same_frame():
    event={'id':'old-future-identity','objectId':7,'frameIndex':5,'decision':'normal','identityReference':reference(),
           'geometryReference':{'objectId':7,'frameIndex':5,'source':'confirmed-normal','checkpoint':True,'bbox':[10,10,40,40]}}
    workspace={'generationId':'current','trackingFeedbackEvents':[event],
               '_retiredTrackingRelationshipEventIds':['old-future-identity']}
    assert _load_identity_references(workspace,5,{})=={}
    from app.tracker import _load_confirmed_geometry_references
    geometry=_load_confirmed_geometry_references(workspace,5,{})
    assert geometry[7][-1].bbox==[10,10,40,40], 'relationship retirement must not erase explicit human geometry'


def test_new_same_frame_identity_can_be_saved_after_old_future_relationship_is_retired():
    old={'id':'old-future-identity','objectId':7,'frameIndex':5,'decision':'normal','identityReference':reference()}
    new={**old,'id':'new-branch-identity','identityReference':{**reference(),'originFrameIndex':3}}
    workspace={'generationId':'current','trackingFeedbackEvents':[old,new],
               '_retiredTrackingRelationshipEventIds':['old-future-identity']}
    refs=_load_identity_references(workspace,5,{})
    assert refs[7]['originFrameIndex']==3
