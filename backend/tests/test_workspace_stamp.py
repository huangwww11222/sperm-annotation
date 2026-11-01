from app import main
from .test_annotation_controls import controls, save
from .test_review_workflow import task


def test_stamp_follows_revision_and_reports_other_users_tracking(controls, monkeypatch):
    client, root, state = controls
    first = client.get('/api/track/workspace/control-video/stamp').json()
    assert first['revision'] == 0 and first['tracking'] is None and first['trackingStamp']
    assert save(client, {**state, 'manualAnnotations': state['manualAnnotations'] + [{**state['manualAnnotations'][0], 'frameIndex': 1}]}).status_code == 200
    second = client.get('/api/track/workspace/control-video/stamp').json()
    assert second['revision'] == 1 and second['updatedBy'] == 1
    monkeypatch.setattr(main, 'TASKS', {'t': {'status': 'queued', 'mediaId': 'control-video', 'userId': 1}})
    assert client.get('/api/track/workspace/control-video/stamp').json()['tracking']['status'] == 'queued'
    monkeypatch.setattr(main, 'TASKS', {'t': {'status': 'running', 'mediaId': 'other-video', 'userId': 1}})
    assert client.get('/api/track/workspace/control-video/stamp').json()['tracking'] is None


def test_stamp_requires_login_and_known_media(controls):
    client, _, _ = controls
    assert client.get('/api/track/workspace/missing/stamp').status_code == 404
    client.headers.pop('Authorization')
    assert client.get('/api/track/workspace/control-video/stamp').status_code == 401
