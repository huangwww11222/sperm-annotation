"""Microfluidic mode over HTTP: adding videos, two passes, blind judging, misses and export."""
import io
import json
import time
import zipfile

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import flow_export, flow_store as store, flow_targets as ft, main, media_reimport
from app.auth import sign_jwt
from app.db import connect
from app.review_workflow import ReviewError
from .test_review_workflow import task  # noqa: F401

W, H, N = 400, 300, 12
HEADERS = {'X-Review-Contract': '3'}


@pytest.fixture
def client(tmp_path, monkeypatch, task):  # noqa: F811
    for name, value in (('TRACK_DATA_DIR', tmp_path / 'media'), ('LEGACY_TRACK_DATA_DIR', tmp_path / 'legacy'), ('TASKS', {})):
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(flow_export, 'DATASET_EXPORT_DIR', tmp_path / 'datasets')
    monkeypatch.setattr(store, '_records', type(store._records)())
    http = TestClient(main.app)
    http.headers['Authorization'] = 'Bearer ' + sign_jwt({'uid': 1})
    yield http
    flow_export.shutdown()


def video(tmp_path, shade):
    path = tmp_path / f'{shade}.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 20, (W, H))
    for _ in range(N):
        writer.write(np.full((H, W, 3), shade, np.uint8))
    writer.release()
    return path.read_bytes()


def upload(client, tmp_path, shade, name=None):
    name = name or f'{shade}.avi'
    response = client.post('/api/track/upload', files={'file': (name, video(tmp_path, shade), 'video/x-msvideo')})
    assert response.status_code == 201, response.text
    return response.json()['mediaId']


def target(number, first=2, count=6, score=0., head='dark', x=50., step=30.):
    frames = [[first + i, x + step * i, 100. + number, 40., 32.] for i in range(count)]
    return {'id': f'T{number:04d}', 'first': first, 'last': first + count - 1, 'frames': frames, 'shown': [row[0] for row in frames],
            'best': 0, 'score': score, 'energy': 12., 'head': head}


def fake_find(targets, scan_frame=5, faint=()):
    """Stands in for the search: publishes the given targets with one-pixel strips."""
    def find(directory, path, info, sha256, detect=None, progress=None):
        record = {'policy': ft.POLICY, 'runId': ft.run_id_of(sha256, N, targets), 'sha256': sha256, 'width': W, 'height': H, 'frameCount': N,
                  'angle': 0., 'slow': False, 'intervals': [1] * (N - 1), 'lostFrames': 0, 'intervalFixes': 0, 'linkedShare': 1., 'unsteady': False,
                  'detector': 'ok', 'detectorError': None, 'scanFrame': scan_frame, 'cell': 220, 'targets': targets, 'faint': list(faint)}
        strips = directory / 'flow' / 'strips'
        strips.mkdir(parents=True, exist_ok=True)
        for item in targets:
            for view in 'er':
                (strips / f"{item['id']}_{view}.jpg").write_bytes(f"{directory.name}/{item['id']}/{view}".encode())
        for view in 'er':
            (directory / 'flow' / f'scan_{view}.jpg').write_bytes(b'scan')
        (directory / 'flow' / 'targets.json').write_text(json.dumps(record))
        return record
    return find


def wait(client, media_id):
    for _ in range(300):
        item = next(i for i in client.get('/api/flow/media').json()['items'] if i['mediaId'] == media_id)
        if item['state'] != 'finding':
            return item
        time.sleep(.02)
    raise AssertionError('never finished')


def add(client, monkeypatch, media_id, targets, sample='', **kw):
    monkeypatch.setattr(ft, 'find', fake_find(targets, **kw))
    response = client.put('/api/flow/media', json={'mediaIds': [media_id], 'sample': sample})
    assert response.status_code == 200, response.text
    return wait(client, media_id)


def get(client, media_id):
    response = client.get('/api/flow/media/' + media_id)
    assert response.status_code == 200, response.text
    return response.json()


def page(client, media_id, number, picked, revision=0, run=None):
    run = run or get(client, media_id)['progress']['runId']
    return client.put(f'/api/flow/media/{media_id}/pages/{number}', json={'runId': run, 'picked': picked, 'ms': 1200, 'revision': revision})


def answer(client, media_id, key, value, dropped=(), revision=0, run=None):
    run = run or get(client, media_id)['progress']['runId']
    return client.put(f'/api/flow/media/{media_id}/answers/{key}', json={'runId': run, 'answer': value, 'dropped': list(dropped), 'ms': 900, 'revision': revision})


def judge(client, media_id, picked, answers):
    detail = get(client, media_id)
    for number, tiles in enumerate(detail['pages']):
        keys = {tile['key'] for tile in tiles}
        assert page(client, media_id, number, [key for key in picked if key in keys], detail['picks'].get(str(number), {}).get('revision', 0)).status_code == 200
    for tile in get(client, media_id)['second']:
        assert answer(client, media_id, tile['key'], answers.get(tile['key'], 'no')).status_code == 200
    return get(client, media_id)


