"""Media deletion uses isolated files/DB, including real I/O and error logs."""
import logging
import cv2
import numpy as np
import shutil

import pytest
from fastapi.testclient import TestClient
from app import main
from app.auth import sign_jwt
from .test_review_workflow import task


@pytest.fixture
def media(tmp_path, monkeypatch, task):
    monkeypatch.setattr(main, 'TRACK_DATA_DIR', tmp_path / 'media')
    monkeypatch.setattr(main, 'LEGACY_TRACK_DATA_DIR', tmp_path / 'legacy')
    monkeypatch.setattr(main, 'TASKS', {})
    root = main.TRACK_DATA_DIR / 'delete-fixture'
    root.mkdir(parents=True)
    writer = cv2.VideoWriter(str(root / 'video.avi'), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
    writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    (root / 'workspace_state.json').write_text('{}')
    client = TestClient(main.app)
    client.headers['Authorization'] = 'Bearer ' + sign_jwt({'uid': 1})
    return client, root


def test_delete_is_persistent_and_does_not_remove_same_named_other_media(media, caplog):
    client, root = media
    sibling = root.with_name(root.name + '_001')
    shutil.copytree(root, sibling)
    with caplog.at_level(logging.INFO, logger='review.media'):
        response = client.delete('/api/track/media/' + root.name)
    assert response.status_code == 200 and response.json()['deleted'] is True
    assert not root.exists() and sibling.exists()
    # A fresh request/client does not restore the video.
    reloaded = TestClient(main.app)
    reloaded.headers.update(client.headers)
    assert [m['mediaId'] for m in reloaded.get('/api/track/media').json()['items']] == [sibling.name]
    assert 'media.deleted' in caplog.text and root.name in caplog.text


def test_failed_filesystem_delete_returns_error_and_log_not_false_success(media, monkeypatch, caplog):
    client, root = media
    def denied(*args, **kwargs):
        if not kwargs.get('ignore_errors'):
            raise PermissionError('fixture read-only directory')
    monkeypatch.setattr(shutil, 'rmtree', denied)
    response = client.delete('/api/track/media/' + root.name)
    assert response.status_code == 500
    assert root.exists()
    assert 'media.delete_failed' in caplog.text and 'fixture read-only directory' in caplog.text


def test_review_baseline_keeps_source_immutable(media):
    client, root = media
    protected = root.with_name('test-video')
    shutil.copytree(root, protected)
    response = client.delete('/api/track/media/test-video')
    assert response.status_code == 409 and '审查基准' in response.json()['detail']
    assert protected.exists()


def test_tracking_blocks_delete(media, monkeypatch):
    client, root = media
    monkeypatch.setattr(main, 'TASKS', {'test': {'status': 'running'}})
    assert client.delete('/api/track/media/' + root.name).status_code == 409
    assert root.exists()


def test_legacy_copy_cannot_reappear_after_deleting_shadowing_directory(media):
    client, root = media
    legacy = main.LEGACY_TRACK_DATA_DIR / root.name
    shutil.copytree(root, legacy)
    response = client.delete('/api/track/media/' + root.name)
    assert response.status_code == 409 and '同名目录' in response.json()['detail']
    assert root.exists() and legacy.exists()


def test_legacy_only_media_can_be_deleted(media):
    client, root = media
    legacy = main.LEGACY_TRACK_DATA_DIR / 'legacy-only'
    shutil.copytree(root, legacy)
    assert client.delete('/api/track/media/legacy-only').status_code == 200
    assert not legacy.exists() and root.exists()


def test_delete_requires_auth(media):
    client, root = media
    client.headers.pop('Authorization')
    assert client.delete('/api/track/media/' + root.name).status_code == 401
    assert root.exists()


@pytest.mark.parametrize('media_id', ['.', '..', '../delete-fixture', 'sub/delete-fixture'])
def test_delete_rejects_noncanonical_ids_without_touching_storage(media, media_id):
    client, root = media
    with pytest.raises(main.HTTPException) as error:
        main.delete_media(media_id, {'uid': 1})
    assert error.value.status_code == 400
    assert root.exists()
