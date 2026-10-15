"""Native boundary evidence distinguishes a clipped Mask from a lost BBox subject."""
import copy

import cv2
import numpy as np
import pytest

from app.services.tracking_evidence import MaskRegion, TrackingEvidenceProbe


def _scene(*, tail=False):
    image = np.full((160, 240, 3), 145, np.uint8)
    cv2.circle(image, (60, 80), 8, (38,) * 3, -1)
    mask = np.zeros((160, 240), np.uint8)
    cv2.circle(mask, (60, 80), 10, 1, -1)
    if tail:
        cv2.line(image, (68, 80), (135, 80), (190,) * 3, 3)
        cv2.line(mask, (60, 80), (135, 80), 1, 8)
    return image, mask


def _probe():
    probe = TrackingEvidenceProbe(fps=44.2, frame_width=240, frame_height=160)
    probe.source_signature = ["video-fixture", 240, 160, 100]
    return probe


def test_slight_mask_head_deficit_with_well_retained_bbox_is_warning():
    image, mask = _scene()
    probe = _probe()
    probe.prime(0, image, {18: [45, 65, 110, 100]})
    # The segmentation clips a little more than the actual exported rectangle.
    mask[:80] = 0
    box = [45, 76, 110, 100]
    report = probe.observe(1, image, {18: box}, {18: MaskRegion(mask)})[18]
    assert .4 <= report['head']['metrics']['maskCoverage'] < .6
    assert report['status'] == 'warning'
    assert report['head']['status'] == 'matched'
    assert report['head']['associationStatus'] == 'reliable'
    assert report['coverage']['status'] == 'local_mask_coverage'
    assert report['coverage']['metrics']['bboxHeadCoverage'] >= .8
    assert report['coverage']['metrics']['headCenterInsideBox'] is True
    assert 'partial_mask_head_boundary' in report['reasons']
    assert report['observationVerified'] is True
    assert report['sourceSignature'] == probe.source_signature
    assert 'identityReference' in report


@pytest.mark.parametrize('box,cutoff', [([60, 65, 110, 100], 60),
                                      ([64, 65, 110, 100], 64),
                                      ([45, 65, 110, 100], 65)])
def test_real_head_crop_or_severe_segmentation_loss_still_is_risk(box, cutoff):
    image, mask = _scene()
    probe = _probe()
    probe.prime(0, image, {18: [45, 65, 110, 100]})
    if cutoff == 65:
        mask[:, :65] = 0  # Head centre remains in BBox, but Mask loses most of it.
    else:
        mask[:, :cutoff] = 0
    report = probe.observe(1, image, {18: box}, {18: MaskRegion(mask)})[18]
    assert report['status'] == 'risk'
    assert report['head']['status'] == 'lost'
    assert report['reasons'] == ['mask_head_missing']
    assert 'identityReference' not in report


def test_full_mask_does_not_hide_a_bbox_that_cuts_through_the_head():
    image, mask = _scene()
    probe = _probe()
    probe.prime(0, image, {18: [45, 65, 110, 100]})
    report = probe.observe(1, image, {18: [60, 65, 110, 100]},
                           {18: MaskRegion(mask)})[18]
    assert report['head']['metrics']['maskCoverage'] == 1.0
    assert report['coverage']['metrics']['bboxHeadCoverage'] < .6
    assert report['status'] == 'risk'
    assert report['head']['status'] == 'lost'
    assert 'identityReference' not in report


def test_no_resolvable_tail_is_uncertain_body_warning_not_lost_subject():
    image, mask = _scene()
    probe = _probe()
    probe.prime(0, image, {10: [45, 65, 130, 110]})
    report = probe.observe(1, image, {10: [49, 69, 72, 92]},
                           {10: MaskRegion(mask)})[10]
    assert report['head']['metrics']['maskCoverage'] == 1.0
    assert report['status'] == 'warning'
    assert report['coverage']['status'] == 'subject_present_uncertain'
    assert report['reasons'] == ['body_extent_unknown']
    assert report['head']['associationStatus'] == 'reliable'
    assert report['observationVerified'] is True


