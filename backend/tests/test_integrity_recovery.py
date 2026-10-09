"""Positive recovery and publication checks following the independent audit."""
import json
import asyncio
import threading
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app import main, media_reimport, annotation_state
from app.auth import VIDEO_COOKIE, sign_jwt
from .test_media_reimport import imports, upload
from .test_review_workflow import task
from .test_independent_integrity_audit import seed, manual, save


def test_video_cookie_supports_range_without_granting_workspace_access(imports):
    client, content = imports
    mid = upload(client,content).json()['mediaId']
    me = client.get('/api/auth/me')
    assert me.status_code == 200
    assert 'HttpOnly' in me.headers['set-cookie'] and 'SameSite=strict' in me.headers['set-cookie']
    assert 'Path=/api/track/video/' in me.headers['set-cookie']
    client.headers.pop('Authorization')
    response = client.get('/api/track/video/'+mid, headers={'Range':'bytes=0-99'})
    assert response.status_code == 206 and response.content == content[:100]
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['content-range'] == f'bytes 0-99/{len(content)}'
    assert client.get('/api/track/workspace/'+mid).status_code == 401
    assert client.get('/api/track/video/'+mid,headers={'Authorization':'Bearer bad'}).status_code == 401
    scoped_token = client.cookies.get(VIDEO_COOKIE)
    assert client.get('/api/track/workspace/'+mid,headers={'Authorization':'Bearer '+scoped_token}).status_code == 401
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/track/video/'+mid).status_code == 401


@pytest.mark.parametrize('cookie', [sign_jwt({'uid':1}), sign_jwt({'uid':999999,'purpose':'source-video'}), 'invalid'])
def test_video_cookie_rejects_invalid_scope_or_removed_identity(imports,cookie):
    client, content = imports
    mid = upload(client,content).json()['mediaId']
    anonymous = TestClient(main.app)
    anonymous.cookies.set(VIDEO_COOKIE, cookie, path='/api/track/video/')
    assert anonymous.get('/api/track/video/'+mid).status_code == 401


def test_seed_replace_failure_preserves_bytes_and_cleans_temporary(imports, monkeypatch):
    client, content = imports
    mid = upload(client,content).json()['mediaId']
    assert client.post('/api/track/annotations',json=seed(mid)).status_code == 201
    root = main.media_dir(mid)
    target = root/'annotations_frame_000000.json'
    before = target.read_bytes()
    original = Path.replace
    def blocked(file, dest):
        if Path(dest) == target: raise PermissionError('injected publication failure')
        return original(file,dest)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'replace',blocked)
        response = client.post('/api/track/annotations',json=seed(mid,8))
        assert response.status_code == 500
    assert target.read_bytes() == before and not list(root.glob('.*.tmp'))
    assert client.get('/api/track/workspace/'+mid).json()['manualAnnotations']
    assert client.post('/api/track/annotations',json=seed(mid,8)).status_code == 201


def test_corrupt_legacy_source_never_becomes_empty_workspace(imports):
    client, content = imports
    mid = upload(client,content).json()['mediaId']
    assert client.post('/api/track/annotations',json=seed(mid)).status_code == 201
    target = main.media_dir(mid)/'annotations_frame_000000.json'
    healthy = target.read_bytes()
    for broken in ('{"partial":', '{}', json.dumps({'frame':{'frameIndex':0},'annotations':[{'source':'manual','object_id':7,'bbox':[1,2,0,3]}]})):
        target.write_text(broken)
        assert client.get('/api/track/workspace/'+mid).status_code == 500
        assert target.read_text() == broken
    target.write_bytes(healthy)
    assert client.get('/api/track/workspace/'+mid).json()['manualAnnotations']


def test_legal_points_and_pixel_baseline_validate_against_source(imports):
    client,content = imports
    mid = upload(client,content).json()['mediaId']
    point = manual(bbox=None,point={'x':10,'y':20})
    assert save(client,mid,[point]).status_code == 200
    baseline = {'source':'manual','objectId':7,'frameIndex':0,'bbox':[1,2,65,8]}
    assert save(client,mid,[point],1,'bad-pixel-baseline',manualBaselines=[baseline]).status_code == 422
    baseline['bbox']=[1,2,64,8]
    assert save(client,mid,[point],1,'good-pixel-baseline',manualBaselines=[baseline]).status_code == 200


