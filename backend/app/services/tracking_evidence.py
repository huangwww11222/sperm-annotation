"""Compact native-image cues for tracking policy; not a sperm classifier.

``supported`` means that a referenced optical core is still in the mask and no
clear major loss was observed. It only permits ordinary BBox geometry warnings
to be downgraded. It neither certifies a complete tail nor accepts annotations.
Call ``prime`` only for an explicitly trusted manual/checkpoint frame. AI seed
boxes must never silently create or move an optical reference.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

import cv2
import numpy as np


@dataclass(frozen=True)
class MaskRegion:
    data: np.ndarray
    offset: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class EvidenceConfig:
    max_roi_side: int = 512
    core_noise_multiplier: float = 5.0
    core_peak_fraction: float = 0.15
    core_min_signal_noise: float = 6.0
    initial_dominance: float = 1.35
    core_area_min_ratio: float = 0.30
    core_area_max_ratio: float = 3.0
    core_peak_min_ratio: float = 0.40
    core_max_axis_ratio: float = 4.5
    mask_head_min_coverage: float = 0.60
    foreign_min_area_ratio: float = 0.12
    foreign_min_contrast_ratio: float = 0.25
    foreign_proximity_radii: float = 2.0
    strong_other_peak_ratio: float = 0.70
    strong_other_area_ratio: float = 0.40
    ridge_noise_multiplier: float = 0.55
    ridge_min_normalized_response: float = 0.55
    tail_outside_radius_fraction: float = 0.45
    tail_min_outside_pixels: int = 6
    tail_mask_min_coverage: float = 0.35
    tail_loss_min_core_radii: float = 3.5
    independent_mask_iou_max: float = 0.20


@dataclass
class _Reference:
    frame: int
    bbox: list[float]
    core: dict
    last_frame: int
    last_point: list[float]
    context_cores: list[dict]


class TrackingEvidenceProbe:
    def __init__(self, *, fps: float, frame_width: int, frame_height: int,
                 config: EvidenceConfig | None = None):
        self.fps = max(1.0, float(fps))
        self.width, self.height = int(frame_width), int(frame_height)
        self.config = config or EvidenceConfig()
        self._references: dict[int, _Reference] = {}

    def _base(self, oid: int, frame: int, box: list[float] | None, ref=None):
        return {"version": 1, "source": "native-frame-mask-v1", "objectId": int(oid),
                "frameIndex": int(frame), "bbox": box, "maskArea": None,
                "status": "unknown", "head": {"status": "unknown", "point": None,
                "previousPoint": None, "previousFrameIndex": None,
                "referencePoint": None if ref is None else list(ref.core['point']),
                "referenceFrameIndex": None if ref is None else ref.frame,
                "confidence": None, "radius": None, "metrics": {}},
                "coverage": {"status": "unknown", "metrics": {}},
                "reasons": [], "thresholds": {}}

    def _box(self, value):
        try:
            box = [float(v) for v in value]
        except (TypeError, ValueError, OverflowError):
            return None
        if len(box) != 4 or not all(math.isfinite(v) for v in box):
            return None
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= self.width and 0 <= y1 < y2 <= self.height):
            return None
        return box

    def _image_ok(self, image):
        return (isinstance(image, np.ndarray) and image.dtype == np.uint8
                and image.ndim in (2, 3) and image.shape[:2] == (self.height, self.width)
                and (image.ndim == 2 or image.shape[2] in (3, 4)))

    def _roi(self, image, box, ref):
        boxes = [box]
        if ref is not None:
            dx = ref.last_point[0] - ref.core['point'][0]
            dy = ref.last_point[1] - ref.core['point'][1]
            boxes.append([ref.bbox[0]+dx, ref.bbox[1]+dy,
                          ref.bbox[2]+dx, ref.bbox[3]+dy])
        diagonal = math.hypot(box[2]-box[0], box[3]-box[1])
        padding = max(12, round(diagonal*0.20))
        x1 = max(0, math.floor(min(b[0] for b in boxes))-padding)
        y1 = max(0, math.floor(min(b[1] for b in boxes))-padding)
        x2 = min(self.width, math.ceil(max(b[2] for b in boxes))+padding)
        y2 = min(self.height, math.ceil(max(b[3] for b in boxes))+padding)
        if max(x2-x1, y2-y1) > self.config.max_roi_side:
            return None
        raw = image[y1:y2, x1:x2]
        gray = raw if raw.ndim == 2 else cv2.cvtColor(raw, cv2.COLOR_BGRA2GRAY if raw.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
        return gray, (x1, y1, x2, y2)

    def _cores(self, gray, bounds, box, ref):
        cfg = self.config
        smooth = cv2.GaussianBlur(gray, (3, 3), 0)
        residual = smooth.astype(np.float32)-cv2.medianBlur(gray, 5).astype(np.float32)
        noise = max(1.0, float(np.median(np.abs(residual)))*1.4826)
        diagonal = math.hypot(box[2]-box[0], box[3]-box[1])
        kernel_size = max(21, min(61, int(round(diagonal*0.20)) | 1))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        dark = cv2.morphologyEx(smooth, cv2.MORPH_BLACKHAT, kernel).astype(np.float32)
        bright = cv2.morphologyEx(smooth, cv2.MORPH_TOPHAT, kernel).astype(np.float32)
        peak = max(float(dark.max()), float(bright.max()))
        threshold = max(4.0, noise*cfg.core_noise_multiplier, peak*cfg.core_peak_fraction)
        items = []
        for polarity, response in (("dark", dark), ("bright", bright)):
            n, labels, stats, centers = cv2.connectedComponentsWithStats((response >= threshold).astype(np.uint8), 8)
            for label in range(1, n):
                area = int(stats[label, cv2.CC_STAT_AREA])
                if area < 8 or area > max(1024, diagonal*diagonal*0.20):
                    continue
                component = labels == label
                ys, xs = np.nonzero(component)
                covariance = np.cov(np.column_stack((xs, ys)), rowvar=False)
                eigen = np.linalg.eigvalsh(covariance)
                axis = math.sqrt(float(eigen[-1])/max(1., float(eigen[0])))
                if axis > cfg.core_max_axis_ratio:
                    continue
                radius = math.sqrt(area/math.pi)
                ring_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                    (2*max(3, round(radius))+1,)*2)
                ring = (cv2.dilate(component.astype(np.uint8), ring_kernel) > 0) & ~component
                if not ring.any():
                    continue
                core_luma = float(smooth[component].mean())
                ring_luma = float(np.median(smooth[ring]))
                contrast = ring_luma-core_luma if polarity == "dark" else core_luma-ring_luma
                px, py = centers[label]
                items.append({"point": [float(px+bounds[0]), float(py+bounds[1])],
                    "radius": radius, "area": area, "peakContrast": float(response[component].max()),
                    "ringContrast": contrast, "axisRatio": axis, "polarity": polarity,
                    "pixels": component, "score": contrast*math.sqrt(area)})
        items.sort(key=lambda c: c['score'], reverse=True)
        return items, noise, threshold

    @staticmethod
    def _compact(core):
        return {key: value for key, value in core.items() if key not in ('pixels', 'score')}

    def prime(self, frame_index: int, frame_bgr: np.ndarray,
              boxes: Mapping[int, Sequence[float]]) -> dict[int, dict]:
        """Explicit trusted-frame entry; never call it just because AI re-seeds."""
        results = {}
        for oid, value in boxes.items():
            # Failed explicit re-priming must not silently keep permission from
            # a different older reference.
            self._references.pop(int(oid), None)
            box = self._box(value); item = self._base(oid, frame_index, box)
            results[oid] = item
            if box is None or not self._image_ok(frame_bgr):
                item['reasons'] = ['invalid_reference_input']; continue
            roi = self._roi(frame_bgr, box, None)
            if roi is None:
                item['reasons'] = ['reference_roi_too_large']; continue
            gray, bounds = roi
            cores, noise, threshold = self._cores(gray, bounds, box, None)
            candidates = [c for c in cores if box[0] <= c['point'][0] < box[2]
                          and box[1] <= c['point'][1] < box[3]
                          and c['ringContrast'] >= noise*self.config.core_min_signal_noise]
            if not candidates:
                item['reasons'] = ['reference_core_unknown']; continue
            primary = candidates[0]
            rival = next((c for c in candidates[1:] if math.dist(c['point'], primary['point']) > primary['radius']*2
                          and c['peakContrast'] >= primary['peakContrast']/self.config.initial_dominance
                          and c['area'] >= primary['area']*self.config.strong_other_area_ratio), None)
            if rival is not None:
                item['head']['status'] = 'ambiguous'; item['reasons'] = ['reference_core_ambiguous']; continue
            compact = self._compact(primary)
            # A weaker core already inside the trusted rectangle is ambiguous
            # context, not a new foreign-object event on every later frame.
            context = [self._compact(c) for c in candidates[1:]
                if c['polarity'] == primary['polarity']
                and math.dist(c['point'], primary['point']) > primary['radius']*2
                and c['area'] >= primary['area']*self.config.foreign_min_area_ratio
                and c['ringContrast'] >= primary['ringContrast']*self.config.foreign_min_contrast_ratio]
            self._references[int(oid)] = _Reference(frame_index, list(box), compact, frame_index, list(primary['point']), context)
            item['head'].update(status='matched', point=primary['point'], referencePoint=primary['point'],
                referenceFrameIndex=frame_index, radius=primary['radius'], confidence=1.0,
                metrics={'polarity': primary['polarity']})
            item['coverage']['metrics']['referenceContextCoreCount'] = len(context)
            item['thresholds'] = {'coreContrast': threshold, 'backgroundNoise': noise}
            item['reasons'] = ['trusted_optical_reference']
        return results

    def _mask_roi(self, region, bounds):
        if not isinstance(region, MaskRegion) or not isinstance(region.data, np.ndarray):
            return None
        if region.data.ndim != 2 or region.data.dtype not in (np.dtype(bool), np.dtype(np.uint8)):
            return None
        if (not isinstance(region.offset, (tuple, list)) or len(region.offset) != 2
                or not all(isinstance(v, (int, np.integer)) for v in region.offset)):
            return None
        mx, my = map(int, region.offset); mh, mw = region.data.shape
        if (max(mh, mw) > self.config.max_roi_side or mx < 0 or my < 0
                or mx+mw > self.width or my+mh > self.height):
            return None
        x1, y1, x2, y2 = bounds
        output = np.zeros((y2-y1, x2-x1), np.uint8)
        ix1, iy1 = max(x1, mx), max(y1, my); ix2, iy2 = min(x2, mx+mw), min(y2, my+mh)
        if ix2 > ix1 and iy2 > iy1:
            output[iy1-y1:iy2-y1, ix1-x1:ix2-x1] = region.data[iy1-my:iy2-my, ix1-mx:ix2-mx] > 0
        return output, int(np.count_nonzero(region.data))

    def _tail(self, gray, bounds, box, core, mask, noise):
        cfg = self.config
        ys, xs = np.indices(gray.shape)
        hx, hy = core['point'][0]-bounds[0], core['point'][1]-bounds[1]
        distance = np.hypot(xs-hx, ys-hy)
        outside = np.maximum.reduce((box[0]-(xs+bounds[0]), (xs+bounds[0])-box[2],
            box[1]-(ys+bounds[1]), (ys+bounds[1])-box[3], np.zeros(gray.shape)))
        border = max(3.0, cfg.tail_outside_radius_fraction*core['radius'])
        min_response = max(cfg.ridge_min_normalized_response, noise*cfg.ridge_noise_multiplier)
        # Compression gradients can connect the head halo to weak unrelated
        # lines at both scales. Only optically clear omitted pixels are loss
        # evidence; weaker extensions remain diagnostics, not a new pause.
        optical_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
            (max(9, min(61, round(core['radius']*3))) | 1,)*2)
        optical_signal = cv2.morphologyEx(cv2.GaussianBlur(gray, (3, 3), 0),
            cv2.MORPH_TOPHAT if core['polarity'] == 'dark' else cv2.MORPH_BLACKHAT,
            optical_kernel).astype(np.float32)
        clear_signal = max(noise*4, core['ringContrast']*.10)
        scales = (max(.9, min(2., core['radius']*.13)), max(1.2, min(3.5, core['radius']*.20)))
        evidence = []
        for sigma in scales:
            smooth = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), sigma)
            xx = cv2.Sobel(smooth, cv2.CV_32F, 2, 0, ksize=3, scale=.25)
            yy = cv2.Sobel(smooth, cv2.CV_32F, 0, 2, ksize=3, scale=.25)
            xy = cv2.Sobel(smooth, cv2.CV_32F, 1, 1, ksize=3, scale=.25)
            delta = np.sqrt((xx-yy)**2+4*xy**2)
            a, b = (xx+yy-delta)/2, (xx+yy+delta)/2
            swap = np.abs(a) > np.abs(b); small, large = np.where(swap, b, a), np.where(swap, a, b)
            signed = -large if core['polarity'] == 'dark' else large
            response = np.maximum(0, signed)*sigma*sigma*np.exp(-2*(small/(np.abs(large)+1e-6))**2)
            binary = cv2.morphologyEx((response >= min_response).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            n, labels, _, _ = cv2.connectedComponentsWithStats(binary, 8)
            branches = []
            for label in range(1, n):
                component = labels == label
                near, far = float(distance[component].min()), float(distance[component].max())
                if near > core['radius']*2.2 or far < core['radius']*4:
                    continue
                tail = component & (distance > core['radius']*2.2)
                count = int(tail.sum())
                if count < 12:
                    continue
                # Phase halos around the head can lie outside a perfectly
                # serviceable body box. A major tail-loss decision requires
                # extended structure beyond that near-head optical fringe.
                clear_tail = tail & (optical_signal >= clear_signal) & (distance > core['radius']*cfg.tail_loss_min_core_radii)
                omitted = clear_tail & (outside > border)
                max_index = int(np.argmax(np.where(clear_tail, outside, -1)))
                oy, ox = np.unravel_index(max_index, gray.shape)
                # A connected component running off the ROI can be an image
                # noise network. It is not affirmative loss evidence.
                touches = bool(component[0].any() or component[-1].any() or component[:, 0].any() or component[:, -1].any())
                branches.append({'span': far, 'tailPixels': count,
                    'outsidePixels': int(omitted.sum()), 'outsideDistance': float(outside[tail].max()),
                    'outsidePoint': [float(ox+bounds[0]), float(oy+bounds[1])],
                    'maskCoverage': float(np.count_nonzero(mask[tail]))/count,
                    'clearTailPixels': int(clear_tail.sum()),
                    'clearMaskCoverage': (float(np.count_nonzero(mask[clear_tail]))/int(clear_tail.sum())) if clear_tail.any() else None,
                    'touchesContextBoundary': touches})
            branches.sort(key=lambda b: b['span'], reverse=True)
            evidence.append({'sigma': sigma, 'branch': branches[0] if branches else None})
        valid = [e['branch'] for e in evidence if e['branch'] and not e['branch']['touchesContextBoundary']]
        tail_loss = False
        if len(valid) == 2:
            coherent = math.dist(valid[0]['outsidePoint'], valid[1]['outsidePoint']) <= max(5, core['radius']*2.5)
            outside_loss = coherent and all(v['outsidePixels'] >= cfg.tail_min_outside_pixels for v in valid)
            mask_loss = all(v['clearTailPixels'] >= 12 and v['clearMaskCoverage'] is not None
                and v['clearMaskCoverage'] < cfg.tail_mask_min_coverage for v in valid)
            tail_loss = outside_loss or mask_loss
        return tail_loss, {'tailScales': evidence, 'tailVisibleAtBothScales': len(valid) == 2,
                           'tailOutsideTolerance': border, 'ridgeResponseThreshold': min_response,
                           'tailClearOpticalContrast': clear_signal,
                           'tailLossMinDistance': core['radius']*cfg.tail_loss_min_core_radii}

    def observe(self, frame_index: int, frame_bgr: np.ndarray | None,
                boxes: Mapping[int, Sequence[float]], masks: Mapping[int, MaskRegion] | None = None) -> dict[int, dict]:
        results = {}; regions = {}; points = {}
        for oid, value in boxes.items():
            ref = self._references.get(int(oid)); box = self._box(value)
            item = self._base(oid, frame_index, box, ref); results[oid] = item
            # Binding metadata remains accurate for naturally unknown optical
            # evidence (e.g. no trusted core or unavailable native image).
            # A zero-sized context validates/counts the ROI without allocating
            # a source-frame mask or requiring an optical reference.
            region = (masks or {}).get(oid)
            binding = self._mask_roi(region, (0, 0, 0, 0))
            if binding is not None:
                item['maskArea'] = binding[1]
            if box is None:
                item['reasons'] = ['invalid_bbox']; continue
            if ref is None:
                item['reasons'] = ['reference_missing']; continue
            if frame_index < ref.frame or frame_index < ref.last_frame:
                item['reasons'] = ['reference_in_future' if frame_index < ref.frame else 'observation_in_future']; continue
            if not self._image_ok(frame_bgr):
                item['reasons'] = ['native_image_unavailable']; continue
            roi = self._roi(frame_bgr, box, ref)
            if roi is None:
                item['reasons'] = ['roi_too_large']; continue
            gray, bounds = roi
            mask_info = self._mask_roi(region, bounds)
            if mask_info is None:
                item['reasons'] = ['mask_unavailable']; continue
            mask, mask_area = mask_info; item['maskArea'] = mask_area; regions[oid] = region
            cores, noise, threshold = self._cores(gray, bounds, box, ref)
            cfg = self.config
            candidates = [c for c in cores if c['polarity'] == ref.core['polarity']
                and cfg.core_area_min_ratio <= c['area']/ref.core['area'] <= cfg.core_area_max_ratio
                and c['peakContrast'] >= ref.core['peakContrast']*cfg.core_peak_min_ratio
                and c['ringContrast'] >= noise*cfg.core_min_signal_noise]
            candidates.sort(key=lambda c: math.dist(c['point'], ref.last_point))
            frame_gap = max(1, frame_index-ref.last_frame)
            diagonal = math.hypot(ref.bbox[2]-ref.bbox[0], ref.bbox[3]-ref.bbox[1])
            max_motion = max(ref.core['radius']*6, diagonal*.5)*30/self.fps*frame_gap
            item['thresholds'] = {'coreContrast': threshold, 'backgroundNoise': noise,
                'maxAssociationDistance': max_motion, 'maskHeadMinCoverage': cfg.mask_head_min_coverage,
                'coreAreaMin': ref.core['area']*cfg.core_area_min_ratio,
                'coreAreaMax': ref.core['area']*cfg.core_area_max_ratio,
                'corePeakMin': ref.core['peakContrast']*cfg.core_peak_min_ratio,
                'foreignMinAreaRatio': cfg.foreign_min_area_ratio,
                'foreignMinContrastRatio': cfg.foreign_min_contrast_ratio,
                'coreMinSignalNoise': cfg.core_min_signal_noise,
                'strongOtherPeakRatio': cfg.strong_other_peak_ratio,
                'strongOtherAreaRatio': cfg.strong_other_area_ratio}
            if not candidates or math.dist(candidates[0]['point'], ref.last_point) > max_motion:
                item['reasons'] = ['optical_core_unknown']; continue
            core = candidates[0]
            covered = float(np.count_nonzero(mask[core['pixels']]))/core['area']
            previous = ref.last_point if ref.last_frame < frame_index else None
            previous_frame = ref.last_frame if previous is not None else None
            item['head'].update(status='matched', point=core['point'], radius=core['radius'],
                previousPoint=None if previous is None else list(previous), previousFrameIndex=previous_frame,
                confidence=min(1.0, core['ringContrast']/(noise*cfg.core_min_signal_noise*2)),
                metrics={"polarity": core['polarity'], "areaRatio": core['area']/ref.core['area'],
                    "ringContrast": core['ringContrast'], "peakContrast": core['peakContrast'],
                    "maskCoverage": covered})
            if mask_area == 0 or covered < cfg.mask_head_min_coverage or not (box[0] <= core['point'][0] < box[2] and box[1] <= core['point'][1] < box[3]):
                item['status'] = 'risk'; item['head']['status'] = 'lost'
                item['reasons'] = ['mask_head_missing']; continue
            points[oid] = core
            padded = np.pad(mask, 1)
            dist = cv2.distanceTransform(padded, cv2.DIST_L2, 5)
            _, thickness, _, location = cv2.minMaxLoc(dist)
            thick_point = [float(location[0]-1+bounds[0]), float(location[1]-1+bounds[1])]
            metrics = {'maskHeadCoverage': covered, 'maxThicknessPoint': thick_point,
                'maxThicknessRadius': float(thickness),
                'thicknessToHeadDistance': math.dist(thick_point, core['point'])}
            item['coverage']['metrics'] = metrics
            other = [c for c in cores if c is not core and c['polarity'] == core['polarity']
                and math.dist(c['point'], core['point']) > core['radius']*2
                and float(np.count_nonzero(mask[c['pixels']]))/c['area'] >= cfg.mask_head_min_coverage]
            strong = [c for c in other if c['peakContrast'] >= core['peakContrast']*cfg.strong_other_peak_ratio
                and c['area'] >= ref.core['area']*cfg.strong_other_area_ratio]
            focused = [c for c in other if c['area'] >= ref.core['area']*cfg.foreign_min_area_ratio
                and c['ringContrast'] >= max(noise*cfg.core_min_signal_noise, ref.core['ringContrast']*cfg.foreign_min_contrast_ratio)
                and math.dist(c['point'], thick_point) <= max(3, c['radius']*cfg.foreign_proximity_radii)
                and math.dist(core['point'], thick_point) > core['radius']*3]
            if strong:
                item['status'] = 'risk'; item['coverage']['status'] = 'merge'
                item['reasons'] = ['multiple_strong_masked_cores']; metrics['otherCoreCount'] = len(strong)
            else:
                known_focus = None
                if focused:
                    foreign = focused[0]
                    metrics['foreignCore'] = self._compact(foreign)
                    dx, dy = core['point'][0]-ref.core['point'][0], core['point'][1]-ref.core['point'][1]
                    known_focus = next((c for c in ref.context_cores
                        if c['polarity'] == foreign['polarity']
                        and min(math.dist(c['point'], foreign['point']),
                            math.dist([c['point'][0]+dx, c['point'][1]+dy], foreign['point']))
                            <= max(3, c['radius']*2.5)), None)
                    if known_focus is None:
                        item['status'] = 'risk'; item['coverage']['status'] = 'foreign_core'
                        item['reasons'] = ['foreign_core_focus']
                        ref.last_frame = frame_index; ref.last_point = list(core['point'])
                        continue
                tail_loss, tail_metrics = self._tail(gray, bounds, box, core, mask, noise)
                metrics.update(tail_metrics)
                if tail_loss:
                    item['status'] = 'risk'; item['coverage']['status'] = 'tail_loss'
                    item['reasons'] = ['connected_structure_loss']
                elif known_focus is not None:
                    # Existing incidental cores are not automatically judged
                    # harmless. Unknown retains the old geometry protection;
                    # in particular it cannot waive the real object-16 loss.
                    item['coverage']['status'] = 'unknown'
                    item['reasons'] = ['accepted_core_context_ambiguous']
                    metrics['baselineContextMatched'] = True
                    metrics['baselineContextCore'] = known_focus
                else:
                    ys, xs = np.nonzero(mask)
                    span = float(np.hypot(xs+bounds[0]-core['point'][0], ys+bounds[1]-core['point'][1]).max())
                    metrics['maskExtentFromCore'] = span
                    if span < core['radius']*3:
                        item['reasons'] = ['body_extent_unknown']
                    else:
                        item['status'] = 'supported'; item['coverage']['status'] = 'no_clear_loss'
                        item['reasons'] = ['referenced_core_matched', 'no_clear_major_loss']
            # Observed location may advance; the explicit optical reference and
            # its appearance/scale never advance from AI output.
            ref.last_frame = frame_index; ref.last_point = list(core['point'])
        self._joint_identity(results, regions, points, boxes)
        return results

    def _joint_identity(self, results, regions, points, boxes):
        ids = list(points)
        for index, a in enumerate(ids):
            for b in ids[index+1:]:
                pa, pb = points[a], points[b]
                distance = math.dist(pa['point'], pb['point'])
                a_box, b_box = boxes[a], boxes[b]
                intersects = min(a_box[2], b_box[2]) > max(a_box[0], b_box[0]) and min(a_box[3], b_box[3]) > max(a_box[1], b_box[1])
                if not intersects and distance >= max(2, min(pa['radius'], pb['radius'])*.5):
                    continue
                overlap = self._mask_iou(regions[a], regions[b], boxes[a], boxes[b])
                for oid, other in ((a, b), (b, a)):
                    metrics = results[oid]['coverage']['metrics']
                    metrics.setdefault('maskOverlap', {})[str(other)] = overlap
                if distance < max(2, min(pa['radius'], pb['radius'])*.5):
                    for oid in (a, b):
                        results[oid]['status'] = 'risk'; results[oid]['coverage']['status'] = 'merge'
                        results[oid]['reasons'] = ['shared_optical_core']
                elif overlap <= self.config.independent_mask_iou_max:
                    for oid, other in ((a, b), (b, a)):
                        results[oid]['coverage']['metrics'].setdefault('independentObjectIds', []).append(int(other))

    @staticmethod
    def _mask_iou(a, b, a_box, b_box):
        ax, ay = a.offset; bx, by = b.offset
        # Only possible shared pixels are inspected; never expand both native
        # masks into full 4K arrays or a union canvas.
        x1 = max(ax, bx)
        y1 = max(ay, by)
        x2 = min(ax+a.data.shape[1], bx+b.data.shape[1])
        y2 = min(ay+a.data.shape[0], by+b.data.shape[0])
        intersection = 0
        if x2 > x1 and y2 > y1:
            intersection = int(np.count_nonzero((a.data[y1-ay:y2-ay, x1-ax:x2-ax] > 0)
                & (b.data[y1-by:y2-by, x1-bx:x2-bx] > 0)))
        union = int(np.count_nonzero(a.data))+int(np.count_nonzero(b.data))-intersection
        return intersection/union if union else 0.0
