"""Anomaly detector for sperm tracking.

Detects three classes of tracking failures that should pause SAM3 for human review:
1) unexpected disappearance away from the 10 px frame boundary,
2) implausible sudden bbox enlargement/shrink/shape/center jumps,
3) near-duplicate bbox overlap between two different tracked objects.

The detector is deliberately conservative for sperm videos: it uses a 5-frame
median baseline and only pauses on HARD events. SOFT metrics are retained for UI
warnings but do not pause immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import statistics
from typing import Any, Iterable


class AnomalyLevel(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    ANOMALY = "anomaly"
    DISAPPEARED = "disappeared"


@dataclass
class AnomalyConfig:
    """Tunable thresholds for typical small, fast sperm bounding boxes."""

    # Stable baseline is the median of the last N clean frames.
    BASELINE_WINDOW: int = 5

    # A target that disappears away from the edge is an immediate anomaly.
    # If its last bbox was within this many pixels of any image side, a normal
    # exit is assumed and tracking continues.
    EDGE_EXIT_MARGIN_PX: float = 10.0

    # Legacy/optional disappearance warning knobs kept for compatibility.
    DISAPPEAR_WARN: int = 2
    DISAPPEAR_ALERT: int = 3

    # Area changes. Growth is the most important "sudden enlargement" signal.
    AREA_SHRINK_SOFT: float = 0.35
    AREA_SHRINK_HARD: float = 0.25
    AREA_GROW_SOFT: float = 1.8
    AREA_GROW_HARD: float = 2.8

    # Width/height growth. These are intentionally stricter than ordinary
    # sperm motion but less strict than a 3x area jump.
    WIDTH_GROW_SOFT: float = 1.4
    WIDTH_GROW_HARD: float = 1.8
    HEIGHT_GROW_SOFT: float = 1.4
    HEIGHT_GROW_HARD: float = 1.8

    # Center displacement per frame. Scaled to FPS relative to 30 fps.
    CENTER_SHIFT_SOFT: float = 25.0
    CENTER_SHIFT_HARD: float = 40.0

    # Bounding-box aspect ratio change.
    ASPECT_CHANGE_SOFT: float = 1.5
    ASPECT_CHANGE_HARD: float = 2.0

    # If two boxes cover >=95% of the smaller box, treat them as one object.
    OVERLAP_WARN: float = 0.85
    OVERLAP_HARD: float = 0.95


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _bbox_wh(bbox) -> tuple[float, float]:
    return max(0.0, float(bbox[2]) - float(bbox[0])), max(0.0, float(bbox[3]) - float(bbox[1]))


def _bbox_area(bbox) -> float:
    w, h = _bbox_wh(bbox)
    return w * h


def _bbox_center(bbox) -> tuple[float, float]:
    return (float(bbox[0]) + float(bbox[2])) / 2.0, (float(bbox[1]) + float(bbox[3])) / 2.0


def _bbox_aspect(bbox) -> float:
    w, h = _bbox_wh(bbox)
    return (w / h) if h > 0 else 1.0


def _median_bbox(history: list) -> list | None:
    if not history:
        return None
    return [
        statistics.median(float(b[0]) for b in history),
        statistics.median(float(b[1]) for b in history),
        statistics.median(float(b[2]) for b in history),
        statistics.median(float(b[3]) for b in history),
    ]


def _intersection_area(a, b) -> float:
    x1 = max(float(a[0]), float(b[0]))
    y1 = max(float(a[1]), float(b[1]))
    x2 = min(float(a[2]), float(b[2]))
    y2 = min(float(a[3]), float(b[3]))
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _iou(a, b) -> float:
    inter = _intersection_area(a, b)
    union = _bbox_area(a) + _bbox_area(b) - inter
    return inter / union if union > 0 else 0.0


def _overlap_coverage(a, b) -> float:
    """Intersection divided by the smaller bbox area.

    This is more useful than IoU for tracking-merge detection because one box
    can sit almost entirely inside the other while IoU remains below 0.95.
    """
    inter = _intersection_area(a, b)
    min_area = min(_bbox_area(a), _bbox_area(b))
    return inter / min_area if min_area > 0 else 0.0


# ---------------------------------------------------------------------------
# State containers
# ---------------------------------------------------------------------------


@dataclass
class _ObjectState:
    level: AnomalyLevel = AnomalyLevel.NORMAL
    soft_count: int = 0
    hard_count: int = 0
    recover_count: int = 0
    missing_count: int = 0
    edge_frames: int = 0
    is_dead: bool = False
    dead_at_frame: int | None = None
    history: list = field(default_factory=list)
    has_paused: bool = False


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
    """Per-frame detector with a 5-frame robust baseline and hard-stop events."""

    def __init__(
        self,
        config: AnomalyConfig | None = None,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: int = 30,
        all_object_ids: Iterable[int] | None = None,
    ) -> None:
        self.config = config or AnomalyConfig()
        self.frame_width = int(frame_width)
        self.frame_height = int(frame_height)
        self.fps = int(fps) if int(fps) > 0 else 30
        self.all_object_ids: set[int] = set(int(x) for x in (all_object_ids or []))
        self.states: dict[int, _ObjectState] = {}
        self.reports: list[AnomalyReport] = []
        self.current_frame_index = -1

    def _state(self, oid: int) -> _ObjectState:
        if oid not in self.states:
            self.states[oid] = _ObjectState()
        return self.states[oid]

    def _is_near_boundary(self, bbox) -> bool:
        if bbox is None:
            return False
        m = self.config.EDGE_EXIT_MARGIN_PX
        x1, y1, x2, y2 = [float(x) for x in bbox[:4]]
        return bool(
            x1 <= m
            or y1 <= m
            or (self.frame_width - x2) <= m
            or (self.frame_height - y2) <= m
        )

    def push(self, frame_index: int, frame_objects: dict[int, list[float]]) -> AnomalyReport:
        cfg = self.config
        self.current_frame_index = int(frame_index)
        report = AnomalyReport(
            frame_index=int(frame_index),
            object_levels={},
            frames=[],
            should_pause=False,
        )

        current_ids = set(int(k) for k in frame_objects.keys())
        first_report = not self.reports

        # ---------------------------------------------------------------
        # 1. Missing targets.
        # ---------------------------------------------------------------
        for oid in sorted(self.all_object_ids - current_ids):
            st = self._state(oid)
            if st.is_dead:
                continue

            st.missing_count += 1
            last_bbox = st.history[-1] if st.history else None
            near_edge = self._is_near_boundary(last_bbox)

            if near_edge:
                # Normal exit: target left through one of the four borders.
                st.edge_frames += 1
                st.level = AnomalyLevel.NORMAL
                report.object_levels[oid] = st.level
                continue

            # Away from any boundary: one missing next frame is enough to stop.
            st.level = AnomalyLevel.DISAPPEARED
            st.is_dead = True
            st.dead_at_frame = int(frame_index)
            report.frames.append(
                AnomalyFrame(
                    frame_index=int(frame_index),
                    object_id=oid,
                    level=AnomalyLevel.DISAPPEARED,
                    reasons=["unexpected_disappearance"],
                    details={
                        "missing_count": st.missing_count,
                        "near_edge": near_edge,
                        "edge_margin_px": cfg.EDGE_EXIT_MARGIN_PX,
                        "last_bbox": list(last_bbox) if last_bbox is not None else None,
                    },
                )
            )
            report.object_levels[oid] = st.level

        # ---------------------------------------------------------------
        # 2. Present targets: size / movement / aspect changes.
        # ---------------------------------------------------------------
        for oid, bbox in frame_objects.items():
            oid = int(oid)
            st = self._state(oid)
            if st.is_dead:
                continue
            self.all_object_ids.add(oid)
            st.missing_count = 0
            st.edge_frames = 0

            recent = st.history[-cfg.BASELINE_WINDOW:] if st.history else []
            history_ready = len(recent) >= cfg.BASELINE_WINDOW
            base = _median_bbox(recent)

            reasons: list[str] = []
            details: dict[str, Any] = {}
            hard_score = 0
            soft_score = 0

            if base is not None:
                base_w, base_h = _bbox_wh(base)
                cur_w, cur_h = _bbox_wh(bbox)
                base_area = _bbox_area(base)
                cur_area = _bbox_area(bbox)

                if base_area > 0:
                    area_ratio = cur_area / base_area
                    details["area_ratio"] = area_ratio
                    details["baseline_area"] = base_area
                    details["current_area"] = cur_area
                    if area_ratio < cfg.AREA_SHRINK_HARD or area_ratio > cfg.AREA_GROW_HARD:
                        hard_score += 1
                        reasons.append(f"area_ratio={area_ratio:.3f} HARD")
                    elif area_ratio < cfg.AREA_SHRINK_SOFT or area_ratio > cfg.AREA_GROW_SOFT:
                        soft_score += 1
                        reasons.append(f"area_ratio={area_ratio:.3f} SOFT")

                if base_w > 0:
                    width_ratio = cur_w / base_w
                    details["width_ratio"] = width_ratio
                    details["baseline_width"] = base_w
                    details["current_width"] = cur_w
                    if width_ratio > cfg.WIDTH_GROW_HARD:
                        hard_score += 1
                        reasons.append(f"width_ratio={width_ratio:.3f} HARD")
                    elif width_ratio > cfg.WIDTH_GROW_SOFT:
                        soft_score += 1
                        reasons.append(f"width_ratio={width_ratio:.3f} SOFT")

                if base_h > 0:
                    height_ratio = cur_h / base_h
                    details["height_ratio"] = height_ratio
                    details["baseline_height"] = base_h
                    details["current_height"] = cur_h
                    if height_ratio > cfg.HEIGHT_GROW_HARD:
                        hard_score += 1
                        reasons.append(f"height_ratio={height_ratio:.3f} HARD")
                    elif height_ratio > cfg.HEIGHT_GROW_SOFT:
                        soft_score += 1
                        reasons.append(f"height_ratio={height_ratio:.3f} SOFT")

                bcx, bcy = _bbox_center(base)
                ccx, ccy = _bbox_center(bbox)
                dist = ((ccx - bcx) ** 2 + (ccy - bcy) ** 2) ** 0.5
                details["center_shift"] = dist
                fps_scale = 30.0 / self.fps
                hard_t = cfg.CENTER_SHIFT_HARD * fps_scale
                soft_t = cfg.CENTER_SHIFT_SOFT * fps_scale
                if dist >= hard_t:
                    hard_score += 1
                    reasons.append(f"center_shift={dist:.1f}px HARD")
                elif dist >= soft_t:
                    soft_score += 1
                    reasons.append(f"center_shift={dist:.1f}px SOFT")

                base_ar = _bbox_aspect(base)
                cur_ar = _bbox_aspect(bbox)
                if base_ar > 0 and cur_ar > 0:
                    ar_ratio = max(base_ar, cur_ar) / min(base_ar, cur_ar)
                    details["aspect_ratio_change"] = ar_ratio
                    if ar_ratio >= cfg.ASPECT_CHANGE_HARD:
                        hard_score += 1
                        reasons.append(f"aspect_change={ar_ratio:.3f} HARD")
                    elif ar_ratio >= cfg.ASPECT_CHANGE_SOFT:
                        soft_score += 1
                        reasons.append(f"aspect_change={ar_ratio:.3f} SOFT")

            if hard_score > 0:
                st.hard_count += 1
                st.soft_count = 0
                st.recover_count = 0
            elif soft_score > 0:
                st.soft_count += 1
                st.hard_count = 0
                st.recover_count = 0
            else:
                st.recover_count += 1
                st.hard_count = 0
                st.soft_count = 0

            if reasons:
                print(
                    f"[detector] frame={frame_index} oid={oid} "
                    f"hard={hard_score} soft={soft_score} reasons={reasons} "
                    f"hist={len(st.history)}"
                )

            if history_ready:
                if hard_score >= 1:
                    st.level = AnomalyLevel.ANOMALY
                elif st.soft_count >= 2 and st.level == AnomalyLevel.NORMAL:
                    st.level = AnomalyLevel.WARNING
                elif soft_score == 0 and st.level == AnomalyLevel.WARNING:
                    st.level = AnomalyLevel.NORMAL
            else:
                # Warm-up: collect history, but don't pause based on metrics.
                st.level = AnomalyLevel.NORMAL

            if hard_score < 1.0 or not history_ready:
                st.history.append([float(b) for b in bbox])
                if len(st.history) > max(cfg.BASELINE_WINDOW * 3, 15):
                    st.history = st.history[-max(cfg.BASELINE_WINDOW * 3, 15):]

            report.frames.append(
                AnomalyFrame(
                    frame_index=int(frame_index),
                    object_id=oid,
                    level=st.level,
                    reasons=reasons,
                    details=details,
                )
            )
            report.object_levels[oid] = st.level

        # ---------------------------------------------------------------
        # 3. Pairwise overlap: high coverage means likely ID merge.
        # Skip only the very first seed frame so two intentionally overlapping
        # manual seed boxes don't immediately prevent starting tracking.
        # ---------------------------------------------------------------
        if not first_report and len(current_ids) >= 2:
            current_items = [(int(oid), bbox) for oid, bbox in frame_objects.items()]
            for i in range(len(current_items)):
                oid_a, bbox_a = current_items[i]
                for j in range(i + 1, len(current_items)):
                    oid_b, bbox_b = current_items[j]
                    coverage = _overlap_coverage(bbox_a, bbox_b)
                    iou = _iou(bbox_a, bbox_b)
                    if coverage >= cfg.OVERLAP_HARD:
                        reason_a = f"bbox_overlap_with={oid_b} coverage={coverage:.3f} HARD"
                        reason_b = f"bbox_overlap_with={oid_a} coverage={coverage:.3f} HARD"
                        details = {
                            "other_object_id": oid_b,
                            "overlap_coverage": coverage,
                            "iou": iou,
                        }
                        self._append_reason(report, oid_a, reason_a, details, AnomalyLevel.ANOMALY)
                        self._append_reason(report, oid_b, reason_b, {**details, "other_object_id": oid_a}, AnomalyLevel.ANOMALY)
                        self._state(oid_a).level = AnomalyLevel.ANOMALY
                        self._state(oid_b).level = AnomalyLevel.ANOMALY
                    elif coverage >= cfg.OVERLAP_WARN:
                        reason_a = f"bbox_overlap_with={oid_b} coverage={coverage:.3f} SOFT"
                        reason_b = f"bbox_overlap_with={oid_a} coverage={coverage:.3f} SOFT"
                        details = {
                            "other_object_id": oid_b,
                            "overlap_coverage": coverage,
                            "iou": iou,
                        }
                        self._append_reason(report, oid_a, reason_a, details, AnomalyLevel.WARNING)
                        self._append_reason(report, oid_b, reason_b, {**details, "other_object_id": oid_a}, AnomalyLevel.WARNING)
                        if self._state(oid_a).level == AnomalyLevel.NORMAL:
                            self._state(oid_a).level = AnomalyLevel.WARNING
                        if self._state(oid_b).level == AnomalyLevel.NORMAL:
                            self._state(oid_b).level = AnomalyLevel.WARNING

        # ---------------------------------------------------------------
        # 4. Aggregate pause. A new HARD event pauses once. Normal recovery
        # resets the per-object latch so a later independent hard event can
        # pause again after a user resumes.
        # ---------------------------------------------------------------
        for oid, st in self.states.items():
            if st.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED) and not st.has_paused:
                st.has_paused = True
                report.should_pause = True
                break

        for st in self.states.values():
            if st.level == AnomalyLevel.NORMAL and st.has_paused and not st.is_dead:
                st.has_paused = False

        self.reports.append(report)
        return report

    @staticmethod
    def _append_reason(
        report: AnomalyReport,
        oid: int,
        reason: str,
        details: dict[str, Any],
        level: AnomalyLevel,
    ) -> None:
        """Merge pairwise overlap findings into the per-object frame report."""
        for af in report.frames:
            if af.object_id == oid:
                if reason not in af.reasons:
                    af.reasons.append(reason)
                af.details.update(details)
                if level == AnomalyLevel.ANOMALY:
                    af.level = AnomalyLevel.ANOMALY
                elif af.level == AnomalyLevel.NORMAL:
                    af.level = AnomalyLevel.WARNING
                report.object_levels[oid] = af.level
                return

        report.frames.append(
            AnomalyFrame(
                frame_index=report.frame_index,
                object_id=oid,
                level=level,
                reasons=[reason],
                details=dict(details),
            )
        )
        report.object_levels[oid] = level

    def scan_history(self, frames: Iterable) -> list[AnomalyReport]:
        for frame_index, frame_objects in frames:
            self.push(frame_index, frame_objects)
        return self.reports