def test_head_retention_does_not_waive_clear_own_tail_outside_bbox():
    image, mask = _scene(tail=True)
    probe = _probe()
    probe.prime(0, image, {17: [45, 65, 145, 100]})
    mask[:, 73:] = 0
    report = probe.observe(1, image, {17: [49, 69, 73, 92]},
                           {17: MaskRegion(mask)})[17]
    assert report['head']['status'] == 'matched'
    assert report['status'] == 'risk'
    assert report['coverage']['status'] == 'tail_loss'


def test_valid_native_observation_without_reference_never_forges_identity():
    image, mask = _scene()
    probe = _probe()
    report = probe.observe(1, image, {18: [45, 65, 110, 100]},
                           {18: MaskRegion(mask)})[18]
    assert report['status'] == 'unknown'
    assert report['head']['status'] == 'unknown'
    assert report['head']['point'] is None
    assert report['observationVerified'] is True
    assert report['maskArea'] == int(np.count_nonzero(mask))
    assert report['reasons'] == ['reference_missing']
    assert 'identityReference' not in report


@pytest.mark.parametrize('encoding', ['bool', 'uint8-one', 'uint8-255'])
def test_native_binary_encodings_preserve_observation_without_forging_identity(encoding):
    image, mask = _scene()
    if encoding == 'bool':
        mask = mask.astype(bool)
    elif encoding == 'uint8-255':
        mask = mask * 255
    report = _probe().observe(1, image, {18: [45, 65, 110, 100]},
                              {18: MaskRegion(mask)})[18]
    assert report['observationVerified'] is True
    assert report['status'] == 'unknown'
    assert 'identityReference' not in report


@pytest.mark.parametrize('failure', ['image', 'image_shape', 'image_dtype',
                                     'bbox', 'mask', 'mask_empty', 'mask_shape',
                                     'mask_offset', 'mask_outside_context',
                                     'mask_outside_bbox', 'mask_nonbinary',
                                     'mask_dtype', 'mask_outside_frame'])
def test_unverified_or_empty_inputs_never_claim_native_observation(failure):
    image, mask = _scene()
    probe = _probe()
    box = [45, 65, 110, 100]
    frame = None if failure == 'image' else image[:100] if failure == 'image_shape' else image.astype(np.float32) if failure == 'image_dtype' else image
    if failure == 'mask_outside_context':
        mask[:] = 0
        cv2.circle(mask, (210, 135), 8, 1, -1)
    if failure == 'mask_outside_bbox':
        mask[:] = 0
        cv2.circle(mask, (120, 80), 3, 1, -1)
    if failure == 'mask_nonbinary':
        mask[80, 60] = 2
    region = None if failure == 'mask' else MaskRegion(np.zeros_like(mask)) if failure == 'mask_empty' else MaskRegion(mask[:, :, None]) if failure == 'mask_shape' else MaskRegion(mask, (-1, 0)) if failure == 'mask_offset' else MaskRegion(mask, (1, 0)) if failure == 'mask_outside_frame' else MaskRegion(mask.astype(np.float32)) if failure == 'mask_dtype' else MaskRegion(mask)
    if failure == 'bbox':
        box = [45, 65, 45, 100]
    report = probe.observe(1, frame, {18: box}, {18: region})[18]
    assert report.get('observationVerified') is not True
    assert report['status'] != 'supported'
    assert 'identityReference' not in report


def test_observation_does_not_mutate_model_bbox_mask_or_image():
    image, mask = _scene()
    boxes = {18: [45, 76, 110, 100]}
    probe = _probe()
    probe.prime(0, image, {18: [45, 65, 110, 100]})
    originals = copy.deepcopy(boxes), image.copy(), mask.copy()
    probe.observe(1, image, boxes, {18: MaskRegion(mask)})
    assert boxes == originals[0]
    np.testing.assert_array_equal(image, originals[1])
    np.testing.assert_array_equal(mask, originals[2])


