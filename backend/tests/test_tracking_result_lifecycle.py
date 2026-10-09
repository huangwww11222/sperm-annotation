"""Result absence is a lifecycle state, not a blanket exception exemption."""
import json
from pathlib import Path

import pytest

from app import main, tracker
from .test_media_reimport import imports, upload, reset
from .test_review_workflow import task


def read(client, mid, suffix=''):
    return client.get('/api/track/result/' + mid + suffix)


def save_empty_workspace(client, mid):
    response = client.put('/api/track/workspace/'+mid,json={'expectedRevision':0,'manualAnnotations':[]},headers={'Idempotency-Key':'empty-workspace'})
    assert response.status_code == 200, response.text


def test_fresh_upload_is_editable_without_inventing_reviewed_empty_frames(imports):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    response = read(client, mid)
    assert response.status_code == 200
    assert response.json() == dict(format='sam3-tracking-results-jsonl', state='not_generated', frames=[], count=0)
    save_empty_workspace(client, mid)
    assert client.get('/api/review/media/'+mid+'/completion-preview').json()['unknownFrames'] == 3
    assert not (main.media_dir(mid)/'tracker_results.json').exists()


def test_manual_only_workspace_and_failed_first_start_do_not_require_ai_results(imports):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    state = {'expectedRevision':0, 'manualAnnotations':[{'objectId':1,'source':'manual','frameIndex':0,'bbox':{'x':10,'y':10,'width':20,'height':20}}]}
    assert client.put('/api/track/workspace/'+mid,json=state,headers={'Idempotency-Key':'manual'}).status_code == 200
    (main.media_dir(mid)/'seed_frame_000000.json').write_text('{}')
    assert read(client, mid).json()['state'] == 'not_generated'
    assert client.get('/api/track/workspace/'+mid).json()['manualAnnotations'] == state['manualAnnotations']


def test_absent_media_and_required_results_remain_errors(imports):
    client, video = imports
    assert read(client, 'does-not-exist').status_code == 404
    mid = upload(client, video).json()['mediaId']
    response = read(client, mid, '?required=true')
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'TRACKING_RESULTS_MISSING'


@pytest.mark.parametrize('content', ['{broken', '{"unexpected":true}', '[{"frame_index":0,"objects":"broken"}]', '[{"frame_index":0,"objects":[{"object_id":1,"bbox":[1,2]}]}]'])
def test_corrupt_results_are_never_successful_empty_results(imports, content):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    (main.media_dir(mid)/'tracker_results.json').write_text(content)
    assert read(client, mid).status_code == 500
    assert client.get('/api/track/result-file/'+mid).status_code == 500


def test_generated_or_previously_read_results_cannot_disappear_silently(imports):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    directory = main.media_dir(mid)
    rows = [{'frame_index':0,'source_frame_index':0,'objects':[{'object_id':1,'bbox':[1,2,10,12]}]}]
    tracker._write_jsonl(directory/'tracker_results.json', rows)
    (directory/'tracker_results.json').unlink()
    assert read(client, mid).status_code == 409
    # Legacy results are recognized on the first successful read after upgrade.
    (directory/'tracker_results.meta.json').unlink()
    (directory/'tracker_results.json').write_text(json.dumps(rows[0])+'\n')
    assert read(client, mid).json()['state'] == 'available'
    (directory/'tracker_results.json').unlink()
    assert read(client, mid).status_code == 409


def test_delete_reupload_and_overwrite_return_to_untracked_state(imports):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    directory = main.media_dir(mid)
    tracker._write_jsonl(directory/'tracker_results.json', [{'frame_index':0,'objects':[]}])
    assert reset(client, mid, revision=0).status_code == 200
    assert read(client, mid).json()['state'] == 'not_generated'
    assert not (directory/'tracker_results.meta.json').exists()
    tracker._write_jsonl(directory/'tracker_results.json', [{'frame_index':0,'objects':[]}])
    assert client.delete('/api/track/media/'+mid).status_code == 200
    mid = upload(client, video).json()['mediaId']
    assert read(client, mid).json()['state'] == 'not_generated'


def test_valid_zero_object_tracking_is_distinct_from_never_tracked(imports):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    tracker._write_jsonl(main.media_dir(mid)/'tracker_results.json', [{'frame_index':0,'objects':[]}])
    body = read(client, mid).json()
    assert body['state'] == 'available' and body['count'] == 1
    assert body['frames'][0]['annotations'] == []
    save_empty_workspace(client, mid)
    assert client.get('/api/review/media/'+mid+'/completion-preview').json()['unknownFrames'] == 2