@pytest.mark.parametrize('delete_during_hash',[False,True])
def test_slow_upload_hash_keeps_same_event_loop_and_other_writes_responsive(imports,monkeypatch,delete_during_hash):
    import httpx
    client,content = imports
    mid = upload(client,content).json()['mediaId']
    started,release = threading.Event(),threading.Event()
    original = media_reimport.file_sha
    calls = 0
    def slow_hash(path):
        nonlocal calls
        if path.parent.name.startswith('_upload-') and calls == 0:
            calls += 1;started.set()
            assert release.wait(8), 'test cleanup must release slow storage'
        return original(path)
    monkeypatch.setattr(media_reimport,'file_sha',slow_hash)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://testserver',headers=dict(client.headers)) as api:
            pending = asyncio.create_task(api.post('/api/track/upload',files={'file':('slow.avi',content,'video/x-msvideo')}))
            try:
                assert await asyncio.to_thread(started.wait,3)
                assert (await asyncio.wait_for(api.get('/api/health'),2)).status_code == 200
                result = await asyncio.wait_for(api.put('/api/track/workspace/'+mid,json={'manualAnnotations':[manual()], 'expectedRevision':0},headers={'Idempotency-Key':'concurrent-other-save'}),2)
                assert result.status_code == 200
                if delete_during_hash:
                    assert (await asyncio.wait_for(api.delete('/api/track/media/'+mid),2)).status_code == 200
            finally:
                release.set()
                result = await asyncio.wait_for(pending,5)
            assert result.status_code == (201 if delete_during_hash else 409)
            if delete_during_hash:
                new_mid = result.json()['mediaId']
                assert (await api.get('/api/track/result/'+new_mid)).json()['state'] == 'not_generated'
    asyncio.run(scenario())


def test_feedback_receipts_grow_linearly_and_replay_exact_history_after_reset(imports):
    client,content = imports
    mid = upload(client,content).json()['mediaId']
    directory = main.media_dir(mid)
    responses = []
    for i in range(240):
        body = {'expectedRevision':i,'index':i}
        def build(previous):
            event = {'id':str(i),'objectId':7,'decision':'normal','frameIndex':i,
                     'sample':{'objectId':7,'frameIndex':i,'reason':'motion','features':{'motionNormalized':2.0}}}
            if i == 239: event = {'id':str(i),'objectId':7,'decision':'reset'}
            events = [*previous.get('trackingFeedbackEvents',[]),event]
            return {**previous,'trackingFeedbackEvents':events,'normalMotionSamples':annotation_state._feedback_samples(events)}
        responses.append(annotation_state.write_state(directory,mid,1,'feedback-growth-'+str(i),body,'feedback',build))
    state = annotation_state.read_state(directory)
    assert not state['normalMotionSamples'] and len(state['_writeReceipts']) == 240
    assert (directory/'workspace_state.json').stat().st_size < 240*1200
    for i in (0,100,238,239):
        replay = annotation_state.write_state(directory,mid,1,'feedback-growth-'+str(i),{'expectedRevision':i,'index':i},'feedback',lambda _:pytest.fail('replay cannot execute mutation'))
        assert replay == responses[i]


def test_upload_hash_read_failure_does_not_publish_or_hide_storage_fault(imports,monkeypatch):
    client,content = imports
    mid = upload(client,content).json()['mediaId']
    before = (main.media_dir(mid)/'media.json').read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(media_reimport,'file_sha',lambda _:(_ for _ in ()).throw(PermissionError('injected hashing read fault')))
        assert upload(client,content).status_code == 500
    assert [d.name for d in main.iter_media_dirs()] == [mid]
    assert not list(main.TRACK_DATA_DIR.glob('_upload-*'))
    assert (main.media_dir(mid)/'media.json').read_bytes() == before
    assert upload(client,content).status_code == 409