def _single_core_pair():
    image = np.full((240, 320, 3), 145, np.uint8)
    cv2.circle(image, (100, 100), 8, (35,) * 3, -1)
    mask = np.zeros((240, 320), np.uint8)
    cv2.circle(mask, (100, 100), 9, 1, -1)
    boxes = {1: [88., 88., 140., 130.], 2: [89., 88., 141., 130.]}
    regions = {oid: MaskRegion(mask.copy()) for oid in boxes}
    probe = TrackingEvidenceProbe(fps=30, frame_width=320, frame_height=240)
    probe.source_signature = ['single-core-fixture', 320, 240, 77]
    probe.prime(0, image, {1: boxes[1]})
    return probe, image, boxes, regions


def test_unreferenced_id_sharing_the_only_visible_core_reports_conflict_without_identity():
    probe, image, boxes, regions = _single_core_pair()
    reports = probe.observe(1, image, boxes, regions)
    unknown = reports[2]
    assert unknown['status'] == 'unknown'
    assert unknown['head']['status'] == 'unknown'
    assert unknown['head']['point'] is None
    assert unknown['head']['referencePoint'] is None
    assert 'identityReference' not in unknown
    conflict = unknown['singleCoreConflict']
    assert conflict['reason'] == 'unreferenced_single_core_shared'
    assert conflict['partnerObjectId'] == 1
    assert conflict['visibleCoreCount'] == 1
    assert conflict['maskIoU'] == 1.0
    assert conflict['maskHeadCoverage'] >= .8
    assert conflict['partnerMaskHeadCoverage'] >= .8
    assert reports[1]['coverage']['metrics']['maskOverlap']['2'] == 1.0
    assert unknown['coverage']['metrics']['maskOverlap']['1'] == 1.0
    assert 2 not in probe._references


@pytest.mark.parametrize('second_head_masked', [True, False])
def test_two_visible_heads_with_mixed_masks_do_not_report_single_core_conflict(second_head_masked):
    probe, image, boxes, regions = _single_core_pair()
    cv2.circle(image, (124, 110), 7, (45,) * 3, -1)
    if second_head_masked:
        for region in regions.values():
            cv2.circle(region.data, (124, 110), 9, 1, -1)
    for frame in range(1, 16):
        reports = probe.observe(frame, image, boxes, regions)
        assert 'singleCoreConflict' not in reports[2]
        assert reports[2]['status'] == 'unknown'
        assert reports[2]['head']['point'] is None
        assert 'identityReference' not in reports[2]


def test_second_visible_core_inside_only_the_partner_box_defeats_single_core_conflict():
    probe, image, boxes, regions = _single_core_pair()
    boxes[2] = [89., 88., 114., 130.]
    cv2.circle(image, (124, 110), 7, (45,) * 3, -1)
    reports = probe.observe(1, image, boxes, regions)
    assert reports[2]['status'] == 'unknown'
    assert 'singleCoreConflict' not in reports[2]


def test_incomplete_partner_context_does_not_claim_only_one_visible_core():
    probe, image, boxes, regions = _single_core_pair()
    boxes[1] = [88., 88., 190., 130.]
    reports = probe.observe(1, image, boxes, regions)
    assert reports[2]['status'] == 'unknown'
    assert 'singleCoreConflict' not in reports[2]


def test_distinct_masks_and_heads_do_not_report_single_core_conflict():
    probe, image, boxes, regions = _single_core_pair()
    cv2.circle(image, (124, 110), 7, (45,) * 3, -1)
    regions[2].data[:] = 0
    cv2.circle(regions[2].data, (124, 110), 9, 1, -1)
    reports = probe.observe(1, image, boxes, regions)
    assert reports[2]['status'] == 'unknown'
    assert 'singleCoreConflict' not in reports[2]


def test_missing_reference_does_not_invent_conflict_without_a_trusted_partner():
    probe, image, boxes, regions = _single_core_pair()
    probe._references.clear()
    reports = probe.observe(1, image, boxes, regions)
    assert all(item['status'] == 'unknown' for item in reports.values())
    assert all('singleCoreConflict' not in item for item in reports.values())
    assert all(item['head']['point'] is None for item in reports.values())


