from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
from PIL import Image

from .config import DEVICE, DTYPE, MODEL_ID
from .services.anomaly_detector import (
    AnomalyConfig,
    AnomalyDetector,
    AnomalyLevel,
    ManualBaseline,
)
from .services.sam3_engine import get_sam3_engine, read_video
from .services.visualization import draw_frame, open_video_writer


RESULT_FILE_NAME = "tracker_results.json"
OVERLAY_FILE_NAME = "tracker_overlay.mp4"
WORKSPACE_STATE_FILE_NAME = "workspace_state.json"


def _json_bbox(values: list[float]) -> list[float]:
    return [round(float(v), 3) for v in values]


def _load_seed(
    annotation_json: str,
    width: int,
    height: int,
    source_frame_count: int,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    data = json.loads(Path(annotation_json).read_text(encoding="utf-8"))
    frame = data.get("frame", {})
    source_start = int(frame.get("frameIndex", 0))
    if source_start < 0 or source_start >= source_frame_count:
        raise ValueError(f"seed frame {source_start} outside source video range")

    annotations = data.get("annotations", [])
    if not isinstance(annotations, list):
        raise ValueError("seed annotation JSON annotations must be a list")

    objects: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for idx, ann in enumerate(annotations):
        if not isinstance(ann, dict):
            raise ValueError(f"annotations[{idx}] must be an object")
        ann_frame = int(ann.get("frameIndex", source_start))
        if ann_frame != source_start:
            raise ValueError(
                f"annotation {ann.get('id')} is on frame {ann_frame}, expected {source_start}"
            )
        bbox = ann.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue

        object_id_raw = ann.get("object_id", ann.get("objectId"))
        if object_id_raw is None:
            raise ValueError(
                f"annotation {ann.get('id', idx)} is missing object_id; "
                "identity must be persisted explicitly and never derived from array order"
            )
        try:
            object_id = int(object_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"annotation {ann.get('id', idx)} has invalid object_id={object_id_raw!r}") from exc
        if object_id <= 0:
            raise ValueError(f"annotation {ann.get('id', idx)} has non-positive object_id={object_id}")
        if object_id in seen_ids:
            raise ValueError(f"duplicate object_id={object_id} in seed annotations")
        seen_ids.add(object_id)

        try:
            x1, y1, x2, y2 = [float(v) for v in bbox]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"annotation {ann.get('id', idx)} bbox contains non-numeric values") from exc
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        x1 = max(0.0, min(float(max(0, width - 1)), x1))
        x2 = max(0.0, min(float(width), x2))
        y1 = max(0.0, min(float(max(0, height - 1)), y1))
        y2 = max(0.0, min(float(height), y2))
        if x2 <= x1 or y2 <= y1:
            continue

        objects.append(
            {
                "object_id": object_id,
                "source_id": ann.get("id", f"object-{object_id}"),
                "id": ann.get("id", f"object-{object_id}"),
                "name": ann.get("name", "object"),
                "label": ann.get("name", "object"),
                "bbox": [x1, y1, x2, y2],
            }
        )

    if not objects:
        raise ValueError("No manual bbox annotations found")

    return data, source_start, objects


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _clean_history_before_seed(
    output_json: Path,
    seed_source_frame: int,
) -> list[tuple[int, dict[int, list[float]]]]:
    """Return prior clean boxes for the long-term anomaly baseline.

    Results at the new seed frame and later will be replaced by this tracking
    request, so they must not affect the baseline.  Previously persisted HARD
    anomalies are also excluded: they are exactly the oversized boxes that the
    detector must not learn as a normal object size.
    """
    frames: list[tuple[int, dict[int, list[float]]]] = []
    for row in _read_jsonl(output_json):
        try:
            source_frame = int(row.get("source_frame_index", row.get("frame_index", -1)))
        except (TypeError, ValueError):
            continue
        if source_frame < 0 or source_frame >= int(seed_source_frame):
            continue

        objects: dict[int, list[float]] = {}
        for obj in row.get("objects", []):
            if not isinstance(obj, dict) or obj.get("anomaly_level") in {
                AnomalyLevel.ANOMALY.value,
                AnomalyLevel.DISAPPEARED.value,
            }:
                continue
            bbox = obj.get("bbox")
            try:
                if not isinstance(bbox, list) or len(bbox) != 4:
                    continue
                clean_bbox = [float(v) for v in bbox]
                if clean_bbox[2] <= clean_bbox[0] or clean_bbox[3] <= clean_bbox[1]:
                    continue
                objects[int(obj["object_id"])] = clean_bbox
            except (KeyError, TypeError, ValueError):
                continue
        if objects:
            frames.append((source_frame, objects))
    return sorted(frames, key=lambda item: item[0])


