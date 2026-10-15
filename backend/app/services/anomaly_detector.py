"""Stateful anomaly detection for SAM3 object tracking.

Size and shape use the latest manual annotation or whole-frame human checkpoint.
Legacy per-object approvals remain bounded supplementary shapes. Predictions
cannot create a checkpoint or change manual provenance, so AI drift cannot
teach itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
from enum import Enum
import logging
import math
import os
from statistics import median
from typing import Any, Iterable


class AnomalyLevel(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    ANOMALY = "anomaly"
    DISAPPEARED = "disappeared"


@dataclass
class AnomalyConfig:
    """All tunable tracking-anomaly thresholds live here."""

    # Ratios are current AI bbox / latest manual bbox.
    MANUAL_AREA_RATIO_MIN_HARD: float = 0.55
    MANUAL_AREA_RATIO_MAX_HARD: float = 1.80
    MANUAL_WIDTH_RATIO_MIN_HARD: float = 0.65
    MANUAL_WIDTH_RATIO_MAX_HARD: float = 1.50
    MANUAL_HEIGHT_RATIO_MIN_HARD: float = 0.65
    MANUAL_HEIGHT_RATIO_MAX_HARD: float = 1.50
    MANUAL_ASPECT_CHANGE_HARD: float = 1.80

    MANUAL_AREA_RATIO_MIN_WARN: float = 0.70
    MANUAL_AREA_RATIO_MAX_WARN: float = 1.55
    MANUAL_DIMENSION_RATIO_MIN_WARN: float = 0.78
    MANUAL_DIMENSION_RATIO_MAX_WARN: float = 1.35
    MANUAL_ASPECT_CHANGE_WARN: float = 1.50

    GEOMETRY_PERSISTENCE_FRAMES: int = 3
    GEOMETRY_REFERENCE_LIMIT: int = 3
    GEOMETRY_EXTREME_AREA_MIN: float = 0.25
    GEOMETRY_EXTREME_AREA_MAX: float = 4.00
    GEOMETRY_EXTREME_DIMENSION_MIN: float = 0.35
    GEOMETRY_EXTREME_DIMENSION_MAX: float = 2.50
    GEOMETRY_EXTREME_ASPECT_CHANGE: float = 4.00
    GEOMETRY_EXTREME_MASK_LOSS: float = 0.25
    # With a current native image/Mask observation, a rotating flexible tail
    # cannot be judged lost from axis-aligned width/height/area alone. These
    # conservative whole-box scale limits still guard clear collapse/explosion
    # against a fixed human reference; AI output never updates the reference.
    GEOMETRY_EXTREME_DIAGONAL_MIN: float = 0.40
    GEOMETRY_EXTREME_DIAGONAL_MAX: float = 2.00
    IMAGE_PERSISTENCE_FRAMES: int = 3
    SHARED_CORE_PERSISTENCE_FRAMES: int = 12
    INDEPENDENT_MASK_IOU_MAX: float = 0.20

    # Motion is expressed in manual-box diagonals per 1/30 second. These are
    # initial decision thresholds, not pixel limits. Only explicit human
    # samples may expand them; recent AI boxes describe motion, not normality.
    MOTION_WARN_NORMALIZED: float = 0.60
    MOTION_PAUSE_NORMALIZED: float = 1.00
    MOTION_EXTREME_NORMALIZED: float = 3.00
    MOTION_PERSISTENCE_FRAMES: int = 3
    MOTION_FEEDBACK_MARGIN: float = 1.20
    MOTION_EXTREME_MULTIPLIER: float = 2.50
    MOTION_MIN_SCALE_PX: float = 8.0
    MOTION_HISTORY_FRAMES: int = 8

    EDGE_EXIT_MARGIN_PX: float = 10.0
    # A bbox clipped by the left/right image edge naturally becomes narrower
    # while an object is leaving the field of view.  This margin is separate
    # from EDGE_EXIT_MARGIN_PX because size-shrink suppression intentionally
    # applies to the two horizontal sides only, not the top/bottom edges.
    SIDE_EXIT_MARGIN_PX: float = float(os.getenv("ANOMALY_SIDE_EXIT_MARGIN_PX", "10"))
    OVERLAP_WARN: float = 0.85
    OVERLAP_HARD: float = 0.95
    OVERLAP_PERSISTENCE_FRAMES: int = 3
    OVERLAP_MERGE_IOU: float = 0.85
    OVERLAP_REFERENCE_COVERAGE_MARGIN: float = 0.03
    OVERLAP_REFERENCE_IOU_MARGIN: float = 0.10
    OVERLAP_REFERENCE_DIMENSION_MIN: float = 0.65
    OVERLAP_REFERENCE_DIMENSION_MAX: float = 1.50
    OVERLAP_REFERENCE_AREA_MIN: float = 0.55
    OVERLAP_REFERENCE_AREA_MAX: float = 1.80
    REVIEW_LOOKBACK_FRAMES: int = int(os.getenv("ANOMALY_REVIEW_LOOKBACK_FRAMES", "5"))


def _bbox_wh(bbox) -> tuple[float, float]:
    return max(0.0, float(bbox[2]) - float(bbox[0])), max(0.0, float(bbox[3]) - float(bbox[1]))


def _bbox_area(bbox) -> float:
    width, height = _bbox_wh(bbox)
    return width * height


def _bbox_center(bbox) -> tuple[float, float]:
    return (float(bbox[0]) + float(bbox[2])) / 2.0, (float(bbox[1]) + float(bbox[3])) / 2.0


def _bbox_aspect(bbox) -> float:
    width, height = _bbox_wh(bbox)
    return width / height if height > 0 else 0.0


def _intersection_area(a, b) -> float:
    x1, y1 = max(float(a[0]), float(b[0])), max(float(a[1]), float(b[1]))
    x2, y2 = min(float(a[2]), float(b[2])), min(float(a[3]), float(b[3]))
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _iou(a, b) -> float:
    intersection = _intersection_area(a, b)
    union = _bbox_area(a) + _bbox_area(b) - intersection
    return intersection / union if union > 0 else 0.0


def _overlap_coverage(a, b) -> float:
    intersection = _intersection_area(a, b)
    smaller = min(_bbox_area(a), _bbox_area(b))
    return intersection / smaller if smaller > 0 else 0.0


@dataclass(frozen=True)
class ManualBaseline:
    bbox: list[float]
    frame_index: int
    name: str | None = None
    source: str = 'manual'
    mask_area: float | None = None


@dataclass(frozen=True)
class ConfirmedGeometryReference:
    bbox: list[float]
    frame_index: int
    mask_area: int | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class OverlapReference:
    pair: tuple[int, int]
    boxes: dict[int, list[float]]
    frame_index: int
    event_id: str | None = None
    source: str = "confirmed-normal"


@dataclass
class _OverlapState:
    count: int = 0
    started_frame: int | None = None
    last_frame: int | None = None


@dataclass
class _ObjectState:
    level: AnomalyLevel = AnomalyLevel.NORMAL
    missing_count: int = 0
    is_dead: bool = False
    dead_at_frame: int | None = None
    motion_history: list[list[float]] = field(default_factory=list)
    motion_frame_indices: list[int] = field(default_factory=list)
    motion_streak: int = 0
    seed_motion_scale: float | None = None
    manual_baseline: ManualBaseline | None = None
    has_paused: bool = False
    geometry_count: int = 0
    geometry_started_frame: int | None = None
    geometry_last_frame: int | None = None
    geometry_category: str | None = None
    mask_area_logged_decision: str | None = None
    image_count: int = 0
    image_started_frame: int | None = None
    image_last_frame: int | None = None
    image_category: str | None = None
    image_partner_object_id: int | None = None
    head_motion_history: list[list[float]] = field(default_factory=list)
    head_motion_frame_indices: list[int] = field(default_factory=list)
    motion_reference_kind: str = "bbox"

    @property
    def history(self) -> list[list[float]]:
        """Compatibility alias; never used as the size/shape baseline."""
        return self.motion_history


@dataclass
class AnomalyFrame:
    frame_index: int
    object_id: int
    level: AnomalyLevel
    reasons: list[str]
    details: dict[str, Any]


@dataclass
class AnomalyReport:
    frame_index: int
    object_levels: dict[int, AnomalyLevel]
    frames: list[AnomalyFrame]
    should_pause: bool
    pause_frame_index: int | None = None


class AnomalyDetector:
    def __init__(
        self,
        config: AnomalyConfig | None = None,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: float = 30,
        all_object_ids: Iterable[int] | None = None,
        manual_baselines: dict[int, ManualBaseline | dict[str, Any]] | None = None,
        normal_feedback: Iterable[dict[str, Any]] | None = None,
        geometry_references: dict[int, ConfirmedGeometryReference | list[ConfirmedGeometryReference]] | None = None,
        overlap_references: dict[tuple[int, int], OverlapReference] | None = None,
    ) -> None:
        self.config = config or AnomalyConfig()
        self.frame_width = int(frame_width)
        self.frame_height = int(frame_height)
        self.fps = float(fps) if float(fps) > 0 else 30.0
        self.all_object_ids = {int(value) for value in (all_object_ids or [])}
        self.states: dict[int, _ObjectState] = {}
        self.reports: list[AnomalyReport] = []
        self.current_frame_index = -1
        self.normal_motion_samples: dict[int, list[float]] = {}
        self.normal_head_motion_samples: dict[int, list[float]] = {}
        self.geometry_references = dict(geometry_references or {})
        self.overlap_references = dict(overlap_references or {})
        self._seed_overlap_references: dict[tuple[int, int], OverlapReference] = {}
        self._overlap_states: dict[tuple[int, int], _OverlapState] = {}
        self._separated_pairs: dict[tuple[int, int], int] = {}
        self.prime_normal_feedback(normal_feedback or [])
        if manual_baselines:
            self.prime_manual_baselines(manual_baselines)

    def _state(self, object_id: int) -> _ObjectState:
        if object_id not in self.states:
            self.states[object_id] = _ObjectState()
        return self.states[object_id]

    def prime_normal_feedback(self, samples: Iterable[dict[str, Any]]) -> None:
        """Load server-validated samples belonging to this media only.

        There is deliberately no API for learning from predictions. A caller
        must persist the user's explicit normal decision before supplying it.
        Other reason types cannot weaken the motion or independent checks.
        """
        self.normal_motion_samples = {}
        self.normal_head_motion_samples = {}
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("reason") != "motion":
                continue
            if sample.get("decision") != "normal" or sample.get("calibrate") is not True:
                continue
            try:
                oid = int(sample["objectId"])
                normalized = float(sample["features"]["motionNormalized"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if oid <= 0 or not math.isfinite(normalized) or normalized <= 0:
                continue
            kind = sample.get("features", {}).get("referenceKind", "bbox")
            if kind not in {"bbox", "head"}:
                continue
            destination = self.normal_head_motion_samples if kind == "head" else self.normal_motion_samples
            destination.setdefault(oid, []).append(normalized)

    def _remember_motion(self, state: _ObjectState, frame_index: int, bbox: list[float]) -> None:
        if state.motion_frame_indices and state.motion_frame_indices[-1] == frame_index:
            state.motion_history[-1] = list(bbox)
            return
        state.motion_history.append(list(bbox))
        state.motion_frame_indices.append(frame_index)
        limit = max(3, int(self.config.MOTION_HISTORY_FRAMES))
        del state.motion_history[:-limit]
        del state.motion_frame_indices[:-limit]

    def _remember_head_motion(self, state: _ObjectState, frame_index: int, point: list[float]) -> None:
        if state.head_motion_frame_indices and state.head_motion_frame_indices[-1] == frame_index:
            state.head_motion_history[-1] = list(point)
            return
        state.head_motion_history.append(list(point))
        state.head_motion_frame_indices.append(frame_index)
        limit = max(3, int(self.config.MOTION_HISTORY_FRAMES))
        del state.head_motion_history[:-limit]
        del state.head_motion_frame_indices[:-limit]

    @staticmethod
    def _reset_image_observation(state: _ObjectState) -> None:
        state.image_count = 0
        state.image_started_frame = state.image_last_frame = None
        state.image_category = None
        state.image_partner_object_id = None

    def set_manual_baseline(self, object_id: int, bbox, frame_index: int, name: str | None = None) -> None:
        """Replace the baseline; callers must pass only ``source=manual``."""
        clean = [float(value) for value in bbox]
        if len(clean) != 4 or _bbox_area(clean) <= 0:
            raise ValueError("manual baseline bbox must contain four valid coordinates")
        oid = int(object_id)
        self.all_object_ids.add(oid)
        state = self._state(oid)
        state.manual_baseline = ManualBaseline(clean, int(frame_index), name)
        state.geometry_count = 0
        state.geometry_started_frame = state.geometry_last_frame = None
        state.geometry_category = None
        self._reset_image_observation(state)
        state.head_motion_history.clear()
        state.head_motion_frame_indices.clear()

    def prime_manual_baselines(self, baselines: dict[int, ManualBaseline | dict[str, Any]]) -> None:
        for raw_oid, value in baselines.items():
            if isinstance(value, ManualBaseline):
                self.set_manual_baseline(raw_oid, value.bbox, value.frame_index, value.name)
                self._state(int(raw_oid)).manual_baseline = value
            else:
                self.set_manual_baseline(
                    raw_oid,
                    value["bbox"],
                    int(value.get("frame_index", value.get("frameIndex", 0))),
                    value.get("name") or value.get("display_name"),
                )

    def prime_history(self, frames: Iterable[tuple[int, dict[int, list[float]]]]) -> None:
        """Prime adjacent-frame motion only, never the manual baseline."""
        for frame_index, frame_objects in frames:
            for raw_oid, bbox in frame_objects.items():
                clean = [float(value) for value in bbox]
                if len(clean) == 4 and _bbox_area(clean) > 0:
                    oid = int(raw_oid)
                    self.all_object_ids.add(oid)
                    self._remember_motion(self._state(oid), int(frame_index), clean)

    def initialize_seed(self, frame_index: int, frame_objects: dict[int, list[float]]) -> None:
        """Prime motion at the seed without changing manual baselines."""
        self.current_frame_index = int(frame_index)
        self._seed_overlap_references.clear()
        self._overlap_states.clear()
        self._separated_pairs.clear()
        levels: dict[int, AnomalyLevel] = {}
        for raw_oid, bbox in frame_objects.items():
            clean = [float(value) for value in bbox]
            if len(clean) != 4 or _bbox_area(clean) <= 0:
                continue
            oid = int(raw_oid)
            self.all_object_ids.add(oid)
            state = self._state(oid)
            self._remember_motion(state, int(frame_index), clean)
            if state.seed_motion_scale is None:
                state.seed_motion_scale = max(self.config.MOTION_MIN_SCALE_PX, math.hypot(*_bbox_wh(clean)))
            state.level = AnomalyLevel.NORMAL
            state.is_dead = False
            state.has_paused = False
            state.motion_streak = 0
            state.geometry_count = 0
            state.geometry_started_frame = state.geometry_last_frame = None
            state.geometry_category = None
            self._reset_image_observation(state)
            state.head_motion_history.clear()
            state.head_motion_frame_indices.clear()
            state.motion_reference_kind = "bbox"
            levels[oid] = AnomalyLevel.NORMAL
        items = sorted((int(oid), list(bbox)) for oid, bbox in frame_objects.items() if int(oid) in levels)
        for index, (oid_a, bbox_a) in enumerate(items):
            for oid_b, bbox_b in items[index+1:]:
                pair = (oid_a, oid_b)
                if _overlap_coverage(bbox_a, bbox_b) >= self.config.OVERLAP_WARN:
                    self._seed_overlap_references[pair] = OverlapReference(
                        pair, {oid_a: bbox_a, oid_b: bbox_b}, int(frame_index), source="manual-seed",
                    )
                else:
                    self._separated_pairs[pair] = int(frame_index)
        self.reports.append(AnomalyReport(int(frame_index), levels, [], False))

    def _is_near_boundary(self, bbox) -> bool:
        if bbox is None:
            return False
        margin = self.config.EDGE_EXIT_MARGIN_PX
        x1, y1, x2, y2 = [float(value) for value in bbox]
        return x1 <= margin or y1 <= margin or self.frame_width - x2 <= margin or self.frame_height - y2 <= margin

    def _is_near_side_boundary(self, bbox) -> bool:
        """Return whether the current bbox is clipped by a left/right edge."""
        if bbox is None:
            return False
        margin = self.config.SIDE_EXIT_MARGIN_PX
        x1, _, x2, _ = [float(value) for value in bbox]
        return x1 <= margin or self.frame_width - x2 <= margin

    def _review_details(self, frame_index: int) -> dict[str, Any]:
        lookback = max(0, int(self.config.REVIEW_LOOKBACK_FRAMES))
        return {
            "review_lookback_frames": lookback,
            "review_start_frame": max(0, int(frame_index) - lookback),
            "review_end_frame": max(0, int(frame_index) - 1),
        }

    def _identity_evidence(self, oid: int, frame_index: int, bbox: list[float], mask_area,
                           payload) -> tuple[dict[str, Any] | None, str | None]:
        """Validate current native geometry before using optional image cues.

        A source label is provenance, not authentication. The internal producer
        must have checked the native image and real mask; this consumer rejects
        stale, mismatched, nonfinite or inconsistent summaries. Unknown identity
        remains unknown. Only a separately verified native observation can
        downgrade weak rectangle-only accusations; it cannot approve identity.
        """
        if payload is None:
            return None, None

        def number(value):
            return type(value) in (int, float) and math.isfinite(value)

        def finite_json(value, depth=0):
            if depth > 16:
                return False
            if value is None or isinstance(value, (str, bool)):
                return True
            if type(value) in (int, float):
                return math.isfinite(value)
            if isinstance(value, dict):
                return all(isinstance(key, str) and finite_json(item, depth + 1) for key, item in value.items())
            if isinstance(value, (list, tuple)):
                return all(finite_json(item, depth + 1) for item in value)
            return False

        def point(value):
            return (isinstance(value, (list, tuple)) and len(value) == 2
                    and all(number(v) for v in value)
                    and 0 <= value[0] < self.frame_width and 0 <= value[1] < self.frame_height)

        if not isinstance(payload, dict) or not finite_json(payload):
            return None, "invalid_or_nonfinite_summary"
        version = payload.get('version')
        if (type(version) is not int or version not in (1, 2)
                or payload.get("source") != f"native-frame-mask-v{version}"
                or type(payload.get("objectId")) is not int or payload.get("objectId") != oid
                or type(payload.get("frameIndex")) is not int or payload.get("frameIndex") != frame_index):
            return None, "object_frame_or_version_mismatch"
        if version == 2:
            expected_signature = getattr(self, 'image_source_signature', None)
            if expected_signature is not None and payload.get('sourceSignature') != expected_signature:
                return None, 'source_signature_mismatch'
        bound_box = payload.get("bbox")
        if (not isinstance(bound_box, (list, tuple)) or len(bound_box) != 4
                or not all(number(v) for v in bound_box)
                or any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(bound_box, bbox))):
            return None, "bbox_mismatch"
        bound_area = payload.get("maskArea")
        if mask_area is None:
            if bound_area is not None:
                return None, "mask_area_mismatch"
        elif (not number(mask_area) or not number(bound_area) or bound_area < 0
              or abs(float(mask_area) - float(bound_area)) > 1e-6):
            return None, "mask_area_mismatch"
        status, head, coverage = payload.get("status"), payload.get("head"), payload.get("coverage")
        if (status not in {"supported", "risk", "unknown", "warning", "uncertain"} or not isinstance(head, dict)
                or not isinstance(coverage, dict)
                or head.get("status") not in {"matched", "lost", "ambiguous", "unknown"}
                or coverage.get("status") not in {"no_clear_loss", "tail_loss", "foreign_core", "merge", "unknown", "mask_contamination", "local_mask_coverage", "subject_present_uncertain"}
                or not isinstance(payload.get("reasons"), list)
                or any(not isinstance(reason, str) for reason in payload["reasons"])):
            return None, "invalid_evidence_status"
        if any('metrics' in part and not isinstance(part['metrics'], dict) for part in (head, coverage)):
            return None, 'invalid_evidence_metrics'
        for label in ("point", "previousPoint", "referencePoint"):
            if head.get(label) is not None and not point(head[label]):
                return None, "invalid_head_point"
        previous_point, previous_frame = head.get("previousPoint"), head.get("previousFrameIndex")
        if previous_point is not None or previous_frame is not None:
            if (not point(previous_point) or type(previous_frame) is not int
                    or not 0 <= previous_frame < frame_index):
                return None, "invalid_previous_head_binding"
        reference_frame = head.get("referenceFrameIndex")
        if reference_frame is not None and (type(reference_frame) is not int or not 0 <= reference_frame <= frame_index):
            return None, "invalid_reference_head_frame"
        has_optical_reference = (version == 2 and any(value is not None for value in (
            head.get('referenceFrameIndex'), head.get('referencePoint'), head.get('referenceBBox'),
            payload.get('geometryReferenceFrameIndex'), payload.get('geometryReferenceBBox'))))
        if status in {"supported", "risk", "warning", "uncertain"} or has_optical_reference:
            # Reusing a current AI bbox must not revive an optical anchor from
            # before a human edit/checkpoint. The referenced core must be bound
            # to the latest applicable human geometry, not just an earlier frame.
            references = list(self._eligible_geometry_references(oid, frame_index))
            baseline = self._state(oid).manual_baseline
            if baseline is not None and baseline.frame_index <= frame_index:
                references.append(baseline)
            newest = max((ref.frame_index for ref in references), default=None)
            reference_point = head.get("referencePoint")
            binding_frame = payload.get('geometryReferenceFrameIndex') if version == 2 else reference_frame
            matching = [ref for ref in references if ref.frame_index == binding_frame]
            if version == 2:
                signature = payload.get('sourceSignature')
                expected_signature = getattr(self, 'image_source_signature', None)
                if expected_signature is not None and signature != expected_signature:
                    return None, 'source_signature_mismatch'
                geometry_box = payload.get('geometryReferenceBBox')
                origin_box = head.get('referenceBBox')
                if (not isinstance(origin_box,list) or len(origin_box)!=4 or not all(number(v) for v in origin_box)
                        or not 0 <= origin_box[0] < origin_box[2] <= self.frame_width
                        or not 0 <= origin_box[1] < origin_box[3] <= self.frame_height
                        or not point(reference_point) or not origin_box[0] <= reference_point[0] <= origin_box[2]
                        or not origin_box[1] <= reference_point[1] <= origin_box[3]):
                    return None,'invalid_identity_origin_binding'
                if (type(binding_frame) is not int or not isinstance(geometry_box,list) or len(geometry_box)!=4
                        or not all(number(v) for v in geometry_box)
                        or not any(all(abs(a-b)<1e-6 for a,b in zip(ref.bbox,geometry_box)) for ref in matching)
                        or type(reference_frame) is not int or not 0 <= reference_frame <= binding_frame):
                    return None, 'identity_geometry_binding_mismatch'
            if (type(reference_frame) is not int or newest is None or binding_frame != newest
                    or not point(reference_point)
                    or (version == 1 and not any(ref.bbox[0] <= reference_point[0] <= ref.bbox[2]
                               and ref.bbox[1] <= reference_point[1] <= ref.bbox[3] for ref in matching))
                    or (previous_frame is not None and previous_frame < reference_frame)):
                return None, "stale_or_unbound_human_optical_reference"
        if head.get("confidence") is not None and (not number(head["confidence"]) or not 0 <= head["confidence"] <= 1):
            return None, "invalid_head_confidence"
        if head.get("radius") is not None and (not number(head["radius"]) or head["radius"] <= 0):
            return None, "invalid_head_radius"
        if head["status"] == "matched":
            location = head.get("point")
            if (not point(location) or not number(head.get("radius")) or head["radius"] <= 0
                    or not number(head.get("confidence")) or head["confidence"] <= 0
                    or not bbox[0] <= location[0] <= bbox[2] or not bbox[1] <= location[1] <= bbox[3]):
                return None, "matched_subject_outside_current_box_or_unverified"
        if status == "supported" and (head["status"] != "matched" or coverage["status"] != "no_clear_loss"
                                      or not number(bound_area) or bound_area <= 0):
            return None, "unsupported_normal_exemption"
        if status == "risk" and (not number(bound_area)
                or not (head["status"] in {"lost", "ambiguous"}
                        or coverage["status"] in {"tail_loss", "foreign_core", "merge"})):
            return None, "unsubstantiated_image_risk"
        if version == 2 and status in ('supported','warning','uncertain'):
            association = head.get('associationStatus')
            if (association not in ('reliable','temporary-occlusion') or not number(bound_area) or bound_area <= 0
                    or not point(head.get('point')) or not bbox[0] <= head['point'][0] <= bbox[2]
                    or not bbox[1] <= head['point'][1] <= bbox[3]
                    or coverage['status'] not in ('no_clear_loss','mask_contamination','local_mask_coverage','subject_present_uncertain')):
                return None, 'unsubstantiated_identity_preservation'
        return copy.deepcopy(payload), None

    @staticmethod
    def _supported_identity(evidence) -> bool:
        return bool(evidence and evidence.get('version') == 2
                    and evidence.get("status") in {"supported","warning","uncertain"}
                    and evidence['head'].get('associationStatus') in {'reliable','temporary-occlusion'}
                    and evidence["coverage"]["status"] in {'no_clear_loss','mask_contamination','local_mask_coverage','subject_present_uncertain'})

    def _native_observation_verified(self, evidence) -> bool:
        """A current native read is not an affirmative identity match.

        This is consumed only after strict object/frame/box/Mask validation.
        The producer sets the flag after reading a valid native image and a
        nonempty Mask, even if no trusted identity reference can be recovered.
        Missing/broken reads and historical summaries keep the legacy fallback.
        """
        expected = getattr(self, 'image_source_signature', None)
        return bool(evidence and evidence.get('version') == 2
                    and evidence.get('observationVerified') is True
                    and isinstance(expected, list) and len(expected) == 4
                    and evidence.get('sourceSignature') == expected
                    and type(evidence.get('maskArea')) in (int, float)
                    and evidence['maskArea'] > 0
                    and not set(evidence.get('reasons', [])).intersection({
                        'invalid_bbox', 'native_image_unavailable', 'source_unavailable',
                        'source_changed', 'roi_too_large', 'mask_unavailable',
                        'reference_in_future', 'observation_in_future'}))

    def _current_subject_supports_mask_change(self, evidence) -> bool:
        """Require affirmative current image evidence before softening area loss.

        A flexible body can rotate, bend or become faint while its Mask area
        drops far below an explicitly accepted historic Mask. Native readability
        alone is insufficient: unknown identity, a temporary occlusion or a
        positive head/tail/merge diagnosis must retain the conservative fallback.
        This helper consumes only strictly validated summaries, never raw input.
        """
        return bool(self._native_observation_verified(evidence)
                    and evidence.get('status') in {'supported', 'warning', 'uncertain'}
                    and evidence['head'].get('status') == 'matched'
                    and evidence['head'].get('associationStatus') == 'reliable'
                    and evidence['coverage'].get('status') in {
                        'no_clear_loss', 'mask_contamination', 'local_mask_coverage',
                        'subject_present_uncertain'})

    def _single_core_conflict(self, oid: int, frame_index: int, evidence,
                              boxes: dict[int, list[float]], mask_areas: dict[int, int | None],
                              all_evidence: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
        """Cross-check a native unknown object's shared visible-core diagnosis.

        A single visible foreground core plus two almost identical real Masks
        is stronger evidence than overlapping rectangles. It is still not an
        identity reference for the unknown object. A separately trusted partner
        must confirm the same current optical point and Mask intersection.
        """
        if (not self._native_observation_verified(evidence) or evidence.get('status') != 'unknown'
                or evidence.get('head', {}).get('status') != 'unknown'
                or evidence['head'].get('point') is not None
                or 'reference_missing' not in evidence.get('reasons', [])):
            return None
        value = evidence.get('singleCoreConflict')
        if not isinstance(value, dict):
            return None
        peer_id = value.get('partnerObjectId')
        if (type(peer_id) is not int or peer_id == oid or peer_id not in boxes
                or type(value.get('visibleCoreCount')) is not int or value['visibleCoreCount'] != 1
                or value.get('reason') != 'unreferenced_single_core_shared'):
            return None

        def number(v):
            return type(v) in (int, float) and math.isfinite(v)

        point, radius = value.get('point'), value.get('radius')
        if (not isinstance(point, list) or len(point) != 2 or not all(number(v) for v in point)
                or not number(radius) or radius <= 0
                or not number(value.get('maskIoU')) or not .90 <= value['maskIoU'] <= 1
                or any(not number(value.get(key)) or not .80 <= value[key] <= 1
                       for key in ('maskHeadCoverage', 'partnerMaskHeadCoverage'))):
            return None
        peer, error = self._identity_evidence(peer_id, frame_index, boxes[peer_id],
                                              mask_areas.get(peer_id), all_evidence.get(peer_id))
        if (error or not self._native_observation_verified(peer)
                or peer.get('status') not in ('supported', 'warning')
                or peer['head']['status'] != 'matched'
                or peer['head'].get('associationStatus') != 'reliable'):
            return None
        peer_point, peer_radius = peer['head']['point'], peer['head']['radius']
        if (math.dist(point, peer_point) > max(2., min(radius, peer_radius)*.5)
                or not .5 <= radius/peer_radius <= 2.):
            return None
        for item, other, current_box, expected_coverage in (
                (evidence, peer_id, boxes[oid], value['maskHeadCoverage']),
                (peer, oid, boxes[peer_id], value['partnerMaskHeadCoverage'])):
            if not current_box[0] <= point[0] <= current_box[2] or not current_box[1] <= point[1] <= current_box[3]:
                return None
            metrics = item.get('coverage', {}).get('metrics', {})
            if not isinstance(metrics, dict):
                return None
            overlaps = metrics.get('maskOverlap', {})
            overlap = overlaps.get(str(other)) if isinstance(overlaps, dict) else None
            if not number(overlap) or abs(overlap-value['maskIoU']) > 1e-6:
                return None
            if item is peer:
                head_metrics = peer['head'].get('metrics', {})
                if not isinstance(head_metrics, dict):
                    return None
                head_coverage = head_metrics.get('maskCoverage')
                if not number(head_coverage) or abs(head_coverage-expected_coverage) > 1e-6:
                    return None
        return copy.deepcopy(value)

    def _image_observation(self, oid: int, frame_index: int, evidence, *,
                           single_core_conflict: dict[str, Any] | None = None
                           ) -> tuple[bool, bool, list[str], dict[str, Any]]:
        state = self._state(oid)
        if evidence and evidence['status'] == 'unknown' and self._native_observation_verified(evidence) and single_core_conflict is None:
            # The read succeeded but identity could not be established. Keep
            # this visible without turning uncertainty into a repeated pause
            # or an assertion that the object is correctly tracked.
            self._reset_image_observation(state)
            return False, True, ['image_identity_unknown'], {
                'image_quality_reasons': evidence.get('reasons', []),
                'image_identity_status': 'unknown',
            }
        if evidence and evidence['status'] == 'warning':
            self._reset_image_observation(state)
            return False, True, ['image_quality_warning'], {'image_quality_reasons': evidence.get('reasons', [])}
        if not evidence or (evidence["status"] not in {"risk", "uncertain"} and single_core_conflict is None):
            self._reset_image_observation(state)
            return False, False, [], {}
        head, coverage = evidence["head"]["status"], evidence["coverage"]["status"]
        category = ('shared_core_unresolved' if single_core_conflict is not None
                    else f"head_{head}" if head in {"lost", "ambiguous"} else coverage)
        partner = single_core_conflict['partnerObjectId'] if single_core_conflict is not None else None
        same_observation = state.image_category == category and state.image_partner_object_id == partner
        if state.image_last_frame == frame_index and same_observation:
            pass
        elif state.image_last_frame == frame_index - 1 and state.image_count and same_observation:
            state.image_count += 1
        else:
            state.image_count = 1
            state.image_started_frame = frame_index
        state.image_last_frame = frame_index
        state.image_category = category
        state.image_partner_object_id = partner
        required = max(1, int(self.config.IMAGE_PERSISTENCE_FRAMES))
        if evidence['status'] == 'uncertain':
            required = max(12, required*4)
        if single_core_conflict is not None:
            required = max(1, int(self.config.SHARED_CORE_PERSISTENCE_FRAMES))
        hard = state.image_count >= required
        reasons = [f"image_{category} observations={state.image_count}/{required} {'HARD' if hard else 'SOFT'}"]
        details = {"risk_type": "image", "image_risk_reasons": reasons,
                   "image_observation_count": state.image_count, "image_observation_required": required,
                   "image_observation_started_frame": state.image_started_frame,
                   "image_observation_category": category, "image_pending": not hard}
        if single_core_conflict is not None:
            details['single_core_conflict'] = single_core_conflict
            details['image_identity_status'] = 'unknown'
            details['image_observation_partner_object_id'] = partner
        if hard:
            details["pause_started_frame"] = state.image_started_frame
        if state.image_count in {1, required}:
            logging.getLogger("review.tracking").info(
                "tracking.image_risk_observed object=%s frame=%s category=%s observations=%s required=%s first=%s pending=%s",
                oid, frame_index, category, state.image_count, required, state.image_started_frame, not hard)
        return hard, not hard, reasons, details

    def _motion_check(self, oid: int, state: _ObjectState, frame_index: int, bbox: list[float], *,
                      head_evidence: dict[str, Any] | None = None,
                      samples: list[float] | None = None) -> tuple[bool, bool, list[str], dict[str, Any]]:
        """Use time/size normalization plus short history without auto-training."""
        cfg = self.config
        kind = "head" if head_evidence is not None else "bbox"
        if state.motion_reference_kind != kind:
            # A bbox-centre streak and a rigid-subject streak measure different
            # things. Likewise, explicit bbox feedback cannot loosen head motion.
            state.motion_streak = 0
            state.motion_reference_kind = kind
        previous_bbox = state.motion_history[-1] if state.motion_history else None
        if kind == "head":
            history = [list(point) for point in state.head_motion_history]
            indices = list(state.head_motion_frame_indices)
            previous = head_evidence.get("previousPoint")
            previous_frame = head_evidence.get("previousFrameIndex")
            if previous is not None and (not indices or previous_frame > indices[-1]):
                history.append(list(previous))
                indices.append(previous_frame)
            if not history:
                return False, False, [], {"motion_reference_kind": "head", "motion_reference_available": False}
            previous_frame = indices[-1]
            old_center, center = history[-1], head_evidence["point"]
            for known_frame, known_box in zip(state.motion_frame_indices, state.motion_history):
                if known_frame == previous_frame:
                    previous_bbox = known_box
                    break
        else:
            if not state.motion_history:
                return False, False, [], {}
            history = [_bbox_center(box) for box in state.motion_history]
            indices = state.motion_frame_indices
            previous_frame = indices[-1]
            old_center, center = history[-1], _bbox_center(bbox)
        gap = max(1, frame_index - previous_frame)
        dx, dy = center[0] - old_center[0], center[1] - old_center[1]
        shift = math.hypot(dx, dy)
        if state.manual_baseline:
            scale = max(cfg.MOTION_MIN_SCALE_PX, math.hypot(*_bbox_wh(state.manual_baseline.bbox)))
        else:
            # Legacy direct clients can omit source=manual. Fix their first
            # seed scale so growing AI boxes cannot loosen motion detection.
            if state.seed_motion_scale is None:
                state.seed_motion_scale = max(cfg.MOTION_MIN_SCALE_PX, math.hypot(*_bbox_wh(previous_bbox or bbox)))
            scale = state.seed_motion_scale
        normalizer = self.fps / (30.0 * gap * scale)
        normalized = shift * normalizer
        if samples is None:
            samples = (self.normal_head_motion_samples if kind == "head" else self.normal_motion_samples).get(oid, [])
        threshold = max(cfg.MOTION_PAUSE_NORMALIZED, max(samples, default=0.0) * cfg.MOTION_FEEDBACK_MARGIN)
        extreme = max(cfg.MOTION_EXTREME_NORMALIZED, threshold * cfg.MOTION_EXTREME_MULTIPLIER)

        # Velocity is diagnostic context. It never increases the thresholds.
        velocities = []
        for index in range(1, len(history)):
            previous = history[index - 1]
            current = history[index]
            elapsed = max(1, indices[index] - indices[index - 1])
            velocities.append(((current[0] - previous[0]) / elapsed, (current[1] - previous[1]) / elapsed))
        recent_velocity = (median(value[0] for value in velocities), median(value[1] for value in velocities)) if velocities else (0.0, 0.0)
        residual = math.hypot(dx / gap - recent_velocity[0], dy / gap - recent_velocity[1]) * self.fps / (30.0 * scale)

        # A bounded return toward the position two observations ago is typical
        # oscillation. It interrupts a suspected drift streak, but remains a
        # warning; a huge jump is never exempted by returning toward old data.
        oscillating = False
        if len(history) >= 2 and normalized < threshold * 1.5:
            older_center = history[-2]
            return_distance = math.hypot(center[0] - older_center[0], center[1] - older_center[1]) / scale
            oscillating = return_distance < 0.35 and dx * (old_center[0] - older_center[0]) + dy * (old_center[1] - older_center[1]) < 0
        if normalized >= threshold and not oscillating:
            state.motion_streak += 1
        else:
            state.motion_streak = 0
        hard = normalized >= extreme or state.motion_streak >= max(2, cfg.MOTION_PERSISTENCE_FRAMES)
        warning = normalized >= cfg.MOTION_WARN_NORMALIZED and not hard
        reason = "extreme_jump" if normalized >= extreme else "sustained_motion" if hard else "confirmed_motion" if samples and normalized <= threshold else "oscillation" if oscillating else "motion"
        details = {
            "center_shift": shift,
            "motion_reference_bbox": list(previous_bbox) if previous_bbox is not None else None,
            "motion_reference_kind": kind,
            "motion_reference_frame": previous_frame,
            "frame_gap": gap,
            "source_fps": self.fps,
            "object_scale": scale,
            "motion_normalized": normalized,
            "motion_warning_threshold": cfg.MOTION_WARN_NORMALIZED,
            "motion_pause_threshold": threshold,
            "motion_extreme_threshold": extreme,
            "motion_streak": state.motion_streak,
            "motion_oscillation": oscillating,
            "motion_reason": reason,
            "trajectory_residual_normalized": residual,
            "recent_velocity": list(recent_velocity),
            "feedback_sample_count": len(samples),
            "normal_sample_matched": bool(samples and normalized <= threshold),
        }
        if kind == "head":
            details.update(motion_reference_point=list(old_center), motion_current_point=list(center))
        reasons = [f"adjacent_center_shift={shift:.1f}px normalized={normalized:.3f} {reason} {'HARD' if hard else 'SOFT'}"] if hard or warning else []
        return hard, warning, reasons, details

    def _geometry_check(self, bbox: list[float], baseline: ManualBaseline | ConfirmedGeometryReference,
                        source: str) -> tuple[bool, bool, list[str], dict[str, Any]]:
        """Evaluate one complete reference; never combine ratios across shapes."""
        cfg = self.config
        hard = warning = False
        reasons: list[str] = []
        details: dict[str, Any] = {}
        base_width, base_height = _bbox_wh(baseline.bbox)
        cur_width, cur_height = _bbox_wh(bbox)
        base_area, cur_area = _bbox_area(baseline.bbox), _bbox_area(bbox)
        base_aspect, cur_aspect = _bbox_aspect(baseline.bbox), _bbox_aspect(bbox)
        area_ratio = cur_area / base_area if base_area else 0.0
        base_diagonal = math.hypot(base_width, base_height)
        diagonal_ratio = math.hypot(cur_width, cur_height)/base_diagonal if base_diagonal else 0.0
        width_ratio = cur_width / base_width if base_width else 0.0
        height_ratio = cur_height / base_height if base_height else 0.0
        aspect_ratio_ratio = cur_aspect / base_aspect if base_aspect else 0.0
        aspect_change = max(aspect_ratio_ratio, 1.0 / aspect_ratio_ratio) if aspect_ratio_ratio > 0 else float("inf")
        near_side_edge = self._is_near_side_boundary(bbox)
        side_exit_shrink = near_side_edge and (
            area_ratio < cfg.MANUAL_AREA_RATIO_MIN_WARN
            or width_ratio < cfg.MANUAL_DIMENSION_RATIO_MIN_WARN
            or height_ratio < cfg.MANUAL_DIMENSION_RATIO_MIN_WARN
        )
        details.update({
            "geometry_reference_bbox": list(baseline.bbox),
            "geometry_reference_frame": baseline.frame_index,
            "geometry_reference_source": source,
            "baseline_area": base_area,
            "current_area": cur_area,
            "area_ratio": area_ratio,
            "diagonal_ratio": diagonal_ratio,
            "width_ratio": width_ratio,
            "height_ratio": height_ratio,
            "aspect_ratio_ratio": aspect_ratio_ratio,
            "aspect_ratio_change": aspect_change,
            "near_side_edge": near_side_edge,
            "side_exit_shrink_ignored": side_exit_shrink,
            "geometry_extreme": (
                (area_ratio < cfg.GEOMETRY_EXTREME_AREA_MIN and not side_exit_shrink)
                or area_ratio > cfg.GEOMETRY_EXTREME_AREA_MAX
                or (min(width_ratio, height_ratio) < cfg.GEOMETRY_EXTREME_DIMENSION_MIN and not side_exit_shrink)
                or max(width_ratio, height_ratio) > cfg.GEOMETRY_EXTREME_DIMENSION_MAX
                or (aspect_change >= cfg.GEOMETRY_EXTREME_ASPECT_CHANGE and not side_exit_shrink)
            ),
            "geometry_thresholds": {
                "manual_area_ratio": {"min": cfg.MANUAL_AREA_RATIO_MIN_HARD, "max": cfg.MANUAL_AREA_RATIO_MAX_HARD,
                                      "extreme_min": cfg.GEOMETRY_EXTREME_AREA_MIN, "extreme_max": cfg.GEOMETRY_EXTREME_AREA_MAX},
                "manual_width_ratio": {"min": cfg.MANUAL_WIDTH_RATIO_MIN_HARD, "max": cfg.MANUAL_WIDTH_RATIO_MAX_HARD,
                                       "extreme_min": cfg.GEOMETRY_EXTREME_DIMENSION_MIN, "extreme_max": cfg.GEOMETRY_EXTREME_DIMENSION_MAX},
                "manual_height_ratio": {"min": cfg.MANUAL_HEIGHT_RATIO_MIN_HARD, "max": cfg.MANUAL_HEIGHT_RATIO_MAX_HARD,
                                        "extreme_min": cfg.GEOMETRY_EXTREME_DIMENSION_MIN, "extreme_max": cfg.GEOMETRY_EXTREME_DIMENSION_MAX},
                "manual_aspect_change": {"min": None, "max": cfg.MANUAL_ASPECT_CHANGE_HARD,
                                         "extreme_min": None, "extreme_max": cfg.GEOMETRY_EXTREME_ASPECT_CHANGE},
            },
        })
        ratios = (
            ("manual_area_ratio", area_ratio, cfg.MANUAL_AREA_RATIO_MIN_HARD, cfg.MANUAL_AREA_RATIO_MAX_HARD, cfg.MANUAL_AREA_RATIO_MIN_WARN, cfg.MANUAL_AREA_RATIO_MAX_WARN),
            ("manual_width_ratio", width_ratio, cfg.MANUAL_WIDTH_RATIO_MIN_HARD, cfg.MANUAL_WIDTH_RATIO_MAX_HARD, cfg.MANUAL_DIMENSION_RATIO_MIN_WARN, cfg.MANUAL_DIMENSION_RATIO_MAX_WARN),
            ("manual_height_ratio", height_ratio, cfg.MANUAL_HEIGHT_RATIO_MIN_HARD, cfg.MANUAL_HEIGHT_RATIO_MAX_HARD, cfg.MANUAL_DIMENSION_RATIO_MIN_WARN, cfg.MANUAL_DIMENSION_RATIO_MAX_WARN),
        )
        for label, ratio, hard_min, hard_max, warn_min, warn_max in ratios:
            # When the current box is clipped by the left/right edge,
            # a low ratio is expected while the object leaves view.
            # Upper bounds remain active so edge-adjacent growth is
            # still reported as contamination/merged-object tracking.
            below_hard = ratio < hard_min and not side_exit_shrink
            below_warn = ratio < warn_min and not side_exit_shrink
            if below_hard or ratio > hard_max:
                hard = True
                reasons.append(f"{label}={ratio:.3f} HARD")
            elif below_warn or ratio > warn_max:
                warning = True
                reasons.append(f"{label}={ratio:.3f} SOFT")
        # Horizontal clipping can also create an extreme aspect ratio;
        # suppress that derivative signal together with shrink only.
        if not side_exit_shrink and aspect_change >= cfg.MANUAL_ASPECT_CHANGE_HARD:
            hard = True
            reasons.append(f"manual_aspect_change={aspect_change:.3f} HARD")
        elif not side_exit_shrink and aspect_change >= cfg.MANUAL_ASPECT_CHANGE_WARN:
            warning = True
            reasons.append(f"manual_aspect_change={aspect_change:.3f} SOFT")

        return hard, warning, reasons, details

    def _eligible_geometry_references(self, oid: int, frame_index: int) -> list[ConfirmedGeometryReference]:
        raw = self.geometry_references.get(oid, [])
        candidates = raw if isinstance(raw, (list, tuple)) else [raw]
        baseline = self._state(oid).manual_baseline
        return sorted(
            (reference for reference in candidates if isinstance(reference, ConfirmedGeometryReference)
             and reference.frame_index <= frame_index
             and (baseline is None or reference.frame_index > baseline.frame_index)),
            key=lambda reference: reference.frame_index,
        )[-max(1, int(self.config.GEOMETRY_REFERENCE_LIMIT)):]

    def _log_mask_area_decision(self, oid: int, frame_index: int, decision: str | None,
                                current=None, reference=None, ratio=None) -> None:
        """Log transitions only; per-frame measurements remain in diagnostics."""
        state = self._state(oid)
        previous = state.mask_area_logged_decision
        if previous == decision:
            return
        state.mask_area_logged_decision = decision
        logger = logging.getLogger('review.tracking')
        if decision is not None:
            logger.info(
                'tracking.mask_area_candidate object=%s frame=%s current=%s reference=%s ratio=%.6f decision=%s previous=%s',
                oid, frame_index, current, reference, ratio, decision, previous)
        elif previous is not None:
            logger.info('tracking.mask_area_candidate_cleared object=%s frame=%s previous=%s current=%s reference=%s ratio=%s',
                        oid, frame_index, previous, current, reference, ratio)

    def _geometry_observation(self, oid: int, frame_index: int, bbox: list[float], mask_area: int | None = None, *,
                              geometry_supported: bool = False,
                              native_observation_verified: bool = False,
                              mask_area_supported: bool = False,
                              log_mask_decision: bool = True,
                              ) -> tuple[bool, bool, list[str], dict[str, Any]]:
        state, cfg = self._state(oid), self.config
        references: list[tuple[ManualBaseline | ConfirmedGeometryReference, str]] = []
        if state.manual_baseline is not None:
            references.append((state.manual_baseline, state.manual_baseline.source))
        confirmed = self._eligible_geometry_references(oid, frame_index)
        references.extend((reference, "confirmed-normal") for reference in confirmed)
        if not references:
            state.geometry_count = 0
            state.geometry_started_frame = state.geometry_last_frame = None
            state.geometry_category = None
            if log_mask_decision:
                self._log_mask_area_decision(oid, frame_index, None, current=mask_area)
            return False, False, [], {}
        checks = [self._geometry_check(bbox, reference, source) for reference, source in references]
        def physical_extreme(check):
            detail = check[3]
            if native_observation_verified:
                return ((detail['diagonal_ratio'] < cfg.GEOMETRY_EXTREME_DIAGONAL_MIN
                         and not detail['side_exit_shrink_ignored'])
                        or detail['diagonal_ratio'] > cfg.GEOMETRY_EXTREME_DIAGONAL_MAX)
            return ((detail["area_ratio"] < cfg.GEOMETRY_EXTREME_AREA_MIN
                     and not detail["side_exit_shrink_ignored"])
                    or detail["area_ratio"] > cfg.GEOMETRY_EXTREME_AREA_MAX)
        hard, warning, reasons, details = min(
            reversed(checks),
            key=lambda check: 3 if (physical_extreme(check) if geometry_supported or native_observation_verified else check[3].get("geometry_extreme"))
            else 2 if check[0] else 1 if check[1] else 0,
        )
        if geometry_supported or native_observation_verified:
            # A rotating tail changes axis-aligned dimensions without proving
            # subject loss. Independent identity support or an explicitly
            # verified native observation downgrades these weak accusations.
            # Unknown identity is never relabelled as affirmative support.
            details["geometry_supported_by_identity"] = geometry_supported
            details["geometry_native_observation_verified"] = native_observation_verified
            details["geometry_bbox_only_extreme"] = bool(details.get("geometry_extreme"))
            details["geometry_extreme"] = physical_extreme((hard, warning, reasons, details))
            if not details["geometry_extreme"]:
                warning = warning or hard
                hard = False
                reasons = [reason.replace(" HARD", " SOFT") for reason in reasons]
                details["geometry_observed_risk_reasons"] = list(reasons)
            if native_observation_verified:
                details['geometry_thresholds']['manual_diagonal_ratio'] = {
                    'min': cfg.GEOMETRY_EXTREME_DIAGONAL_MIN,
                    'max': cfg.GEOMETRY_EXTREME_DIAGONAL_MAX,
                    'extreme_min': cfg.GEOMETRY_EXTREME_DIAGONAL_MIN,
                    'extreme_max': cfg.GEOMETRY_EXTREME_DIAGONAL_MAX,
                }
                # Ratio diagnostics are still useful, but only the new physical
                # scale guard may be the actionable geometry cause here.
                reasons = [reason.replace(' HARD', ' SOFT') for reason in reasons]
                if details['geometry_extreme']:
                    hard = True
                    reasons.append(f"manual_diagonal_ratio={details['diagonal_ratio']:.3f} HARD")
                details['geometry_bbox_only_warning'] = bool(warning and not hard)
        details["confirmed_geometry_frame"] = max((reference.frame_index for reference in confirmed), default=None)
        details["geometry_reference_count"] = len(references)
        confirmed_areas = [float(reference.mask_area) for reference in confirmed
                           if isinstance(reference.mask_area, (int, float)) and math.isfinite(reference.mask_area)
                           and reference.mask_area > 0]
        if state.manual_baseline and state.manual_baseline.mask_area:
            confirmed_areas.append(state.manual_baseline.mask_area)
        mask_decision = None
        if confirmed_areas and isinstance(mask_area, (int, float)) and math.isfinite(mask_area) and mask_area >= 0:
            # Loss is extreme only relative to every explicitly accepted mask.
            # Neither the initial rectangular prompt nor AI history supplies it.
            mask_reference = min(confirmed_areas)
            ratio = mask_area / mask_reference
            details.update(current_mask_area=mask_area, mask_reference_area=mask_reference, mask_area_ratio=ratio)
            if ratio < cfg.GEOMETRY_EXTREME_MASK_LOSS and not details.get("side_exit_shrink_ignored"):
                # A historical area ratio alone cannot overrule affirmative
                # current head/coverage evidence. This is a warning, not a new
                # human approval; independent image/motion/scale protection is
                # evaluated unchanged and a later failed read restores fallback.
                supported = native_observation_verified and mask_area_supported
                mask_decision = ('reliable_current_subject_warning' if supported
                                 else 'extreme_mask_loss_fallback')
                details['mask_area_decision'] = mask_decision
                if supported:
                    warning = True
                else:
                    hard = True
                    details["geometry_extreme"] = True
                reasons.append(f"confirmed_mask_area_ratio={ratio:.3f} {'SOFT' if supported else 'HARD'}")
        if log_mask_decision:
            self._log_mask_area_decision(oid, frame_index, mask_decision, current=mask_area,
                                        reference=details.get('mask_reference_area'),
                                        ratio=details.get('mask_area_ratio'))
        extreme = bool(details.get("geometry_extreme"))
        details["geometry_risk_reasons"] = [reason for reason in reasons if reason.endswith(" HARD")]
        shrinking = any(details[label] < minimum for label, minimum in (
            ("area_ratio", cfg.MANUAL_AREA_RATIO_MIN_HARD),
            ("width_ratio", cfg.MANUAL_WIDTH_RATIO_MIN_HARD),
            ("height_ratio", cfg.MANUAL_HEIGHT_RATIO_MIN_HARD),
        ))
        growing = any(details[label] > maximum for label, maximum in (
            ("area_ratio", cfg.MANUAL_AREA_RATIO_MAX_HARD),
            ("width_ratio", cfg.MANUAL_WIDTH_RATIO_MAX_HARD),
            ("height_ratio", cfg.MANUAL_HEIGHT_RATIO_MAX_HARD),
        ))
        # Width/height may exchange which metric detects the same direction.
        # Opposite directions cannot accumulate one alleged sustained shrink.
        category = ("shrink" if shrinking and not growing else "growth" if growing and not shrinking else "shape") if hard else None
        if hard and not extreme:
            if state.geometry_last_frame == frame_index and state.geometry_category == category:
                pass  # Re-reading a frame must not count as another observation.
            elif state.geometry_last_frame == frame_index-1 and state.geometry_count and state.geometry_category == category:
                state.geometry_count += 1
            else:
                state.geometry_count = 1
                state.geometry_started_frame = frame_index
            state.geometry_last_frame = frame_index
            state.geometry_category = category
        else:
            state.geometry_count = 0
            state.geometry_started_frame = state.geometry_last_frame = None
            state.geometry_category = None
        required = max(1, int(cfg.GEOMETRY_PERSISTENCE_FRAMES))
        sustained = bool(hard and not extreme and state.geometry_count >= required)
        details.update(geometry_risk=bool(hard), geometry_observation_count=state.geometry_count,
                       geometry_observation_required=required, geometry_observation_started_frame=state.geometry_started_frame,
                       geometry_observation_category=category,
                       geometry_pending=bool(hard and not extreme and not sustained))
        if hard:
            details["risk_type"] = "geometry"
        elif not warning:
            # Fixed limits belong in actionable diagnostics, not every normal
            # object row of a long or crowded tracking result.
            details.pop("geometry_thresholds", None)
        if extreme or sustained:
            details["pause_started_frame"] = frame_index if extreme else state.geometry_started_frame
        elif hard:
            warning = True
            reasons = [reason.replace(" HARD", " SOFT") for reason in reasons]
        return extreme or sustained, warning, reasons, details

    def _pair_reference_matches(self, reference: OverlapReference, boxes: dict[int, list[float]],
                                coverage: float, iou: float) -> bool:
        cfg = self.config
        pair = reference.pair
        if any(oid not in reference.boxes for oid in pair):
            return False
        old_a, old_b = (reference.boxes[oid] for oid in pair)
        if coverage > _overlap_coverage(old_a, old_b)+cfg.OVERLAP_REFERENCE_COVERAGE_MARGIN:
            return False
        if iou > _iou(old_a, old_b)+cfg.OVERLAP_REFERENCE_IOU_MARGIN:
            return False
        for oid in pair:
            old_width, old_height = _bbox_wh(reference.boxes[oid])
            width, height = _bbox_wh(boxes[oid])
            if min(old_width, old_height) <= 0:
                return False
            if not all(cfg.OVERLAP_REFERENCE_DIMENSION_MIN <= ratio <= cfg.OVERLAP_REFERENCE_DIMENSION_MAX
                       for ratio in (width/old_width, height/old_height)):
                return False
            ratio = width*height/(old_width*old_height)
            if not cfg.OVERLAP_REFERENCE_AREA_MIN <= ratio <= cfg.OVERLAP_REFERENCE_AREA_MAX:
                return False
        return True

    def _independent_mask_pair(self, pair: tuple[int, int], evidence: dict[int, dict[str, Any]]) -> bool:
        left, right = (evidence.get(oid) for oid in pair)
        if not self._supported_identity(left) or not self._supported_identity(right):
            return False
        minimum_separation = max(2.0, min(left["head"]["radius"], right["head"]["radius"]) * .5)
        if math.dist(left["head"]["point"], right["head"]["point"]) <= minimum_separation:
            return False
        for item, other in ((left, pair[1]), (right, pair[0])):
            metrics = item["coverage"].get("metrics", {})
            overlaps = metrics.get("maskOverlap", {}) if isinstance(metrics, dict) else {}
            overlap = overlaps.get(str(other)) if isinstance(overlaps, dict) else None
            if (type(overlap) not in (int, float) or not math.isfinite(overlap)
                    or not 0 <= overlap < self.config.INDEPENDENT_MASK_IOU_MAX):
                return False
        return True

    def _pair_observation(self, pair: tuple[int, int], boxes: dict[int, list[float]], frame_index: int, *,
                          identity_evidence: dict[int, dict[str, Any]] | None = None
                          ) -> dict[str, Any] | None:
        cfg = self.config
        bbox_a, bbox_b = (boxes[oid] for oid in pair)
        coverage, iou = _overlap_coverage(bbox_a, bbox_b), _iou(bbox_a, bbox_b)
        state = self._overlap_states.setdefault(pair, _OverlapState())
        if coverage < cfg.OVERLAP_WARN:
            self._overlap_states.pop(pair, None)
            self._separated_pairs[pair] = frame_index
            return None
        references = []
        reference = self.overlap_references.get(pair)
        if (isinstance(reference, OverlapReference) and reference.frame_index <= frame_index
                and reference.frame_index > self._separated_pairs.get(pair, -1) and all(
                    self._state(oid).manual_baseline is None
                    or self._state(oid).manual_baseline.frame_index <= reference.frame_index for oid in pair
                )):
            references.append(reference)
        if pair not in self._separated_pairs:
            seed = self._seed_overlap_references.get(pair)
            if seed is not None:
                references.append(seed)
        matched = next((reference for reference in references
                        if self._pair_reference_matches(reference, boxes, coverage, iou)), None)
        independent = self._independent_mask_pair(pair, identity_evidence or {})
        native_reviewed = all(self._native_observation_verified((identity_evidence or {}).get(oid)) for oid in pair)
        # Similar rectangles are not proof that naturally approaching heads
        # became one tracked identity. Native head/core/major-loss risks have
        # their own independent observation streaks in _image_observation;
        # a high BBox IoU must not double-count or accelerate that same evidence.
        suspect = (coverage >= cfg.OVERLAP_HARD and iou >= cfg.OVERLAP_MERGE_IOU
                   and matched is None and not independent and not native_reviewed)
        if suspect:
            if state.last_frame == frame_index:
                pass
            elif state.last_frame == frame_index-1 and state.count:
                state.count += 1
            else:
                state.count = 1
                state.started_frame = frame_index
            state.last_frame = frame_index
        else:
            state.count = 0
            state.started_frame = state.last_frame = None
        required = max(1, int(cfg.OVERLAP_PERSISTENCE_FRAMES))
        hard = bool(suspect and state.count >= required)
        return {"object_ids": list(pair), "coverage": coverage, "iou": iou,
                "level": AnomalyLevel.ANOMALY.value if hard else AnomalyLevel.WARNING.value,
                "risk_streak": state.count, "persistence_frames": required,
                "threshold": cfg.OVERLAP_HARD, "merge_iou_threshold": cfg.OVERLAP_MERGE_IOU,
                "suspected_merge": suspect, "pause_started_frame": state.started_frame if hard else None,
                "observation_started_frame": state.started_frame,
                "reference_frame": matched.frame_index if matched else None,
                "reference_source": matched.source if matched else None,
                "reference_boxes": matched.boxes if matched else None,
                "normal_reference_matched": matched is not None,
                "independent_masks_supported": independent,
                "native_bbox_overlap_only": native_reviewed}

    def prime_observations(self, frames: Iterable[dict[str, Any]], seed_frame: int,
                           seed_boxes: dict[int, list[float]], *, reset_frames: dict[int, int] | None = None) -> None:
        """Restore short risk observation across rounds without trusting AI seeds.

        Call after ``initialize_seed``. Only geometry/pair counters change: no
        report, motion history, accepted shape or manual baseline is added.
        ``reset_frames`` ends observations at an explicit human reset/correction.
        """
        reset_frames = {int(oid): int(frame) for oid, frame in (reset_frames or {}).items()}
        for oid, state in self.states.items():
            if state.manual_baseline is not None:
                reset_frames[oid] = max(reset_frames.get(oid, -1), state.manual_baseline.frame_index)
        seed_frame = int(seed_frame)
        active = set(seed_boxes)
        count = max(int(self.config.GEOMETRY_PERSISTENCE_FRAMES), int(self.config.OVERLAP_PERSISTENCE_FRAMES),
                    int(self.config.IMAGE_PERSISTENCE_FRAMES), int(self.config.SHARED_CORE_PERSISTENCE_FRAMES), 1)
        rows: dict[int, dict[str, Any]] = {}
        for row in frames:
            if not isinstance(row, dict):
                continue
            try:
                frame = int(row.get("source_frame_index", row.get("frame_index", -1)))
            except (TypeError, ValueError):
                continue
            if 0 <= frame <= seed_frame and isinstance(row.get("objects"), list):
                rows[frame] = row
        chosen = sorted(rows)[-count:]
        if not chosen:
            # An initial human seed is an anchor, never the first predicted
            # observation of a three-frame risk window.
            return
        parsed: list[tuple[int, dict[int, list[float]], dict[int, int | None]]] = []
        evidence_by_frame: dict[int, dict[int, dict[str, Any]]] = {}
        previous_details: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for frame in chosen:
            boxes, masks = {}, {}
            for obj in rows[frame]["objects"]:
                if not isinstance(obj, dict):
                    continue
                try:
                    oid = int(obj.get("object_id", obj.get("objectId")))
                    bbox = [float(value) for value in obj["bbox"]]
                except (TypeError, ValueError, KeyError):
                    continue
                if oid not in active or len(bbox) != 4 or not all(math.isfinite(value) for value in bbox) or _bbox_area(bbox) <= 0:
                    continue
                boxes[oid], masks[oid] = bbox, obj.get("mask_area")
                details = obj.get("anomaly_details", {})
                if isinstance(details, dict):
                    evidence, evidence_error = self._identity_evidence(oid, frame, bbox, masks[oid], details.get("identity_evidence"))
                    if evidence is not None:
                        evidence_by_frame.setdefault(frame, {})[oid] = evidence
                    elif evidence_error:
                        logging.getLogger('review.tracking').warning(
                            'tracking.image_evidence_rejected object=%s frame=%s phase=history reason=%s',
                            oid, frame, evidence_error)
                    for evidence in details.get("overlap_pairs", []):
                        if isinstance(evidence, dict) and isinstance(evidence.get("object_ids"), list) and len(evidence["object_ids"]) == 2:
                            pair = tuple(sorted(evidence["object_ids"]))
                            previous_details.setdefault(pair, []).append({**evidence, "observed_frame": frame})
            if frame == seed_frame:
                boxes = {oid: list(bbox) for oid, bbox in seed_boxes.items()}
            parsed.append((frame, boxes, masks))
        if not parsed or parsed[-1][0] != seed_frame:
            parsed.append((seed_frame, {oid: list(bbox) for oid, bbox in seed_boxes.items()}, {}))

        # A pending pair cannot become trusted solely because its AI output was
        # reused as this round's seed. Retain only the earlier trusted reference
        # recorded by the server; separation or a recorded risk invalidates it.
        for pair in list(self._seed_overlap_references):
            boundary = max(reset_frames.get(oid, -1) for oid in pair)
            evidence = [item for item in previous_details.get(pair, []) if item["observed_frame"] > boundary]
            suspicious = any(item.get("suspected_merge") or item.get("risk_streak", 0) for item in evidence)
            separated = any(set(pair).issubset(boxes) and _overlap_coverage(*(boxes[oid] for oid in pair)) < self.config.OVERLAP_WARN
                            for frame, boxes, _ in parsed if boundary < frame < seed_frame)
            old_reference = next((item for item in reversed(evidence)
                                  if item.get("normal_reference_matched") and item.get("reference_source") == "manual-seed"
                                  and isinstance(item.get("reference_boxes"), dict)), None)
            if suspicious or separated:
                self._seed_overlap_references.pop(pair, None)
            elif old_reference is not None:
                try:
                    boxes = {oid: list(old_reference["reference_boxes"].get(oid, old_reference["reference_boxes"].get(str(oid)))) for oid in pair}
                    frame = int(old_reference["reference_frame"])
                    if frame <= seed_frame and all(len(box) == 4 and _bbox_area(box) > 0 for box in boxes.values()):
                        self._seed_overlap_references[pair] = OverlapReference(pair, boxes, frame, source="manual-seed")
                except (TypeError, ValueError, KeyError):
                    self._seed_overlap_references.pop(pair, None)
        self._overlap_states.clear()
        # initialize_seed may have seen a current separation. Replaying the
        # prior frames must not prematurely treat a future separation as past.
        self._separated_pairs.clear()
        for state in self.states.values():
            state.geometry_count = 0
            state.geometry_started_frame = state.geometry_last_frame = None
            state.geometry_category = None
            self._reset_image_observation(state)
        for frame, boxes, masks in parsed:
            frame_evidence = {}
            for oid in active:
                state = self._state(oid)
                if oid not in boxes or frame <= reset_frames.get(oid, -1) or (state.manual_baseline and frame < state.manual_baseline.frame_index):
                    state.geometry_count = 0
                    state.geometry_started_frame = state.geometry_last_frame = None
                    state.geometry_category = None
                    self._reset_image_observation(state)
                    continue
                # A reused AI seed is not a new human approval. Validate the
                # saved cues against the effective seed geometry before replay.
                evidence, _ = self._identity_evidence(oid, frame, boxes[oid], masks.get(oid),
                                                      evidence_by_frame.get(frame, {}).get(oid))
                if evidence is not None:
                    frame_evidence[oid] = evidence
                self._geometry_observation(oid, frame, boxes[oid], masks.get(oid),
                                           geometry_supported=self._supported_identity(evidence),
                                           native_observation_verified=self._native_observation_verified(evidence),
                                           mask_area_supported=self._current_subject_supports_mask_change(evidence),
                                           log_mask_decision=False)
                conflict = self._single_core_conflict(oid, frame, evidence, boxes, masks,
                                                       evidence_by_frame.get(frame, {}))
                self._image_observation(oid, frame, evidence, single_core_conflict=conflict)
                if evidence and evidence["status"] in {"supported", "risk"} and evidence["head"]["status"] == "matched":
                    self._remember_head_motion(state, frame, evidence["head"]["point"])
            for pair in list(self._overlap_states):
                if not set(pair).issubset(boxes):
                    self._overlap_states.pop(pair, None)
            ids = sorted(boxes)
            for index, oid in enumerate(ids):
                for other in ids[index+1:]:
                    pair = (oid, other)
                    if frame <= max(reset_frames.get(oid, -1), reset_frames.get(other, -1)):
                        self._overlap_states.pop(pair, None)
                        continue
                    self._pair_observation(pair, boxes, frame, identity_evidence=frame_evidence)

    def push(self, frame_index: int, frame_objects: dict[int, list[float]], *, ignored_object_ids: Iterable[int] | None = None,
             mask_areas: dict[int, int] | None = None,
             identity_evidence: dict[int, dict[str, Any]] | None = None) -> AnomalyReport:
        cfg = self.config
        frame_index = int(frame_index)
        self.current_frame_index = frame_index
        report = AnomalyReport(frame_index, {}, [], False)
        current_ids = {int(value) for value in frame_objects}
        ignored_ids = {int(value) for value in (ignored_object_ids or [])}
        first_report = not self.reports
        valid_evidence: dict[int, dict[str, Any]] = {}
        visible_boxes = {int(oid): bbox for oid, bbox in frame_objects.items()
                         if int(oid) not in ignored_ids and not self._state(int(oid)).is_dead}

        # Explicitly omitted annotation frames do not terminate model identity
        # or turn a hidden anomaly into a permanent exemption in later frames.
        for oid in ignored_ids:
            state = self._state(oid)
            state.level = AnomalyLevel.NORMAL
            state.motion_streak = 0
            state.has_paused = False
            state.is_dead = False
            state.geometry_count = 0
            state.geometry_started_frame = state.geometry_last_frame = None
            state.geometry_category = None
            self._reset_image_observation(state)
            if oid in frame_objects:
                self._remember_motion(state, frame_index, frame_objects[oid])

        for oid in sorted(self.all_object_ids - current_ids - ignored_ids):
            state = self._state(oid)
            if state.is_dead:
                continue
            state.missing_count += 1
            state.geometry_count = 0
            state.geometry_started_frame = state.geometry_last_frame = None
            state.geometry_category = None
            self._reset_image_observation(state)
            last_bbox = state.motion_history[-1] if state.motion_history else None
            if self._is_near_boundary(last_bbox):
                state.level = AnomalyLevel.NORMAL
                report.object_levels[oid] = state.level
                continue
            state.level = AnomalyLevel.DISAPPEARED
            state.is_dead = True
            state.dead_at_frame = frame_index
            baseline = state.manual_baseline
            report.frames.append(AnomalyFrame(frame_index, oid, state.level, ["unexpected_disappearance"], {
                "missing_count": state.missing_count,
                "last_bbox": list(last_bbox) if last_bbox else None,
                "manual_baseline_bbox": list(baseline.bbox) if baseline else None,
                "manual_baseline_frame": baseline.frame_index if baseline else None,
                **self._review_details(frame_index),
            }))
            report.object_levels[oid] = state.level

        for raw_oid, raw_bbox in frame_objects.items():
            oid = int(raw_oid)
            if oid in ignored_ids:
                continue
            bbox = [float(value) for value in raw_bbox]
            state = self._state(oid)
            if state.is_dead:
                continue
            self.all_object_ids.add(oid)
            state.missing_count = 0
            reasons: list[str] = []
            details: dict[str, Any] = {"current_bbox": list(bbox), **self._review_details(frame_index)}
            hard = False
            warning = False

            baseline = state.manual_baseline
            details.update({
                "manual_baseline_bbox": list(baseline.bbox) if baseline else None,
                "manual_baseline_frame": baseline.frame_index if baseline else None,
                "manual_baseline_name": baseline.name if baseline else None,
            })
            payload = identity_evidence.get(oid) if isinstance(identity_evidence, dict) else None
            evidence, evidence_error = self._identity_evidence(oid, frame_index, bbox,
                                                               (mask_areas or {}).get(oid), payload)
            if evidence is not None:
                valid_evidence[oid] = evidence
                details["identity_evidence"] = evidence
            elif evidence_error:
                details["identity_evidence_error"] = evidence_error
                logging.getLogger("review.tracking").warning(
                    "tracking.image_evidence_rejected object=%s frame=%s reason=%s", oid, frame_index, evidence_error)
            hard, warning, geometry_reasons, geometry_details = self._geometry_observation(
                oid, frame_index, bbox, (mask_areas or {}).get(oid),
                geometry_supported=self._supported_identity(evidence),
                native_observation_verified=self._native_observation_verified(evidence),
                mask_area_supported=self._current_subject_supports_mask_change(evidence),
            )
            reasons.extend(geometry_reasons)
            details.update(geometry_details)

            head = (evidence["head"] if evidence and evidence["status"] in {"supported", "risk", "warning", "uncertain"}
                    and evidence["head"]["status"] == "matched" else None)
            if evidence and evidence['status'] == 'uncertain' and evidence['head'].get('associationStatus') == 'temporary-occlusion':
                # A tail-changing BBox centre must not substitute for an
                # explicitly ambiguous optical head during bounded occlusion.
                motion_hard, motion_warning, motion_reasons, motion_details = False, True, [], {'motion_reference_kind': 'temporarily-unavailable'}
            else:
                motion_hard, motion_warning, motion_reasons, motion_details = self._motion_check(
                    oid, state, frame_index, bbox, head_evidence=head)
            hard = hard or motion_hard
            warning = warning or motion_warning
            reasons.extend(motion_reasons)
            details.update(motion_details)
            if motion_hard:
                details["pause_started_frame"] = min(details.get("pause_started_frame", frame_index), frame_index)

            conflict = self._single_core_conflict(oid, frame_index, evidence, visible_boxes,
                                                   mask_areas or {}, identity_evidence or {})
            image_hard, image_warning, image_reasons, image_details = self._image_observation(
                oid, frame_index, evidence, single_core_conflict=conflict)
            previous_start = details.get("pause_started_frame")
            details.update(image_details)
            if image_hard and isinstance(previous_start, int):
                details["pause_started_frame"] = min(previous_start, details["pause_started_frame"])
            elif not image_hard and hard:
                # A pending image observation must not relabel an immediate
                # catastrophe or independent motion pause as its first risk.
                details["risk_type"] = "geometry" if geometry_details.get("geometry_risk") else "motion"
            hard = hard or image_hard
            warning = warning or image_warning
            reasons.extend(image_reasons)

            state.level = AnomalyLevel.ANOMALY if hard else AnomalyLevel.WARNING if warning else AnomalyLevel.NORMAL
            if not hard:
                self._remember_motion(state, frame_index, bbox)
                if head is not None:
                    self._remember_head_motion(state, frame_index, head["point"])
            report.frames.append(AnomalyFrame(frame_index, oid, state.level, reasons, details))
            report.object_levels[oid] = state.level

        visible_ids = current_ids - ignored_ids
        for pair in list(self._overlap_states):
            if not set(pair).issubset(visible_ids):
                self._overlap_states.pop(pair, None)
        if not first_report and len(visible_ids) >= 2:
            items = sorted((int(oid), bbox) for oid, bbox in frame_objects.items() if int(oid) not in ignored_ids)
            boxes = {oid: bbox for oid, bbox in items}
            for index, (oid_a, bbox_a) in enumerate(items):
                for oid_b, bbox_b in items[index + 1:]:
                    evidence = self._pair_observation((oid_a, oid_b), boxes, frame_index,
                                                      identity_evidence=valid_evidence)
                    if evidence is None:
                        continue
                    level = AnomalyLevel(evidence["level"])
                    suffix = "HARD" if level == AnomalyLevel.ANOMALY else "SOFT"
                    for oid, other in ((oid_a, oid_b), (oid_b, oid_a)):
                        existing = next((item for item in report.frames if item.object_id == oid), None)
                        pairs = [*(existing.details.get("overlap_pairs", []) if existing else []), evidence]
                        strongest = max(pairs, key=lambda item: (item["level"] == "anomaly", item["coverage"], item["iou"]))
                        common = {"overlap_pairs": pairs, "overlap_coverage": strongest["coverage"],
                                  "iou": strongest["iou"], "other_object_id": next(value for value in strongest["object_ids"] if value != oid),
                                  **self._review_details(frame_index)}
                        if level == AnomalyLevel.ANOMALY:
                            starts = [evidence["pause_started_frame"]]
                            if existing and isinstance(existing.details.get("pause_started_frame"), int):
                                starts.append(existing.details["pause_started_frame"])
                            common["pause_started_frame"] = min(starts)
                        self._append_reason(report, oid, f"bbox_overlap_with={other} coverage={evidence['coverage']:.3f} {suffix}", common, level)
                    self._state(oid_a).level = report.object_levels[oid_a]
                    self._state(oid_b).level = report.object_levels[oid_b]

        for state in self.states.values():
            if state.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED) and not state.has_paused:
                state.has_paused = True
                report.should_pause = True
            elif state.level == AnomalyLevel.NORMAL and state.has_paused and not state.is_dead:
                state.has_paused = False

        self.reports.append(report)
        if report.should_pause:
            report.pause_frame_index = min(
                item.details.get("pause_started_frame", frame_index) for item in report.frames
                if item.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED)
            )
            logging.getLogger("review.tracking").info(
                "tracking.anomaly_pause frame=%s pause_frame=%s objects=%s", frame_index, report.pause_frame_index,
                [{"objectId": item.object_id, "reasons": item.reasons,
                  "motion": item.details.get("motion_normalized"), "threshold": item.details.get("motion_pause_threshold"),
                  "samples": item.details.get("feedback_sample_count", 0),
                  "geometry_reference_source": item.details.get("geometry_reference_source"),
                  "geometry_reference_frame": item.details.get("geometry_reference_frame"),
                  "confirmed_geometry_frame": item.details.get("confirmed_geometry_frame"),
                  "geometry_category": item.details.get("geometry_observation_category"),
                  "geometry_observations": item.details.get("geometry_observation_count"),
                  "geometry_ratios": {key: item.details.get(key) for key in ("area_ratio", "width_ratio", "height_ratio", "aspect_ratio_change")},
                  "risk_type": item.details.get("risk_type"),
                  "image_category": item.details.get("image_observation_category"),
                  "image_observations": item.details.get("image_observation_count"),
                  "motion_reference_kind": item.details.get("motion_reference_kind"),
                  "pause_started_frame": item.details.get("pause_started_frame")}
                 for item in report.frames if item.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED)],
            )
        return report

    @staticmethod
    def _append_reason(report: AnomalyReport, oid: int, reason: str, details: dict[str, Any], level: AnomalyLevel) -> None:
        for frame in report.frames:
            if frame.object_id != oid:
                continue
            frame.reasons.append(reason)
            frame.details.update(details)
            if level == AnomalyLevel.ANOMALY or frame.level == AnomalyLevel.NORMAL:
                frame.level = level
            report.object_levels[oid] = frame.level
            return
        report.frames.append(AnomalyFrame(report.frame_index, oid, level, [reason], dict(details)))
        report.object_levels[oid] = level

    def scan_history(self, frames: Iterable) -> list[AnomalyReport]:
        for frame_index, frame_objects in frames:
            self.push(frame_index, frame_objects)
        return self.reports