def test_unreferenced_single_core_collapse_eventually_pauses_in_detector():
    from app.services.anomaly_detector import AnomalyDetector, ManualBaseline

    probe, image, boxes, regions = _single_core_pair()
    original = {1: boxes[1], 2: [110., 88., 162., 130.]}
    detector = AnomalyDetector(frame_width=320, frame_height=240,
        all_object_ids=boxes, manual_baselines={oid: ManualBaseline(box, 0)
                                                for oid, box in original.items()})
    detector.image_source_signature = probe.source_signature
    detector.initialize_seed(0, original)
    first_hard = None
    for frame in range(1, 13):
        evidence = probe.observe(frame, image, boxes, regions)
        report = detector.push(frame, boxes,
            mask_areas={oid: int(np.count_nonzero(region.data)) for oid, region in regions.items()},
            identity_evidence=evidence)
        if report.should_pause and first_hard is None:
            first_hard = frame
        assert evidence[2]['head']['point'] is None
        assert evidence[2]['status'] == 'unknown'
        assert 'identityReference' not in evidence[2]
    assert first_hard == 12


def test_temporary_single_core_occlusion_clears_when_two_heads_reappear():
    from app.services.anomaly_detector import AnomalyDetector, ManualBaseline

    probe, image, boxes, regions = _single_core_pair()
    original = {1: boxes[1], 2: [110., 88., 162., 130.]}
    detector = AnomalyDetector(frame_width=320, frame_height=240,
        all_object_ids=boxes, manual_baselines={oid: ManualBaseline(box, 0)
                                                for oid, box in original.items()})
    detector.image_source_signature = probe.source_signature
    detector.initialize_seed(0, original)
    one_core = image.copy()
    for frame in range(1, 25):
        current = one_core.copy()
        if frame == 12:
            cv2.circle(current, (124, 110), 7, (45,) * 3, -1)
        evidence = probe.observe(frame, current, boxes, regions)
        report = detector.push(frame, boxes,
            mask_areas={oid: int(np.count_nonzero(region.data)) for oid, region in regions.items()},
            identity_evidence=evidence)
        assert report.should_pause is (frame == 24)
        if frame == 12:
            assert 'singleCoreConflict' not in evidence[2]
        assert evidence[2]['head']['point'] is None
        assert 'identityReference' not in evidence[2]


def test_real_ambiguous_manual_prime_then_both_ids_collapse_to_one_core_is_not_silent():
    from app.services.anomaly_detector import AnomalyDetector, ManualBaseline

    image = np.full((240, 320, 3), 145, np.uint8)
    for center in ((90, 100), (122, 100), (145, 100)):
        cv2.circle(image, center, 5, (35,) * 3, -1)
    original = {1: [65., 75., 115., 125.], 2: [105., 75., 165., 125.]}
    boxes = {1: original[1], 2: [66., 75., 116., 125.]}
    mask = np.zeros((240, 320), np.uint8)
    cv2.circle(mask, (90, 100), 7, 1, -1)
    regions = {oid: MaskRegion(mask.copy()) for oid in boxes}
    probe = TrackingEvidenceProbe(fps=30, frame_width=320, frame_height=240)
    probe.source_signature = ['ambiguous-prime-fixture', 320, 240, 88]
    primed = probe.prime(0, image, original)
    assert primed[1]['head']['status'] == 'matched'
    assert primed[2]['head']['status'] == 'ambiguous'
    detector = AnomalyDetector(frame_width=320, frame_height=240,
        all_object_ids=boxes, manual_baselines={oid: ManualBaseline(box, 0)
                                                for oid, box in original.items()})
    detector.image_source_signature = probe.source_signature
    detector.initialize_seed(0, original)
    pauses = []
    for frame in range(1, 16):
        evidence = probe.observe(frame, image, boxes, regions)
        report = detector.push(frame, boxes,
            mask_areas={oid: int(np.count_nonzero(mask)) for oid in boxes},
            identity_evidence=evidence)
        if report.should_pause:
            pauses.append(frame)
        assert evidence[2]['status'] == 'unknown'
        assert evidence[2]['head']['point'] is None
        assert 'identityReference' not in evidence[2]
    assert pauses == [12]
