"""Pause policy with real geometry/state, isolated from SAM3 model quality."""
from __future__ import annotations

import pytest

from app.services.anomaly_detector import AnomalyDetector, AnomalyLevel, ConfirmedGeometryReference


def box(width=40.0, height=20.0, cx=200.0, cy=150.0):
    return [cx-width/2, cy-height/2, cx+width/2, cy+height/2]


def detector(objects=None, **kwargs):
    objects = objects or {1: box()}
    result = AnomalyDetector(frame_width=1000, frame_height=600, fps=kwargs.pop('fps', 4), **kwargs)
    for oid, bbox in objects.items():
        result.set_manual_baseline(oid, bbox, 0)
    result.initialize_seed(10, objects)
    return result


@pytest.mark.parametrize('candidate', [box(height=12), box(width=62), box(height=10)])
def test_moderate_geometry_observes_three_frames_and_pauses_at_first(candidate):
    subject = detector()
    reports = [subject.push(frame, {1: candidate}) for frame in (11, 12, 13)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[0].object_levels[1] == AnomalyLevel.WARNING
    assert reports[-1].pause_frame_index == 11
    assert reports[-1].frames[0].details['pause_started_frame'] == 11


def test_normal_frame_clears_pending_geometry_without_teaching_a_new_baseline():
    subject = detector()
    assert not subject.push(11, {1: box(height=12)}).should_pause
    assert not subject.push(12, {1: box()}).should_pause
    reports = [subject.push(frame, {1: box(height=12)}) for frame in (13, 14, 15)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 13
    assert subject.states[1].manual_baseline.bbox == box()


def test_nonconsecutive_source_frames_do_not_complete_a_geometry_streak():
    subject = detector()
    reports = [subject.push(frame, {1: box(height=12)}) for frame in (11, 13, 14, 15)]
    assert [report.should_pause for report in reports] == [False, False, False, True]
    assert reports[-1].pause_frame_index == 13


def test_many_objects_do_not_share_geometry_observation_counts():
    objects = {oid: box(cx=60+((oid-1)%10)*80, cy=60+((oid-1)//10)*120) for oid in range(1, 31)}
    subject = detector(objects)
    for frame, risky in [(11, 1), (12, 2), (13, 3)]:
        prediction = {**objects, risky: box(height=12, cx=60+((risky-1)%10)*80, cy=60+((risky-1)//10)*120)}
        assert not subject.push(frame, prediction).should_pause


def test_thirty_objects_small_jitter_remains_continuous_at_hospital_fps():
    objects = {oid: box(cx=100+((oid-1)%10)*90, cy=100+((oid-1)//10)*150) for oid in range(1, 31)}
    subject = AnomalyDetector(frame_width=2048, frame_height=1500, fps=44.2, all_object_ids=objects)
    for oid, bbox in objects.items():
        subject.set_manual_baseline(oid, bbox, 0)
    subject.initialize_seed(130, objects)
    for frame in range(131, 151):
        prediction = {}
        for oid, original in objects.items():
            cx, cy = (original[0]+original[2])/2, (original[1]+original[3])/2
            shift = 1 if (oid+frame)%2 else -1
            prediction[oid] = box(width=40+shift, height=20-shift*.2, cx=cx+shift*.5, cy=cy)
        report = subject.push(frame, prediction)
        assert not report.should_pause
        assert len(report.frames) == 30


@pytest.mark.parametrize('candidate', [box(width=20, height=8), box(width=80, height=45), box(width=13.6), box(width=104), box(width=80, height=10)])
def test_extreme_geometry_still_pauses_immediately(candidate):
    subject = detector()
    report = subject.push(11, {1: candidate})
    assert report.should_pause
    assert report.pause_frame_index == 11


def test_exact_extreme_area_boundary_remains_an_observation():
    subject = detector()
    assert not subject.push(11, {1: box(width=20, height=10)}).should_pause


def test_explicit_confirmed_mask_area_detects_extreme_loss_without_bbox_change():
    reference = ConfirmedGeometryReference(box(), 2, mask_area=400)
    subject = detector(geometry_references={1: reference})
    report = subject.push(11, {1: box()}, mask_areas={1: 30})
    assert report.should_pause
    assert any('mask_area' in reason for reason in report.frames[0].reasons)


def test_several_complete_human_confirmations_can_alternate_without_pause():
    references = [ConfirmedGeometryReference(box(width=20, height=40), 2),
                  ConfirmedGeometryReference(box(width=30, height=30), 3),
                  ConfirmedGeometryReference(box(width=40, height=12), 4)]
    subject = detector(geometry_references={1: references})
    for frame, candidate in enumerate([reference.bbox for reference in references]*2, start=11):
        assert not subject.push(frame, {1: candidate}).should_pause


def test_complete_references_cannot_mix_width_and_height_into_a_new_shape():
    subject = detector(geometry_references={1: [ConfirmedGeometryReference(box(width=20, height=40), 2)]})
    reports = [subject.push(frame, {1: box(width=40, height=40)}) for frame in (11, 12, 13)]
    assert [report.should_pause for report in reports] == [False, False, True]


def test_future_human_confirmation_does_not_exempt_an_earlier_prediction():
    subject = detector(geometry_references={1: [ConfirmedGeometryReference(box(height=12), 30)]})
    assert subject.push(11, {1: box(height=12)}).object_levels[1] == AnomalyLevel.WARNING
    assert subject.push(12, {1: box(height=12)}).object_levels[1] == AnomalyLevel.WARNING
    assert subject.push(13, {1: box(height=12)}).should_pause


def test_existing_seed_overlap_does_not_pause_unchanged_objects():
    objects = {1: box(), 2: box(cx=200.5)}
    subject = detector(objects)
    for frame in range(11, 17):
        report = subject.push(frame, objects)
        assert not report.should_pause
        assert report.object_levels[1] == AnomalyLevel.WARNING


def test_new_converging_overlap_requires_three_frames_and_rewinds_to_first():
    subject = detector({1: box(cx=100), 2: box(cx=250)})
    prediction = {1: box(cx=150), 2: box(cx=150.5)}
    reports = [subject.push(frame, prediction) for frame in (11, 12, 13)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 11


def test_containment_with_small_iou_is_not_identity_merge_evidence():
    subject = detector({1: box(), 2: box(width=10, height=6, cx=350)})
    prediction = {1: box(), 2: box(width=10, height=6)}
    for frame in range(11, 17):
        assert not subject.push(frame, prediction).should_pause


def test_brief_crossing_and_separation_resets_pair_risk():
    objects = {1: box(cx=100), 2: box(cx=250)}
    subject = detector(objects)
    crossing = {1: box(cx=150), 2: box(cx=150.5)}
    assert not subject.push(11, crossing).should_pause
    assert not subject.push(12, objects).should_pause
    reports = [subject.push(frame, crossing) for frame in (13, 14, 15)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 13


def test_high_overlap_skip_in_source_frames_does_not_complete_pair_streak():
    subject = detector({1: box(cx=100), 2: box(cx=250)})
    prediction = {1: box(cx=150), 2: box(cx=150.5)}
    reports = [subject.push(frame, prediction) for frame in (11, 13, 14, 15)]
    assert [report.should_pause for report in reports] == [False, False, False, True]
    assert reports[-1].pause_frame_index == 13


def test_all_overlap_partners_are_structured_and_do_not_overwrite_each_other():
    subject = detector({1: box(), 2: box(cx=200.5), 3: box(cx=201)})
    report = subject.push(11, {1: box(), 2: box(cx=200.5), 3: box(cx=201)})
    partners = report.frames[0].details['overlap_pairs']
    assert {tuple(pair['object_ids']) for pair in partners} == {(1, 2), (1, 3)}
    assert all('coverage' in pair and 'iou' in pair for pair in partners)


def test_confirmed_pair_does_not_exempt_a_different_partner():
    from app.services.anomaly_detector import OverlapReference
    accepted = {1: box(cx=150), 2: box(cx=150.5)}
    subject = detector({**accepted, 3: box(cx=350)},
                       overlap_references={(1, 2): OverlapReference((1, 2), accepted, 8)})
    prediction = {**accepted, 3: box(cx=151)}
    reports = [subject.push(frame, prediction) for frame in (11, 12, 13)]
    assert [report.should_pause for report in reports] == [False, False, True]
    risks = reports[-1].frames[0].details['overlap_pairs']
    assert next(pair for pair in risks if pair['object_ids']==[1, 2])['level'] == 'warning'
    assert next(pair for pair in risks if pair['object_ids']==[1, 3])['level'] == 'anomaly'


def row(frame, objects, report=None):
    return {'source_frame_index': frame, 'objects': [
        {'object_id': oid, 'bbox': bbox, 'source': 'ai',
         'anomaly_details': next((item.details for item in report.frames if item.object_id==oid), {}) if report else {}}
        for oid, bbox in objects.items()]}


def test_pending_geometry_survives_a_fresh_tracking_round():
    subject = detector()
    prediction = {1: box(height=12)}
    history = [row(frame, prediction, subject.push(frame, prediction)) for frame in (11, 12)]
    restarted = detector()
    restarted.initialize_seed(12, prediction)
    restarted.prime_observations(history, 12, prediction)
    report = restarted.push(13, prediction)
    assert report.should_pause and report.pause_frame_index == 11


def test_pending_overlap_does_not_become_normal_because_it_is_an_ai_seed():
    subject = detector({1: box(cx=100), 2: box(cx=250)})
    prediction = {1: box(cx=150), 2: box(cx=150.5)}
    history = [row(frame, prediction, subject.push(frame, prediction)) for frame in (11, 12)]
    restarted = detector({1: box(cx=100), 2: box(cx=250)})
    restarted.initialize_seed(12, prediction)
    restarted.prime_observations(history, 12, prediction)
    report = restarted.push(13, prediction)
    assert report.should_pause and report.pause_frame_index == 11


def test_confirming_geometry_clears_old_risk_on_restore():
    subject = detector()
    prediction = {1: box(height=12)}
    history = [row(frame, prediction, subject.push(frame, prediction)) for frame in (11, 12)]
    restarted = detector(geometry_references={1: ConfirmedGeometryReference(box(height=12), 12)})
    restarted.initialize_seed(12, prediction)
    restarted.prime_observations(history, 12, prediction)
    assert not restarted.push(13, prediction).should_pause


def test_reset_observation_does_not_reuse_the_old_first_risk_frame():
    subject = detector()
    prediction = {1: box(height=12)}
    history = [row(frame, prediction, subject.push(frame, prediction)) for frame in (11, 12)]
    restarted = detector()
    restarted.initialize_seed(12, prediction)
    restarted.prime_observations(history, 12, prediction, reset_frames={1: 12})
    assert not restarted.push(13, prediction).should_pause
    assert not restarted.push(14, prediction).should_pause
    report = restarted.push(15, prediction)
    assert report.should_pause and report.pause_frame_index == 13


def test_only_latest_three_complete_confirmations_are_accepted():
    references = [ConfirmedGeometryReference(box(width=20, height=40), 1),
                  *(ConfirmedGeometryReference(box(), frame) for frame in (2, 3, 4))]
    subject = detector(geometry_references={1: references})
    assert subject.push(11, {1: box(width=20, height=40)}).should_pause


def test_new_manual_baseline_supersedes_older_normal_geometry():
    subject = detector(geometry_references={1: [ConfirmedGeometryReference(box(width=20, height=40), 9)]})
    subject.set_manual_baseline(1, box(), 10)
    assert subject.push(11, {1: box(width=20, height=40)}).should_pause


def test_extreme_motion_keeps_current_pause_frame_while_geometry_is_pending():
    subject = detector(fps=30)
    assert not subject.push(11, {1: box(height=12)}).should_pause
    report = subject.push(12, {1: box(height=12, cx=600)})
    assert report.should_pause and report.pause_frame_index == 12


def test_existing_overlap_that_becomes_more_similar_can_still_pause():
    subject = detector({1: box(), 2: box(cx=204)})
    reports = [subject.push(frame, {1: box(), 2: box()}) for frame in (11, 12, 13)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 11


def test_confirmed_pair_expires_after_separation():
    from app.services.anomaly_detector import OverlapReference
    accepted = {1: box(), 2: box(cx=200.5)}
    subject = detector(accepted, overlap_references={(1, 2): OverlapReference((1, 2), accepted, 8)})
    assert not subject.push(11, {1: box(cx=100), 2: box(cx=250)}).should_pause
    reports = [subject.push(frame, accepted) for frame in (12, 13, 14)]
    assert [report.should_pause for report in reports] == [False, False, True]


def test_overlap_warning_cannot_downgrade_an_extreme_mask_loss():
    objects = {1: box(), 2: box(cx=200.5)}
    subject = detector(objects, geometry_references={1: ConfirmedGeometryReference(box(), 2, mask_area=400)})
    report = subject.push(11, objects, mask_areas={1: 30, 2: 400})
    assert report.should_pause and report.object_levels[1] == AnomalyLevel.ANOMALY


def test_initial_seed_is_not_one_of_the_three_predicted_observations():
    subject = detector()
    prediction = {1: box(height=12)}
    subject.initialize_seed(10, prediction)
    subject.prime_observations([], 10, prediction)
    reports = [subject.push(frame, prediction) for frame in (11, 12, 13)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 11


def test_real_manual_pair_update_clears_earlier_ai_observation_on_restore():
    objects = {1: box(cx=100), 2: box(cx=250)}
    previous = detector(objects)
    prediction = {1: box(), 2: box(cx=200.5)}
    history = [row(frame, prediction, previous.push(frame, prediction)) for frame in (11, 12)]
    restarted = detector(objects)
    restarted.set_manual_baseline(1, prediction[1], 12)
    restarted.initialize_seed(12, prediction)
    restarted.prime_observations(history, 12, prediction)
    assert not restarted.push(13, prediction).should_pause


def test_opposite_geometry_directions_are_not_one_sustained_risk():
    subject = detector({1: box(width=100, height=100)})
    reports = [subject.push(frame, {1: box(width=100, height=height)})
               for frame, height in zip(range(11, 16), (64, 160, 64, 64, 64))]
    assert [report.should_pause for report in reports] == [False, False, False, False, True]
    assert reports[-1].pause_frame_index == 13


def test_shrinking_width_then_height_is_still_the_same_direction():
    subject = detector({1: box(width=100, height=100)})
    predictions = (box(width=64, height=100), box(width=100, height=64), box(width=64, height=100))
    reports = [subject.push(frame, {1: candidate}) for frame, candidate in zip((11, 12, 13), predictions)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 11


def test_chunk_replay_does_not_join_opposite_geometry_directions():
    subject = detector({1: box(width=100, height=100)})
    predictions = {11: {1: box(width=100, height=64)}, 12: {1: box(width=100, height=160)}}
    history = [row(frame, prediction, subject.push(frame, prediction)) for frame, prediction in predictions.items()]
    restarted = detector({1: box(width=100, height=100)})
    restarted.initialize_seed(12, predictions[12])
    restarted.prime_observations(history, 12, predictions[12])
    reports = [restarted.push(frame, {1: box(width=100, height=64)}) for frame in (13, 14, 15)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 13


def test_continuous_mixed_axis_shape_risk_has_its_own_streak():
    subject = detector({1: box(width=100, height=100)})
    candidates = (box(width=160, height=60), box(width=60, height=160), box(width=160, height=60))
    reports = [subject.push(frame, {1: candidate}) for frame, candidate in zip((11, 12, 13), candidates)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].pause_frame_index == 11
    assert all(report.frames[0].details['geometry_observation_category'] == 'shape' for report in reports)


def test_shrink_to_shape_does_not_share_a_streak():
    subject = detector({1: box(width=100, height=100)})
    candidates = (box(width=100, height=64), box(width=160, height=60), box(width=60, height=160), box(width=160, height=60))
    reports = [subject.push(frame, {1: candidate}) for frame, candidate in zip((11, 12, 13, 14), candidates)]
    assert [report.should_pause for report in reports] == [False, False, False, True]
    assert reports[-1].pause_frame_index == 12


def test_aspect_only_risk_is_observed_as_shape():
    subject = detector({1: box(width=100, height=100)})
    reports = [subject.push(frame, {1: box(width=149, height=78)}) for frame in (11, 12, 13)]
    assert [report.should_pause for report in reports] == [False, False, True]
    assert reports[-1].frames[0].details['geometry_observation_category'] == 'shape'
