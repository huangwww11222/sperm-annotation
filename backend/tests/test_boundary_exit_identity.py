"""Boundary exits must not be converted into a neighbour's identity."""
import copy
import pytest
from app import endpoint_tracking as endpoint


def episode():
    own = [[80,30,100,60],[80,12,100,42],[80,0,100,24],[80,0,100,12],
           [80,0,105,55],[80,0,105,60],[80,0,105,60]]
    rows=[]
    for fi,b in enumerate(own):
        findings=[] if fi<4 else [dict(kind='subject_identity',objectId=1,bbox=b,reasons=['mask_head_missing'])]
        rows.append(dict(source_frame_index=fi,objects=[dict(object_id=1,bbox=b,source='sam3'),dict(object_id=2,bbox=[80,40,110,70],source='sam3')],reviewEvidence=dict(findings=findings)))
    return rows


def test_composite_cannot_keep_exit_box_alive_on_neighbour():
    from app.joint_tracking import combine_tracks
    rows=episode()
    reverse={fi:{91:[80,40,110,70]} for fi in range(len(rows))}
    combine_tracks(rows,reverse,{'91':2},{fi:[] for fi in range(len(rows))},width=200,height=100)
    assert [o['object_id'] for o in rows[4]['objects']]==[2]


def test_exit_removes_only_transferred_identity_and_keeps_partial_head(caplog):
    from app.track_lifecycle import protect_boundary_exits
    rows=episode();before=copy.deepcopy(rows)
    result=protect_boundary_exits(rows,width=200,height=100)
    assert rows==before
    assert all([o['object_id'] for o in r['objects']]==[1,2] for r in result[:4])
    assert all([o['object_id'] for o in r['objects']]==[2] for r in result[4:])
    assert result[4]['reviewEvidence']['boundaryExit']['exitedObjectIds']==[1]
    assert result[4]['reviewEvidence']['findings']==[]
    assert result[4]['reviewEvidence']['boundaryExit']['findings'][0]['objectId']==1
    assert 'tracking.boundary_exit' in caplog.text
    assert protect_boundary_exits(result,width=200,height=100)==result


def test_interior_occlusion_and_overlap_do_not_become_exits():
    from app.track_lifecycle import protect_boundary_exits
    for mode in ('interior','no_image_risk','turn_back','incomplete_timeline'):
        rows=episode()
        if mode=='interior':
            for r in rows:
                for o in r['objects']:o['bbox']=[o['bbox'][0],o['bbox'][1]+25,o['bbox'][2],o['bbox'][3]+25]
        if mode=='no_image_risk':
            for r in rows:r['reviewEvidence']['findings']=[]
        if mode=='turn_back':rows[3]['objects'][0]['bbox']=[80,4,100,28]
        if mode=='incomplete_timeline':del rows[2]
        assert protect_boundary_exits(rows,width=200,height=140)==rows,mode


@pytest.mark.parametrize('edge',['left','right','bottom'])
def test_exit_protection_works_on_other_image_edges(edge):
    from app.track_lifecycle import protect_boundary_exits
    rows=episode()
    for row in rows:
        for obj in row['objects']:
            x1,y1,x2,y2=obj['bbox']
            if edge=='bottom':obj['bbox']=[x1,100-y2,x2,100-y1]
            elif edge=='left':obj['bbox']=[y1,x1,y2,x2]
            else:obj['bbox']=[100-y2,x1,100-y1,x2]
    result=protect_boundary_exits(rows,width=200 if edge=='bottom' else 100,height=100 if edge=='bottom' else 200)
    assert all([o['object_id'] for o in r['objects']]==[2] for r in result[4:])


def test_manual_reanchor_and_confirmed_tail_identity_prevent_automatic_removal():
    from app.track_lifecycle import protect_boundary_exits
    rows=episode()
    result=protect_boundary_exits(rows,width=200,height=100,manual={5:{1:[80,40,110,70]}})
    assert [o['object_id'] for o in result[4]['objects']]==[2]
    assert all([o['object_id'] for o in r['objects']]==[1,2] for r in result[5:])
    for r in rows:r['reviewEvidence']['endpointCheck']=dict(matches={'91':1})
    assert protect_boundary_exits(rows,width=200,height=100)==rows