def no_scan(monkeypatch):
    monkeypatch.setattr(store, 'SCAN_EVERY', 10 ** 9)


def test_a_microfluidic_video_leaves_the_wet_library_and_its_flow(client, tmp_path, monkeypatch):
    no_scan(monkeypatch)
    wet, flow = upload(client, tmp_path, 10), upload(client, tmp_path, 90)
    item = add(client, monkeypatch, flow, [target(1), target(2)], sample='样本1')
    assert item['state'] == 'wall' and item['sample'] == '样本1' and item['progress']['targets'] == 2
    assert [i['mediaId'] for i in client.get('/api/track/media').json()['items']] == [wet]
    assert client.post('/api/prelabel/' + flow).status_code == 409
    assert client.post('/api/flow/media/remove', json={'mediaIds': [flow]}).status_code == 200
    assert {i['mediaId'] for i in client.get('/api/track/media').json()['items']} == {wet, flow}
    assert client.get('/api/flow/media').json()['items'] == []


def test_videos_with_wet_work_are_refused_as_a_whole(client, tmp_path, monkeypatch):
    first, second = upload(client, tmp_path, 10), upload(client, tmp_path, 90)
    monkeypatch.setattr('app.annotation_state.read_state', lambda directory: {'manualAnnotations': [{'objectId': 1}] if directory.name == second else []})
    response = client.put('/api/flow/media', json={'mediaIds': [first, second]})
    assert response.status_code == 409 and response.json()['code'] == 'FLOW_HAS_WET_WORK'
    assert client.get('/api/flow/media').json()['items'] == []  # Neither was moved.
    assert client.put('/api/flow/media', json={'mediaIds': ['nope']}).status_code == 404


def test_a_failed_search_is_shown_and_can_be_retried(client, tmp_path, monkeypatch):
    media_id = upload(client, tmp_path, 10)

    def broken(*a, **k):
        raise RuntimeError('原视频第 3 帧读取失败')
    monkeypatch.setattr(ft, 'find', broken)
    client.put('/api/flow/media', json={'mediaIds': [media_id]})
    item = wait(client, media_id)
    assert item['state'] == 'failed' and '第 3 帧' in item['task']['message'] and item['progress'] is None
    assert client.get('/api/flow/media/' + media_id).json()['code'] == 'FLOW_NOT_READY'
    monkeypatch.setattr(ft, 'find', fake_find([target(1)]))
    again = client.post(f'/api/flow/media/{media_id}/find', json={}, headers={'Idempotency-Key': 'k1'})
    assert again.status_code == 202
    assert client.post(f'/api/flow/media/{media_id}/find', json={}, headers={'Idempotency-Key': 'k1'}).json()['taskId'] == again.json()['taskId']
    assert wait(client, media_id)['state'] in ('wall', 'scan')


def test_judging_is_blind(client, tmp_path, monkeypatch):
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(i, score=.9 if i % 2 else 0., head='pale' if i % 3 == 0 else 'dark') for i in range(1, 30)])
    text = client.get('/api/flow/media/' + media_id).text
    for word in ('score', 'head', 'energy', 'plant', 'pale', 'bbox'):
        assert word not in text
    detail = json.loads(text)
    assert [len(p) for p in detail['pages']] == [24, 5]
    assert set(detail['pages'][0][0]) == {'key', 'n', 'best', 'frames'}
    keys = [tile['key'] for p in detail['pages'] for tile in p]
    assert keys != sorted(keys)  # Shuffled ...
    assert keys == [tile['key'] for p in get(client, media_id)['pages'] for tile in p]  # ... but the same every time.


def test_wall_pages_survive_retries_and_refuse_stale_edits(client, tmp_path, monkeypatch):
    no_scan(monkeypatch)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(i) for i in range(1, 4)])
    first = page(client, media_id, 0, ['T0001'])
    assert first.status_code == 200 and first.json()['revision'] == 1
    assert page(client, media_id, 0, ['T0001']).json()['revision'] == 1  # The same request again: nothing new.
    stale = page(client, media_id, 0, ['T0002'])
    assert stale.status_code == 409 and stale.json()['code'] == 'FLOW_CONFLICT'
    assert page(client, media_id, 0, ['T0002'], revision=1).json()['revision'] == 2
    assert page(client, media_id, 0, ['T0099'], revision=2).status_code == 422
    assert page(client, media_id, 1, [], revision=0).status_code == 422
    assert page(client, media_id, 0, [], run='other', revision=2).json()['code'] == 'FLOW_RUN_CHANGED'


