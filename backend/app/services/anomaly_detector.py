"""Stateful anomaly detection for SAM3 object tracking.

Size and shape are always compared with the most recent *manual* annotation.
AI predictions are retained only as short-term motion history, so a slowly
shrinking or growing prediction can never teach the detector a new normal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
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

    # Motion is measured against the preceding clean AI frame, never against
    # the fixed manual box because normal sperm movement must remain possible.
    CENTER_SHIFT_SOFT: float = 25.0
    CENTER_SHIFT_HARD: float = 40.0

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


@dataclass
class _ObjectState:
    level: AnomalyLevel = AnomalyLevel.NORMAL
    missing_count: int = 0
    is_dead: bool = False
    dead_at_frame: int | None = None
    motion_history: list[list[float]] = field(default_factory=list)
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
    ) -> None:
        self.config = config or AnomalyConfig()
        self.frame_width = int(frame_width)
        self.frame_height = int(frame_height)
        self.fps = float(fps) if float(fps) > 0 else 30.0
        self.all_object_ids = {int(value) for value in (all_object_ids or [])}
        self.states: dict[int, _ObjectState] = {}
        self.reports: list[AnomalyReport] = []
        self.current_frame_index = -1
        if manual_baselines:
            self.prime_manual_baselines(manual_baselines)

    def _state(self, object_id: int) -> _ObjectState:
        if object_id not in self.states:
            self.states[object_id] = _ObjectState()
        return self.states[object_id]

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
        for _frame_index, frame_objects in frames:
            for raw_oid, bbox in frame_objects.items():
                clean = [float(value) for value in bbox]
                if len(clean) == 4 and _bbox_area(clean) > 0:
                    oid = int(raw_oid)
                    self.all_object_ids.add(oid)
                    self._state(oid).motion_history.append(clean)

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
            state.motion_history.append(clean)
            state.level = AnomalyLevel.NORMAL
            state.is_dead = False
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

    def push(self, frame_index: int, frame_objects: dict[int, list[float]]) -> AnomalyReport:
        cfg = self.config
        frame_index = int(frame_index)
        self.current_frame_index = frame_index
        report = AnomalyReport(frame_index, {}, [], False)
        current_ids = {int(value) for value in frame_objects}
        first_report = not self.reports

        for oid in sorted(self.all_object_ids - current_ids):
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
            if baseline is not None:
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
                    "manual_baseline_bbox": list(baseline.bbox),
                    "manual_baseline_frame": baseline.frame_index,
                    "manual_baseline_name": baseline.name,
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

            # Moving far from an old manual box is valid; only an implausible
            # one-frame jump is evaluated here.
            previous_bbox = state.motion_history[-1] if state.motion_history else None
            if previous_bbox is not None:
                previous_center = _bbox_center(previous_bbox)
                current_center = _bbox_center(bbox)
                center_shift = ((current_center[0] - previous_center[0]) ** 2 + (current_center[1] - previous_center[1]) ** 2) ** 0.5
                details["center_shift"] = center_shift
                details["motion_reference_bbox"] = list(previous_bbox)
                fps_scale = 30.0 / self.fps
                if center_shift >= cfg.CENTER_SHIFT_HARD * fps_scale:
                    hard = True
                    reasons.append(f"adjacent_center_shift={center_shift:.1f}px HARD")
                elif center_shift >= cfg.CENTER_SHIFT_SOFT * fps_scale:
                    warning = True
                    reasons.append(f"adjacent_center_shift={center_shift:.1f}px SOFT")

            state.level = AnomalyLevel.ANOMALY if hard else AnomalyLevel.WARNING if warning else AnomalyLevel.NORMAL
            if not hard:
                state.motion_history.append(list(bbox))
            report.frames.append(AnomalyFrame(frame_index, oid, state.level, reasons, details))
            report.object_levels[oid] = state.level

        if not first_report and len(current_ids) >= 2:
            items = [(int(oid), bbox) for oid, bbox in frame_objects.items()]
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
                    self._state(oid_a).level = level
                    self._state(oid_b).level = level

        for state in self.states.values():
            if state.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED) and not state.has_paused:
                state.has_paused = True
                report.should_pause = True
            elif state.level == AnomalyLevel.NORMAL and state.has_paused and not state.is_dead:
                state.has_paused = False

        self.reports.append(report)
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
