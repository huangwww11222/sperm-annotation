import io
import json
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import annotation_completion, annotation_export as direct, db, main, package_import, training_export as export
from app.auth import sign_jwt
from .test_annotation_export import review_pass
from .test_review_completion import source, body, INFO
from .test_review_workflow import task


@pytest.fixture
def package(source, monkeypatch, tmp_path):
    """A real training ZIP exported with its original video, plus an empty media library."""
    from app import api_common as routes, confirmation_workflow as confirmation
    from .test_training_export import video_file
    video = video_file(source, n=3)
    with db.connect() as c:
        confirmation.migrate(c)
        export.migrate(c)
    original_media_dir = main.media_dir
    monkeypatch.setattr(routes, 'completion_context', lambda mid: (source, video, INFO))
    monkeypatch.setattr(main, 'media_dir', lambda mid: source)
    monkeypatch.setattr(export, 'DATASET_EXPORT_DIR', tmp_path / 'packages')
    monkeypatch.setattr(export, 'enqueue', lambda eid: None)
    review_pass(source)
    final = direct.freeze(source, video, 'complete-media', 1, 'snapshot', body(source), INFO)['finalVersionId']
    job = export.create(1, 'export', dict(finalVersionIds=[final], format='yolo', splitRatio=.8, includeSourceVideos=True))
    export.run(job['exportId'])
    zip_path = tmp_path / 'dataset.zip'
    shutil.copyfile(export.download(job['exportId'], 1), zip_path)
    with zipfile.ZipFile(zip_path) as z:
        frames = json.loads(z.read(f'provenance/{final}.json'))['frames']
    # Import into a separate, empty library on this "other" server.
    monkeypatch.setattr(main, 'media_dir', original_media_dir)
    monkeypatch.setattr(main, 'TRACK_DATA_DIR', tmp_path / 'library')
    monkeypatch.setattr(main, 'LEGACY_TRACK_DATA_DIR', tmp_path / 'legacy')
    monkeypatch.setattr(main, 'TASKS', {})
    (tmp_path / 'library').mkdir()
    return zip_path, frames, tmp_path


def run(zip_path, tmp_path, name='work'):
    work = tmp_path / name
    work.mkdir()
    return package_import.import_package(zip_path, 1, work)