def test_the_second_pass_holds_picks_detector_hits_and_pale_heads(client, tmp_path, monkeypatch):
    no_scan(monkeypatch)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(1), target(2, score=.3), target(3, head='pale'), target(4, score=.29), target(5, head='clear'), target(6)])
    assert get(client, media_id)['second'] is None
    page(client, media_id, 0, ['T0001'])
    assert {t['key'] for t in get(client, media_id)['second']} == {'T0001', 'T0002', 'T0003'}
    # Changing the wall afterwards changes the second pass; answers of targets that left it no longer count.
    answer(client, media_id, 'T0001', 'yes')
    page(client, media_id, 0, ['T0006'], revision=1)
    detail = get(client, media_id)
    assert {t['key'] for t in detail['second']} == {'T0006', 'T0002', 'T0003'}
    assert detail['answers'] == {} and detail['progress']['yes'] == 0
    assert answer(client, media_id, 'T0001', 'no').json()['code'] == 'FLOW_NOT_IN_SECOND'


def test_answers_keep_clicked_off_frames_and_at_least_one_frame(client, tmp_path, monkeypatch):
    no_scan(monkeypatch)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(1, count=3), target(2)])
    page(client, media_id, 0, ['T0001'])
    assert answer(client, media_id, 'T0001', 'yes', [2, 3, 4]).status_code == 422
    assert answer(client, media_id, 'T0001', 'yes', [9]).status_code == 422
    assert answer(client, media_id, 'T0001', 'maybe').status_code == 422
    saved = answer(client, media_id, 'T0001', 'yes', [4])
    assert saved.status_code == 200 and saved.json()['dropped'] == [4] and saved.json()['progress']['state'] == 'done'
    assert answer(client, media_id, 'T0001', 'no').json()['code'] == 'FLOW_CONFLICT'
    assert answer(client, media_id, 'T0001', 'no', revision=1).json()['revision'] == 2
    assert get(client, media_id)['answers']['T0001'] == {'answer': 'no', 'dropped': [], 'revision': 2}


def test_finding_again_keeps_answers_of_the_same_targets_and_starts_clean_for_new_ones(client, tmp_path, monkeypatch):
    no_scan(monkeypatch)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(1), target(2)])
    judge(client, media_id, ['T0001'], {'T0001': 'yes'})
    old_run = get(client, media_id)['progress']['runId']
    refused = client.post(f'/api/flow/media/{media_id}/find', json={})
    assert refused.status_code == 409 and refused.json()['code'] == 'FLOW_HAS_ANSWERS'
    client.post(f'/api/flow/media/{media_id}/find', json={'force': True})
    assert wait(client, media_id)['state'] == 'done'  # Same targets, same run: nothing lost.
    monkeypatch.setattr(ft, 'find', fake_find([target(1), target(2), target(3)]))
    client.post(f'/api/flow/media/{media_id}/find', json={'force': True})
    item = wait(client, media_id)
    assert item['state'] == 'wall' and item['progress']['runId'] != old_run and item['progress']['yes'] == 0
    assert answer(client, media_id, 'T0001', 'no', run=old_run).json()['code'] == 'FLOW_RUN_CHANGED'
    with connect() as c:
        assert c.execute('SELECT COUNT(*) FROM flow_answers WHERE run_id=?', (old_run,)).fetchone()[0] == 1
    assert client.post('/api/flow/media/remove', json={'mediaIds': [media_id]}).json()['code'] == 'FLOW_HAS_ANSWERS'


def test_the_whole_frame_check_comes_first_and_counts_as_blind_only_then(client, tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'SCAN_EVERY', 1)
    media_id = upload(client, tmp_path, 10)
    item = add(client, monkeypatch, media_id, [target(1), target(2, x=200.)])
    assert item['state'] == 'scan'
    run = item['progress']['runId']
    put = lambda points: client.put(f'/api/flow/media/{media_id}/scan', json={'runId': run, 'points': points, 'ms': 10})
    assert put([[W + 1, 5]]).status_code == 422 and put([[1, 2, 3]]).status_code == 422
    # Target 1 is at (140, 101) in frame 5; (300, 250) has no target.
    assert put([[141, 100], [300, 250]]).json()['progress']['state'] == 'wall'
    assert client.get('/api/flow/misses').json()['blindMarks'] == 2 and client.get('/api/flow/misses').json()['blindUnmatched'] == 1
    page(client, media_id, 0, [])
    put([[10, 10]])  # Changed after the wall was seen: no longer blind ... but a first blind check stays recorded as such only if untouched.
    assert client.get('/api/flow/misses').json()['blindMarks'] == 0


def confirmed_elsewhere(client, tmp_path, monkeypatch, shade=200, count=9):
    media_id = upload(client, tmp_path, shade)
    add(client, monkeypatch, media_id, [target(i) for i in range(1, count + 1)])
    keys = [f'T{i:04d}' for i in range(1, count + 1)]
    judge(client, media_id, keys, {key: 'yes' for key in keys})
    return media_id


