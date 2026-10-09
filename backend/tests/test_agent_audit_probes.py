"""Independent test-engineer audit probes (2026-10-09).

These encode the *documented* expectations (AGENTS.md / docs/TESTING.md) for
state combinations that the fixed regression entry does not exercise directly.
A failure here is a finding to report; it is deliberately not fixed in place.

Scope note: the assertions describe the intended contract. They are NOT tuned
to whatever the current implementation happens to do.
"""
import json

from fastapi.testclient import TestClient

from app import main, tracker, annotation_state
from app.auth import sign_jwt
from .test_media_reimport import imports, upload, seed_workspace, reset
from .test_review_workflow import task  # noqa: F401  (fixture dependency of imports)

RESULT = 'tracker_results.json'
MARKER = annotation_state.RESULT_META_FILE


def _write_rows(directory, rows):
    tracker._write_jsonl(directory / RESULT, rows)


def test_lost_generated_result_is_not_presented_as_untracked(imports):
    """A generated result that disappears must not look like 'never tracked'.

    docs/TESTING.md: 已生成文件丢失 must stay an error; the media list must not
    silently advertise the video as having no result, otherwise the operator is
    told "尚未追踪" while a real result was lost.
    """
    client, content = imports
    mid = upload(client, content).json()['mediaId']
    directory = main.media_dir(mid)
    _write_rows(directory, [{'frame_index': 0, 'source_frame_index': 0,
                             'objects': [{'object_id': 1, 'bbox': [1, 2, 10, 12]}]}])
    assert client.get('/api/track/result/' + mid).json()['state'] == 'available'
    # Simulate storage loss after the marker recorded a real generation.
    (directory / RESULT).unlink()
    assert (directory / MARKER).is_file(), 'marker must record the lost generation'
    assert client.get('/api/track/result/' + mid).status_code == 409
    entry = next(i for i in client.get('/api/track/media').json()['items'] if i['mediaId'] == mid)
    # The list must not claim "no tracking result" for a lost/recoverable result.
    assert entry.get('hasTrackingResult') is not False, (
        'media list hides a lost generated result and reports it as untracked'
    )


def test_corrupt_result_can_still_be_deleted_and_reuploaded(imports):
    """Read failure must not make the media unmanageable (recovery pairing)."""
    client, content = imports
    mid = upload(client, content).json()['mediaId']
    directory = main.media_dir(mid)
    (directory / RESULT).write_text('{broken json')
    assert client.get('/api/track/result/' + mid).status_code == 500
    deleted = client.delete('/api/track/media/' + mid)
    assert deleted.status_code == 200, deleted.text
    mid2 = upload(client, content).json()['mediaId']
    assert client.get('/api/track/result/' + mid2).json()['state'] == 'not_generated'


def test_workspace_replay_survives_a_fresh_client_with_same_key(imports):
    """Save-succeeded-but-response-lost: the original key must replay the same
    committed response even after the process/page is rebuilt."""
    client, content = imports
    mid = upload(client, content).json()['mediaId']
    body = {'expectedRevision': 0, 'manualAnnotations': [
        {'objectId': 1, 'source': 'manual', 'frameIndex': 0,
         'bbox': {'x': 10, 'y': 10, 'width': 20, 'height': 20}}]}
    first = client.put('/api/track/workspace/' + mid, json=body,
                       headers={'Idempotency-Key': 'lost-response-1'})
    assert first.status_code == 200, first.text
    fresh = TestClient(main.app)
    fresh.headers.update({'Authorization': 'Bearer ' + sign_jwt({'uid': 1})})
    replay = fresh.put('/api/track/workspace/' + mid, json=body,
                       headers={'Idempotency-Key': 'lost-response-1'})
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json(), 'replayed response must match the committed one'
    assert fresh.get('/api/track/workspace/' + mid).json()['revision'] == first.json()['revision']


def test_idempotency_receipt_is_scoped_per_account(imports):
    """Two accounts using the same retry key must not share a receipt."""
    client, content = imports
    mid = upload(client, content).json()['mediaId']
    body = {'expectedRevision': 0, 'manualAnnotations': []}
    assert client.put('/api/track/workspace/' + mid, json=body,
                      headers={'Idempotency-Key': 'shared-key'}).status_code == 200
    other = TestClient(main.app)
    other.headers.update({'Authorization': 'Bearer ' + sign_jwt({'uid': 2})})
    # User 2 has never written; the same key must perform its own write against
    # the current revision, not replay user 1's receipt.
    response = other.put('/api/track/workspace/' + mid, json={'expectedRevision': 1,
                        'manualAnnotations': []}, headers={'Idempotency-Key': 'shared-key'})
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 2