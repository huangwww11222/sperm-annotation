"""Click proposals never mutate saved annotations or invent fallback boxes."""
import pytest
from app import head_click
from .test_media_reimport import imports, upload
from .test_review_workflow import task


def test_click_tolerance_and_ambiguous_heads():
    assert head_click.choose([[20,20,30,30,.8]], 32,25,64,48)['bbox'] == [20,20,30,30]
    with pytest.raises(head_click.ProposalError):
        head_click.choose([[20,20,30,30,.8],[32,20,42,30,.9]],31,25,64,48)
    with pytest.raises(head_click.ProposalError):
        head_click.choose([],25,25,64,48)
    with pytest.raises(head_click.ProposalError):
        head_click.choose([[0,0,float('nan'),30,.9]],25,25,64,48)


def test_proposal_exact_frame_and_no_persistence(imports, monkeypatch):
    client, video = imports
    mid=upload(client,video).json()['mediaId']
    calls=[]
    def predict(image,x,y):
        calls.append((image.shape,x,y))
        return {'bbox':[20,20,30,30], 'confidence':.8}
    monkeypatch.setattr(head_click,'propose',predict)
    result=client.post('/api/track/head-box/'+mid,json={'frameIndex':1,'x':25,'y':25})
    assert result.status_code==200,result.text
    assert result.json()['frameIndex']==1
    assert calls==[((48,64,3),25,25)]
    assert client.get('/api/track/workspace/'+mid).json().get('manualAnnotations',[])==[]
    assert client.post('/api/track/head-box/'+mid,json={'frameIndex':99,'x':25,'y':25}).status_code==404
    assert client.post('/api/track/head-box/'+mid,json={'frameIndex':1,'x':999,'y':25}).status_code==422
    client.headers.pop('Authorization')
    assert client.post('/api/track/head-box/'+mid,json={'frameIndex':1,'x':25,'y':25}).status_code==401


def test_unavailable_model_recovers_without_writing(imports,monkeypatch):
    client,video=imports
    mid=upload(client,video).json()['mediaId']
    monkeypatch.setenv('HEAD_DETECTOR_MODEL','/missing/head.pt')
    path='/api/track/head-box/'+mid
    response=client.post(path,json={'frameIndex':0,'x':20,'y':20})
    assert response.status_code==503
    monkeypatch.setattr(head_click,'propose',lambda *args:{'bbox':[15,15,25,25],'confidence':.9})
    assert client.post(path,json={'frameIndex':0,'x':20,'y':20}).status_code==200
    assert client.get('/api/track/workspace/'+mid).json().get('manualAnnotations',[])==[]


def test_malformed_duplicate_and_distant_candidates():
    assert head_click.choose([[20,20,30,30,.9],[20.2,20.1,30.1,30.2,.8]],25,25,64,48)['bbox']==[20,20,30,30]
    with pytest.raises(head_click.ProposalError):head_click.choose([[20,20,30,30,.9]],60,45,64,48)
    with pytest.raises(head_click.ProposalError):head_click.choose([[30,20,20,30,.9]],25,25,64,48)


def test_local_crop_maps_back_to_source_and_lock_recovers(tmp_path,monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    weight=tmp_path/'head.pt';weight.write_bytes(b'fixture-not-a-model')
    monkeypatch.setenv('HEAD_DETECTOR_MODEL',str(weight))
    class Values:
        def __init__(self,data):self.data=data
        def cpu(self):return self
        def tolist(self):return self.data
    calls=[]
    def predict(image,**kwargs):
        calls.append(image.shape)
        return [SimpleNamespace(boxes=SimpleNamespace(xyxy=Values([[250,250,270,270]]),conf=Values([.9])))]
    model=SimpleNamespace(predict=predict)
    monkeypatch.setattr(head_click,'_model',model)
    monkeypatch.setattr(head_click,'_model_path',str(weight.resolve()))
    image=np.zeros((600,800,3),dtype=np.uint8)
    assert head_click.propose(image,400,300)['bbox']==[394,294,414,314]
    assert calls==[(512,512,3)]
    def fail(*args,**kwargs):raise RuntimeError('injected predictor failure')
    model.predict=fail
    with pytest.raises(RuntimeError):head_click.propose(image,400,300)
    model.predict=predict
    assert head_click.propose(image,400,300)['bbox']==[394,294,414,314]