def test_first_anchor_disambiguates_later_shared_head_including_old_matches():
    rows=[];reverse={}
    for fi in range(8):
        a=[10,70-fi*8,20,80-fi*8] if fi<4 else [49,19,73,43]
        b=[50,20,62,32]
        rows.append(dict(source_frame_index=fi,objects=[dict(object_id=1,bbox=a),dict(object_id=2,bbox=b)]))
        reverse[fi]={91:[48,18,74,44]}
    result=endpoint.reconcile_matches(rows,reverse,{'91':1})
    assert result=={'91':2}
    assert endpoint.reconcile_matches(rows,reverse,result)==result
    for row in rows:row['objects'][0]['bbox']=[50,20,62,32]
    assert endpoint.reconcile_matches(rows,reverse,{'91':1})=={'91':1} # ambiguity is not a new match


def test_saved_reverse_disagreement_can_correct_existing_mapping_without_mutation():
    rows=episode()
    # Independent reverse track consistently belongs to object 2, not the exiting 1.
    for fi,r in enumerate(rows):
        r['reviewEvidence']['endpointCheck']=dict(state='checked',policy='joint-endpoints-v3',anchorFrame=6,anchorHash='a'*64,matches={'91':1})
        r['reviewEvidence']['findings'].append(dict(kind='endpoint_disagreement',objectId=1,tailObjectId=91,bbox=r['objects'][0]['bbox'],reverseBox=[78,38,112,72]))
    rows[-1]['reviewEvidence']['endpointCheck']['terminalAnnotations']=[dict(object_id=91,bbox=[80,40,110,70],source='manual')]
    before=copy.deepcopy(rows)
    # Existing read projection must also reconsider a previously accepted pair.
    assert endpoint.project_endpoints(rows)[-1]['reviewEvidence']['endpointCheck']['matches']=={'91':2}
    result=endpoint.project_endpoints(rows,width=200,height=100)
    assert rows==before
    assert result[-1]['reviewEvidence']['endpointCheck']['matches']=={'91':2}
    assert all([o['object_id'] for o in r['objects']]==[2] for r in result[4:])
    assert endpoint.project_endpoints(result,width=200,height=100)==result


def test_api_download_and_export_agree_after_prior_tail_reconciliation(tmp_path,monkeypatch):
    import json
    from app import main,annotation_completion
    rows=episode();end=len(rows)-1
    for r in rows:
        r['reviewEvidence']['endpointCheck']=dict(state='checked',policy='joint-endpoints-v3',anchorFrame=end,anchorHash='a'*64,matches={'91':1})
        r['reviewEvidence']['findings'].append(dict(kind='endpoint_disagreement',objectId=1,tailObjectId=91,bbox=r['objects'][0]['bbox'],reverseBox=[78,38,112,72]))
    rows[-1]['reviewEvidence']['endpointCheck']['terminalAnnotations']=[dict(object_id=91,bbox=[80,40,110,70],source='manual')]
    state=dict(updatedBy=1,manualAnnotations=[dict(source='manual',objectId=1,endpointOriginalObjectId=91,endpointAnchorHash='a'*64,frameIndex=end,bbox=dict(x=40,y=40,width=15,height=30))])
    info=dict(width=200,height=100,fps=30.,frameCount=len(rows))
    (tmp_path/'workspace_state.json').write_text(json.dumps(state))
    (tmp_path/'tracker_results.json').write_text('\n'.join(json.dumps(r) for r in rows))
    (tmp_path/'media.json').write_text(json.dumps(info))
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(main,'media_dir',lambda _:tmp_path)
    monkeypatch.setattr(main,'find_video',lambda _:tmp_path/'source.avi')
    result=main.tracking_result('exit',None,False,{'uid':1})
    assert result['terminalIdentity']['matches']=={'91':2}
    assert [o['objectId'] for o in result['frames'][4]['annotations']]==[2]
    downloaded=[json.loads(line) for line in main.tracking_result_file('exit',False,{'uid':1}).body.decode().splitlines()]
    assert [o['object_id'] for o in downloaded[4]['objects']]==[2]
    _,frames=annotation_completion.source(tmp_path,1,info)
    assert list(frames[4])==[2] and list(frames[end])==[2]
    assert frames[end][2]['bbox']==[80,40,110,70]
    assert all(p.read_bytes()==before[p.name] for p in tmp_path.iterdir() if p.name in before)
