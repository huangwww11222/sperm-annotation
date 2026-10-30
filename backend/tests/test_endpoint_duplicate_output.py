import copy
import pytest
from app import endpoint_tracking as endpoint


def saved_rows():
    # Human head is tight; SAM box contains padding, so IoU is only 0.25.
    rows=[]
    for i in range(6):
        head=[20+i,20,30+i,30]
        report=dict(state='checked',policy='joint-endpoints-v2',anchorFrame=5,anchorHash='a'*64,matches={},unmatchedTailIds=[91],checkedFrames=6)
        if i==5:report['terminalAnnotations']=[dict(object_id=91,bbox=head,source='manual')]
        rows.append(dict(source_frame_index=i,objects=[dict(object_id=1,bbox=[15+i,15,35+i,35])],reviewEvidence=dict(endpointCheck=report,findings=[dict(kind='endpoint_identity_uncertain',objectId=None,tailObjectId=91,bbox=head)])))
    return rows


def test_old_saved_output_reconciles_same_head_without_model_or_mutating_input():
    original=saved_rows();before=copy.deepcopy(original)
    result=endpoint.project_endpoints(original)
    assert original==before
    assert result[-1]['reviewEvidence']['endpointCheck']['matches']=={'91':1}
    assert len(result[-1]['objects'])==1
    assert result[-1]['objects'][0]['object_id']==1
    assert result[-1]['objects'][0]['bbox']==[25,20,35,30]


def test_natural_close_heads_remain_two_manual_targets_when_identity_is_ambiguous():
    rows=saved_rows()
    for row in rows:
        row['objects'].append(dict(object_id=2,bbox=list(row['objects'][0]['bbox'])))
    tail=rows[-1]['reviewEvidence']['endpointCheck']['terminalAnnotations']
    tail.append(dict(object_id=92,bbox=[26,20,36,30],source='manual'))
    result=endpoint.project_endpoints(rows)
    assert result[-1]['reviewEvidence']['endpointCheck']['matches']=={}
    assert len(result[-1]['objects'])==2
    assert len({o['object_id'] for o in result[-1]['objects']})==2


def test_one_frame_overlap_does_not_establish_identity():
    rows=saved_rows()
    for row in rows[:-1]:row['reviewEvidence']['findings'][0]['bbox']=[70,70,80,80]
    result=endpoint.project_endpoints(rows)
    assert result[-1]['reviewEvidence']['endpointCheck']['matches']=={}
    # Complete human endpoint remains authoritative even when identity needs review.
    assert [o['object_id'] for o in result[-1]['objects']]==[91]


def test_unmatched_tail_id_cannot_overwrite_another_matched_head():
    rows=saved_rows();tail=rows[-1]['reviewEvidence']['endpointCheck']['terminalAnnotations']
    tail.append(dict(object_id=1,bbox=[70,70,80,80],source='manual'))
    result=endpoint.project_endpoints(rows)
    assert len({o['object_id'] for o in result[-1]['objects']})==2
    assert endpoint.terminal_aliases(result[-1])[91]==1
    assert endpoint.terminal_aliases(result[-1])[1]!=1


def test_failed_or_legacy_check_never_silently_discards_forward_geometry():
    for policy,state in [('human-endpoints-v1','checked'),('joint-endpoints-v2','failed')]:
        rows=saved_rows();rows[-1]['reviewEvidence']['endpointCheck'].update(policy=policy,state=state)
        assert endpoint.project_endpoints(rows)==rows


def test_explicit_empty_human_tail_is_empty_in_combined_output():
    rows=saved_rows();rows[-1]['reviewEvidence']['endpointCheck'].update(state='insufficient',terminalAnnotations=[],matches={})
    assert endpoint.project_endpoints(rows)[-1]['objects']==[]


def test_shared_projection_keeps_download_and_export_identity_consistent(tmp_path, monkeypatch):
    import json
    from app import main, annotation_completion
    rows=saved_rows()
    (tmp_path/'tracker_results.json').write_text('\n'.join(json.dumps(r) for r in rows))
    workspace=dict(updatedBy=1,manualAnnotations=[dict(id='tail',objectId=91,frameIndex=5,name='sperm 91',source='manual',bbox=dict(x=25,y=20,width=10,height=10))])
    (tmp_path/'workspace_state.json').write_text(json.dumps(workspace))
    _,frames=annotation_completion.source(tmp_path,1,dict(width=100,height=100,fps=30.,frameCount=6))
    assert list(frames[5])==[1]
    assert frames[5][1]['bbox']==[25,20,35,30]
    # Keep raw source unchanged; normalize all public reads through the same function.
    monkeypatch.setattr(main,'media_dir',lambda _:tmp_path)
    monkeypatch.setattr(main,'find_video',lambda _:tmp_path/'source.avi')
    monkeypatch.setattr(main.annotation_state,'read_rows',lambda *args,**kwargs:copy.deepcopy(rows))
    result=main.tracking_result('test',frameIndex=None,required=False,user={'uid':1})
    assert result['terminalIdentity']['matches']=={'91':1}
    raw=main.tracking_result_file('test',user={'uid':1})
    data=[json.loads(line) for line in raw.body.decode().splitlines()]
    assert [(o['object_id'],o['bbox']) for o in data[-1]['objects']]==[(1,[25,20,35,30])]
    workspace['deletedFrameObjects']=[dict(objectId=91,frameIndex=5)]
    (tmp_path/'workspace_state.json').write_text(json.dumps(workspace))
    assert annotation_completion.source(tmp_path,1,dict(width=100,height=100,fps=30.,frameCount=6))[1][5]=={}
    raw=main.tracking_result_file('test',user={'uid':1})
    assert json.loads(raw.body.decode().splitlines()[-1])['objects']==[]
