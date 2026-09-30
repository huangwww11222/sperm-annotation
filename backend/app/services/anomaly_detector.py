"""Stateful anomaly detection for SAM3 object tracking.

Size and shape use the latest manual annotation and, when present, one shape
explicitly confirmed as normal by the user. Predictions cannot add references
or change the manual annotation, so gradual AI drift does not teach itself.
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


@dataclass(frozen=True)
class ConfirmedGeometryReference:
    bbox: list[float]
    frame_index: int


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
        geometry_references: dict[int, ConfirmedGeometryReference] | None = None,
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
        self._state(oid).manual_baseline = ManualBaseline(clean, int(frame_index), name)

    def prime_manual_baselines(self, baselines: dict[int, ManualBaseline | dict[str, Any]]) -> None:
        for raw_oid, value in baselines.items():
            if isinstance(value, ManualBaseline):
                self.set_manual_baseline(raw_oid, value.bbox, value.frame_index, value.name)
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
            levels[oid] = AnomalyLevel.NORMAL
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

    def push(self, frame_index: int, frame_objects: dict[int, list[float]], *, ignored_object_ids: Iterable[int] | None = None) -> AnomalyReport:
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
            if oid in frame_objects:
                self._remember_motion(state, frame_index, frame_objects[oid])

        for oid in sorted(self.all_object_ids - current_ids - ignored_ids):
            state = self._state(oid)
            if state.is_dead:
                continue
            state.missing_count += 1
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
            references: list[tuple[ManualBaseline | ConfirmedGeometryReference, str]] = []
            if baseline is not None:
                references.append((baseline, "manual"))
            confirmed = self.geometry_references.get(oid)
            # A newer manual correction supersedes prior accepted shapes. The
            # frame check also protects direct detector/scan callers from
            # applying a future confirmation to earlier predictions.
            if confirmed and confirmed.frame_index <= frame_index and (baseline is None or confirmed.frame_index > baseline.frame_index):
                references.append((confirmed, "confirmed-normal"))
            if references:
                checks = [self._geometry_check(bbox, reference, source) for reference, source in references]
                # Accept a coherent match to either known shape. Prefer the
                # latest confirmation on ties, including a genuinely new
                # anomaly, so its diagnostics describe the current reference.
                hard, warning, geometry_reasons, geometry_details = min(
                    reversed(checks), key=lambda check: 2 if check[0] else 1 if check[1] else 0,
                )
                reasons.extend(geometry_reasons)
                details.update(geometry_details)
                details["confirmed_geometry_frame"] = confirmed.frame_index if any(source == "confirmed-normal" for _, source in references) else None

            motion_hard, motion_warning, motion_reasons, motion_details = self._motion_check(oid, state, frame_index, bbox)
            hard = hard or motion_hard
            warning = warning or motion_warning
            reasons.extend(motion_reasons)
            details.update(motion_details)

            state.level = AnomalyLevel.ANOMALY if hard else AnomalyLevel.WARNING if warning else AnomalyLevel.NORMAL
            if not hard:
                self._remember_motion(state, frame_index, bbox)
            report.frames.append(AnomalyFrame(frame_index, oid, state.level, reasons, details))
            report.object_levels[oid] = state.level

        if not first_report and len(current_ids) >= 2:
            items = [(int(oid), bbox) for oid, bbox in frame_objects.items() if int(oid) not in ignored_ids]
            for index, (oid_a, bbox_a) in enumerate(items):
                for oid_b, bbox_b in items[index + 1:]:
                    coverage = _overlap_coverage(bbox_a, bbox_b)
                    if coverage < cfg.OVERLAP_WARN:
                        continue
                    level = AnomalyLevel.ANOMALY if coverage >= cfg.OVERLAP_HARD else AnomalyLevel.WARNING
                    suffix = "HARD" if level == AnomalyLevel.ANOMALY else "SOFT"
                    common = {"overlap_coverage": coverage, "iou": _iou(bbox_a, bbox_b), **self._review_details(frame_index)}
                    self._append_reason(report, oid_a, f"bbox_overlap_with={oid_b} coverage={coverage:.3f} {suffix}", {**common, "other_object_id": oid_b}, level)
                    self._append_reason(report, oid_b, f"bbox_overlap_with={oid_a} coverage={coverage:.3f} {suffix}", {**common, "other_object_id": oid_a}, level)
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
            logging.getLogger("review.tracking").info(
                "tracking.anomaly_pause frame=%s objects=%s", frame_index,
                [{"objectId": item.object_id, "reasons": item.reasons, "motion": item.details.get("motion_normalized"), "threshold": item.details.get("motion_pause_threshold"), "samples": item.details.get("feedback_sample_count", 0), "geometry_reference_source": item.details.get("geometry_reference_source"), "geometry_reference_frame": item.details.get("geometry_reference_frame"), "confirmed_geometry_frame": item.details.get("confirmed_geometry_frame")} for item in report.frames if item.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED)],
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