def test_known_sperm_are_mixed_in_unnoticed_and_measure_what_people_overlook(client, tmp_path, monkeypatch):
    no_scan(monkeypatch)
    lender = confirmed_elsewhere(client, tmp_path, monkeypatch)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(i) for i in range(1, 49)])
    detail = get(client, media_id)
    keys = [tile['key'] for p in detail['pages'] for tile in p]
    own = {f'T{i:04d}' for i in range(1, 49)}
    planted = [key for key in keys if key not in own]
    assert len(planted) == 2 and all(set(t) == {'key', 'n', 'best', 'frames'} for p in detail['pages'] for t in p)
    run = detail['progress']['runId']
    strip = client.get(f'/api/flow/media/{media_id}/strips/{planted[0]}', params={'run': run})
    assert strip.text.startswith(lender + '/') and 'immutable' in strip.headers['cache-control']
    done = judge(client, media_id, [planted[0], 'T0001'], {'T0001': 'yes'})
    assert done['progress']['state'] == 'done' and [t['key'] for t in done['second']] == ['T0001']  # Planted ones are not judged again.
    misses = client.get('/api/flow/misses').json()
    assert (misses['planted'], misses['plantedMissed']) == (2, 1)
    # The lender is deleted: the wall goes on without the borrowed tiles.
    assert client.delete('/api/track/media/' + lender).status_code == 200
    store._records.clear()
    assert {tile['key'] for p in get(client, media_id)['pages'] for tile in p} == own


def test_second_pass_rescues_are_counted(client, tmp_path, monkeypatch):
    no_scan(monkeypatch)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(1), target(2, score=.9), target(3, head='pale')])
    judge(client, media_id, ['T0001'], {'T0001': 'yes', 'T0002': 'yes', 'T0003': 'yes'})
    misses = client.get('/api/flow/misses').json()
    assert misses['yes'] == 3 and misses['secondPassOnly'] == 2


def test_a_deleted_video_never_lends_its_answers_to_a_new_one(client, tmp_path, monkeypatch):
    media_id = upload(client, tmp_path, 10, 'a.avi')
    add(client, monkeypatch, media_id, [target(1)])
    assert client.delete('/api/track/media/' + media_id).status_code == 200
    assert client.get('/api/flow/media').json()['items'] == []
    assert upload(client, tmp_path, 60, 'a.avi') != media_id


# ------------------------------------------------------------------ labels and export

def record(targets, faint=(), intervals=None):
    return {'width': W, 'height': H, 'frameCount': N, 'intervals': intervals or [1] * (N - 1), 'targets': targets, 'faint': list(faint), 'scanFrame': 5}


def labels(targets, answers, frame, faint=(), intervals=None):
    source = {'answers': {key: {'answer': value, 'dropped': dropped} for key, (value, dropped) in answers.items()}}
    sperm, ignore = flow_export.frame_labels(record(targets, faint, intervals), source, frame)
    return [s['targetId'] for s in sperm], [(i['reason'], i['targetId']) for i in ignore]


def test_only_sperm_become_labels_and_only_unclear_things_are_masked():
    targets = [target(1), target(2), target(3), target(4)]
    answers = {'T0001': ('yes', []), 'T0002': ('unsure', []), 'T0003': ('no', [])}  # T0004 was never picked.
    assert labels(targets, answers, 3) == (['T0001'], [('unclear', 'T0002')])


def test_frames_clicked_off_are_masked_and_so_are_their_unseen_neighbours():
    item = target(1, first=0, count=12)
    item['shown'] = [0, 3, 6, 9, 11]
    answers = {'T0001': ('yes', [6])}
    assert labels([item], answers, 3)[0] == ['T0001'] and labels([item], answers, 6) == ([], [('clicked_off', 'T0001')])
    assert labels([item], answers, 5)[1] == [('clicked_off', 'T0001')] and labels([item], answers, 8)[1] == [('clicked_off', 'T0001')]
    assert labels([item], answers, 2)[0] == ['T0001'] and labels([item], answers, 10)[0] == ['T0001']


def test_a_sperm_that_was_not_found_in_a_frame_is_masked_where_it_must_be():
    item = target(1, first=3, count=4)
    item['frames'] = [row for row in item['frames'] if row[0] != 5]
    item['shown'] = [3, 4, 6]
    source = {'answers': {'T0001': {'answer': 'yes', 'dropped': []}}}
    sperm, ignore = flow_export.frame_labels(record([item]), source, 5)
    x1, y1, x2, y2 = ignore[0]['bbox']
    assert not sperm and ignore[0]['reason'] == 'not_found' and (x1 + x2) / 2 == pytest.approx(110)  # Between 80 and 140.
    # Two normal frame times passed from 4 to 5, one from 5 to 6: the gap is filled in real time.
    uneven = flow_export.frame_labels(record([item], intervals=[1, 1, 1, 1, 2] + [1] * 6), source, 5)[1][0]['bbox']
    assert (uneven[0] + uneven[2]) / 2 == pytest.approx(80 + 60 * 2 / 3)
    assert labels([item], {'T0001': ('yes', [])}, 2)[1] == [('not_found', 'T0001')]  # One frame before it was first found.
    assert labels([item], {'T0001': ('yes', [])}, 11)[1] == [('not_found', 'T0001')]
    assert labels([item], {'T0001': ('no', [])}, 5) == ([], [])


