import copy
import json
import pytest
from app import annotation_review as r
from .test_review_completion import source, INFO
from .test_review_workflow import task


def row(fi, findings, objects=None):
    return dict(source_frame_index=fi,objects=objects or [],reviewEvidence=dict(findings=findings))


def f(kind, oid=1, bbox=None):
    return dict(kind=kind,objectId=oid,bbox=bbox or [10,10,20,20],message='不应在页面显示的长句原文')


def test_identity_signals_remain_evidence_only_even_when_geometry_differs():
    rows=[row(0,[f(k) for k in ['endpoint_disagreement','endpoint_identity_uncertain','joint_uncertain','subject_identity','backward_disagreement']])]
    before=copy.deepcopy(rows)
    assert r.doubt_items(rows)==[]
    assert rows==before


def test_manual_object_risks_are_exempt_but_reverse_evidence_is_not():
    reverse=dict(kind='endpoint_identity_uncertain',objectId=None,tailObjectId=9,bbox=[60,60,70,70])
    rows=[row(0,[f('detector_mismatch',1),f('association_uncertain',2),reverse],
              [dict(object_id=1,source='manual',bbox=[10,10,20,20]),dict(object_id=2,source='ai',bbox=[30,30,40,40])]),
          row(1,[reverse],[dict(object_id=1,source='manual',bbox=[10,10,20,20])])]
    items=[i for i in r.doubt_items(rows) if i.get('source')!='track_ended']
    assert all(1 not in i['objectIds'] for i in items)
    assert [i['category'] for i in items]==['missing','association']


def test_already_labelled_candidate_does_not_create_an_extra_location():
    rows=[row(0,[f('unlabelled_candidate',None,[9,9,21,21])],[dict(object_id=1,source='manual',bbox=[10,10,20,20])])]
    assert r.doubt_items(rows)==[]


def test_reason_sorting_object_limit_short_labels_and_frame_specific_observations():
    findings=[f('detector_mismatch',oid) for oid in range(1,18)]
    findings+=[f('association_uncertain',30),f('unlabelled_candidate',None,[60,60,70,70])]
    reverse=dict(kind='endpoint_identity_uncertain',objectId=None,tailObjectId=9,bbox=[60,60,70,70])
    rows=[row(0,findings+[reverse]),row(1,[f('head_boundary',40),reverse])]
    items=r.doubt_items(rows)
    assert [i['category'] for i in items[:3]]==['missing','association','position']
    assert all(len(i['objectIds'])<=3 and len(i['labels'])==1 and len(i['message'])<=12 for i in items)
    assert sorted(oid for i in items for oid in i['objectIds'])==[*range(1,18),30,40]
    assert all(len(o['objectIds'])<=3 and len(o['candidates'])<=3 for i in items for o in i['observations'])


def test_detector_single_observations_do_not_flood_the_checklist():
    rows=[row(0,[f('unlabelled_candidate',None,[x,10,x+5,15]) for x in range(10,90,10)])]
    assert r.doubt_items(rows)==[]


def test_dense_actionable_objects_stay_in_small_groups_across_frames():
    rows=[row(fi,[f('detector_mismatch',oid) for oid in range(1,28)]) for fi in range(25)]
    items=r.doubt_items(rows)
    assert len(items)==45  # 9 groups of three objects, over five short spans.
    represented={(o['frameIndex'],oid) for i in items for o in i['observations'] for oid in o['objectIds']}
    assert represented=={(fi,oid) for fi in range(25) for oid in range(1,28)}
    assert all(len(i['objectIds'])<=3 and i['endFrame']-i['startFrame']<5 for i in items)


def test_workspace_manual_provenance_is_frame_local_and_geometry_unchanged(source):
    rows=[row(0,[f('head_boundary',1)], [dict(object_id=1,bbox=[2,3,4,5])]),
          row(1,[f('head_boundary',1)], [dict(object_id=1,bbox=[20,20,30,30])])]
    (source/'tracker_results.json').write_text('\n'.join(json.dumps(x) for x in rows))
    before=(source/'tracker_results.json').read_bytes()
    frames,enriched=r.load(source,INFO)
    assert frames[0][0]['bbox']==[64,48,128,96]  # Actual human override, not tracker geometry.
    items=r.doubt_items(enriched)
    assert len(items)==1 and items[0]['frame']==1 and items[0]['objectIds']==[1]
    assert (source/'tracker_results.json').read_bytes()==before


@pytest.mark.parametrize('converted',[False,True])
def test_terminal_manual_aliases_exempt_the_effective_object(source,converted):
    state=json.loads((source/'workspace_state.json').read_text())
    manual=dict(source='manual',objectId=1 if converted else 91,frameIndex=2,name='sperm 1',bbox=dict(x=10,y=10,width=10,height=10))
    if converted:manual.update(endpointOriginalObjectId=91,endpointAnchorHash='anchor')
    state['manualAnnotations'].append(manual)
    (source/'workspace_state.json').write_text(json.dumps(state))
    rows=[row(2,[f('head_boundary',1)],[dict(object_id=1,bbox=[64,48,128,96])])]
    rows[0]['reviewEvidence']['endpointCheck']=dict(state='checked',matches={'91':1},anchorHash='anchor',terminalAnnotations=[dict(object_id=91,bbox=[64,48,128,96])])
    (source/'tracker_results.json').write_text(json.dumps(rows[0]))
    frames,enriched=r.load(source,INFO)
    assert frames[2][0]['objectId']==1
    assert enriched[0]['_reviewManualIds']==[1] and r.doubt_items(enriched)==[]


def test_check_failure_is_not_mistaken_for_a_suppressed_identity_signal():
    items=r.doubt_items([row(0,[f('check_failed',None)])])
    assert len(items)==1 and items[0]['message']=='自动检查失败' and items[0]['category']=='check'
