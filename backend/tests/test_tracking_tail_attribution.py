"""Only visibly attributable own structure may turn a tail hint into a pause.

Images and masks below are controlled synthetic inputs, not clinical ground
truth or reconstructed SAM3 masks. Weak connections remain uncertainty.
"""
import copy

import cv2
import numpy as np
import pytest

from app.services.tracking_evidence import MaskRegion, TrackingEvidenceProbe


def _scene(*, weak_bridge=True, bright=False):
    background = 90 if bright else 145
    reverse = -1 if bright else 1
    image = np.full((180, 260, 3), background, np.uint8)
    head = (80, 90)
    # Native phase image: rigid head, faint halo and a clearly continuous tail.
    cv2.circle(image, head, 11, (background + reverse * 28,) * 3, -1)
    cv2.circle(image, head, 8, (210 if bright else 38,) * 3, -1)
    cv2.line(image, (88, 90), (150, 90), (background + reverse * 35,) * 3, 3)
    mask = np.zeros(image.shape[:2], np.uint8)
    cv2.circle(mask, head, 11, 1, -1)
    cv2.line(mask, head, (150, 90), 1, 8)
    full_box = [69., 79., 155., 102.]
    if weak_bridge:
        # A strong detached background line becomes Hessian-connected through
        # a dim bridge. Its endpoint alone does not establish tail ownership.
        cv2.line(image, (38, 90), (58, 90), (background + reverse * 35,) * 3, 3)
        cv2.line(image, (58, 90), (70, 90), (background + reverse * 7,) * 3, 3)
    return image, mask, full_box


def _probe():
    probe = TrackingEvidenceProbe(fps=44.2, frame_width=260, frame_height=180)
    probe.source_signature = ['tail-attribution-fixture', 260, 180, 1]
    return probe


@pytest.mark.parametrize('bright', [False, True])
def test_dim_bridge_to_detached_structure_is_uncertainty_not_own_tail_loss(bright):
    reference, mask, box = _scene(weak_bridge=False, bright=bright)
    current, _, _ = _scene(bright=bright)
    probe = _probe()
    probe.prime(0, reference, {4: [40., 65., 160., 125.]})
    report = probe.observe(1, current, {4: box}, {4: MaskRegion(mask)})[4]
    assert report['head']['status'] == 'matched'
    assert report['status'] == 'warning'
    assert report['coverage']['status'] == 'subject_present_uncertain'
    assert report['reasons'] == ['tail_structure_attribution_uncertain']
    metrics = report['coverage']['metrics']
    assert metrics['tailAttributionUncertain'] is True
    assert any(v['branch']['unattributedOutsidePixels'] > 0 for v in metrics['tailScales'])
    assert all(v['branch']['outsidePixels'] == 0 for v in metrics['tailScales'])


@pytest.mark.parametrize('bright', [False, True])
def test_clear_continuous_own_tail_outside_rectangle_remains_risk(bright):
    image, full_mask, full_box = _scene(weak_bridge=False, bright=bright)
    probe = _probe()
    probe.prime(0, image, {4: full_box})
    mask = np.zeros_like(full_mask)
    cv2.circle(mask, (80, 90), 11, 1, -1)
    report = probe.observe(1, image, {4: [69., 79., 92., 102.]},
                           {4: MaskRegion(mask)})[4]
    assert report['head']['status'] == 'matched'
    assert report['status'] == 'risk'
    assert report['coverage']['status'] == 'tail_loss'
    assert all(v['branch']['outsidePixels'] > 50 for v in report['coverage']['metrics']['tailScales'])


def test_trusted_normal_checkpoint_cannot_hide_new_clear_tail_loss():
    image, full_mask, full_box = _scene(weak_bridge=False)
    probe = _probe()
    probe.prime(10, image, {4: full_box})
    normal = probe.observe(11, image, {4: full_box}, {4: MaskRegion(full_mask)})[4]
    assert normal['coverage']['status'] == 'no_clear_loss'
    mask = full_mask.copy()
    mask[:, 95:] = 0
    risk = probe.observe(12, image, {4: [69., 79., 95., 102.]}, {4: MaskRegion(mask)})[4]
    assert risk['coverage']['status'] == 'tail_loss'
    assert risk['status'] == 'risk'



def test_existing_detached_background_does_not_become_tail_when_a_dim_bridge_appears():
    reference, mask, box = _scene(weak_bridge=False)
    # This visible background line already exists in the trusted frame. It is
    # not a new lost part simply because a weak gradient later connects it.
    cv2.line(reference, (38, 90), (58, 90), (180,) * 3, 3)
    current, _, _ = _scene()
    probe = _probe()
    probe.prime(0, reference, {4: [40., 65., 160., 125.]})
    for frame in (1, 2, 3):
        report = probe.observe(frame, current, {4: box}, {4: MaskRegion(mask)})[4]
        assert report['status'] == 'warning'
        assert report['reasons'] == ['tail_structure_attribution_uncertain']
        assert report['coverage']['metrics']['tailAttributionUncertain'] is True


