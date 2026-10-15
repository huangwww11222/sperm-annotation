"""A selected object's normal judgement does not approve its neighbour.

All filesystem state is pytest temporary data. Real feedback publication,
replay, and pause state are used; inference is deliberately not involved.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2

import pytest
from fastapi import HTTPException

from app import annotation_state, tracker
from app.services import tracking_image_evidence
from app.services.tracking_evidence import TrackingEvidenceProbe
from app.services.anomaly_detector import ManualBaseline
from app.video_frames import FrameReadError, source_signature
from .test_tracking_confirmation_policy import _approval, _save
from .test_tracking_evidence import _scene, _region
from .test_annotation_controls import controls, task, feedback


def setup_pair(root):
    state, body, _ = _approval(root, reasons=['bbox_overlap_with=9 coverage=0.951 HARD'])
    row = json.loads((root / 'tracker_results.json').read_text())
    row['objects'] = row['objects'][:2]
    row['objects'][1].update(anomaly_level='anomaly',
                             anomaly_reasons=['bbox_overlap_with=7 coverage=0.951 HARD'],
                             anomaly_details={})
    (root / 'tracker_results.json').write_text(json.dumps(row))
    (root / 'media.json').write_text(json.dumps({'width': 200, 'height': 200, 'frameCount': 20}))
    state['pausedAnomalies'] = [{'objectId': 7}, {'objectId': 9}]
    (root / 'workspace_state.json').write_text(json.dumps(state))
    return state, {**body, 'scope': 'object'}


def second_body(first):
    return {'expectedRevision': first['revision'], 'objectId': 9, 'frameIndex': 5,
            'decision': 'normal', 'calibrate': False, 'scope': 'object'}


def test_single_normal_does_not_approve_pair_or_resolve_other_object(tmp_path):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    assert first['pausedAnomalies'] == [{'objectId': 9}]
    assert first['lastPausedContext']['frameIndex'] == 5
    assert not first['trackingFeedbackEvents'][0].get('overlapReferences'), 'single normal must not approve neighbour'
    assert first['trackingFeedbackEvents'][0]['scope'] == 'object'
    assert first['trackingFeedbackEvents'][0]['geometryReference']['checkpoint'] is True
    assert _save(tmp_path, body) == first


def test_second_normal_approves_pair_only_after_both_same_pause_snapshots(tmp_path):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    second = _save(tmp_path, second_body(first), 'accept-neighbour')
    events = second['trackingFeedbackEvents']
    assert not events[0].get('overlapReferences'), 'immutable first approval must remain individual'
    assert second['pausedAnomalies'] == [] and second['lastPausedContext'] is None
    assert events[1]['pauseId'] == events[0]['pauseId']
    refs = events[1]['overlapReferences']
    assert [item['objectIds'] for item in refs] == [[7, 9]]
    assert refs[0]['approvalVersion'] == 2
    assert refs[0]['confirmedEventIds'] == [events[0]['id'], events[1]['id']]
    assert _save(tmp_path, body) == first


def test_partner_box_changed_between_normals_cannot_combine_old_acceptance(tmp_path):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    state = annotation_state.read_state(tmp_path)
    state['manualAnnotations'] = [{'source': 'manual', 'objectId': 7, 'frameIndex': 5,
                                  'bbox': {'x': 55, 'y': 50, 'width': 20, 'height': 5}}]
    (tmp_path / 'workspace_state.json').write_text(json.dumps(state))
    second = _save(tmp_path, second_body(first), 'accept-after-other-edit')
    assert not second['trackingFeedbackEvents'][-1].get('overlapReferences')


def test_new_pause_same_frame_cannot_combine_previous_pause_acceptance(tmp_path):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    pause = {'frame_index': 5, 'reasons': [{'object_id': 9, 'type': 'overlap',
             'reasons': ['bbox_overlap_with=7 coverage=0.951 HARD'], 'details': {}}]}
    annotation_state.publish_tracking_pause(tmp_path, tmp_path.name, pause)
    second = _save(tmp_path, second_body(first), 'accept-new-pause')
    assert not second['trackingFeedbackEvents'][-1].get('overlapReferences')


def test_frame_normal_is_explicit_dual_approval_in_one_atomic_write(tmp_path):
    _, body = setup_pair(tmp_path)
    response = _save(tmp_path, {**body, 'scope': 'frame'})
    events = response['trackingFeedbackEvents']
    assert len(events) == 2 and response['pausedAnomalies'] == []
    assert all(event['overlapReferences'][0]['approvalVersion'] == 2 for event in events)
    assert len({event['pauseId'] for event in events}) == 1


def test_failed_second_approval_keeps_other_pending_and_replays_first_exactly(tmp_path, monkeypatch):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    before = (tmp_path / 'workspace_state.json').read_bytes()
    original = Path.replace
    def reject(path, target):
        if Path(target) == tmp_path / 'workspace_state.json':
            raise OSError('injected second object feedback replace failure')
        return original(path, target)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', reject)
        with pytest.raises(HTTPException) as exc:
            _save(tmp_path, second_body(first), 'accept-other')
        assert exc.value.status_code == 500
    assert (tmp_path / 'workspace_state.json').read_bytes() == before
    assert _save(tmp_path, body) == first
    second = _save(tmp_path, second_body(first), 'accept-other')
    assert second['pausedAnomalies'] == []
    assert _save(tmp_path, second_body(first), 'accept-other') == second


@pytest.mark.parametrize('decision', ['reset', 'corrected'])
def test_newer_partner_decision_cannot_use_an_older_normal_for_pair(tmp_path, decision):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    state = annotation_state.read_state(tmp_path)
    state['trackingFeedbackEvents'].append({'objectId': 7, 'frameIndex': 5, 'decision': decision})
    (tmp_path / 'workspace_state.json').write_text(json.dumps(state))
    second = _save(tmp_path, second_body(first), 'after-partner-decision')
    assert not second['trackingFeedbackEvents'][-1].get('overlapReferences')


@pytest.mark.parametrize('field,value', [
    ('deletedObjectIds', [7]),
    ('deletedFrameObjects', [{'objectId': 7, 'frameIndex': 5}]),
    ('manualAnnotations', [{'source': 'manual', 'objectId': 7, 'frameIndex': 5,
                            'bbox': {'x': 55, 'y': 50, 'width': 20, 'height': 5}}]),
])
def test_edit_or_delete_undo_starts_new_individual_confirmation_snapshot(tmp_path, field, value):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    saved = annotation_state.read_state(tmp_path)
    changed = {**saved, field: value}
    annotation_state.preserve_pause(saved, changed, {})
    # Undo to exactly equal boxes must not reassemble old individual decisions.
    restored = {**changed, field: saved.get(field, [])}
    annotation_state.preserve_pause(changed, restored, {})
    (tmp_path / 'workspace_state.json').write_text(json.dumps(restored))
    second = _save(tmp_path, second_body(first), 'after-change-undo')
    assert not second['trackingFeedbackEvents'][-1].get('overlapReferences')


def test_single_normal_uses_saved_manual_box_and_does_not_learn_old_ai_motion(tmp_path):
    state, body = setup_pair(tmp_path)
    state['manualAnnotations'] = [{'source': 'manual', 'objectId': 7, 'frameIndex': 5,
                                  'bbox': {'x': 55, 'y': 50, 'width': 20, 'height': 5}}]
    (tmp_path / 'workspace_state.json').write_text(json.dumps(state))
    response = _save(tmp_path, {**body, 'calibrate': True})
    event = response['trackingFeedbackEvents'][-1]
    assert event['geometryReference']['bbox'] == [110, 100, 150, 110]
    assert not event.get('identityReference')
    assert response['normalMotionSamples'] == []
    assert annotation_state.read_state(tmp_path)['manualAnnotations'] == state['manualAnnotations']


def test_client_identity_reference_cannot_be_promoted_to_trusted_subject(tmp_path):
    _, body = setup_pair(tmp_path)
    response = _save(tmp_path, {**body, 'identityReference': {'version': 2, 'status': 'reliable',
                         'point': [127, 105]}, 'pauseId': 'client-pause', 'pairSnapshots': {'9': 'client-snapshot'}})
    event = response['trackingFeedbackEvents'][-1]
    assert not event.get('identityReference')
    assert event['pauseId'] != 'client-pause'
    assert event['pairSnapshots']['9'] != 'client-snapshot'


def setup_native_identity(root):
    state, body, _ = _approval(root, reasons=['image_foreign_core HARD'])
    image, mask, box = _scene()
    video = root / 'native.avi'
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 30, (320, 240))
    for _ in range(6):
        writer.write(image)
    writer.release()
    reader = cv2.VideoCapture(str(video))
    ok, decoded = reader.read()
    reader.release()
    assert ok
    probe = TrackingEvidenceProbe(fps=30, frame_width=320, frame_height=240)
    probe.source_signature = source_signature(video)
    probe.prime(0, decoded, {7: box})
    evidence = probe.observe(5, decoded, {7: box}, {7: _region(mask)})[7]
    assert evidence['identityReference']['originFrameIndex'] == 0
    row = {'source_frame_index': 5, 'objects': [{'object_id': 7, 'bbox': box,
           'mask_area': int(mask.sum()), 'anomaly_level': 'anomaly',
           'anomaly_reasons': ['image_foreign_core HARD'],
           'anomaly_details': {'identity_evidence': evidence}}]}
    (root / 'tracker_results.json').write_text(json.dumps(row))
    (root / 'media.json').write_text(json.dumps({'width': 320, 'height': 240, 'fps': 30,
                                                'frameCount': 6, 'videoName': 'native.avi'}))
    state['generationId'] = 'native-generation'
    (root / 'workspace_state.json').write_text(json.dumps(state))
    return state, body, row, video


def test_native_identity_is_separate_from_current_geometry_and_reads_one_frame(tmp_path, monkeypatch):
    state, body, row, video = setup_native_identity(tmp_path)
    original = tracking_image_evidence.open_capture_at
    reads = []
    def capture(path, frame):
        reads.append((Path(path), frame))
        return original(path, frame)
    monkeypatch.setattr(tracking_image_evidence, 'open_capture_at', capture)
    response = _save(tmp_path, body)
    event = response['trackingFeedbackEvents'][-1]
    identity = event['identityReference']
    assert reads == [(video, 5)]
    assert identity['originFrameIndex'] == 0 and identity['frameIndex'] == 5
    assert identity['generationId'] == 'native-generation'
    assert identity['sourceSignature'] == source_signature(video)
    assert identity['bbox'] == event['geometryReference']['bbox'] == row['objects'][0]['bbox']
    assert response['confirmedReference']['identityReference'] == identity
    assert annotation_state.read_state(tmp_path)['manualAnnotations'] == state['manualAnnotations']
    assert _save(tmp_path, body) == response
    assert len(reads) == 1  # receipt replay does not rerun image analysis


@pytest.mark.parametrize('field,value', [
    ('version', 1), ('source', 'native-frame-mask-v1'), ('frameIndex', 4),
    ('bbox', [0, 0, 20, 20]), ('sourceSignature', ['wrong-source']),
    ('appearance', {'polarity': 'dark'}),
])
def test_bad_or_old_subject_reference_is_not_rebuilt_from_a_new_strongest_peak(tmp_path, field, value):
    _, body, row, _ = setup_native_identity(tmp_path)
    row['objects'][0]['anomaly_details']['identity_evidence']['identityReference'][field] = value
    (tmp_path / 'tracker_results.json').write_text(json.dumps(row))
    response = _save(tmp_path, body)
    event = response['trackingFeedbackEvents'][-1]
    assert 'identityReference' not in event
    assert event['geometryReference']['checkpoint'] is True


def test_native_source_decode_failure_preserves_pause_and_exact_retry(tmp_path, monkeypatch):
    _, body, _, _ = setup_native_identity(tmp_path)
    before = (tmp_path / 'workspace_state.json').read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(tracking_image_evidence, 'open_capture_at',
                      lambda *args: (_ for _ in ()).throw(FrameReadError('injected native decode failure')))
        with pytest.raises(HTTPException) as exc:
            _save(tmp_path, body)
        assert exc.value.status_code == 503
    assert (tmp_path / 'workspace_state.json').read_bytes() == before
    response = _save(tmp_path, body)
    assert response['pausedAnomalies'] == []
    assert len(response['trackingFeedbackEvents']) == 1
    assert _save(tmp_path, body) == response


@pytest.mark.parametrize('field,value', [
    ('deletedObjectIds', [7]),
    ('deletedFrameObjects', [{'objectId': 7, 'frameIndex': 5}]),
    ('manualAnnotations', [{'source': 'manual', 'objectId': 7, 'frameIndex': 5,
                            'bbox': {'x': 55, 'y': 50, 'width': 20, 'height': 5}}]),
])
def test_fully_accepted_pair_is_not_revived_by_edit_or_delete_undo(tmp_path, field, value):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    _save(tmp_path, second_body(first), 'accept-neighbour')
    accepted = annotation_state.read_state(tmp_path)
    assert set(tracker._load_confirmed_overlap_references(accepted, 5, {})) == {(7, 9)}
    changed = {**accepted, field: value}
    annotation_state.preserve_pause(accepted, changed, {})
    restored = {**changed, field: accepted.get(field, [])}
    annotation_state.preserve_pause(changed, restored, {})
    assert tracker._load_confirmed_overlap_references(restored, 5, {}) == {}
    assert restored['trackingFeedbackEvents'] == accepted['trackingFeedbackEvents']


def test_pair_other_object_edit_and_name_change_do_not_retire_approved_members(tmp_path):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    _save(tmp_path, second_body(first), 'accept-neighbour')
    accepted = annotation_state.read_state(tmp_path)
    changed = {**accepted, 'manualAnnotations': [{'source': 'manual', 'objectId': 12, 'frameIndex': 6,
                          'name': 'unrelated', 'bbox': {'x': 10, 'y': 10, 'width': 5, 'height': 5}}]}
    annotation_state.preserve_pause(accepted, changed, {})
    assert set(tracker._load_confirmed_overlap_references(changed, 6, {})) == {(7, 9)}
    renamed = {**changed, 'manualAnnotations': [{**changed['manualAnnotations'][0], 'name': 'renamed'}]}
    annotation_state.preserve_pause(changed, renamed, {})
    assert renamed['_overlapReferenceEpochs'] == changed['_overlapReferenceEpochs']
    assert set(tracker._load_confirmed_overlap_references(renamed, 6, {})) == {(7, 9)}


def test_new_media_generation_or_missing_second_owner_never_restores_pair(tmp_path):
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    _save(tmp_path, second_body(first), 'accept-neighbour')
    accepted = annotation_state.read_state(tmp_path)
    assert set(tracker._load_confirmed_overlap_references(accepted, 5, {})) == {(7, 9)}
    assert tracker._load_confirmed_overlap_references({**accepted, 'generationId': 'another-generation'}, 5, {}) == {}
    assert tracker._load_confirmed_overlap_references({**accepted, 'trackingFeedbackEvents': accepted['trackingFeedbackEvents'][1:]}, 5, {}) == {}


def test_same_frame_new_manual_source_cannot_restore_old_subject_outside_new_box(tmp_path):
    _, body, _, _ = setup_native_identity(tmp_path)
    _save(tmp_path, body)
    accepted = annotation_state.read_state(tmp_path)
    assert tracker._load_identity_references(accepted, 5, {})[7]['point'][0] < 80
    # The public seed endpoint can publish true manual provenance at this same
    # native frame independently of ordinary workspace save/epoch bookkeeping.
    changed = ManualBaseline([180, 89, 269, 112], 5, source='manual')
    assert tracker._load_identity_references(accepted, 5, {7: changed}) == {}


@pytest.mark.parametrize('action', ['rewind', 'restart-branch'])
def test_later_start_cannot_revive_pair_from_discarded_future_branch(controls, monkeypatch, action):
    from app import main
    client, directory, _ = controls
    monkeypatch.setattr(main, 'SAM3_ENABLED', True)
    original_rows = annotation_state.read_rows(directory)
    approval = feedback(client, scope='frame', calibrate=False)
    assert approval.status_code == 200, approval.text
    accepted = annotation_state.read_state(directory)
    assert set(tracker._load_confirmed_overlap_references(accepted, 2, {})) == {(7, 9)}
    if action == 'rewind':
        cut = client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0})
    else:
        annotation_state.publish_tracking_pause(directory, 'control-video', {'frame_index': 2,
             'reasons': [{'object_id': 7, 'type': 'overlap', 'reasons': ['bbox_overlap_with=9 HARD'], 'details': {}}]})
        cut = client.post('/api/track/restart-branch/control-video', json={
                'expectedRevision': 1, 'expectedPausedFrame': 2, 'startFrame': 0,
                'confirmDiscardFuture': True}, headers={'Idempotency-Key': 'retire-future-pair'})
    assert cut.status_code == 200, cut.text
    assert cut.json()['removedRows'] == 2
    after_cut = annotation_state.read_state(directory)
    assert after_cut['trackingFeedbackEvents'] == accepted['trackingFeedbackEvents']
    assert after_cut['manualAnnotations'] == accepted['manualAnnotations']
    # Model output is simulated here. Real APIs/source publication and startup
    # loaders are used: recreate frames 1/2, then start again past the old check.
    recreated = original_rows[1:]
    for row in recreated:
        row['objects'][1]['bbox'] = [10.5, 10, 30.5, 30]
    with (directory / 'tracker_results.json').open('a') as stream:
        for row in recreated:
            stream.write(json.dumps(row) + '\n')
    assert [row['source_frame_index'] for row in annotation_state.read_rows(directory)] == [0, 1, 2]
    restarted = annotation_state.read_state(directory)
    baselines = tracker._load_latest_manual_baselines(directory / 'seed.json', 2)
    assert tracker._load_confirmed_overlap_references(restarted, 2, baselines) == {}, \
        'later startup revived a pair approved only in the discarded future branch'


@pytest.mark.parametrize('filename', ['tracker_results.json', 'workspace_state.json'])
def test_normal_rewind_each_file_failure_rolls_back_relations_and_retries(controls, monkeypatch, filename):
    from app import main, tracking_restart
    client, directory, _ = controls
    monkeypatch.setattr(main, 'SAM3_ENABLED', True)
    approved = feedback(client, scope='frame', calibrate=False)
    before = {name: (directory / name).read_bytes() for name in tracking_restart.FILES}
    original = tracking_restart._publish_file
    with monkeypatch.context() as patch:
        def fail(staged, target):
            if target.name == filename:
                raise OSError('injected normal rewind file publication failure')
            original(staged, target)
        patch.setattr(tracking_restart, '_publish_file', fail)
        failed = client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0})
        assert failed.status_code == 503
    assert {name: (directory / name).read_bytes() for name in before} == before
    assert feedback(client, scope='frame', calibrate=False).json() == approved.json()
    result = client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0})
    assert result.status_code == 200
    current = annotation_state.read_state(directory)
    assert current['revision'] == approved.json()['revision']
    assert current['_retiredTrackingRelationshipEventIds']
    assert feedback(client, scope='frame', calibrate=False).json() == approved.json()


def test_normal_rewind_sql_commit_failure_keeps_files_and_old_feedback_receipt(controls, monkeypatch):
    from app import main, tracking_restart
    client, directory, _ = controls
    monkeypatch.setattr(main, 'SAM3_ENABLED', True)
    approved = feedback(client, scope='frame', calibrate=False).json()
    before = {name: (directory / name).read_bytes() for name in tracking_restart.FILES}
    connect = tracking_restart.connect
    class RejectCommit:
        def __init__(self):
            self.connection = connect()
        def execute(self, *args):
            return self.connection.execute(*args)
        def commit(self):
            raise OSError('injected rewind SQL commit failure')
        def close(self):
            self.connection.close()
    with monkeypatch.context() as patch:
        patch.setattr(tracking_restart, 'connect', RejectCommit)
        response = client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0})
        assert response.status_code == 503
    assert {name: (directory / name).read_bytes() for name in before} == before
    assert feedback(client, scope='frame', calibrate=False).json() == approved
    assert client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0}).status_code == 200


def test_normal_rewind_uncommitted_journal_recovers_before_workspace_read(controls, monkeypatch):
    from app import main, tracking_restart
    client, directory, _ = controls
    monkeypatch.setattr(main, 'SAM3_ENABLED', True)
    feedback(client, scope='frame', calibrate=False)
    before = {name: (directory / name).read_bytes() for name in tracking_restart.FILES}
    publish = tracking_restart._publish_file
    with monkeypatch.context() as patch:
        patch.setattr(tracking_restart, 'recover_directory', lambda _: None)
        def half_publish(staged, target):
            if target.name == 'workspace_state.json':
                raise OSError('simulated normal rewind process interruption')
            publish(staged, target)
        patch.setattr(tracking_restart, '_publish_file', half_publish)
        assert client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0}).status_code == 503
        assert (directory / 'tracker_results.json').read_bytes() != before['tracker_results.json']
    annotation_state.read_state(directory)
    assert {name: (directory / name).read_bytes() for name in before} == before


def test_normal_rewind_same_frame_approval_geometry_and_revision_survive(controls, monkeypatch):
    from app import main
    client, directory, _ = controls
    monkeypatch.setattr(main, 'SAM3_ENABLED', True)
    approved = feedback(client, scope='frame', calibrate=False).json()
    response = client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 1})
    assert response.status_code == 200
    current = annotation_state.read_state(directory)
    assert current['revision'] == approved['revision']
    assert set(tracker._load_confirmed_overlap_references(current, 1, {})) == {(7, 9)}
    assert set(tracker._load_confirmed_geometry_references(current, 1, {})) == {7, 9}
    assert feedback(client, scope='frame', calibrate=False).json() == approved


def test_normal_rewind_sql_request_replay_never_truncates_new_output_again(controls):
    from app import tracking_restart
    _, directory, _ = controls
    feedback(controls[0], scope='frame', calibrate=False)
    first = tracking_restart.rewind(directory, 0, uid=1, request_key='normal-rewind-retry')
    with (directory / 'tracker_results.json').open('a') as stream:
        stream.write(json.dumps({'source_frame_index': 2, 'objects': [{'object_id': 7, 'bbox': [10, 10, 30, 30]}]}) + '\n')
    later = (directory / 'tracker_results.json').read_bytes()
    assert tracking_restart.rewind(directory, 0, uid=1, request_key='normal-rewind-retry') == first
    assert (directory / 'tracker_results.json').read_bytes() == later
    with pytest.raises(HTTPException) as exc:
        tracking_restart.rewind(directory, 1, uid=1, request_key='normal-rewind-retry')
    assert exc.value.status_code == 409


@pytest.mark.parametrize('fail', [False, True])
def test_standalone_rewind_has_no_account_or_database_dependency(tmp_path, monkeypatch, fail):
    from app import tracking_restart
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    _save(tmp_path, second_body(first), 'accept-neighbour')
    before = {name: (tmp_path / name).read_bytes() for name in tracking_restart.FILES}
    monkeypatch.setattr(tracking_restart, 'connect', lambda: (_ for _ in ()).throw(AssertionError('standalone accessed DB')))
    if fail:
        with monkeypatch.context() as patch:
            publish = tracking_restart._publish_file
            def reject(staged, target):
                if target.name == 'workspace_state.json':
                    raise OSError('standalone second file failure')
                publish(staged, target)
            patch.setattr(tracking_restart, '_publish_file', reject)
            with pytest.raises(HTTPException) as exc:
                tracking_restart.rewind(tmp_path, 0)
            assert exc.value.status_code == 503
        assert {name: (tmp_path / name).read_bytes() for name in before} == before
    result = tracking_restart.rewind(tmp_path, 0)
    assert result['removedRows'] == 1
    current = annotation_state.read_state(tmp_path)
    assert current['revision'] == 2 and current['_retiredTrackingRelationshipEventIds']
    assert not tracker._load_confirmed_overlap_references(current, 5, {})


def test_standalone_commit_marker_failure_restores_old_rows_and_relationships(tmp_path, monkeypatch):
    from app import tracking_restart
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    _save(tmp_path, second_body(first), 'accept-neighbour')
    before = {name: (tmp_path / name).read_bytes() for name in tracking_restart.FILES}
    write = tracking_restart._write
    with monkeypatch.context() as patch:
        def fail(path, data):
            if path.name == 'commit.tmp':
                raise OSError('injected standalone commit marker failure')
            write(path, data)
        patch.setattr(tracking_restart, '_write', fail)
        with pytest.raises(HTTPException) as exc:
            tracking_restart.rewind(tmp_path, 0)
        assert exc.value.status_code == 503
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert set(tracker._load_confirmed_overlap_references(annotation_state.read_state(tmp_path), 5, {})) == {(7, 9)}


def test_standalone_committed_cleanup_does_not_overwrite_later_editor_save(tmp_path, monkeypatch):
    from app import tracking_restart
    _, body = setup_pair(tmp_path)
    first = _save(tmp_path, body)
    _save(tmp_path, second_body(first), 'accept-neighbour')
    with monkeypatch.context() as patch:
        patch.setattr(tracking_restart, 'recover_directory', lambda _: None)
        result = tracking_restart.rewind(tmp_path, 0)
        assert result['removedRows'] == 1
        changed = json.loads((tmp_path / 'workspace_state.json').read_text())
        changed.update(currentFrame=9, revision=3)
        (tmp_path / 'workspace_state.json').write_text(json.dumps(changed))
    assert annotation_state.read_state(tmp_path)['revision'] == 3
    assert annotation_state.read_state(tmp_path)['currentFrame'] == 9


def test_legacy_rewind_without_workspace_does_not_create_empty_source_overlay(tmp_path):
    from app import tracking_restart
    (tmp_path / 'tracker_results.json').write_text('\n'.join(json.dumps({'source_frame_index': frame,
          'objects': [{'object_id': 7, 'bbox': [10, 10, 30, 30]}]}) for frame in (0, 1)))
    manual = tmp_path / 'annotations_frame_000000.json'
    manual.write_text('{"protected":"legacy manual source"}')
    before = manual.read_bytes()
    assert tracking_restart.rewind(tmp_path, 0)['removedRows'] == 1
    assert not (tmp_path / 'workspace_state.json').exists()
    assert manual.read_bytes() == before


def test_normal_rewind_only_invalidates_old_preview_after_source_commit(controls, monkeypatch):
    from app import main, tracking_restart
    client, directory, _ = controls
    monkeypatch.setattr(main, 'SAM3_ENABLED', True)
    feedback(client, scope='frame', calibrate=False)
    overlay = directory / 'tracker_overlay.mp4'
    overlay.write_bytes(b'old preview')
    publish = tracking_restart._publish_file
    with monkeypatch.context() as patch:
        def reject(staged, target):
            if target.name == 'workspace_state.json':
                raise OSError('injected source publication failure before preview invalidation')
            publish(staged, target)
        patch.setattr(tracking_restart, '_publish_file', reject)
        assert client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0}).status_code == 503
    assert overlay.read_bytes() == b'old preview'
    assert client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0}).status_code == 200
    assert not overlay.exists()


@pytest.mark.parametrize('render_fails', [False, True])
def test_preview_cleanup_failure_cannot_download_obsolete_cache(controls, monkeypatch, caplog, render_fails):
    from app import main, tracking_preview
    client, directory, _ = controls
    monkeypatch.setattr(main, 'SAM3_ENABLED', True)
    feedback(client, scope='frame', calibrate=False)
    overlay = directory / 'tracker_overlay.mp4'
    overlay.write_bytes(b'old preview')
    stamp = directory / 'tracker_overlay.meta.json'
    stamp.write_text(json.dumps({'signature': tracking_preview.signature(directory / 'video.avi', directory / 'tracker_results.json')}))
    unlink = Path.unlink
    with monkeypatch.context() as patch:
        def fail(path, *args, **kwargs):
            if path == overlay:
                raise PermissionError('injected locked old preview')
            return unlink(path, *args, **kwargs)
        patch.setattr(Path, 'unlink', fail)
        assert client.post('/api/track/rewind', json={'mediaId': 'control-video', 'startFrame': 0}).status_code == 200
    assert 'tracking.preview_cleanup_pending' in caplog.text
    assert overlay.read_bytes() == b'old preview'
    def render(video, target, info, rows):
        assert [row['source_frame_index'] for row in rows] == [0]
        if render_fails:
            raise OSError('injected current preview rendering failure')
        target.write_bytes(b'new preview from current branch')
    monkeypatch.setattr(tracking_preview, '_render_overlay_video', render)
    response = client.get('/api/track/overlay/control-video')
    if render_fails:
        assert response.status_code == 503
        assert response.content != b'old preview'
    else:
        assert response.status_code == 200
        assert response.content == b'new preview from current branch'


def test_committed_recovery_does_not_delete_a_later_new_preview(controls, monkeypatch):
    from app import tracking_restart
    _, directory, _ = controls
    feedback(controls[0], scope='frame', calibrate=False)
    overlay = directory / 'tracker_overlay.mp4'
    overlay.write_bytes(b'old preview')
    with monkeypatch.context() as patch:
        patch.setattr(tracking_restart, 'recover_directory', lambda _: None)
        tracking_restart.rewind(directory, 0)
        fresh = directory / 'fresh-preview.tmp'
        fresh.write_bytes(b'later newly generated preview')
        fresh.replace(overlay)
    annotation_state.read_state(directory)
    assert overlay.read_bytes() == b'later newly generated preview'
