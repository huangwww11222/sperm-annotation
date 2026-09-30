"""Durable annotation edits and explicit tracking feedback for one media.

A single atomic workspace replacement includes its revision and replay receipts.
Raw tracking output is retained so removing tombstones can undo deletion; every
consumer must apply the same stable-object deletion rules.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

log = logging.getLogger("review.annotation")
FILE_NAME = "workspace_state.json"
SERVER_FIELDS = {"normalMotionSamples", "trackingFeedbackEvents", "_writeReceipts", "revision", "deletedAnnotationFrames", "generationId"}


def read_state(directory: Path) -> dict[str, Any]:
    path = directory / FILE_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("workspace must be an object")
        return data
    except (OSError, ValueError) as exc:
        log.exception("annotation.workspace_read_failed media=%s", directory.name)
        raise HTTPException(500, "工作区文件无法读取，请保留当前编辑并重试") from exc


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if not k.startswith("_")}


def require_generation(state: dict[str, Any], generation: str | None):
    if state.get("generationId") and generation != state["generationId"]:
        log.warning("annotation.generation_conflict media=%s", state.get("mediaId"))
        raise HTTPException(409, "视频已覆盖重新标注，请重新读取工作区；不能写回旧标注或追踪分支")


def is_deleted(workspace: dict[str, Any], frame_index: int, object_id: int) -> bool:
    if object_id in workspace.get("deletedObjectIds", []):
        return True
    if any(item.get("objectId") == object_id and item.get("frameIndex") == frame_index
           for item in workspace.get("deletedFrameObjects", []) if isinstance(item, dict)):
        return True
    old = set(map(str, workspace.get("deletedTrackingIds", [])))
    return str(object_id) in old or f"ai-{frame_index}-{object_id}" in old or f"manual-{frame_index}-{object_id}" in old


def validate_deletions(state: dict[str, Any]) -> None:
    global_ids = state.get("deletedObjectIds", [])
    frames = state.get("deletedFrameObjects", [])
    if not isinstance(global_ids, list) or not isinstance(frames, list):
        raise HTTPException(422, "删除范围必须为数组")
    if any(type(oid) is not int or oid <= 0 for oid in global_ids):
        raise HTTPException(422, "删除对象必须使用稳定的正整数 objectId")
    seen = set()
    for item in frames:
        if not isinstance(item, dict) or type(item.get("objectId")) is not int or item["objectId"] <= 0 or type(item.get("frameIndex")) is not int or item["frameIndex"] < 0:
            raise HTTPException(422, "单帧删除必须包含有效 objectId/frameIndex")
        pair = (item["frameIndex"], item["objectId"])
        if pair in seen:
            raise HTTPException(422, "单帧删除记录重复")
        seen.add(pair)
    state["deletedObjectIds"] = sorted(set(global_ids))


def write_state(directory: Path, media_id: str, uid: int, key: str,
                body: dict[str, Any], operation: str,
                build: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Caller holds source_write lock. Replay precedes revision validation."""
    previous = read_state(directory)
    revision = int(previous.get("revision", 0))
    expected = body.get("expectedRevision")
    legacy = expected is None and operation == "workspace"
    if legacy:
        if previous.get("generationId"):
            raise HTTPException(428, "视频已覆盖重新标注，请刷新页面后使用新版客户端保存")
        # Legacy clients can still save ordinary edits, but main preserves all
        # server control fields. They cannot add/remove deletion tombstones.
        key = "legacy-" + uuid.uuid4().hex
        expected = revision
    elif not key or len(key) > 128:
        raise HTTPException(428, "请刷新页面：保存需要有效的重试标识")
    try:
        digest = hashlib.sha256(json.dumps([operation, uid, body], sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "工作区包含无效数值") from exc
    receipts = dict(previous.get("_writeReceipts", {}))
    receipt_key = f"{uid}:{key}"
    prior = receipts.get(receipt_key)
    if prior:
        if prior["digest"] != digest:
            raise HTTPException(409, "同一重试标识不能用于不同操作")
        log.info("annotation.workspace_replay media=%s user=%s key=%s operation=%s", media_id, uid, key, operation)
        return prior["response"]
    if type(expected) is not int or expected != revision:
        log.warning("annotation.workspace_conflict media=%s user=%s expected=%s actual=%s", media_id, uid, expected, revision)
        raise HTTPException(409, "工作区版本已变化，请保留当前编辑并重新读取")
    try:
        clean = build(previous)
        validate_deletions(clean)
    except HTTPException as exc:
        log.warning("annotation.workspace_rejected media=%s user=%s operation=%s status=%s reason=%s", media_id, uid, operation, exc.status_code, exc.detail)
        raise
    clean.update(format="annotation-workspace-v1", mediaId=media_id, updatedBy=uid, revision=revision + 1)
    clean.pop("expectedRevision", None)
    response = {"ok": True, "mediaId": media_id, "revision": revision + 1,
                "filename": FILE_NAME,
                "manualAnnotationCount": len(clean.get("manualAnnotations", [])),
                "manualBaselineCount": len(clean.get("manualBaselines", []))}
    if operation == "feedback":
        response.update(normalMotionSamples=clean.get("normalMotionSamples", []),
                        trackingFeedbackEvents=clean.get("trackingFeedbackEvents", []),
                        pausedAnomalies=clean.get("pausedAnomalies", []),
                        lastPausedContext=clean.get("lastPausedContext"))
    receipts[receipt_key] = {"digest": digest, "response": response}
    # The workspace is a single atomic resource, including successful receipts.
    clean["_writeReceipts"] = receipts
    encoded = json.dumps(clean, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 64 * 1024 * 1024:
        raise HTTPException(413, "工作区状态超过 64 MiB")
    temporary = directory / f".{FILE_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(directory / FILE_NAME)
    except OSError as exc:
        log.exception("annotation.workspace_save_failed media=%s user=%s key=%s operation=%s", media_id, uid, key, operation)
        raise HTTPException(500, "保存失败，修改尚未确认，请使用原请求重试") from exc
    finally:
        temporary.unlink(missing_ok=True)
    log.info("annotation.workspace_saved media=%s user=%s revision=%s operation=%s key=%s", media_id, uid, revision + 1, operation, key)
    if any(previous.get(field, []) != clean.get(field, []) for field in ("deletedObjectIds", "deletedFrameObjects", "deletedTrackingIds")):
        log.info("annotation.deletion_changed media=%s user=%s revision=%s global_ids=%s frame_rules=%s key=%s", media_id, uid, revision + 1, clean.get("deletedObjectIds", []), len(clean.get("deletedFrameObjects", [])), key)
    if operation == "feedback":
        event = clean["trackingFeedbackEvents"][-1]
        log.info("annotation.feedback_saved media=%s user=%s object=%s frame=%s decision=%s calibrated=%s geometry_reference_frame=%s reasons=%s revision=%s key=%s", media_id, uid, event["objectId"], event.get("frameIndex"), event["decision"], "sample" in event, event.get("geometryReference", {}).get("frameIndex"), event.get("reasons", []), revision + 1, key)
    return response


def read_rows(directory: Path) -> list[dict[str, Any]]:
    path = directory / "tracker_results.json"
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        try:
            content = json.loads(raw)
        except ValueError:
            content = [json.loads(line) for line in raw.splitlines() if line.strip()]
        rows = content if isinstance(content, list) else content.get("frames", content.get("results", [content]))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid tracking rows")
        return rows
    except (ValueError, OSError, AttributeError) as exc:
        log.exception("annotation.tracking_read_failed media=%s", directory.name)
        raise HTTPException(500, "追踪结果无法读取，不能将读取失败当作空标注") from exc


def deletion_preview(directory: Path, object_id: int) -> dict[str, Any]:
    workspace = read_state(directory)
    occurrences: dict[int, str] = {}
    name = f"对象 {object_id}"
    for row in read_rows(directory):
        frame = int(row.get("source_frame_index", row.get("frame_index", row.get("frameIndex", -1))))
        for obj in row.get("objects", []):
            if int(obj.get("object_id", obj.get("objectId", 0))) == object_id and not is_deleted(workspace, frame, object_id):
                occurrences[frame] = "ai"
                name = obj.get("name") or name
    for obj in workspace.get("manualAnnotations", []):
        frame = int(obj.get("frameIndex", -1))
        if obj.get("objectId") == object_id and not is_deleted(workspace, frame, object_id):
            occurrences[frame] = "manual"
            name = obj.get("name") or name
    return {"mediaId": directory.name, "objectId": object_id, "revision": int(workspace.get("revision", 0)),
            "name": name, "totalCount": len(occurrences), "frameCount": len(occurrences),
            "manualCount": sum(source == "manual" for source in occurrences.values()),
            "aiCount": sum(source == "ai" for source in occurrences.values()),
            "firstFrame": min(occurrences) if occurrences else None,
            "lastFrame": max(occurrences) if occurrences else None}


def feedback_state(previous: dict[str, Any], directory: Path, body: dict[str, Any], uid: int) -> dict[str, Any]:
    object_id, frame = body.get("objectId"), body.get("frameIndex")
    decision = body.get("decision")
    if type(object_id) is not int or object_id <= 0 or decision not in {"normal", "corrected", "reset"}:
        raise HTTPException(422, "人工确认对象或决定无效")
    if "calibrate" in body and type(body["calibrate"]) is not bool:
        raise HTTPException(422, "校准选项必须为布尔值")
    samples = list(previous.get("normalMotionSamples", []))
    events = list(previous.get("trackingFeedbackEvents", []))
    event = {"id": uuid.uuid4().hex, "objectId": object_id, "frameIndex": frame, "decision": decision, "userId": uid}
    if decision == "reset":
        samples = [sample for sample in samples if sample.get("objectId") != object_id]
    else:
        if type(frame) is not int or frame < 0 or is_deleted(previous, frame, object_id):
            raise HTTPException(422, "确认帧无效或该对象已删除")
        context = previous.get("lastPausedContext") or {}
        pending = any(item.get("object_id", item.get("objectId")) == object_id and not item.get("resolved")
                      for item in previous.get("pausedAnomalies", []) if isinstance(item, dict))
        if context.get("frameIndex") != frame or not pending:
            raise HTTPException(409, "该对象已不属于当前待确认暂停，请重新读取工作区")
        row = next((row for row in read_rows(directory)
                    if int(row.get("source_frame_index", row.get("frame_index", -1))) == frame), None)
        if row is None:
            raise HTTPException(409, "该帧追踪结果不存在，请重新读取异常")
        obj = next((obj for obj in row.get("objects", []) if obj.get("object_id", obj.get("objectId")) == object_id), None)
        anomaly = next((a for a in row.get("anomalies", []) if a.get("object_id") == object_id), None)
        if not anomaly and not (obj and obj.get("anomaly_level") in {"anomaly", "disappeared", "warning"}):
            raise HTTPException(409, "该对象没有待确认的追踪异常")
        details = (anomaly or {}).get("details", (obj or {}).get("anomaly_details", {})) or {}
        reasons = (anomaly or {}).get("reasons", (obj or {}).get("anomaly_reasons", []))
        event.update(reasons=reasons, details=details, calibrate=bool(body.get("calibrate", False)))
        if decision == "corrected":
            manual = next((obj for obj in previous.get("manualAnnotations", []) if obj.get("objectId") == object_id and obj.get("frameIndex") == frame and obj.get("source") == "manual"), None)
            if manual is None:
                raise HTTPException(409, "请先在当前帧修正并保存该对象，再确认修正")
            meta = json.loads((directory / "media.json").read_text(encoding="utf-8"))
            box = manual.get("bbox", {})
            try:
                width, height = float(meta["width"]), float(meta["height"])
                corrected = [box["x"] * width / 100, box["y"] * height / 100,
                             (box["x"] + box["width"]) * width / 100, (box["y"] + box["height"]) * height / 100]
                original = (obj or {}).get("bbox", details.get("current_bbox"))
                if not all(math.isfinite(v) for v in corrected) or corrected[2] <= corrected[0] or corrected[3] <= corrected[1]:
                    raise ValueError("invalid corrected bbox")
                if original and all(abs(a-b) < 0.001 for a,b in zip(corrected, original)):
                    raise HTTPException(409, "框尚未改变，请修正后确认；若原框正确请使用无异常")
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(422, "人工修正框无效") from exc
        else:
            # Accepting a size/shape warning is an explicit human geometry
            # judgement, even without redrawing. Keep its server-side snapshot
            # separate from manual annotations and optional movement learning.
            geometry_reason = any(str(reason).startswith(("manual_area_ratio=", "manual_width_ratio=", "manual_height_ratio=", "manual_aspect_change=")) for reason in reasons)
            if geometry_reason:
                try:
                    raw_box = (obj or {}).get("bbox")
                    if not isinstance(raw_box, list) or len(raw_box) != 4:
                        raise ValueError("missing confirmed bbox")
                    box = [float(value) for value in raw_box]
                    if not all(math.isfinite(value) for value in box) or box[2] <= box[0] or box[3] <= box[1]:
                        raise ValueError("invalid confirmed bbox")
                except (TypeError, ValueError, OverflowError) as exc:
                    raise HTTPException(422, "追踪框无效，不能保存正常尺寸参照；请重新读取或修正该对象") from exc
                event["geometryReference"] = {"objectId": object_id, "frameIndex": frame, "bbox": box, "source": "confirmed-normal"}
            # A motion sample never exempts shape, identity, overlap or loss.
            value = details.get("motion_normalized")
            motion_reason = any(str(reason).startswith(("adjacent_center_shift", "center_displacement", "motion_", "normalized_motion", "tracking_motion")) for reason in reasons)
            if bool(body.get("calibrate", False)) and motion_reason and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                sample = {"objectId": object_id, "frameIndex": frame, "reason": "motion", "decision": "normal", "calibrate": True, "features": {"motionNormalized": value}}
                samples = [s for s in samples if not (s.get("objectId") == object_id and s.get("frameIndex") == frame and s.get("reason") == "motion")]
                samples.append(sample)
                event["sample"] = sample
    events.append(event)
    result = {**previous, "normalMotionSamples": samples, "trackingFeedbackEvents": events}
    pause_frame = (previous.get("lastPausedContext") or {}).get("frameIndex", frame)
    # A frame deletion may have been saved by an older client without updating
    # its presentation-only pause list. Deleted targets never remain blockers.
    result["pausedAnomalies"] = [
        item for item in previous.get("pausedAnomalies", [])
        if (decision == "reset" or item.get("object_id", item.get("objectId")) != object_id)
        and not is_deleted(previous, pause_frame, item.get("object_id", item.get("objectId")))
    ]
    if not result["pausedAnomalies"]:
        result["lastPausedContext"] = None
    return result