def rewrite(zip_path, target, change):
    with zipfile.ZipFile(zip_path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    change(files)
    with zipfile.ZipFile(target, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return target


def test_exported_package_round_trips_boxes_as_unreviewed_manual_annotations(package):
    zip_path, frames, tmp_path = package
    result = run(zip_path, tmp_path)
    assert result['skipped'] == [] and len(result['imported']) == 1
    item = result['imported'][0]
    assert item['videoName'] == 'original.avi' and item['frameCount'] == 3
    directory = main.media_dir(item['mediaId'])
    meta = json.loads((directory / 'media.json').read_text())
    assert meta['importedFrom']['finalVersionId'].startswith('annexp_') and meta['importedFrom']['importedBy'] == 1
    # The effective content seen by review/export equals the exported final boxes.
    _, imported = annotation_completion.source(directory, 1, main.source_media_info(directory), require_author=False)
    exported = {f['frameIndex']: {o['objectId']: o['bbox'] for o in f['objects']} for f in frames}
    for fi, objects in exported.items():
        if objects:
            assert {oid: pytest.approx(box) for oid, box in objects.items()} == {oid: o['bbox'] for oid, o in imported[fi].items()}
        else:
            assert fi not in imported, 'declared-empty frames stay unknown until a reviewer confirms them'
    # The sender's review pass is not imported: export stays locked until reviewed here.
    from app import annotation_review as review
    assert review.status(item['mediaId'], directory, main.source_media_info(directory))['status'] == 'none'


def test_reimporting_the_same_package_never_overwrites_the_existing_video(package):
    zip_path, _, tmp_path = package
    first = run(zip_path, tmp_path)['imported'][0]
    again = run(zip_path, tmp_path, 'again')
    assert again['imported'] == [] and again['skipped'][0]['mediaId'] == first['mediaId']
    assert '未覆盖' in again['skipped'][0]['reason']


def test_package_without_original_video_is_explained(package):
    zip_path, _, tmp_path = package
    def drop_video(files):
        manifest = json.loads(files['manifest.json'])
        files.pop(manifest['sources'][0]['videoFile'])
        manifest['sources'][0]['videoFile'] = None
        files['manifest.json'] = json.dumps(manifest).encode()
    result = run(rewrite(zip_path, tmp_path / 'novideo.zip', drop_video), tmp_path)
    assert result['imported'] == [] and '包含原视频' in result['skipped'][0]['reason']
    assert not any(p for p in (tmp_path / 'library').iterdir() if not p.name.startswith('_'))


@pytest.mark.parametrize('fault,reason', [
    ('video', '校验失败'), ('box', '框无效'), ('frames', '帧数'), ('path', '路径无效'),
])
def test_damaged_package_creates_no_media(package, fault, reason):
    zip_path, _, tmp_path = package
    def damage(files):
        manifest = json.loads(files['manifest.json'])
        source = manifest['sources'][0]
        provenance = f"provenance/{source['finalVersionId']}.json"
        data = json.loads(files[provenance])
        if fault == 'video':
            files[source['videoFile']] = files[source['videoFile']][:-10] + b'0' * 10
        elif fault == 'box':
            next(f for f in data['frames'] if f['objects'])['objects'][0]['bbox'] = [10, 10, 9999, 20]
        elif fault == 'frames':
            data['frames'].pop()
        else:
            source['videoFile'] = '../../etc/passwd'
        files[provenance] = json.dumps(data).encode()
        files['manifest.json'] = json.dumps(manifest).encode()
    result = run(rewrite(zip_path, tmp_path / 'damaged.zip', damage), tmp_path)
    assert result['imported'] == [] and reason in result['skipped'][0]['reason']
    assert not any(p for p in (tmp_path / 'library').iterdir() if not p.name.startswith('_'))


def test_failed_workspace_write_removes_the_half_imported_video(package, monkeypatch):
    zip_path, _, tmp_path = package
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(package_import.annotation_state, 'write_state', fail)
    result = run(zip_path, tmp_path)
    assert result['imported'] == [] and '撤销' in result['skipped'][0]['reason']
    assert not any(p for p in (tmp_path / 'library').iterdir() if not p.name.startswith('_'))


def test_import_route_requires_login_and_rejects_non_packages(package):
    zip_path, _, _ = package
    client = TestClient(main.app)
    upload = lambda data: client.post('/api/track/import-package', files={'file': ('dataset.zip', data, 'application/zip')})
    assert upload(zip_path.read_bytes()).status_code == 401
    client.headers['Authorization'] = 'Bearer ' + sign_jwt({'uid': 1})
    assert upload(b'not a zip').status_code == 422
    other = io.BytesIO()
    with zipfile.ZipFile(other, 'w') as z:
        z.writestr('data.yaml', 'nc: 1')
    assert upload(other.getvalue()).status_code == 422
    response = upload(zip_path.read_bytes())
    assert response.status_code == 201 and len(response.json()['imported']) == 1
    listed = client.get('/api/track/media').json()
    assert any(m['mediaId'] == response.json()['imported'][0]['mediaId'] for m in (listed.get('items', listed) if isinstance(listed, dict) else listed))


def test_new_video_never_reuses_an_id_whose_review_history_survived_deletion(package):
    # review_pass in the fixture left a passed review for 'complete-media'; its directory is gone here.
    assert not main.media_dir('complete-media').exists()
    assert main.allocate_media_id('complete-media') == 'complete-media_001'
    assert main.allocate_media_id('fresh-video') == 'fresh-video'
