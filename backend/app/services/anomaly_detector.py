"""Stateful anomaly detection for SAM3 object tracking.

Size and shape use the latest manual annotation or whole-frame human checkpoint.
Legacy per-object approvals remain bounded supplementary shapes. Predictions
cannot create a checkpoint or change manual provenance, so AI drift cannot
teach itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
            self.normal_motion_samples.setdefault(oid, []).append(normalized)

    def _remember_motion(self, state: _ObjectState, frame_index: int, bbox: list[float]) -> None:
        if state.motion_frame_indices and state.motion_frame_indices[-1] == frame_index:
            state.motion_history[-1] = list(bbox)
            return
        state.motion_history.append(list(bbox))
        state.motion_frame_indices.append(frame_index)
        limit = max(3, int(self.config.MOTION_HISTORY_FRAMES))
        del state.motion_history[:-limit]
        del state.motion_frame_indices[:-limit]

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

    def _motion_check(self, oid: int, state: _ObjectState, frame_index: int, bbox: list[float]) -> tuple[bool, bool, list[str], dict[str, Any]]:
        """Use time/size normalization plus short history without auto-training."""
        if not state.motion_history:
            return False, False, [], {}
        cfg = self.config
        previous_bbox = state.motion_history[-1]
        previous_frame = state.motion_frame_indices[-1]
        gap = max(1, frame_index - previous_frame)
        old_center, center = _bbox_center(previous_bbox), _bbox_center(bbox)
        dx, dy = center[0] - old_center[0], center[1] - old_center[1]
        shift = math.hypot(dx, dy)
        if state.manual_baseline:
            scale = max(cfg.MOTION_MIN_SCALE_PX, math.hypot(*_bbox_wh(state.manual_baseline.bbox)))
        else:
            # Legacy direct clients can omit source=manual. Fix their first
            # seed scale so growing AI boxes cannot loosen motion detection.
            if state.seed_motion_scale is None:
                state.seed_motion_scale = max(cfg.MOTION_MIN_SCALE_PX, math.hypot(*_bbox_wh(previous_bbox)))
            scale = state.seed_motion_scale
        normalizer = self.fps / (30.0 * gap * scale)
        normalized = shift * normalizer
        samples = self.normal_motion_samples.get(oid, [])
        threshold = max(cfg.MOTION_PAUSE_NORMALIZED, max(samples, default=0.0) * cfg.MOTION_FEEDBACK_MARGIN)
        extreme = max(cfg.MOTION_EXTREME_NORMALIZED, threshold * cfg.MOTION_EXTREME_MULTIPLIER)

        # Velocity is diagnostic context. It never increases the thresholds.
        velocities = []
        for index in range(1, len(state.motion_history)):
            previous = _bbox_center(state.motion_history[index - 1])
            current = _bbox_center(state.motion_history[index])
            elapsed = max(1, state.motion_frame_indices[index] - state.motion_frame_indices[index - 1])
            velocities.append(((current[0] - previous[0]) / elapsed, (current[1] - previous[1]) / elapsed))
        recent_velocity = (median(value[0] for value in velocities), median(value[1] for value in velocities)) if velocities else (0.0, 0.0)
        residual = math.hypot(dx / gap - recent_velocity[0], dy / gap - recent_velocity[1]) * self.fps / (30.0 * scale)

        # A bounded return toward the position two observations ago is typical
        # oscillation. It interrupts a suspected drift streak, but remains a
        # warning; a huge jump is never exempted by returning toward old data.
        oscillating = False
        if len(state.motion_history) >= 2 and normalized < threshold * 1.5:
            older_center = _bbox_center(state.motion_history[-2])
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
            "motion_reference_bbox": list(previous_bbox),
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

    def _geometry_observation(self, oid: int, frame_index: int, bbox: list[float], mask_area: int | None = None
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
            return False, False, [], {}
        checks = [self._geometry_check(bbox, reference, source) for reference, source in references]
        hard, warning, reasons, details = min(
            reversed(checks),
            key=lambda check: 3 if check[3].get("geometry_extreme") else 2 if check[0] else 1 if check[1] else 0,
        )
        details["confirmed_geometry_frame"] = max((reference.frame_index for reference in confirmed), default=None)
        details["geometry_reference_count"] = len(references)
        confirmed_areas = [float(reference.mask_area) for reference in confirmed
                           if isinstance(reference.mask_area, (int, float)) and math.isfinite(reference.mask_area)
                           and reference.mask_area > 0]
        if state.manual_baseline and state.manual_baseline.mask_area:
            confirmed_areas.append(state.manual_baseline.mask_area)
        if confirmed_areas and isinstance(mask_area, (int, float)) and math.isfinite(mask_area) and mask_area >= 0:
            # Loss is extreme only relative to every explicitly accepted mask.
            # Neither the initial rectangular prompt nor AI history supplies it.
            mask_reference = min(confirmed_areas)
            ratio = mask_area / mask_reference
            details.update(current_mask_area=mask_area, mask_reference_area=mask_reference, mask_area_ratio=ratio)
            if ratio < cfg.GEOMETRY_EXTREME_MASK_LOSS and not details.get("side_exit_shrink_ignored"):
                hard = True
                details["geometry_extreme"] = True
                reasons.append(f"confirmed_mask_area_ratio={ratio:.3f} HARD")
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

    def _pair_observation(self, pair: tuple[int, int], boxes: dict[int, list[float]], frame_index: int
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
        suspect = coverage >= cfg.OVERLAP_HARD and iou >= cfg.OVERLAP_MERGE_IOU and matched is None
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
                "normal_reference_matched": matched is not None}

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
        count = max(int(self.config.GEOMETRY_PERSISTENCE_FRAMES), int(self.config.OVERLAP_PERSISTENCE_FRAMES), 1)
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
        for frame, boxes, masks in parsed:
            for oid in active:
                state = self._state(oid)
                if oid not in boxes or frame <= reset_frames.get(oid, -1) or (state.manual_baseline and frame < state.manual_baseline.frame_index):
                    state.geometry_count = 0
                    state.geometry_started_frame = state.geometry_last_frame = None
                    state.geometry_category = None
                    continue
                self._geometry_observation(oid, frame, boxes[oid], masks.get(oid))
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
                    self._pair_observation(pair, boxes, frame)

    def push(self, frame_index: int, frame_objects: dict[int, list[float]], *, ignored_object_ids: Iterable[int] | None = None,
             mask_areas: dict[int, int] | None = None) -> AnomalyReport:
        cfg = self.config
        frame_index = int(frame_index)
        self.current_frame_index = frame_index
        report = AnomalyReport(frame_index, {}, [], False)
        current_ids = {int(value) for value in frame_objects}
        ignored_ids = {int(value) for value in (ignored_object_ids or [])}
        first_report = not self.reports

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
            hard, warning, geometry_reasons, geometry_details = self._geometry_observation(
                oid, frame_index, bbox, (mask_areas or {}).get(oid),
            )
            reasons.extend(geometry_reasons)
            details.update(geometry_details)

            motion_hard, motion_warning, motion_reasons, motion_details = self._motion_check(oid, state, frame_index, bbox)
            hard = hard or motion_hard
            warning = warning or motion_warning
            reasons.extend(motion_reasons)
            details.update(motion_details)
            if motion_hard:
                details["pause_started_frame"] = min(details.get("pause_started_frame", frame_index), frame_index)

            state.level = AnomalyLevel.ANOMALY if hard else AnomalyLevel.WARNING if warning else AnomalyLevel.NORMAL
            if not hard:
                self._remember_motion(state, frame_index, bbox)
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
                    evidence = self._pair_observation((oid_a, oid_b), boxes, frame_index)
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