def _load_latest_manual_baselines(
    seed_path: Path,
    seed_source_frame: int,
) -> dict[int, ManualBaseline]:
    """Restore each object's newest explicit manual bbox up to the seed frame.

    ``annotations_frame_*.json`` is durable per-media state, so this works for
    resume requests and after a backend restart.  The current ``seed_frame`` is
    also considered for direct API clients.  An AI/warning/anomaly box is never
    accepted, even if it is being reused as a valid SAM3 seed.
    """
    candidates = list(seed_path.parent.glob("annotations_frame_*.json"))
    if seed_path not in candidates:
        candidates.append(seed_path)

    latest: dict[int, ManualBaseline] = {}
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            frame_index = int(payload.get("frame", {}).get("frameIndex", -1))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if frame_index < 0 or frame_index > int(seed_source_frame):
            continue

        for annotation in payload.get("annotations", []):
            if not isinstance(annotation, dict) or annotation.get("source") != "manual":
                continue
            try:
                object_id = int(annotation.get("object_id", annotation.get("objectId")))
                bbox = [float(value) for value in annotation["bbox"]]
            except (KeyError, TypeError, ValueError):
                continue
            if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                continue
            previous = latest.get(object_id)
            if previous is None or frame_index >= previous.frame_index:
                latest[object_id] = ManualBaseline(
                    bbox=bbox,
                    frame_index=frame_index,
                    name=annotation.get("name") or None,
                )

    # workspace_state.json is written whenever the browser changes a manual
    # box.  It can therefore be newer than the last explicit Tracking seed.
    # Only entries explicitly marked manual are accepted; AI predictions can
    # never become a baseline merely by being persisted in the workspace.
    workspace_file = seed_path.parent / WORKSPACE_STATE_FILE_NAME
    try:
        workspace = json.loads(workspace_file.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        workspace = {}
    for item in workspace.get("manualBaselines", []):
        if not isinstance(item, dict) or item.get("source") != "manual":
            continue
        try:
            object_id = int(item.get("objectId", item.get("object_id")))
            frame_index = int(item.get("frameIndex", item.get("frame_index", -1)))
            bbox = [float(value) for value in item["bbox"]]
        except (KeyError, TypeError, ValueError):
            continue
        if frame_index < 0 or frame_index > int(seed_source_frame):
            continue
        if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        previous = latest.get(object_id)
        if previous is None or frame_index >= previous.frame_index:
            latest[object_id] = ManualBaseline(
                bbox=bbox,
                frame_index=frame_index,
                name=item.get("name") or None,
            )
    return latest


def rewind_tracking_results(
    path: Path,
    video_file: Path,
    cutoff_frame: int,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop only stale future SAM3 rows *after* ``cutoff_frame``.

    Frame N is the user's new authoritative branch point. It must be kept so
    existing AI boxes on that frame can be combined with human edits/additions
    and then submitted as the next SAM3 seed. Rows strictly before N are also
    retained. The replacement tracking run will upsert/replace frame N when its
    new result is written.
    """
    cutoff = int(cutoff_frame)
    if cutoff < 0:
        raise ValueError("cutoff_frame must be >= 0")

    rows = _read_jsonl(path)
    kept: list[dict[str, Any]] = []
    removed = 0
    for row in rows:
        try:
            frame = int(row.get("source_frame_index", row.get("frame_index", -1)))
        except Exception:
            continue
        if frame <= cutoff:
            kept.append(row)
        else:
            removed += 1

    _write_jsonl(path, kept)

    overlay_path = path.parent / OVERLAY_FILE_NAME
    if video_file.is_file():
        video_meta = dict(meta or {})
        if not video_meta:
            video_meta = _probe_video(video_file)
        try:
            _render_overlay_video(video_file, overlay_path, video_meta, kept)
        except Exception as exc:
            print(f"[tracking-rewind] overlay regeneration failed: {exc}")

    # Annotation JSON is user data and also the durable source of manual
    # baselines.  Rewinding tracking must never delete it; the next save simply
    # overwrites the selected frame if the user confirms a new annotation.
    deleted_seed_files = 0

    print(
        f"[tracking-rewind] cutoff={cutoff} kept_rows={len(kept)} "
        f"removed_rows={removed} deleted_future_seeds={deleted_seed_files}"
    )
    return {
        "cutoffFrame": cutoff,
        "keptRows": len(kept),
        "removedRows": removed,
        "deletedFutureSeedFiles": deleted_seed_files,
        "overlayRegenerated": video_file.is_file(),
    }


def track_video(
    video_path: str,
    annotation_json: str,
    output_json: str,
    max_frames: int,
    bbox_mode: str = "pixel",
    start_frame: int | None = None,
) -> dict[str, Any]:
    """Run SAM3 on the exact original source-frame sequence.

    The persisted result is JSONL with one frame object per line, matching the
    supplied tracker_results.json structure. SAM3 frame_index and source_frame_index
    are intentionally identical in raw-frame mode.
    """
    if bbox_mode != "pixel":
        raise ValueError("FastAPI tracker expects pixel bbox input")
    if max_frames < 1:
        raise ValueError("max_frames must be >= 1")

    video_file = Path(video_path)
    cap_meta = _probe_video(video_file)
    width = cap_meta["width"]
    height = cap_meta["height"]
    source_frame_count = cap_meta["frameCount"]
    source_fps = cap_meta["fps"]

    source_json, seed_source_frame, objects = _load_seed(
        annotation_json,
        width,
        height,
        source_frame_count,
    )
    requested_source_start = seed_source_frame if start_frame is None else int(start_frame)
    if requested_source_start != seed_source_frame:
        raise ValueError("startFrame must match the annotation frameIndex")

    # Raw-frame mode: keep the exact source frame sequence.
    # This deliberately uses the original FPS and original frame indices so
    # SAM3, tracker_results.json and the browser all share one timeline.
    frames, meta = read_video(video_file, target_fps=None)
    source_indices = [int(x) for x in meta.get("source_frame_indices", [])]
    if not frames or not source_indices:
        raise ValueError("No source frames available")

    # In raw-frame mode the source frame index is the SAM3 session index.
    seed_frame = seed_source_frame
    if seed_frame >= len(frames):
        raise ValueError(f"Seed frame {seed_frame} outside decoded video")

    available = len(frames) - seed_frame
    requested = min(int(max_frames), available)
    if requested <= 0:
        raise ValueError("No frames remain from selected start frame")

    engine = get_sam3_engine(MODEL_ID, DEVICE, DTYPE)

    # IMPORTANT for a 4 GB GPU: preprocessing/storage stay on CPU, while the
    # actual SAM3 inference model remains on CUDA. These are the same controls
    # used by the validated standalone backend.
    session = engine.make_tracker_session(frames)
    engine.add_manual_boxes(session, seed_frame, objects)

    # 每次 SAM3 推理请求都有新 detector，但先灌入当前分支在 seed 前的
    # 已验证历史框。这样续追时也能和整个对象历史尺寸比较，而不是只看本轮。
    active_object_ids = {int(o["object_id"]) for o in objects}
    manual_baselines = {
        object_id: baseline
        for object_id, baseline in _load_latest_manual_baselines(Path(annotation_json), seed_source_frame).items()
        if object_id in active_object_ids
    }
    detector = AnomalyDetector(
        config=AnomalyConfig(),
        fps=source_fps,
        frame_width=width,
        frame_height=height,
        all_object_ids=active_object_ids,
        manual_baselines=manual_baselines,
    )
    detector.prime_history(_clean_history_before_seed(Path(output_json), seed_source_frame))

    # object_id → name 映射，让 tracking 结果继承用户标注的名字
    object_names: dict[int, str] = {int(o["object_id"]): o.get("name") or f"object-{o['object_id']}" for o in objects}

    # 把 Tracking 的 seed 帧也写入 tracker_results.json。
    # 以前这里直接跳过 seed frame，导致常见的第 0 帧标注根本不会进入结果文件。
    seed_source_idx = source_indices[seed_frame]
    seed_rows = [{
        "object_id": int(obj["object_id"]),
        "name": object_names.get(int(obj["object_id"]), obj.get("name") or f"object-{obj['object_id']}"),
        "bbox": _json_bbox(obj["bbox"]),
        "score": None,
        "source": "manual_seed",
        "mask_area": None,
        "sam3_object_id": int(obj["object_id"]),
    } for obj in objects]
    new_rows: list[dict[str, Any]] = [{
        "frame_index": seed_frame,
        "source_frame_index": seed_source_idx,
        "objects": seed_rows,
    }]

    # 人工确认的 seed 作为可信历史写入，但不把它当作 AI 输出触发暂停。
    detector.initialize_seed(
        seed_frame,
        {int(obj["object_id"]): list(obj["bbox"]) for obj in objects},
    )

    result_anomaly_paused = None
    last_processed_frame = seed_source_idx
    end_frame_exclusive = seed_frame + requested
    for output in engine.propagate_manual(
        session,
        max_frames=requested,
        start_frame_idx=seed_frame,
    ):
        frame_idx = int(output.frame_idx)
        if frame_idx >= end_frame_exclusive:
            break
        if frame_idx <= seed_frame or frame_idx >= len(source_indices):
            continue

        detections, _ = engine.decode_tracker_output(session, output)
        source_idx = source_indices[frame_idx]

        object_rows: list[dict[str, Any]] = []
        for detection in detections:
            bbox = _json_bbox(detection.bbox)
            oid = int(detection.object_id)
            obj_row = {
                "object_id": oid,
                "name": object_names.get(oid, f"object-{oid}"),
                "bbox": bbox,
                "score": round(float(detection.score), 15)
                if detection.score is not None
                else None,
                "source": "manual_sam3_tracker",
                "mask_area": int(detection.mask_area)
                if detection.mask_area is not None
                else None,
                "sam3_object_id": int(detection.sam3_object_id)
                if detection.sam3_object_id is not None
                else oid,
            }
            object_rows.append(obj_row)

        # ── 异常检测 ──
        frame_objs_for_detector: dict[int, list[float]] = {}
        for obj_row in object_rows:
            oid = int(obj_row["object_id"])
            frame_objs_for_detector[oid] = list(obj_row["bbox"])
        anomaly_report = detector.push(frame_idx, frame_objs_for_detector)

        # ── 把 anomaly level + reasons 写入每个 object row ──
        for af in anomaly_report.frames:
            for obj_row in object_rows:
                if int(obj_row["object_id"]) == af.object_id:
                    obj_row["anomaly_level"] = af.level.value
                    obj_row["anomaly_reasons"] = af.reasons
                    obj_row["anomaly_details"] = af.details
                    break

        row = {
            "frame_index": frame_idx,
            "source_frame_index": source_idx,
            "objects": object_rows,
            "anomalies": [
                {
                    "object_id": int(af.object_id),
                    "level": af.level.value,
                    "reasons": af.reasons,
                    "details": af.details,
                }
                for af in anomaly_report.frames
                if af.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED)
            ],
        }
        new_rows.append(row)
        last_processed_frame = source_idx
        print(
            f"[sam3] frame={frame_idx} source={source_idx} "
            f"tracked_objects={len(object_rows)}"
        )

        # HARD 异常 → 在当前异常帧保存结果后立即暂停。
        if anomaly_report.should_pause:
            pause_objects = []
            for af in anomaly_report.frames:
                if af.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED):
                    details = af.details or {}
                    area_ratio = details.get("area_ratio")
                    width_ratio = details.get("width_ratio")
                    height_ratio = details.get("height_ratio")
                    is_shrink = (
                        isinstance(area_ratio, (int, float)) and area_ratio < detector.config.MANUAL_AREA_RATIO_MIN_HARD
                    ) or (
                        isinstance(width_ratio, (int, float)) and width_ratio < detector.config.MANUAL_WIDTH_RATIO_MIN_HARD
                    ) or (
                        isinstance(height_ratio, (int, float)) and height_ratio < detector.config.MANUAL_HEIGHT_RATIO_MIN_HARD
                    )
                    pause_type = (
                        "disappearance" if af.level == AnomalyLevel.DISAPPEARED
                        else "overlap" if any("bbox_overlap_with=" in r for r in af.reasons)
                        else "size_shrink" if is_shrink
                        else "size_growth" if any(r.startswith(("manual_area_ratio=", "manual_width_ratio=", "manual_height_ratio=")) for r in af.reasons)
                        else "shape_change" if any(r.startswith("manual_aspect_change=") for r in af.reasons)
                        else "tracking_motion"
                    )
                    other_id = details.get("other_object_id")
                    display_name = object_names.get(int(af.object_id), f"object-{af.object_id}")
                    pause_objects.append({
                        "object_id": int(af.object_id),
                        "name": display_name,
                        "display_name": display_name,
                        "type": pause_type,
                        "reasons": list(af.reasons),
                        "details": details,
                        "currentBox": details.get("current_bbox"),
                        "manualBaselineBox": details.get("manual_baseline_bbox"),
                        "manualBaselineFrame": details.get("manual_baseline_frame"),
                        "metrics": {
                            "areaRatio": area_ratio,
                            "widthRatio": width_ratio,
                            "heightRatio": height_ratio,
                            "aspectRatio": details.get("aspect_ratio_ratio"),
                            "overlapCoverage": details.get("overlap_coverage"),
                        },
                        "reviewStartFrame": details.get("review_start_frame"),
                        "reviewEndFrame": details.get("review_end_frame"),
                        "reviewLookbackFrames": details.get("review_lookback_frames"),
                        "other_object_id": other_id,
                        "other_display_name": object_names.get(int(other_id), f"object-{other_id}") if other_id is not None else None,
                        "ratio": area_ratio,
                        "prevArea": details.get("baseline_area"),
                        "currArea": details.get("current_area"),
                    })
            print(f"[anomaly] HARD detected → pause frame={frame_idx}: {pause_objects}")
            result_anomaly_paused = {
                "frame_index": frame_idx,
                "reasons": pause_objects,
                "levels": {str(k): v.value for k, v in anomaly_report.object_levels.items()},
            }
            break

    merged_rows = _merge_rows(Path(output_json), new_rows, keep_before_source_frame=seed_source_idx)
    overlay_path = Path(output_json).parent / OVERLAY_FILE_NAME
    _render_overlay_video(video_file, overlay_path, meta, merged_rows)

    result = {
        "frames": merged_rows,
        "resultFile": str(Path(output_json).resolve()),
        "overlayVideo": str(overlay_path.resolve()),
        "startFrame": requested_source_start,
        "requestedFrames": requested,
        "processedFrames": len(new_rows),
        "lastProcessedFrame": last_processed_frame,
        "reachedVideoEnd": last_processed_frame >= source_frame_count - 1,
        "sourceFps": source_fps,
        "processFps": source_fps,
        "sampleInterval": 1,
        "sourceFrameIndices": source_indices,
        "model": engine.model_id,
        "device": str(engine.device),
        "dtype": str(engine.torch_dtype).replace("torch.", ""),
        "media": {
            "name": video_file.name,
            "width": width,
            "height": height,
            "fps": source_fps,
            "frameCount": source_frame_count,
        },
        "anomaly_paused": result_anomaly_paused,
    }
    print(f"[sam3] result jsonl: {output_json}")
    print(f"[sam3] overlay mp4: {overlay_path}")
    return result


def _merge_rows(
    path: Path,
    new_rows: list[dict[str, Any]],
    keep_before_source_frame: int | None = None,
) -> list[dict[str, Any]]:
    """Merge a new tracking segment into the result file.

    When a tracking run starts/resumes at a seed frame, stale rows at or after
    that seed must be removed; otherwise a newly paused run could still expose
    old future tracking results after the pause frame. Rows strictly before the
    new seed are retained, while the new segment owns the seed-and-later range.
    """
    merged: dict[int, dict[str, Any]] = {}
    for row in _read_jsonl(path):
        try:
            key = int(row.get("source_frame_index", row.get("frame_index", -1)))
        except Exception:
            continue
        if key < 0:
            continue
        if keep_before_source_frame is not None and key >= int(keep_before_source_frame):
            continue
        merged[key] = row

    for row in new_rows:
        key = int(row["source_frame_index"])
        merged[key] = row

    ordered = [merged[key] for key in sorted(merged)]
    _write_jsonl(path, ordered)
    return ordered


def _render_overlay_video(
    source_video: Path,
    output_path: Path,
    source_meta: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    """Write a full-length overlay MP4 at the original source FPS.

    Every original frame is decoded in order. Frames with a tracking row are
    rendered with boxes; other frames are copied unchanged. This keeps the
    overlay video on exactly the same frame/time axis as the browser source.
    """
    frame_map = {
        int(row.get("source_frame_index", row.get("frame_index", -1))): row
        for row in rows
        if int(row.get("source_frame_index", row.get("frame_index", -1))) >= 0
    }

    cap = cv2.VideoCapture(str(source_video))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {source_video}")

    width = int(source_meta.get("width") or cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(source_meta.get("height") or cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    fps = float(source_meta.get("fps") or cap.get(cv2.CAP_PROP_FPS) or 30.0)
    meta = {"width": width, "height": height, "fps": fps}

    writer = None
    frame_idx = 0
    try:
        writer = open_video_writer(output_path, meta)
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            row = frame_map.get(frame_idx)
            objects = []
            if row:
                for obj in row.get("objects", []):
                    objects.append({
                        "track_id": obj.get("object_id", 0),
                        "object_id": obj.get("object_id", 0),
                        "bbox": obj.get("bbox"),
                        "score": obj.get("score"),
                        "source": obj.get("source", "manual_sam3_tracker"),
                    })

            rendered = draw_frame(
                image,
                objects,
                title=f"SAM3 TRACKER | frame {frame_idx}",
            )
            writer.write(rendered)
            frame_idx += 1
    finally:
        cap.release()
        if writer is not None:
            writer.release()

def _probe_video(path: Path) -> dict[str, Any]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {path}")
    try:
        return {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            "frameCount": int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
            "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0),
        }
    finally:
        cap.release()


def get_tracker_engine():
    return get_sam3_engine(MODEL_ID, DEVICE, DTYPE)
