from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .annotation_seed import Box, JSONSeedAnnotations


@dataclass
class FrameDifferenceResult:
    """Frame-difference planning result used before SAM3 tracking."""

    frame_index: Optional[int]
    frame_offset: Optional[int]
    bbox: Optional[tuple[int, int, int, int]]
    score: Optional[float]
    known_box_count: int
    search_frames: int
    status: str  # new_object_found | no_new_object | invalid
    message: str

    @property
    def recommended_track_frames(self) -> int:
        """Number of source frames SAM3 should process, including the seed frame."""
        if self.frame_offset is None:
            # search_frames counts forward frames N+1..N+k; SAM3 also needs seed frame N.
            return max(1, self.search_frames + 1)
        return max(1, self.frame_offset + 1)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["bbox"] = list(self.bbox) if self.bbox is not None else None
        data["recommendedTrackFrames"] = self.recommended_track_frames
        return data


def _clip_box(b: Box, w: int, h: int, margin: int = 0):
    return (
        max(0, int(np.floor(b.xtl)) - margin),
        max(0, int(np.floor(b.ytl)) - margin),
        min(w, int(np.ceil(b.xbr)) + margin),
        min(h, int(np.ceil(b.ybr)) + margin),
    )


def _mask_boxes(shape, boxes, margin):
    mask = np.zeros(shape, np.uint8)
    h, w = shape[:2]
    for b in boxes:
        x1, y1, x2, y2 = _clip_box(b, w, h, margin)
        if x2 > x1 and y2 > y1:
            cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
    return mask


def _make_roi_mask(shape, *, side="none", ratio=1 / 3, rect=None):
    h, w = shape[:2]
    m = np.zeros((h, w), np.uint8)
    if rect is not None:
        x1, y1, x2, y2 = [int(v) for v in rect]
        m[max(0, y1): min(h, y2), max(0, x1): min(w, x2)] = 255
        return m
    if side == "left":
        m[:, : int(w * ratio)] = 255
    elif side == "right":
        m[:, w - int(w * ratio):] = 255
    elif side == "top":
        m[: int(h * ratio), :] = 255
    elif side == "bottom":
        m[int(h * (1 - ratio)):, :] = 255
    else:
        m[:, :] = 255
    return m


def _track_known_boxes(prev_gray, gray, boxes, search_margin=50):
    """Track known seed boxes with local template matching."""
    h, w = gray.shape[:2]
    out = []
    for b in boxes:
        x1, y1, x2, y2 = _clip_box(b, w, h, 0)
        if x2 - x1 < 8 or y2 - y1 < 6:
            out.append(b)
            continue
        templ = cv2.GaussianBlur(prev_gray[y1:y2, x1:x2], (3, 3), 0)
        sx1 = max(0, x1 - search_margin)
        sy1 = max(0, y1 - search_margin)
        sx2 = min(w, x2 + search_margin)
        sy2 = min(h, y2 + search_margin)
        search = cv2.GaussianBlur(gray[sy1:sy2, sx1:sx2], (3, 3), 0)
        if search.shape[0] < templ.shape[0] or search.shape[1] < templ.shape[1]:
            out.append(b)
            continue
        res = cv2.matchTemplate(search, templ, cv2.TM_CCOEFF_NORMED)
        _, corr, _, loc = cv2.minMaxLoc(res)
        nx1, ny1 = sx1 + loc[0], sy1 + loc[1]
        if corr < 0.45:
            out.append(b)
        else:
            ww, hh = x2 - x1, y2 - y1
            out.append(
                Box(
                    track_id=b.track_id,
                    frame=b.frame + 1,
                    xtl=nx1,
                    ytl=ny1,
                    xbr=nx1 + ww,
                    ybr=ny1 + hh,
                    outside=b.outside,
                    occluded=b.occluded,
                    label=b.label,
                    annotation_id=b.annotation_id,
                    source=b.source,
                )
            )
    return out