def test_masks_stop_where_the_target_has_left_the_picture():
    item = target(1, first=2, count=6, x=180., step=40.)  # x=380 in frame 7, x=420 in frame 8: half of it is still in the picture.
    assert labels([item], {'T0001': ('yes', [])}, 8)[1] == [('not_found', 'T0001')]
    assert labels([item], {'T0001': ('yes', [])}, 10)[1] == []


def test_faint_things_nobody_judged_are_masked():
    assert labels([], {}, 4, faint=[[[4, 50., 60., 30., 30.], [5, 80., 60., 30., 30.]]]) == ([], [('faint', None)])


def export(client, media_ids, key='e1', **extra):
    response = client.post('/api/flow/exports', json={'mediaIds': media_ids, 'step': 1, **extra}, headers={**HEADERS, 'Idempotency-Key': key})
    if response.status_code != 200:
        return response.json()
    for _ in range(400):
        job = next(j for j in client.get('/api/flow/exports').json()['items'] if j['exportId'] == response.json()['exportId'])
        if job['state'] in ('ready', 'failed'):
            return job
        time.sleep(.02)
    raise AssertionError('export never finished')


def test_export_needs_finished_videos_and_holds_everything_a_trainer_needs(client, tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'SCAN_EVERY', 1)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(1), target(2, x=200.)], sample='样本1')
    assert export(client, [media_id])['code'] == 'FLOW_NOT_DONE'
    run = get(client, media_id)['progress']['runId']
    client.put(f'/api/flow/media/{media_id}/scan', json={'runId': run, 'points': [], 'ms': 5})
    judge(client, media_id, ['T0001', 'T0002'], {'T0001': 'yes', 'T0002': 'unsure'})
    job = export(client, [media_id], 'e2', step=2)
    assert job['state'] == 'ready', job
    assert client.post('/api/flow/exports', json={'mediaIds': [media_id], 'step': 2}, headers={**HEADERS, 'Idempotency-Key': 'e2'}).json()['exportId'] == job['exportId']
    blob = client.get(job['downloadUrl']).content
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        manifest = json.loads(z.read('manifest.json'))
        frames = [s['frameIndex'] for s in manifest['samples']]
        assert frames == [0, 2, 4, 6, 8, 10]  # Every second frame; frame 5, the whole-frame check, would be left out.
        sample = next(s for s in manifest['samples'] if s['frameIndex'] == 4)
        label = z.read(sample['label']).decode().split()
        assert label[0] == '0' and float(label[1]) == pytest.approx(110 / W) and float(label[2]) == pytest.approx(101 / H)
        assert [i['reason'] for i in json.loads(z.read(f"ignore/{sample['split']}/{sample['sampleId']}.json"))] == ['unclear']
        assert len([n for n in z.namelist() if n.startswith('images/')]) == 6
        assert 'T0001' in z.read('judgements.csv').decode('utf-8-sig') and manifest['sources'][0]['sample'] == '样本1'
        assert manifest['counts']['sperm'] == sum(len(s['objects']) for s in manifest['samples']) > 0 and manifest['splitBy'] == 'video'
    full = export(client, [media_id], 'e3', includeSourceVideos=True)
    with zipfile.ZipFile(io.BytesIO(client.get(full['downloadUrl']).content)) as z:
        source = json.loads(z.read('manifest.json'))['sources'][0]
        assert z.read(source['videoFile']) == main.find_video(main.media_dir(media_id)).read_bytes()
        assert [t['id'] for t in json.loads(z.read(source['targetsFile']))['targets']] == ['T0001', 'T0002']
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert not [n for n in z.namelist() if n.startswith(('videos/', 'targets/'))]
    assert client.delete('/api/flow/exports/' + job['exportId']).json()['deleted'] and client.get(job['downloadUrl']).status_code == 404


def test_the_checked_frame_is_never_exported(client, tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'SCAN_EVERY', 1)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(1)], scan_frame=4)
    run = get(client, media_id)['progress']['runId']
    client.put(f'/api/flow/media/{media_id}/scan', json={'runId': run, 'points': [], 'ms': 5})
    judge(client, media_id, [], {})
    job = export(client, [media_id], step=2)
    with zipfile.ZipFile(io.BytesIO(client.get(job['downloadUrl']).content)) as z:
        assert [s['frameIndex'] for s in json.loads(z.read('manifest.json'))['samples']] == [0, 2, 6, 8, 10]


def test_samples_stay_whole_across_train_and_val():
    sources = [{'mediaId': f'm{i}', 'sample': 'A' if i < 3 else 'B' if i < 6 else 'C'} for i in range(9)]
    splits, by = flow_export._splits(sources, .8)
    assert by == 'sample' and set(splits.values()) == {'train', 'val'}
    for group in ('A', 'B', 'C'):
        assert len({splits[s['mediaId']] for s in sources if s['sample'] == group}) == 1
    assert flow_export._splits(sources, .8)[0] == splits
    alone, by = flow_export._splits(sources[:3], .8)
    assert by == 'video' and set(alone.values()) == {'train', 'val'}
    assert set(flow_export._splits(sources[:1], .8)[0].values()) == {'train'}