@pytest.mark.parametrize('bright', [False, True])
def test_dim_entire_tail_outside_is_uncertain_not_certified_complete(bright):
    image, mask, full_box = _scene(weak_bridge=False, bright=bright)
    background, reverse = (90, -1) if bright else (145, 1)
    cv2.line(image, (88, 90), (150, 90), (background + reverse * 7,) * 3, 3)
    truncated = np.zeros_like(mask)
    cv2.circle(truncated, (80, 90), 11, 1, -1)
    probe = _probe()
    probe.prime(0, image, {4: full_box})
    report = probe.observe(1, image, {4: [69., 79., 92., 102.]},
                           {4: MaskRegion(truncated)})[4]
    assert report['status'] == 'warning'
    assert report['coverage']['status'] == 'subject_present_uncertain'
    assert report['reasons'] == ['body_extent_unknown']


def test_uncertain_structure_does_not_mutate_model_or_native_inputs():
    reference, mask, box = _scene(weak_bridge=False)
    current, _, _ = _scene()
    probe = _probe()
    probe.prime(0, reference, {4: [40., 65., 160., 125.]})
    previous = copy.deepcopy(box), current.copy(), mask.copy()
    probe.observe(1, current, {4: box}, {4: MaskRegion(mask)})
    assert box == previous[0]
    np.testing.assert_array_equal(current, previous[1])
    np.testing.assert_array_equal(mask, previous[2])


@pytest.mark.parametrize('real_loss', [False, True])
def test_producer_to_detector_distinguishes_dim_attachment_from_real_own_loss(real_loss):
    from app.services.anomaly_detector import AnomalyDetector, ManualBaseline

    reference, full_mask, full_box = _scene(weak_bridge=False)
    trusted_box = full_box if real_loss else [40., 65., 160., 125.]
    image, _, _ = _scene(weak_bridge=not real_loss)
    box = [69., 79., 105., 102.] if real_loss else full_box
    mask = full_mask.copy()
    if real_loss:
        mask[:, 105:] = 0
    probe = _probe()
    probe.prime(0, reference, {4: trusted_box})
    detector = AnomalyDetector(frame_width=260, frame_height=180, fps=44.2,
        all_object_ids=[4], manual_baselines={4: ManualBaseline(trusted_box, 0)})
    detector.image_source_signature = probe.source_signature
    detector.initialize_seed(0, {4: trusted_box})
    decisions = []
    for frame in (1, 2, 3):
        evidence = probe.observe(frame, image, {4: box}, {4: MaskRegion(mask)})[4]
        report = detector.push(frame, {4: box}, mask_areas={4: int(mask.sum())},
                               identity_evidence={4: evidence})
        decisions.append(report.should_pause)
    assert decisions == ([False, False, True] if real_loss else [False, False, False])


def test_uncertain_tail_diagnostic_has_searchable_object_and_frame_log(caplog):
    reference, mask, box = _scene(weak_bridge=False)
    current, _, _ = _scene()
    probe = _probe()
    probe.prime(0, reference, {4: [40., 65., 160., 125.]})
    with caplog.at_level('DEBUG', logger='review.tracking'):
        report = probe.observe(7, current, {4: box}, {4: MaskRegion(mask)})[4]
    assert report['reasons'] == ['tail_structure_attribution_uncertain']
    assert 'tracking.tail_attribution_uncertain object=4 frame=7' in caplog.text
    assert 'outsidePixels=' in caplog.text


def test_tail_uncertainty_does_not_conceal_real_head_loss():
    reference, mask, box = _scene(weak_bridge=False)
    current, _, _ = _scene()
    mask[:, :83] = 0
    probe = _probe()
    probe.prime(0, reference, {4: [40., 65., 160., 125.]})
    report = probe.observe(1, current, {4: [83., 79., 155., 102.]},
                           {4: MaskRegion(mask)})[4]
    assert report['status'] == 'risk'
    assert report['head']['status'] == 'lost'
    assert report['reasons'] == ['mask_head_missing']


@pytest.mark.parametrize('invalid', ['image', 'mask'])
def test_invalid_optical_input_stays_unknown_and_healthy_recovery_stays_uncertain(invalid):
    reference, mask, box = _scene(weak_bridge=False)
    current, _, _ = _scene()
    probe = _probe()
    probe.prime(0, reference, {4: [40., 65., 160., 125.]})
    failed = probe.observe(1, None if invalid == 'image' else current, {4: box},
                           {} if invalid == 'mask' else {4: MaskRegion(mask)})[4]
    assert failed['status'] == 'unknown'
    assert failed['observationVerified'] is False
    restored = probe.observe(2, current, {4: box}, {4: MaskRegion(mask)})[4]
    assert restored['status'] == 'warning'
    assert restored['reasons'] == ['tail_structure_attribution_uncertain']