def test_missing_generated_results_cannot_be_submitted_as_explicit_empty(imports, caplog):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    save_empty_workspace(client, mid)
    directory = main.media_dir(mid)
    tracker._write_jsonl(directory/'tracker_results.json', [{'frame_index':0,'objects':[]}])
    preview = client.get('/api/review/media/'+mid+'/completion-preview').json()
    (directory/'tracker_results.json').unlink()
    body = {'expectedSourceRevision':preview['sourceRevision'], 'confirmComplete':True,
            'explicitEmptyFrameRanges':[{'start':0,'end':2}]}
    response = client.post('/api/review/media/'+mid+'/complete', json=body, headers={'Idempotency-Key':'missing-result-submit'})
    assert response.status_code == 409
    assert response.json()['code'] == 'TRACKING_RESULTS_MISSING'
    assert 'annotation.completion_results_missing' in caplog.text


def test_failed_reset_restores_result_and_its_history_together(imports, monkeypatch, caplog):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    save_empty_workspace(client, mid)
    directory = main.media_dir(mid)
    tracker._write_jsonl(directory/'tracker_results.json', [{'frame_index':0,'objects':[]}])
    before = {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    original = Path.write_text
    def fail_after_moves(path, *args, **kwargs):
        if path == directory/'workspace_state.json':
            raise PermissionError('injected reset publication failure')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'write_text', fail_after_moves)
        response = reset(client, mid, revision=1)
    assert response.status_code == 503
    assert 'media.reset_failed' in caplog.text
    assert {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()} == before
    assert read(client, mid).json()['state'] == 'available'
    assert reset(client, mid, revision=1).status_code == 200
    assert read(client, mid).json()['state'] == 'not_generated'


@pytest.mark.parametrize('has_file,has_marker,presence,flag', [
    (False,False,'not_generated',False),
    (True,False,'present',True),
    (False,True,'missing',None),
    (True,True,'present',True),
])
def test_result_presence_is_consistent_across_list_detail_and_download(imports, has_file, has_marker, presence, flag):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    directory = main.media_dir(mid)
    row = {'frame_index':0,'objects':[{'object_id':1,'bbox':[1,2,10,12]}]}
    if has_file:
        (directory/'tracker_results.json').write_text(json.dumps(row)+'\n')
    if has_marker:
        (directory/'tracker_results.meta.json').write_text('{"generated":true}')
    entry = next(item for item in client.get('/api/track/media').json()['items'] if item['mediaId'] == mid)
    assert entry['trackingResultState'] == presence
    assert entry['hasTrackingResult'] is flag
    detail = read(client, mid)
    download = client.get('/api/track/result-file/'+mid)
    if has_file:
        assert detail.status_code == download.status_code == 200
        assert detail.json()['state'] == 'available'
        assert json.loads(download.text)['objects'] == row['objects']
        assert (directory/'tracker_results.meta.json').is_file()
    elif has_marker:
        assert detail.status_code == download.status_code == 409
        assert detail.json()['detail']['code'] == download.json()['detail']['code'] == 'TRACKING_RESULTS_MISSING'
        # Restoring the real file recovers every reader, not just its error label.
        (directory/'tracker_results.json').write_text(json.dumps(row)+'\n')
        assert read(client, mid).json()['state'] == 'available'
        assert client.get('/api/track/result-file/'+mid).status_code == 200
        restored = next(item for item in client.get('/api/track/media').json()['items'] if item['mediaId'] == mid)
        assert restored['trackingResultState'] == 'present' and restored['hasTrackingResult'] is True
    else:
        assert detail.status_code == 200 and detail.json()['state'] == 'not_generated'
        assert download.status_code == 404 and download.json()['detail']['code'] == 'TRACKING_RESULTS_NOT_GENERATED'
        assert not (directory/'tracker_results.meta.json').exists()


def test_raw_download_distinguishes_missing_media_and_required_results(imports):
    client, video = imports
    response = client.get('/api/track/result-file/no-such-media')
    assert response.status_code == 404 and response.json()['detail']['code'] == 'MEDIA_NOT_FOUND'
    mid = upload(client, video).json()['mediaId']
    response = client.get('/api/track/result-file/'+mid+'?required=true')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'TRACKING_RESULTS_MISSING'


def test_raw_legacy_result_backfills_history_before_later_loss(imports):
    client, video = imports
    mid = upload(client, video).json()['mediaId']
    directory = main.media_dir(mid)
    (directory/'tracker_results.json').write_text('{"frame_index":0,"objects":[]}\n')
    assert client.get('/api/track/result-file/'+mid).status_code == 200
    assert (directory/'tracker_results.meta.json').is_file()
    (directory/'tracker_results.json').unlink()
    assert client.get('/api/track/result-file/'+mid).status_code == 409
