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
import logging
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
    bbox_head_min_coverage: float = 0.60
    mask_head_boundary_min_coverage: float = 0.40
    bbox_head_boundary_min_coverage: float = 0.80
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
    unresolved_shared_mask_iou_min: float = 0.90
    unresolved_shared_core_min_coverage: float = 0.80


@dataclass
class _Reference:
    frame: int
    bbox: list[float]
    core: dict
    last_frame: int
    last_point: list[float]
    context_cores: list[dict]
    binding_frame: int
    binding_bbox: list[float]
    ambiguous_since: int | None = None


class TrackingEvidenceProbe:
    def __init__(self, *, fps: float, frame_width: int, frame_height: int,
                 config: EvidenceConfig | None = None):
        self.fps = max(1.0, float(fps))
        self.width, self.height = int(frame_width), int(frame_height)
        self.config = config or EvidenceConfig()
        self._references: dict[int, _Reference] = {}
        self.source_signature: list | None = None
        self._last_reports: dict[int, dict] = {}

    def _base(self, oid: int, frame: int, box: list[float] | None, ref=None):
        return {"version": 2, "source": "native-frame-mask-v2", "sourceSignature": self.source_signature,
                "objectId": int(oid), "geometryReferenceFrameIndex": None if ref is None else ref.binding_frame,
                "geometryReferenceBBox": None if ref is None else list(ref.binding_bbox),
                "frameIndex": int(frame), "bbox": box, "maskArea": None,
                "observationVerified": False,
                "status": "unknown", "head": {"status": "unknown", "point": None,
                "previousPoint": None, "previousFrameIndex": None,
                "referencePoint": None if ref is None else list(ref.core['point']),
                "referenceFrameIndex": None if ref is None else ref.frame,
                "referenceBBox": None if ref is None else list(ref.bbox),
                "confidence": None, "radius": None, "associationStatus": "unknown", "metrics": {}},
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
        if x2 <= x1 or y2 <= y1 or max(x2-x1, y2-y1) > self.config.max_roi_side:
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
                original = labels == label
                modes = self._component_modes(response, original, noise)
                for component, merged in modes:
                    core = self._measure_core(component, response, smooth, bounds, polarity)
                    if core is None:
                        continue
                    core['componentAmbiguous'] = merged
                    items.append(core)
        items.sort(key=lambda c: c['score'], reverse=True)
        return items, noise, threshold

    def _component_modes(self, response, component, noise):
        """Split a connected optical blob into diagnostic modes, never new identities.

        A bridge/halo can connect two visible subjects. Its joint centroid is
        not a trustworthy head. Distinct strong local peaks retain candidate
        locations, but all modes stay ambiguous until the blob separates.
        """
        area = int(component.sum())
        values = np.where(component, response, 0)
        peak = float(values.max())
        local = cv2.dilate(values, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
        maxima = component & (values >= local-1e-5) & (values >= max(noise*6, peak*.45))
        n, labels, _, centers = cv2.connectedComponentsWithStats(maxima.astype(np.uint8), 8)
        peaks = sorted([(float(values[labels == i].max()), list(centers[i]))
                        for i in range(1, n)], reverse=True)
        selected = []
        separation = max(5., math.sqrt(area/math.pi)*1.40)
        for strength, location in peaks:
            if any(math.dist(location, previous) < separation for _, previous in selected):
                continue
            # Two maxima on one noisy plateau are not separate optical modes.
            if selected:
                other = selected[0][1]
                samples = max(3, round(math.dist(other, location)))
                line = np.linspace(other, location, samples)
                valley = min(float(values[round(p[1]), round(p[0])]) for p in line)
                if valley > min(strength, selected[0][0])*.98:
                    continue
            selected.append((strength, location))
            if len(selected) >= 4:
                break
        if len(selected) < 2:
            return [(component, False)]
        ys, xs = np.nonzero(component)
        positions = np.column_stack((xs, ys))
        distances = np.stack([np.sum((positions-point)**2, axis=1) for _, point in selected])
        owners = np.argmin(distances, axis=0)
        modes = []
        for index in range(len(selected)):
            part = np.zeros(component.shape, bool)
            part[ys[owners == index], xs[owners == index]] = True
            if int(part.sum()) >= 8:
                modes.append((part, True))
        return modes if len(modes) >= 2 else [(component, False)]

    def _measure_core(self, component, response, smooth, bounds, polarity):
        cfg = self.config
        area = int(component.sum())
        ys, xs = np.nonzero(component)
        covariance = np.cov(np.column_stack((xs, ys)), rowvar=False)
        eigen = np.linalg.eigvalsh(covariance)
        axis = math.sqrt(float(eigen[-1])/max(1., float(eigen[0])))
        if axis > cfg.core_max_axis_ratio:
            return None
        radius = math.sqrt(area/math.pi)
        ring_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
            (2*max(3, round(radius))+1,)*2)
        ring = (cv2.dilate(component.astype(np.uint8), ring_kernel) > 0) & ~component
        if not ring.any():
            return None
        core_luma = float(smooth[component].mean())
        ring_luma = float(np.median(smooth[ring]))
        contrast = ring_luma-core_luma if polarity == "dark" else core_luma-ring_luma
        px, py = float(xs.mean()), float(ys.mean())
        return {"point": [float(px+bounds[0]), float(py+bounds[1])],
            "radius": radius, "area": area, "peakContrast": float(response[component].max()),
            "ringContrast": contrast, "axisRatio": axis, "polarity": polarity,
            "pixels": component, "score": contrast*math.sqrt(area)}


    @staticmethod
    def _compact(core):
        return {key: value for key, value in core.items() if key not in ('pixels', 'score')}

    @staticmethod
    def _inside(point, box):
        return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]

    def prime(self, frame_index: int, frame_bgr: np.ndarray,
              boxes: Mapping[int, Sequence[float]]) -> dict[int, dict]:
        """Create optical identities only from explicit trusted manual geometry.

        Simultaneous objects compete for native cores. A weaker exclusive core
        belongs to its own rectangle before a brighter core already inside a
        neighbor's rectangle. Identical single-core prompts are left visible to
        the shared-identity protection rather than silently approved.
        """
        results, prepared = {}, {}
        valid_boxes = {int(oid): self._box(value) for oid, value in boxes.items()}
        for oid, box in valid_boxes.items():
            self._references.pop(oid, None)
            item = self._base(oid, frame_index, box); results[oid] = item
            if box is None or not self._image_ok(frame_bgr):
                item['reasons'] = ['invalid_reference_input']; continue
            roi = self._roi(frame_bgr, box, None)
            if roi is None:
                item['reasons'] = ['reference_roi_too_large']; continue
            gray, bounds = roi
            cores, noise, threshold = self._cores(gray, bounds, box, None)
            candidates = [c for c in cores if self._inside(c['point'], box)
                and c['ringContrast'] >= noise*self.config.core_min_signal_noise]
            if not candidates:
                item['reasons'] = ['reference_core_unknown']; continue
            largest = max(c['area'] for c in candidates)
            strongest = max(c['peakContrast'] for c in candidates)
            candidates = [c for c in candidates if c['area'] >= largest*.30
                          and c['peakContrast'] >= strongest*.40]
            # Prefer a core with unambiguous geometric ownership. Overlapping
            # boxes do not authorize both IDs to claim the strongest neighbor.
            def ownership(c):
                owners = sum(self._inside(c['point'], other) for other in valid_boxes.values() if other is not None)
                return (owners, -c['score'])
            candidates.sort(key=ownership)
            # Shared two-mode rectangles cannot assign identity by ID order.
            same_polarity = [c for c in candidates if c['polarity'] == candidates[0]['polarity']]
            if len(same_polarity) > 1 and all(ownership(c)[0] > 1 for c in same_polarity):
                item['head']['status'] = 'ambiguous'; item['reasons'] = ['reference_joint_assignment_ambiguous']; continue
            prepared[oid] = (candidates, noise, threshold)
        chosen = {}
        order = sorted(prepared, key=lambda oid: (sum(self._inside(prepared[oid][0][0]['point'], box)
                       for box in valid_boxes.values() if box is not None), len(prepared[oid][0])))
        for oid in order:
            candidates, noise, threshold = prepared[oid]; box = valid_boxes[oid]; item = results[oid]
            available = [c for c in candidates if all(math.dist(c['point'], old['point']) > max(2, min(c['radius'], old['radius'])*.5)
                         or box == valid_boxes[other] for other, old in chosen.items())]
            if not available:
                item['head']['status'] = 'ambiguous'; item['reasons'] = ['reference_identity_shared']; continue
            primary = available[0]
            primary_owners = sum(self._inside(primary['point'], other) for other in valid_boxes.values() if other is not None)
            rival = next((c for c in available[1:] if math.dist(c['point'], primary['point']) > primary['radius']*2
                          and c['peakContrast'] >= primary['peakContrast']/self.config.initial_dominance
                          and c['area'] >= primary['area']*self.config.strong_other_area_ratio
                          and sum(self._inside(c['point'], other) for other in valid_boxes.values() if other is not None) <= primary_owners), None)
            if rival is not None or primary.get('componentAmbiguous'):
                item['head']['status'] = 'ambiguous'; item['reasons'] = ['reference_core_ambiguous']; continue
            chosen[oid] = primary
            compact = self._compact(primary)
            context = [self._compact(c) for c in candidates if c is not primary
                and c['polarity'] == primary['polarity']
                and math.dist(c['point'], primary['point']) > primary['radius']*2
                and c['area'] >= primary['area']*self.config.foreign_min_area_ratio
                and c['ringContrast'] >= primary['ringContrast']*self.config.foreign_min_contrast_ratio]
            self._references[oid] = _Reference(frame_index, list(box), compact, frame_index,
                    list(primary['point']), context, frame_index, list(box))
            item.update(geometryReferenceFrameIndex=frame_index, geometryReferenceBBox=list(box))
            item['head'].update(status='matched', associationStatus='reliable', point=primary['point'],
                referencePoint=primary['point'], referenceFrameIndex=frame_index,
                radius=primary['radius'], confidence=1.0, metrics={'polarity': primary['polarity']})
            item['coverage']['metrics']['referenceContextCoreCount'] = len(context)
            item['thresholds'] = {'coreContrast': threshold, 'backgroundNoise': noise}
            item['reasons'] = ['trusted_optical_reference']
        return results

    def bind_geometry(self, object_id, frame_index, bbox):
        ref = self._references.get(object_id); box = self._box(bbox)
        if ref is not None and box is not None and ref.frame <= frame_index:
            ref.binding_frame, ref.binding_bbox = frame_index, box

    def export_references(self, frame_index, boxes):
        """Compact server state; no Mask or source-frame pixels are retained."""
        result = {}
        for oid, raw_box in boxes.items():
            ref = self._references.get(oid); box = self._box(raw_box)
            if ref is None or box is None or ref.last_frame != frame_index or ref.ambiguous_since is not None:
                continue
            if not self._inside(ref.last_point, box):
                continue
            result[oid] = {'version': 2, 'source': 'native-frame-mask-v2',
                'sourceSignature': self.source_signature, 'objectId': int(oid),
                'status': 'reliable', 'originFrameIndex': ref.frame,
                'referenceBBox': list(ref.bbox), 'referencePoint': list(ref.core['point']),
                'geometryReferenceFrameIndex': ref.binding_frame,
                'frameIndex': frame_index, 'bbox': box, 'point': list(ref.last_point),
                'radius': ref.core['radius'], 'appearance': self._compact(ref.core),
                'contextCores': list(ref.context_cores)}
        return result

    def restore_references(self, references, *, frame_limit):
        restored = set()
        for oid, value in references.items():
            if not isinstance(value, dict) or value.get('version') != 2 or value.get('source') != 'native-frame-mask-v2' or value.get('status') != 'reliable' or value.get('objectId') != oid:
                continue
            if self.source_signature is None or value.get('sourceSignature') != self.source_signature:
                continue
            origin, frame = value.get('originFrameIndex'), value.get('frameIndex')
            box, original = self._box(value.get('bbox')), self._box(value.get('referenceBBox'))
            appearance = value.get('appearance'); point = value.get('point'); original_point = value.get('referencePoint')
            if type(origin) is not int or type(frame) is not int or not 0 <= origin <= frame <= frame_limit or box is None or original is None or not isinstance(appearance, dict):
                continue
            if not self._valid_point(point) or not self._inside(point, box) or not self._valid_point(original_point) or not self._inside(original_point, original):
                continue
            if appearance.get('polarity') not in ('dark', 'bright') or any(type(appearance.get(k)) not in (int,float) or not math.isfinite(appearance[k]) or appearance[k] <= 0 for k in ('area','radius','peakContrast','ringContrast','axisRatio')):
                continue
            core = {k: appearance[k] for k in ('polarity','area','radius','peakContrast','ringContrast','axisRatio')}; core['point'] = list(original_point)
            contexts = value.get('contextCores', [])
            if not isinstance(contexts, list) or len(contexts) > 16:
                continue
            contexts = [c for c in contexts if isinstance(c, dict) and self._valid_point(c.get('point'))
                        and c.get('polarity') in ('dark','bright') and type(c.get('radius')) in (int,float) and math.isfinite(c['radius']) and c['radius'] > 0]
            self._references[oid] = _Reference(origin, original, core, frame, list(point), contexts,
                    frame, list(box)); restored.add(oid)
        return restored

    def _valid_point(self, point):
        return isinstance(point,(tuple,list)) and len(point)==2 and all(type(v) in (int,float) and math.isfinite(v) for v in point) and 0 <= point[0] < self.width and 0 <= point[1] < self.height

    def _candidate(self, ref, cores, noise, frame_index):
        cfg = self.config
        candidates = [c for c in cores if c['polarity'] == ref.core['polarity']
            and cfg.core_area_min_ratio <= c['area']/ref.core['area'] <= cfg.core_area_max_ratio
            and c['peakContrast'] >= ref.core['peakContrast']*cfg.core_peak_min_ratio
            and c['ringContrast'] >= noise*cfg.core_min_signal_noise]
        # A previously reliable position beats a connected-component midpoint.
        candidates.sort(key=lambda c: math.dist(c['point'], ref.last_point)/max(2,ref.core['radius'])
                         + abs(math.log(c['area']/ref.core['area']))*.35)
        frame_gap = max(1, frame_index-ref.last_frame)
        diagonal = math.hypot(ref.bbox[2]-ref.bbox[0], ref.bbox[3]-ref.bbox[1])
        max_motion = max(ref.core['radius']*6, diagonal*.5)*30/self.fps*min(frame_gap,8)
        if not candidates or math.dist(candidates[0]['point'], ref.last_point) > max_motion:
            return None, max_motion
        return candidates[0], max_motion

    def advance_references(self, frame_index, frame_bgr):
        """Legacy optical warmup only: no AI geometry or mask teaches identity."""
        pending = {}
        for oid, ref in self._references.items():
            if frame_index < ref.last_frame or not self._image_ok(frame_bgr):
                continue
            dx,dy = ref.last_point[0]-ref.core['point'][0], ref.last_point[1]-ref.core['point'][1]
            box = [ref.bbox[0]+dx,ref.bbox[1]+dy,ref.bbox[2]+dx,ref.bbox[3]+dy]
            roi = self._roi(frame_bgr, box, ref)
            if roi is None: continue
            gray,bounds = roi; cores,noise,_ = self._cores(gray,bounds,box,ref)
            core,_ = self._candidate(ref,cores,noise,frame_index)
            if core is None or core.get('componentAmbiguous'):
                ref.ambiguous_since = ref.ambiguous_since if ref.ambiguous_since is not None else frame_index
                continue
            pending[oid] = core
        conflicts = self._candidate_conflicts(pending)
        for oid, core in pending.items():
            ref = self._references[oid]
            if oid in conflicts:
                ref.ambiguous_since = ref.ambiguous_since if ref.ambiguous_since is not None else frame_index
            else:
                ref.last_point = list(core['point']); ref.last_frame = frame_index; ref.ambiguous_since = None

    @staticmethod
    def _candidate_conflicts(points):
        conflicts = set(); ids = list(points)
        for index,a in enumerate(ids):
            for b in ids[index+1:]:
                if math.dist(points[a]['point'],points[b]['point']) < max(2,min(points[a]['radius'],points[b]['radius'])*.5):
                    conflicts.update((a,b))
        return conflicts

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
        # Accept binary uint8 encodings (0/1 or 0/255), not arbitrary confidence
        # maps silently thresholded into an apparently verified native Mask.
        if region.data.dtype == np.dtype(np.uint8):
            foreground = int(region.data.max(initial=0))
            if foreground and np.any((region.data != 0) & (region.data != foreground)):
                return None
        x1, y1, x2, y2 = bounds
        output = np.zeros((y2-y1, x2-x1), np.uint8)
        ix1, iy1 = max(x1, mx), max(y1, my); ix2, iy2 = min(x2, mx+mw), min(y2, my+mh)
        if ix2 > ix1 and iy2 > iy1:
            output[iy1-y1:iy2-y1, ix1-x1:ix2-x1] = region.data[iy1-my:iy2-my, ix1-mx:ix2-mx] > 0
        return output, int(np.count_nonzero(region.data))

    def _tail(self, gray, bounds, box, core, mask, noise, other_cores=()):
        cfg = self.config
        ys, xs = np.indices(gray.shape)
        hx, hy = core['point'][0]-bounds[0], core['point'][1]-bounds[1]
        distance = np.hypot(xs-hx, ys-hy)
        other_head_zone = np.zeros(gray.shape, bool)
        for other in other_cores:
            # A connected bright ridge routed through another optically
            # identifiable head can be its tail/halo. Connectivity alone must
            # not assign that neighbour's structure to our current object.
            ox,oy = other['point'][0]-bounds[0],other['point'][1]-bounds[1]
            other_head_zone |= np.hypot(xs-ox,ys-oy) <= max(3.,other['radius']*2)
        other_head_zone &= distance > core['radius']*2.2
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
            # Stop attribution at a neighbouring core, keeping only the
            # connected own-side prefix. A long clear prefix already outside
            # our rectangle remains loss evidence; a neighbour's continuation
            # beyond that head cannot supply the missing pixels/span.
            binary[other_head_zone] = 0
            foreign_boundary = cv2.dilate(other_head_zone.astype(np.uint8),np.ones((3,3),np.uint8)) > 0
            # A Hessian ridge can stay connected through compression gradients
            # even when the visible line beside the object is detached. Clear
            # endpoint pixels alone do not establish that this is our tail.
            # Require a continuous optical path from the near-head region,
            # using the existing contrast/noise threshold and at most the same
            # one-pixel gap repair as the ridge extraction. Do not bridge past
            # another visible head or infer ownership from a model Mask.
            optical_path = cv2.morphologyEx((optical_signal >= clear_signal).astype(np.uint8),
                cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            optical_path = (optical_path > 0) & (binary > 0) & ~other_head_zone
            _, optical_labels = cv2.connectedComponents(optical_path.astype(np.uint8), 8)
            own_labels = np.unique(optical_labels[optical_path & (distance <= core['radius']*2.2)])
            own_path = np.isin(optical_labels, own_labels) & (optical_labels > 0)
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
                raw_omitted = clear_tail & (outside > border)
                attributable_tail = clear_tail & own_path
                omitted = attributable_tail & (outside > border)
                # Preserve the visible candidate endpoint for diagnostics when
                # its ownership is unknown; never present the ROI origin as a
                # measured tail point just because no path was attributable.
                endpoint_pixels = attributable_tail if attributable_tail.any() else clear_tail
                max_index = int(np.argmax(np.where(endpoint_pixels, outside, -1)))
                oy, ox = np.unravel_index(max_index, gray.shape)
                # A connected component running off the ROI can be an image
                # noise network. It is not affirmative loss evidence.
                touches = bool(component[0].any() or component[-1].any() or component[:, 0].any() or component[:, -1].any())
                branches.append({'span': far, 'tailPixels': count,
                    'outsidePixels': int(omitted.sum()),
                    'outsideDistance': float(outside[attributable_tail].max()) if attributable_tail.any() else 0.0,
                    'unattributedOutsidePixels': int((raw_omitted & ~own_path).sum()),
                    'opticallyConnectedTailPixels': int(attributable_tail.sum()),
                    'attributionRequiresOpticalPath': True,
                    'outsidePointAttributable': bool(attributable_tail.any()),
                    'outsidePoint': [float(ox+bounds[0]), float(oy+bounds[1])],
                    'maskCoverage': float(np.count_nonzero(mask[tail]))/count,
                    'clearTailPixels': int(clear_tail.sum()),
                    'clearMaskCoverage': (float(np.count_nonzero(mask[clear_tail]))/int(clear_tail.sum())) if clear_tail.any() else None,
                    'touchesContextBoundary': touches,
                    'attributionStoppedAtOtherCore': bool(np.any(component & foreign_boundary))})
            branches.sort(key=lambda b: b['span'], reverse=True)
            evidence.append({'sigma': sigma, 'branch': branches[0] if branches else None})
        valid = [e['branch'] for e in evidence if e['branch'] and not e['branch']['touchesContextBoundary']]
        tail_loss = False
        if len(valid) == 2:
            coherent = math.dist(valid[0]['outsidePoint'], valid[1]['outsidePoint']) <= max(5, core['radius']*2.5)
            outside_loss = coherent and all(v['outsidePixels'] >= max(cfg.tail_min_outside_pixels, round(core['radius']*2))
                and v['outsideDistance'] > max(border, core['radius']) for v in valid)
            mask_loss = all(v['clearTailPixels'] >= 12 and v['clearMaskCoverage'] is not None
                and v['clearMaskCoverage'] < cfg.tail_mask_min_coverage for v in valid)
            # The exported annotation is a rectangle. A small optical ridge
            # omitted from a Mask but still inside that rectangle is a quality
            # warning, not affirmative evidence that the BBox lost its tail.
            tail_loss = outside_loss
        return tail_loss, {'tailScales': evidence, 'tailVisibleAtBothScales': len(valid) == 2,
                           'tailOutsideTolerance': border, 'ridgeResponseThreshold': min_response,
                           'tailClearOpticalContrast': clear_signal,
                           'tailLossMinDistance': core['radius']*cfg.tail_loss_min_core_radii,
                           'localMaskCoverageWarning': bool(len(valid) == 2 and mask_loss),
                           'tailAttributionUncertain': any(e['branch'] and
                               e['branch']['unattributedOutsidePixels'] >= cfg.tail_min_outside_pixels
                               for e in evidence),
                           'tailAssociationAmbiguous': any(e['branch'] and e['branch']['attributionStoppedAtOtherCore'] for e in evidence)}

    def observe(self, frame_index: int, frame_bgr: np.ndarray | None,
                boxes: Mapping[int, Sequence[float]], masks: Mapping[int, MaskRegion] | None = None) -> dict[int, dict]:
        results = {}; regions = {}; points = {}; pending_updates = {}; unreferenced_optics = {}
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
            if ref is not None and (frame_index < ref.frame or frame_index < ref.last_frame):
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
            # This records that the source image and nonempty native Mask were
            # actually inspected. It is deliberately separate from identity:
            # an absent/ambiguous human optical reference stays unknown.
            bx1, by1 = max(0, math.ceil(box[0])-bounds[0]), max(0, math.ceil(box[1])-bounds[1])
            bx2, by2 = min(mask.shape[1], math.ceil(box[2])-bounds[0]), min(mask.shape[0], math.ceil(box[3])-bounds[1])
            item['observationVerified'] = (mask_area > 0 and bx2 > bx1 and by2 > by1
                and bool(np.any(mask[by1:by2, bx1:bx2])))
            cores, noise, threshold = self._cores(gray, bounds, box, ref)
            if ref is None:
                if item['observationVerified']:
                    unreferenced_optics[oid] = (cores, noise, bounds, mask)
                item['reasons'] = ['reference_missing']; continue
            cfg = self.config
            core, max_motion = self._candidate(ref, cores, noise, frame_index)
            item['thresholds'] = {'coreContrast': threshold, 'backgroundNoise': noise,
                'maxAssociationDistance': max_motion, 'maskHeadMinCoverage': cfg.mask_head_min_coverage,
                'bboxHeadMinCoverage': cfg.bbox_head_min_coverage,
                'maskHeadBoundaryMinCoverage': cfg.mask_head_boundary_min_coverage,
                'bboxHeadBoundaryMinCoverage': cfg.bbox_head_boundary_min_coverage,
                'coreAreaMin': ref.core['area']*cfg.core_area_min_ratio,
                'coreAreaMax': ref.core['area']*cfg.core_area_max_ratio,
                'corePeakMin': ref.core['peakContrast']*cfg.core_peak_min_ratio,
                'foreignMinAreaRatio': cfg.foreign_min_area_ratio,
                'foreignMinContrastRatio': cfg.foreign_min_contrast_ratio,
                'coreMinSignalNoise': cfg.core_min_signal_noise,
                'strongOtherPeakRatio': cfg.strong_other_peak_ratio,
                'strongOtherAreaRatio': cfg.strong_other_area_ratio}
            if core is None:
                item['reasons'] = ['optical_core_unknown']; continue
            covered = float(np.count_nonzero(mask[core['pixels']]))/core['area']
            core_y, core_x = np.nonzero(core['pixels'])
            core_x = core_x + bounds[0]; core_y = core_y + bounds[1]
            bbox_covered = float(np.count_nonzero((core_x >= box[0]) & (core_x < box[2])
                & (core_y >= box[1]) & (core_y < box[3])))/core['area']
            head_inside = self._inside(core['point'], box)
            head_edge_distance = min(core['point'][0]-box[0], box[2]-core['point'][0],
                core['point'][1]-box[1], box[3]-core['point'][1])
            boundary_metrics = {'maskHeadCoverage': covered, 'bboxHeadCoverage': bbox_covered,
                'headCenterInsideBox': head_inside, 'headCenterEdgeDistance': head_edge_distance}
            item['coverage']['metrics'].update(boundary_metrics)
            slight_mask_deficit = (covered < cfg.mask_head_min_coverage
                and covered >= cfg.mask_head_boundary_min_coverage
                and bbox_covered >= cfg.bbox_head_boundary_min_coverage and head_inside)
            previous = ref.last_point if ref.last_frame < frame_index else None
            previous_frame = ref.last_frame if previous is not None else None
            item['head'].update(status='matched', associationStatus='reliable', point=core['point'], radius=core['radius'],
                previousPoint=None if previous is None else list(previous), previousFrameIndex=previous_frame,
                confidence=min(1.0, core['ringContrast']/(noise*cfg.core_min_signal_noise*2)),
                metrics={"polarity": core['polarity'], "areaRatio": core['area']/ref.core['area'],
                    "ringContrast": core['ringContrast'], "peakContrast": core['peakContrast'],
                    "maskCoverage": covered})
            if (mask_area == 0 or (covered < cfg.mask_head_min_coverage and not slight_mask_deficit)
                    or bbox_covered < cfg.bbox_head_min_coverage or not head_inside):
                item['status'] = 'risk'; item['head']['status'] = 'lost'
                item['reasons'] = ['mask_head_missing']; continue
            if core.get('componentAmbiguous'):
                item['status'] = 'uncertain'; item['head']['status'] = 'ambiguous'
                item['head']['associationStatus'] = 'temporary-occlusion'
                item['coverage']['status'] = 'subject_present_uncertain'
                item['coverage']['metrics'].update(maskHeadCoverage=covered, subjectWithinBox=True)
                item['reasons'] = ['connected_optical_modes_ambiguous']
                ref.ambiguous_since = ref.ambiguous_since if ref.ambiguous_since is not None else frame_index
                logging.getLogger('review.tracking').info('tracking.identity_frozen object=%s frame=%s lastReliable=%s reason=%s', oid, frame_index, ref.last_frame, item['reasons'][0])
                continue
            points[oid] = core
            padded = np.pad(mask, 1)
            dist = cv2.distanceTransform(padded, cv2.DIST_L2, 5)
            _, thickness, _, location = cv2.minMaxLoc(dist)
            thick_point = [float(location[0]-1+bounds[0]), float(location[1]-1+bounds[1])]
            metrics = {**boundary_metrics, 'maxThicknessPoint': thick_point,
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
            possible_neighbors = [c for c in cores if c is not core and c['polarity'] == core['polarity']
                and math.dist(c['point'],core['point']) > core['radius']*2
                and c['area'] >= ref.core['area']*cfg.foreign_min_area_ratio
                and c['ringContrast'] >= noise*cfg.core_min_signal_noise]
            tail_loss, tail_metrics = self._tail(gray,bounds,box,core,mask,noise,possible_neighbors)
            metrics.update(tail_metrics)
            if tail_loss:
                # Own-subject retention and Mask contamination are orthogonal
                # to clear, attributable structure excluded by its rectangle.
                item['status'] = 'risk'; item['coverage']['status'] = 'tail_loss'
                item['reasons'] = ['connected_structure_loss']
            elif strong:
                item['status'] = 'warning'; item['coverage']['status'] = 'mask_contamination'
                item['reasons'] = ['own_subject_retained_with_other_masked_cores']; metrics['otherCoreCount'] = len(strong)
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
                        item['status'] = 'warning'; item['coverage']['status'] = 'mask_contamination'
                        item['reasons'] = ['own_subject_retained_with_foreign_mask_focus']
                if item['status'] == 'warning':
                    pass
                elif known_focus is not None:
                    # Existing incidental cores are not automatically judged
                    # harmless. Unknown retains the old geometry protection;
                    # in particular it cannot waive the real object-16 loss.
                    item['coverage']['status'] = 'subject_present_uncertain'
                    item['status'] = 'uncertain'
                    item['reasons'] = ['accepted_core_context_ambiguous']
                    metrics['baselineContextMatched'] = True
                    metrics['baselineContextCore'] = known_focus
                else:
                    ys, xs = np.nonzero(mask)
                    span = float(np.hypot(xs+bounds[0]-core['point'][0], ys+bounds[1]-core['point'][1]).max())
                    metrics['maskExtentFromCore'] = span
                    if tail_metrics.get('tailAttributionUncertain'):
                        item['status'] = 'warning'; item['coverage']['status'] = 'subject_present_uncertain'
                        item['reasons'] = ['tail_structure_attribution_uncertain']
                        logging.getLogger('review.tracking').debug(
                            'tracking.tail_attribution_uncertain object=%s frame=%s outsidePixels=%s',
                            oid, frame_index, [e['branch']['unattributedOutsidePixels']
                                if e['branch'] else 0 for e in tail_metrics['tailScales']])
                    elif tail_metrics.get('localMaskCoverageWarning'):
                        item['status'] = 'warning'; item['coverage']['status'] = 'local_mask_coverage'
                        item['reasons'] = ['local_mask_coverage_only']
                    elif span < core['radius']*3:
                        # A reliable own head is present, while no resolvable
                        # body establishes loss. This is an observation limit,
                        # not evidence that a smaller/rotated BBox lost a tail.
                        item['status'] = 'warning'; item['coverage']['status'] = 'subject_present_uncertain'
                        item['reasons'] = ['body_extent_unknown']
                    else:
                        item['status'] = 'supported'; item['coverage']['status'] = 'no_clear_loss'
                        item['reasons'] = ['referenced_core_matched', 'no_clear_major_loss']
            if slight_mask_deficit and item['status'] not in ('risk', 'uncertain'):
                item['status'] = 'warning'; item['coverage']['status'] = 'local_mask_coverage'
                item['reasons'] = ['partial_mask_head_boundary'] + item['reasons']
            # Observed location may advance; the explicit optical reference and
            # its appearance/scale never advance from AI output.
            pending_updates[oid] = core
        self._joint_identity(results, regions, points, boxes)
        self._unreferenced_single_core_conflicts(results, regions, points, boxes, unreferenced_optics)
        for oid, core in pending_updates.items():
            ref = self._references[oid]
            if results[oid]['coverage']['status'] == 'merge':
                ref.ambiguous_since = ref.ambiguous_since if ref.ambiguous_since is not None else frame_index
                results[oid]['head']['associationStatus'] = 'ambiguous'
            else:
                ref.last_frame = frame_index; ref.last_point = list(core['point']); ref.ambiguous_since = None
        references = self.export_references(frame_index, boxes)
        for oid, reference in references.items():
            results[oid]['identityReference'] = reference
        self._last_reports = results
        return results

    def _unreferenced_single_core_conflicts(self, results, regions, points, boxes, observations):
        """Record current optical collapse without inventing the missing ID's head.

        Near-identical Masks sharing the sole visible native core merit bounded
        observation. The diagnostic is separate from identity/reference state:
        a short complete occlusion may be natural, and a second visible core or
        independent Mask defeats this particular collapse evidence entirely.
        """
        cfg = self.config
        for oid, (cores, noise, bounds, mask) in observations.items():
            item = results[oid]
            if item['status'] != 'unknown' or item['reasons'] != ['reference_missing']:
                continue
            conflicts = []
            for partner, core in points.items():
                peer = results[partner]
                if (peer['status'] not in ('supported', 'warning')
                        or peer['head']['status'] != 'matched'
                        or peer['head'].get('associationStatus') != 'reliable'
                        or not peer['observationVerified']):
                    continue
                union = [min(boxes[oid][0], boxes[partner][0]), min(boxes[oid][1], boxes[partner][1]),
                    max(boxes[oid][2], boxes[partner][2]), max(boxes[oid][3], boxes[partner][3])]
                if not (bounds[0] <= union[0] and bounds[1] <= union[1]
                        and union[2] <= bounds[2] and union[3] <= bounds[3]):
                    continue
                # Count both optical polarities across both rectangles, not
                # just the Mask foreground. Uninspected context or a second
                # visible subject cannot establish a sole shared native core.
                visible = [candidate for candidate in cores
                    if (self._inside(candidate['point'], boxes[oid]) or self._inside(candidate['point'], boxes[partner]))
                    and candidate['ringContrast'] >= noise*cfg.core_min_signal_noise]
                if any(candidate.get('componentAmbiguous') for candidate in visible):
                    continue
                distinct = []
                for candidate in visible:
                    if all(math.dist(candidate['point'], other['point']) >= max(2., min(candidate['radius'], other['radius'])*.5)
                           for other in distinct):
                        distinct.append(candidate)
                if len(distinct) != 1:
                    continue
                observed = distinct[0]
                if not self._inside(observed['point'], boxes[oid]) or not self._inside(observed['point'], boxes[partner]):
                    continue
                coverage = float(np.count_nonzero(mask[observed['pixels']]))/observed['area']
                if coverage < cfg.unresolved_shared_core_min_coverage:
                    continue
                if math.dist(observed['point'], core['point']) >= max(2., min(observed['radius'], core['radius'])*.5):
                    continue
                peer_coverage = peer['head']['metrics']['maskCoverage']
                if peer_coverage < cfg.unresolved_shared_core_min_coverage:
                    continue
                overlap = self._mask_iou(regions[oid], regions[partner], boxes[oid], boxes[partner])
                if overlap < cfg.unresolved_shared_mask_iou_min:
                    continue
                for current, other in ((oid, partner), (partner, oid)):
                    results[current]['coverage']['metrics'].setdefault('maskOverlap', {})[str(other)] = overlap
                conflicts.append({'reason': 'unreferenced_single_core_shared',
                    'partnerObjectId': int(partner), 'point': list(observed['point']),
                    'radius': observed['radius'], 'visibleCoreCount': len(distinct),
                    'maskIoU': overlap, 'maskHeadCoverage': coverage,
                    'partnerMaskHeadCoverage': peer_coverage})
            if conflicts:
                # One deterministic pair bounds payload size and gives the
                # detector an explicit partner for its continuity checks.
                item['singleCoreConflict'] = max(conflicts, key=lambda value: (value['maskIoU'], -value['partnerObjectId']))

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