def _candidate_contours(
    prev_gray,
    gray,
    known_boxes,
    *,
    diff_threshold,
    known_margin,
    min_area,
    median_known_area,
    median_known_w,
    median_known_h,
    min_area_ratio,
    min_w_ratio,
    min_h_ratio,
    max_aspect,
    roi_mask=None,
):
    p = cv2.GaussianBlur(prev_gray, (3, 3), 0)
    g = cv2.GaussianBlur(gray, (3, 3), 0)
    diff = cv2.absdiff(p, g)
    _, mask = cv2.threshold(diff, diff_threshold, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

    if roi_mask is not None:
        mask = cv2.bitwise_and(mask, mask, mask=roi_mask)

    known_like = _mask_boxes(mask.shape, known_boxes, known_margin)
    mask[known_like > 0] = 0

    out = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        area = float(cv2.contourArea(c))
        x, y, w, h = cv2.boundingRect(c)
        rect_area = w * h
        ar = max(w, h) / max(1, min(w, h))
        if area < min_area:
            continue
        if rect_area < min_area_ratio * median_known_area:
            continue
        if w < min_w_ratio * median_known_w or h < min_h_ratio * median_known_h:
            continue
        if ar > max_aspect:
            continue
        score = min(1.0, area / max(min_area * 10.0, 1.0))
        out.append((x, y, x + w, y + h, score))
    return sorted(out, key=lambda z: z[-1], reverse=True)


def _center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def _center_distance(a, b):
    ac, bc = _center(a), _center(b)
    return float(np.hypot(ac[0] - bc[0], ac[1] - bc[1]))


def _template_track_candidate(prev_gray, gray, box, search_radius, min_corr=0.45):
    x1, y1, x2, y2 = map(int, box[:4])
    h, w = gray.shape[:2]
    x1 = max(0, min(w - 1, x1))
    y1 = max(0, min(h - 1, y1))
    x2 = max(x1 + 2, min(w, x2))
    y2 = max(y1 + 2, min(h, y2))
    templ = cv2.GaussianBlur(prev_gray[y1:y2, x1:x2], (3, 3), 0)
    sx1 = max(0, x1 - search_radius)
    sy1 = max(0, y1 - search_radius)
    sx2 = min(w, x2 + search_radius)
    sy2 = min(h, y2 + search_radius)
    search = cv2.GaussianBlur(gray[sy1:sy2, sx1:sx2], (3, 3), 0)
    if search.shape[0] < templ.shape[0] or search.shape[1] < templ.shape[1]:
        return None
    res = cv2.matchTemplate(search, templ, cv2.TM_CCOEFF_NORMED)
    _, corr, _, loc = cv2.minMaxLoc(res)
    if corr < min_corr:
        return None
    nx1, ny1 = sx1 + loc[0], sy1 + loc[1]
    ww, hh = x2 - x1, y2 - y1
    return (nx1, ny1, nx1 + ww, ny1 + hh, float(corr))


def find_next_new_object_frame(
    video_path: str | Path,
    annotation_json: str | Path,
    start_frame: int,
    *,
    diff_threshold: int = 12,
    known_margin: int = 25,
    min_area: int = 15,
    min_area_ratio: float = 0.55,
    min_w_ratio: float = 0.50,
    min_h_ratio: float = 0.50,
    max_aspect: float = 3.0,
    border_relax_ratio: float = 0.40,
    border_pixels: int = 6,
    confirm_frames: int = 3,
    max_candidate_step: float = 28.0,
    max_search_frames: Optional[int] = None,
    known_search_margin: int = 50,
    template_match_threshold: float = 0.35,
    max_confirm_miss: int = 2,
    roi_side: str = "none",
    roi_ratio: float = 1 / 3,
    roi_rect: Optional[tuple] = None,
    verbose_log: bool = True,
) -> FrameDifferenceResult:
    """Find the first persistent new-object cue after a seed frame.

    The JSON contains the current frame's known seed boxes. Frame difference finds
    motion candidates, while template matching verifies persistence and excludes
    the tracked known-object regions.
    """
    if confirm_frames < 1:
        raise ValueError("confirm_frames must be >= 1")

    ann = JSONSeedAnnotations(annotation_json)
    known_start = ann.get_boxes_at_frame(start_frame)
    if not known_start:
        raise ValueError(f"No seed boxes at start_frame={start_frame}")

    areas = np.array([b.width * b.height for b in known_start], dtype=float)
    ws = np.array([b.width for b in known_start], dtype=float)
    hs = np.array([b.height for b in known_start], dtype=float)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Unable to open video: {video_path}")

    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if start_frame < 0 or start_frame >= n:
            raise ValueError(f"start_frame must be in [0,{n - 1}], got {start_frame}")

        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        ok, prev = cap.read()
        if not ok:
            raise RuntimeError(f"Unable to read start_frame={start_frame}")

        prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
        roi_mask = _make_roi_mask(
            prev_gray.shape, side=roi_side, ratio=roi_ratio, rect=roi_rect
        )
        known_boxes = known_start
        search_limit = (
            n - 1 if max_search_frames is None
            else min(n - 1, start_frame + max_search_frames)
        )
        pending = None
        searched = 0
        if verbose_log:
            print(
                f"[frame-diff] START video={Path(video_path).name} seed_frame={start_frame} "
                f"total_frames={n} max_search={max(0, search_limit - start_frame)} "
                f"known_boxes={len(known_start)} diff_threshold={diff_threshold} "
                f"confirm_frames={confirm_frames} max_miss={max_confirm_miss} "
                f"roi={roi_rect if roi_rect is not None else roi_side}"
            )

        for fi in range(start_frame + 1, search_limit + 1):
            ok, frame = cap.read()
            if not ok:
                break

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            tracked = _track_known_boxes(
                prev_gray, gray, known_boxes, known_search_margin
            )
            searched = fi - start_frame

            raw_diff = cv2.absdiff(
                cv2.GaussianBlur(prev_gray, (3, 3), 0),
                cv2.GaussianBlur(gray, (3, 3), 0),
            )
            diff_mask = (raw_diff >= diff_threshold).astype(np.uint8)
            if roi_mask is not None:
                diff_mask = cv2.bitwise_and(diff_mask, diff_mask, mask=(roi_mask > 0).astype(np.uint8))
            diff_pixels = int(np.count_nonzero(diff_mask))
            diff_ratio = float(diff_pixels / max(1, diff_mask.size))
            raw_candidate_count = None
            relaxed_candidate_count = None

            if pending is not None:
                tracked_c = _template_track_candidate(
                    pending["template_gray"],
                    gray,
                    pending["box"],
                    int(max_candidate_step * 2),
                    template_match_threshold,
                )
                step_ok = (
                    tracked_c is not None
                    and _center_distance(pending["box"], tracked_c[:4])
                    <= max_candidate_step
                )
                if verbose_log:
                    print(
                        f"[frame-diff] compare={fi - 1}->{fi} frame={fi} offset={searched} diff_pixels={diff_pixels} "
                        f"diff_ratio={diff_ratio:.6f} tracked_known={len(tracked)} pending=1 "
                        f"pending_hits={pending['hits']} pending_miss={pending['miss']} step_ok={step_ok} "
                        f"template_corr={round(float(tracked_c[4]), 4) if tracked_c else None}"
                    )
                if not step_ok:
                    pending["miss"] += 1
                    pending["template_gray"] = gray.copy()
                    if pending["miss"] > max_confirm_miss:
                        pending = None
                else:
                    cb = tracked_c[:4]
                    known_mask = _mask_boxes(gray.shape, tracked, known_margin)
                    x1, y1, x2, y2 = map(int, cb)
                    patch = known_mask[
                        max(0, y1): min(gray.shape[0], y2),
                        max(0, x1): min(gray.shape[1], x2),
                    ]
                    if patch.size and np.mean(patch > 0) > 0.10:
                        pending = None
                    else:
                        pending["miss"] = 0
                        pending["box"] = cb
                        pending["hits"] += 1
                        pending["template_gray"] = gray.copy()
                        if pending["hits"] >= confirm_frames:
                            b = pending["first_box"]
                            if verbose_log:
                                print(
                                    f"[frame-diff] FOUND first_frame={pending['first_frame']} "
                                    f"offset={pending['first_frame'] - start_frame} confirm_hits={pending['hits']} "
                                    f"bbox={tuple(map(int, pending['first_box'][:4]))} searched_frames={searched}"
                                )
                            return FrameDifferenceResult(
                                frame_index=pending["first_frame"],
                                frame_offset=pending["first_frame"] - start_frame,
                                bbox=tuple(map(int, b[:4])),
                                score=float(b[4]),
                                known_box_count=len(known_start),
                                search_frames=searched,
                                status="new_object_found",
                                message="持续帧差候选已确认，建议 SAM3 追踪到该帧。",
                            )

            if pending is None:
                cands = _candidate_contours(
                    prev_gray,
                    gray,
                    tracked,
                    diff_threshold=diff_threshold,
                    known_margin=known_margin,
                    min_area=min_area,
                    median_known_area=float(np.median(areas)),
                    median_known_w=float(np.median(ws)),
                    median_known_h=float(np.median(hs)),
                    min_area_ratio=min_area_ratio,
                    min_w_ratio=min_w_ratio,
                    min_h_ratio=min_h_ratio,
                    max_aspect=max_aspect,
                    roi_mask=roi_mask,
                )
                raw_candidate_count = len(cands)
                if not cands:
                    relaxed = _candidate_contours(
                        prev_gray,
                        gray,
                        tracked,
                        diff_threshold=diff_threshold,
                        known_margin=known_margin,
                        min_area=min_area,
                        median_known_area=float(np.median(areas)),
                        median_known_w=float(np.median(ws)),
                        median_known_h=float(np.median(hs)),
                        min_area_ratio=border_relax_ratio,
                        min_w_ratio=min_w_ratio,
                        min_h_ratio=min_h_ratio,
                        max_aspect=max_aspect,
                        roi_mask=roi_mask,
                    )
                    relaxed_candidate_count = len(relaxed)
                    cands = [
                        c for c in relaxed
                        if min(
                            c[0], c[1],
                            gray.shape[1] - c[2],
                            gray.shape[0] - c[3],
                        ) <= border_pixels
                    ]

                if verbose_log:
                    top = list(cands[0][:5]) if cands else None
                    print(
                        f"[frame-diff] compare={fi - 1}->{fi} frame={fi} offset={searched} diff_pixels={diff_pixels} "
                        f"diff_ratio={diff_ratio:.6f} tracked_known={len(tracked)} "
                        f"candidates={len(cands)} raw_candidates={raw_candidate_count} "
                        f"relaxed={relaxed_candidate_count} pending=0 top={top}"
                    )

                if cands:
                    cand = cands[0]
                    pending = {
                        "first_frame": fi,
                        "first_box": cand,
                        "box": cand,
                        "hits": 1,
                        "miss": 0,
                        "template_gray": gray.copy(),
                    }
                    if confirm_frames <= 1:
                        return FrameDifferenceResult(
                            frame_index=fi,
                            frame_offset=fi - start_frame,
                            bbox=tuple(map(int, cand[:4])),
                            score=float(cand[4]),
                            known_box_count=len(known_start),
                            search_frames=searched,
                            status="new_object_found",
                            message="检测到持续帧差候选。",
                        )

            known_boxes = tracked
            prev_gray = gray

        # 没找到新对象时，也返回一个可控的搜索窗口，避免 SAM3 被完全跳过。
        if verbose_log:
            print(
                f"[frame-diff] DONE status=no_new_object searched_frames={searched} "
                f"search_range={start_frame + 1}-{start_frame + searched}"
            )
        return FrameDifferenceResult(
            frame_index=None,
            frame_offset=None,
            bbox=None,
            score=None,
            known_box_count=len(known_start),
            search_frames=searched,
            status="no_new_object",
            message="在搜索窗口内未确认新的持续运动目标，SAM3 将使用已搜索窗口继续追踪。",
        )
    finally:
        cap.release()