def test_export_refuses_a_video_that_changed_or_lost_its_targets(client, tmp_path, monkeypatch):
    no_scan(monkeypatch)
    media_id = upload(client, tmp_path, 10)
    add(client, monkeypatch, media_id, [target(1)])
    judge(client, media_id, ['T0001'], {'T0001': 'yes'})
    source = main.find_video(main.media_dir(media_id))
    original = source.read_bytes()
    source.write_bytes(video(tmp_path, 11))
    failed = export(client, [media_id], 'x1')
    assert failed['state'] == 'failed' and '已变化' in failed['errorMessage']
    source.write_bytes(original)
    (main.media_dir(media_id) / 'flow' / 'targets.json').unlink()
    store._records.clear()
    failed = export(client, [media_id], 'x2')
    assert failed['state'] == 'failed' and '重新找目标' in failed['errorMessage']
    assert not list((tmp_path / 'datasets').rglob('*.partial'))


def test_restart_marks_running_exports_as_interrupted(client):
    with connect() as c:
        c.execute("INSERT INTO flow_exports(id,actor_id,request_key,request_hash,request_json,state,created_at,updated_at) VALUES ('flow_x',1,'k','h','{\"mediaIds\":[],\"step\":4}','running','t','t')")
    flow_export.recover_interrupted()
    job = client.get('/api/flow/exports').json()['items'][0]
    assert job['state'] == 'failed' and '重启' in job['errorMessage']


def test_real_search_end_to_end(client, tmp_path, monkeypatch):
    """The whole chain on a real (synthetic) video: no stand-ins except the detector."""
    from .test_flow_targets import crowd, picture, write_video
    from app import head_click
    no_scan(monkeypatch)
    monkeypatch.setattr(head_click, 'detect_batch', lambda images, conf=None, caller='prelabel': [[] for _ in images])
    path = tmp_path / 'real.avi'
    write_video(path, picture(crowd(70, 18) + [(-30.0, 300.0, 85.0, 'pale', 7)], 12))
    media_id = client.post('/api/track/upload', files={'file': ('real.avi', path.read_bytes(), 'video/x-msvideo')}).json()['mediaId']
    assert client.put('/api/flow/media', json={'mediaIds': [media_id]}).json()['queued'] == [media_id]
    for _ in range(600):
        item = next(i for i in client.get('/api/flow/media').json()['items'] if i['mediaId'] == media_id)
        if item['state'] != 'finding':
            break
        time.sleep(.1)
    assert item['state'] == 'wall' and item['progress']['detector'] == 'ok', item
    detail = get(client, media_id)
    tile = detail['pages'][0][0]
    strip = cv2.imdecode(np.frombuffer(client.get(f'/api/flow/media/{media_id}/strips/{tile["key"]}', params={'run': detail['progress']['runId']}).content, np.uint8), 0)
    assert strip.shape == (detail['cell'], detail['cell'] * tile['n'])
    assert client.get(f'/api/flow/media/{media_id}/scan-image', params={'run': detail['progress']['runId'], 'view': 'r'}).status_code == 200
    keys = [t['key'] for p in detail['pages'] for t in p]
    done = judge(client, media_id, keys[:3], {keys[0]: 'yes'})
    assert done['progress']['state'] == 'done'
    assert any(t['key'] not in keys[:3] for t in done['second'])  # The pale head got its second look without a click.
    job = export(client, [media_id], step=3)
    assert job['state'] == 'ready' and job['counts']['sperm'] > 0


# ------------------------------------------------------------------ optional blind re-check, and packages

def recheck_detail(client, media_id):
    response = client.get(f'/api/flow/media/{media_id}', params={'recheck': True})
    assert response.status_code == 200, response.text
    return response.json()


def judge_again(client, media_id, picked, answers):
    detail = recheck_detail(client, media_id)
    run = detail['progress']['runId']
    for number, tiles in enumerate(detail['pages']):
        keys = {tile['key'] for tile in tiles}
        assert page(client, media_id, number, [k for k in picked if k in keys], 0, run).status_code == 200
    for tile in recheck_detail(client, media_id)['second']:
        value, dropped = answers.get(tile['key'], ('no', []))
        assert answer(client, media_id, tile['key'], value, dropped, 0, run).status_code == 200
    return run


def finished(client, tmp_path, monkeypatch, count=4, shade=10):
    no_scan(monkeypatch)
    media_id = upload(client, tmp_path, shade)
    add(client, monkeypatch, media_id, [target(i) for i in range(1, count + 1)], sample='样本1')
    judge(client, media_id, ['T0001', 'T0002'], {'T0001': 'yes', 'T0002': 'yes'})
    return media_id


def settle(client, media_id, rid, key, value, dropped=(), revision=0):
    return client.put(f'/api/flow/media/{media_id}/recheck/{rid}/settles/{key}', json={'answer': value, 'dropped': list(dropped), 'revision': revision})


def label_count(client, media_id, key):
    job = export(client, [media_id], key)
    assert job['state'] == 'ready', job
    return job['counts']['sperm']


def test_recheck_is_optional_and_discarding_it_changes_nothing(client, tmp_path, monkeypatch):
    media_id = finished(client, tmp_path, monkeypatch)
    before = label_count(client, media_id, 'a')  # Exported without any re-check.
    started = client.post(f'/api/flow/media/{media_id}/recheck').json()
    assert client.post(f'/api/flow/media/{media_id}/recheck').json()['id'] == started['id']
    assert wait(client, media_id)['state'] == 'done'  # Still finished, still exportable.
    judge_again(client, media_id, [], {})
    assert label_count(client, media_id, 'b') == before
    assert answer(client, media_id, 'T0001', 'no', revision=1).json()['code'] == 'FLOW_RECHECKING'
    assert client.post(f'/api/flow/media/{media_id}/find', json={'force': True}).json()['code'] == 'FLOW_RECHECKING'
    assert client.delete(f"/api/flow/media/{media_id}/recheck/{started['id']}").status_code == 200
    assert get(client, media_id)['recheck'] is None and label_count(client, media_id, 'c') == before
    assert answer(client, media_id, 'T0001', 'no', revision=1).status_code == 200
    assert client.post(f'/api/flow/media/{media_id}/recheck').json()['id'] != started['id']


def test_recheck_shows_nothing_of_the_first_round_until_it_is_complete(client, tmp_path, monkeypatch):
    media_id = finished(client, tmp_path, monkeypatch, count=30)
    rid = client.post(f'/api/flow/media/{media_id}/recheck').json()['id']
    detail = recheck_detail(client, media_id)
    assert detail['picks'] == {} and detail['answers'] == {} and detail['second'] is None and detail['differences'] is None
    assert [t['key'] for p in detail['pages'] for t in p] != [t['key'] for p in get(client, media_id)['pages'] for t in p]
    assert client.post(f'/api/flow/media/{media_id}/recheck/{rid}/compare').json()['code'] == 'FLOW_NOT_DONE'
    assert settle(client, media_id, rid, 'T0001', 'yes').status_code == 409
    assert client.get(f'/api/flow/media/{media_id}/strips/T0001', params={'run': rid}).status_code == 200


def test_differences_are_settled_and_only_then_replace_the_labels(client, tmp_path, monkeypatch):
    media_id = finished(client, tmp_path, monkeypatch)
    before = label_count(client, media_id, 'a')
    rid = client.post(f'/api/flow/media/{media_id}/recheck').json()['id']
    run = judge_again(client, media_id, ['T0001', 'T0002', 'T0003'], {'T0001': ('yes', []), 'T0002': ('yes', [4]), 'T0003': ('yes', [])})
    shown = client.post(f'/api/flow/media/{media_id}/recheck/{rid}/compare').json()
    assert [d['key'] for d in shown['differences']] == ['T0002', 'T0003'] and shown['recheck']['state'] == 'settling'
    assert answer(client, media_id, 'T0003', 'no', revision=1, run=run).json()['code'] == 'FLOW_RECHECK_SHOWN'
    assert settle(client, media_id, rid, 'T0001', 'no').status_code == 409  # Agreed: nothing to settle.
    assert settle(client, media_id, rid, 'T0003', 'yes').json()['recheck']['state'] == 'settling'
    assert label_count(client, media_id, 'b') == before
    done = settle(client, media_id, rid, 'T0002', 'yes', [4]).json()['recheck']
    assert done['state'] == 'done' and done['stats'] == {'sperm': 3, 'bothYes': 2, 'firstMissed': 1, 'secondMissed': 0, 'differences': 2} and done['samePerson']
    assert label_count(client, media_id, 'c') == before + 6 - 1  # One sperm more, one frame less.
    assert wait(client, media_id)['progress']['yes'] == 3
    misses = client.get('/api/flow/misses').json()['recheck']
    assert misses['videos'] == 1 and misses['firstMissed'] == 1
    assert settle(client, media_id, rid, 'T0003', 'no').json()['code'] == 'FLOW_CONFLICT'
    assert settle(client, media_id, rid, 'T0003', 'no', revision=1).status_code == 200 and label_count(client, media_id, 'd') == before - 1


def test_full_agreement_needs_no_settling(client, tmp_path, monkeypatch):
    media_id = finished(client, tmp_path, monkeypatch)
    rid = client.post(f'/api/flow/media/{media_id}/recheck').json()['id']
    judge_again(client, media_id, ['T0001', 'T0002'], {'T0001': ('yes', []), 'T0002': ('yes', [])})
    assert client.post(f'/api/flow/media/{media_id}/recheck/{rid}/compare').json()['recheck']['state'] == 'done'


def send(client, blob, folder=''):
    return client.post('/api/flow/import-package', files={'file': ('p.zip', blob, 'application/zip')}, data={'folderPath': folder})


def package(client, media_id, key='p', **extra):
    return client.get(export(client, [media_id], key, includeSourceVideos=True, **extra)['downloadUrl']).content


def changed(blob, name, edit):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(out, 'w') as z:
        for entry in source.namelist():
            data = source.read(entry)
            z.writestr(entry, json.dumps(edit(json.loads(data))) if entry == name else data)
    return out.getvalue()


def test_a_package_comes_back_judged_and_can_be_rechecked_or_exported_at_once(client, tmp_path, monkeypatch):
    media_id = finished(client, tmp_path, monkeypatch)
    before, blob = label_count(client, media_id, 'a'), package(client, media_id)
    again = send(client, blob).json()
    assert not again['imported'] and '服务器已有' in again['skipped'][0]['reason']
    client.delete('/api/track/media/' + media_id)
    assert client.get('/api/flow/misses').json()['yes'] == 0
    report = send(client, blob, '别人/样本1').json()
    new = report['imported'][0]['mediaId']
    item = wait(client, new)
    assert item['state'] == 'done' and item['imported'] and item['folderPath'] == '别人/样本1' and item['progress']['yes'] == 2 and item['progress']['lastActorName'] is None
    assert client.get('/api/track/media').json()['items'] == []
    assert label_count(client, new, 'b') == before  # No re-check needed.
    strip = client.get(f'/api/flow/media/{new}/strips/T0001', params={'run': item['progress']['runId']})
    assert cv2.imdecode(np.frombuffer(strip.content, np.uint8), 0).shape[1] == ft.STRIP_CELL * 6
    rid = client.post(f'/api/flow/media/{new}/recheck').json()['id']
    judge_again(client, new, ['T0001'], {'T0001': ('yes', [])})
    assert [d['key'] for d in client.post(f'/api/flow/media/{new}/recheck/{rid}/compare').json()['differences']] == ['T0002']


def test_settled_answers_travel_with_the_package(client, tmp_path, monkeypatch):
    media_id = finished(client, tmp_path, monkeypatch)
    rid = client.post(f'/api/flow/media/{media_id}/recheck').json()['id']
    judge_again(client, media_id, ['T0001', 'T0002', 'T0003'], {k: ('yes', []) for k in ('T0001', 'T0002', 'T0003')})
    client.post(f'/api/flow/media/{media_id}/recheck/{rid}/compare')
    settle(client, media_id, rid, 'T0003', 'yes')
    blob = package(client, media_id)
    client.delete('/api/track/media/' + media_id)
    new = send(client, blob).json()['imported'][0]['mediaId']
    assert wait(client, new)['progress']['yes'] == 3


def test_damaged_or_foreign_packages_are_refused_without_leaving_anything(client, tmp_path, monkeypatch):
    media_id = finished(client, tmp_path, monkeypatch)
    blob = package(client, media_id)
    plain = client.get(export(client, [media_id], 'plain')['downloadUrl']).content
    client.delete('/api/track/media/' + media_id)

    def move(data):
        data['targets'][0]['frames'][0][1] += 5
        return data

    def wild(data):
        data['targets'][0]['shown'] = [2, 2]
        return data

    def missing(data):
        data['answers'].pop('T0001')
        data['picked'].append('T0003')
        return data
    for bad, words in ((plain, '包含原视频'), (changed(blob, 'targets/v000.json', move), '目标记录'), (changed(blob, 'targets/v000.json', wild), '目标记录'),
                       (changed(blob, 'judgements/v000.json', missing), '没有判完'),
                       (changed(blob, 'judgements/v000.json', lambda d: {**d, 'answers': {'T0099': {'answer': 'yes', 'dropped': []}}}), '对不上')):
        report = send(client, bad).json()
        assert not report['imported'] and words in report['skipped'][0]['reason'], report
    assert send(client, b'nope').status_code == 422
    assert send(client, changed(blob, 'manifest.json', lambda d: {**d, 'kind': 'x'})).status_code == 422
    assert client.get('/api/flow/media').json()['items'] == [] and client.get('/api/track/media').json()['items'] == []


def test_an_interrupted_import_finishes_on_retry(client, tmp_path, monkeypatch):
    media_id = finished(client, tmp_path, monkeypatch)
    blob = package(client, media_id)
    client.delete('/api/track/media/' + media_id)
    real = ft.redraw
    monkeypatch.setattr(ft, 'redraw', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('磁盘满')))
    new = send(client, blob).json()['imported'][0]['mediaId']
    assert wait(client, new)['state'] == 'failed'
    monkeypatch.setattr(ft, 'redraw', real)
    client.post(f'/api/flow/media/{new}/find', json={})
    assert wait(client, new)['state'] == 'done'
