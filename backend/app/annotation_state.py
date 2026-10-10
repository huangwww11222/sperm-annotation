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
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

log = logging.getLogger("review.annotation")
FILE_NAME = "workspace_state.json"
RESULT_META_FILE = "tracker_results.meta.json"
SERVER_FIELDS = {"normalMotionSamples", "trackingFeedbackEvents", "_writeReceipts", "revision", "deletedAnnotationFrames", "generationId", "pausedAnomalies", "lastPausedContext"}


def read_state(directory: Path, *, recover_pause: bool = True) -> dict[str, Any]:
    from .tracking_restart import recover_directory
    recover_directory(directory)
    if recover_pause:
        recover_pause_publication(directory)
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


def tracking_frame_index(row: dict[str, Any]) -> int:
    frame = row.get('source_frame_index', row.get('frame_index', row.get('frameIndex')))
    if type(frame) is not int or frame < 0:
        raise ValueError('invalid tracking frame identity')
    return frame


def tracking_object_id(obj: dict[str, Any]) -> int:
    oid = obj.get('object_id', obj.get('objectId', obj.get('sam3_object_id')))
    if type(oid) is not int or oid <= 0:
        raise ValueError('invalid tracking object identity')
    if 'object_id' in obj and 'objectId' in obj and obj['objectId'] != oid:
        raise ValueError('conflicting stable tracking identities')
    return oid


def normalize_tracking_row(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(row.get('objects'), list):
        raise ValueError('invalid tracking frame')
    frame = tracking_frame_index(row)
    objects = []
    seen = set()
    for obj in row['objects']:
        if not isinstance(obj, dict):
            raise ValueError('invalid tracking object')
        oid = tracking_object_id(obj)
        if oid in seen:
            raise ValueError('duplicate tracking object identity')
        seen.add(oid)
        objects.append({**obj, 'object_id':oid})
    return {**row, 'frame_index':frame, 'source_frame_index':frame, 'objects':objects}


def validate_workspace_geometry(state: dict[str, Any], info: dict[str, Any]) -> None:
    """Validate before publishing a receipt; legal point annotations remain valid."""
    count = info.get('frameCount')
    width, height = info.get('width'), info.get('height')
    def number(value):
        return type(value) in (int,float) and math.isfinite(value)
    def identity(obj):
        if not isinstance(obj, dict) or obj.get('source') != 'manual' or type(obj.get('objectId')) is not int or obj['objectId'] <= 0:
            raise ValueError('人工对象须有 source=manual 和正整数 objectId')
        fi = obj.get('frameIndex')
        if type(fi) is not int or fi < 0 or (number(count) and count > 0 and fi >= count):
            raise ValueError('人工对象帧号无效或超过原视频范围')
        return fi,obj['objectId']
    def box(values, maximum_x, maximum_y):
        if not isinstance(values,list) or len(values) != 4 or not all(number(v) for v in values):
            raise ValueError('框坐标须为四个有限数值')
        x1,y1,x2,y2 = values
        if x2 <= x1 or y2 <= y1 or min(x1,y1) < -1e-6 or x2 > maximum_x+1e-6 or y2 > maximum_y+1e-6:
            raise ValueError('框尺寸无效或越过原图边界')
    try:
        for field in ('manualAnnotations','manualBaselines'):
            values = state.get(field,[])
            if not isinstance(values,list):
                raise ValueError(f'{field} 必须为数组')
            seen = set()
            for obj in values:
                pair = identity(obj)
                if pair in seen:
                    raise ValueError('同帧人工对象身份重复')
                seen.add(pair)
                if field == 'manualBaselines':
                    if not number(width) or width <= 0 or not number(height) or height <= 0:
                        raise ValueError('原视频尺寸不可用，无法校验人工基准')
                    box(obj.get('bbox'),width,height)
                    continue
                bbox,point = obj.get('bbox'),obj.get('point')
                if bbox is None and point is None:
                    raise ValueError('人工对象缺少框或点坐标')
                if bbox is not None:
                    if not isinstance(bbox,dict) or not all(number(bbox.get(k)) for k in ('x','y','width','height')):
                        raise ValueError('人工框坐标无效')
                    box([bbox['x'],bbox['y'],bbox['x']+bbox['width'],bbox['y']+bbox['height']],100,100)
                if point is not None and (not isinstance(point,dict) or not all(number(point.get(k)) and 0 <= point[k] <= 100 for k in ('x','y'))):
                    raise ValueError('点坐标无效或越过原图边界')
        for deletion in state.get('deletedFrameObjects',[]):
            fi = deletion.get('frameIndex') if isinstance(deletion,dict) else None
            if type(fi) is not int or fi < 0 or (number(count) and count > 0 and fi >= count):
                raise ValueError('单帧删除帧号越过原视频范围')
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


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


def _feedback_samples(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = {}
    by_object = {}
    for event in events:
        if event.get('decision') == 'reset':
            for key in by_object.pop(event.get('objectId'),set()):
                samples.pop(key,None)
        sample = event.get('sample')
        if sample:
            key = (sample.get('objectId'),sample.get('frameIndex'),sample.get('reason'))
            samples.pop(key,None)
            samples[key] = sample
            by_object.setdefault(key[0],set()).add(key)
    return list(samples.values())


def _compact_receipt(receipt: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    response = receipt['response']
    historical = response.get('trackingFeedbackEvents')
    if not isinstance(historical,list) or historical != events[:len(historical)]:
        return receipt
    response = {k:v for k,v in response.items() if k != 'trackingFeedbackEvents'}
    derived = response.get('normalMotionSamples') == _feedback_samples(historical)
    if derived:
        response.pop('normalMotionSamples',None)
    return {**receipt, 'response':response, 'feedbackEventCount':len(historical), 'derivedMotionSamples':derived}


def _replay_response(receipt: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if 'feedbackEventCount' not in receipt:
        return receipt['response']
    count = receipt['feedbackEventCount']
    history = state.get('trackingFeedbackEvents',[])
    if type(count) is not int or count < 0 or count > len(history):
        log.error('annotation.receipt_history_missing media=%s',state.get('mediaId'))
        raise HTTPException(500,'保存回执对应的反馈历史无法读取，请检查存储')
    events = history[:count]
    response = {**receipt['response'], 'trackingFeedbackEvents':events}
    if receipt.get('derivedMotionSamples'):
        response['normalMotionSamples'] = _feedback_samples(events)
    return response


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
        return _replay_response(prior, previous)
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
    # Events are immutable append-only history. Reference each committed prefix
    # instead of embedding it again in every feedback receipt; replay still
    # returns the exact historical response, including samples before a reset.
    events = clean.get('trackingFeedbackEvents',[])
    receipts = {k:_compact_receipt(v,events) for k,v in receipts.items()}
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


def remember_tracking_results(directory: Path) -> None:
    """Remember published/legacy results independently of their continued existence."""
    marker = directory / RESULT_META_FILE
    if marker.is_file():
        return
    temporary = marker.with_name(marker.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text('{"format":"tracking-results-state-v1","generated":true}', encoding='utf-8')
        temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)


def tracking_result_presence(directory: Path) -> str:
    """Cheap lifecycle metadata; 'present' does not certify file contents."""
    from .tracking_restart import recover_directory
    recover_directory(directory)
    recover_pause_publication(directory)
    if (directory / 'tracker_results.json').is_file():
        return 'present'
    return 'missing' if (directory / RESULT_META_FILE).exists() else 'not_generated'


def read_rows(directory: Path, *, validate_geometry: bool = False) -> list[dict[str, Any]]:
    path = directory / "tracker_results.json"
    presence = tracking_result_presence(directory)
    if presence != 'present':
        if presence == 'missing':
            log.error('annotation.tracking_results_missing media=%s', directory.name)
            raise HTTPException(409, {'code':'TRACKING_RESULTS_MISSING', 'message':'已生成的追踪结果文件缺失，请管理员检查存储或恢复备份'})
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            remember_tracking_results(directory)
            return []
        try:
            content = json.loads(raw)
        except ValueError:
            content = [json.loads(line) for line in raw.splitlines() if line.strip()]
        rows = content if isinstance(content, list) else content.get("frames", content.get("results", [content]))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid tracking rows")
        rows = [normalize_tracking_row(row) for row in rows]
        seen_frames = set()
        for row in rows:
            fi = row['source_frame_index']
            if fi in seen_frames:
                raise ValueError('duplicate tracking frame')
            seen_frames.add(fi)
            for obj in row['objects']:
                box = obj.get('bbox')
                # Feedback has its own 422 geometry contract; result consumers
                # must validate before converting/skipping any saved boxes.
                if not validate_geometry:
                    continue
                if not isinstance(box, list) or len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
                    raise ValueError('invalid tracking geometry')
                if box[2] <= box[0] or box[3] <= box[1]:
                    raise ValueError('invalid tracking box extent')
        remember_tracking_results(directory)
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
    candidates = previous.get("_pauseCandidates", previous.get("pausedAnomalies", []))
    result["_pauseCandidates"] = [item for item in candidates if decision == "reset" or item.get("object_id", item.get("objectId")) != object_id]
    result["_pauseContext"] = (previous.get("_pauseContext") or previous.get("lastPausedContext")) if result["_pauseCandidates"] else None
    return result


def preserve_pause(previous: dict[str, Any], clean: dict[str, Any], payload: dict[str, Any], directory: Path | None = None) -> None:
    """PUT can enrich labels or delete/undo objects, but cannot accept a pause."""
    candidates = previous.get("_pauseCandidates", previous.get("pausedAnomalies", []))
    context = previous.get("_pauseContext") or previous.get("lastPausedContext")
    labels = {item.get("objectId", item.get("object_id")): item for item in payload.get("pausedAnomalies", []) if isinstance(item, dict)} if isinstance(payload.get("pausedAnomalies", []), list) else {}
    display_fields = ("displayName", "title", "summary", "metrics", "baselineFrame", "reviewRange", "reviewNotice", "suggestion", "geometryReferenceFrame", "geometryReferenceSource")
    enriched = [{**item, **{field: labels.get(item.get("objectId", item.get("object_id")), {}).get(field, item.get(field)) for field in display_fields if field in labels.get(item.get("objectId", item.get("object_id")), {})}} for item in candidates]
    clean["_pauseCandidates"] = enriched
    clean["_pauseContext"] = context
    pause_frame = (context or {}).get("frameIndex")
    clean["pausedAnomalies"] = [item for item in enriched if not item.get("resolved") and not is_deleted(clean, pause_frame, item.get("objectId", item.get("object_id")))] if type(pause_frame) is int else []
    if directory is not None and not previous.get("pausedAnomalies") and clean["pausedAnomalies"]:
        # A discarded branch must not be restored merely by undoing a deletion.
        if not any(row["source_frame_index"] == pause_frame for row in read_rows(directory)):
            clean["_pauseCandidates"] = []
            clean["_pauseContext"] = None
            clean["pausedAnomalies"] = []
    clean["lastPausedContext"] = context if clean["pausedAnomalies"] else None


def publish_tracking_pause(directory: Path, media_id: str, pause: dict[str, Any] | None, publication_id: str | None = None) -> None:
    """Persist real task output without advancing the editor's saved revision."""
    from .review_source_lock import source_write
    @source_write
    def publish():
        state = read_state(directory, recover_pause=False)
        if not (directory / FILE_NAME).is_file():
            # Creating pause metadata cannot hide legacy durable manual seeds.
            from .main import _legacy_workspace_state
            state = _legacy_workspace_state(directory, media_id) or state
        notices = []
        if pause:
            titles = {"disappearance": "目标丢失", "overlap": "目标框发生重叠", "size_shrink": "框相对最近人工标注明显缩小", "size_growth": "框相对最近人工标注明显扩大或变形", "shape_change": "框相对最近人工标注明显扩大或变形", "tracking_motion": "目标运动异常"}
            for item in pause.get("reasons", []):
                oid = item.get("object_id", item.get("objectId"))
                if type(oid) is not int or oid <= 0:
                    raise HTTPException(500, "追踪暂停对象身份无效")
                reasons = item.get("reasons", [])
                geometry = item.get("type") in {"size_shrink", "size_growth", "shape_change"} or any(str(r).startswith(("manual_area_ratio=", "manual_width_ratio=", "manual_height_ratio=", "manual_aspect_change=")) for r in reasons)
                notices.append(dict(objectId=oid, displayName=item.get("display_name") or item.get("name") or f"精子 {oid}", title=titles.get(item.get("type"), "追踪异常"), summary="请检查当前目标及暂停前的框是否正确。", metrics=[], suggestion="修正后确认，或回到更早的错误帧重建追踪分支。", acceptsGeometry=geometry, canLearn=any(str(r).startswith("adjacent_center_shift=") for r in reasons), rawReasons=reasons))
        context = {"mediaId": media_id, "frameIndex": pause["frame_index"]} if pause and notices else None
        state.update(_pauseCandidates=notices, _pauseContext=context, pausedAnomalies=notices, lastPausedContext=context)
        if publication_id is not None:
            state["_pausePublicationId"] = publication_id
        temporary = directory / ("." + FILE_NAME + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(directory / FILE_NAME)
            from .tracking_restart import _sync_directory
            _sync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)
    publish()


PAUSE_PENDING_FILE = ".tracking-pause-publication.json"


def prepare_pause_publication(directory: Path, rows: list[dict[str, Any]], pause: dict[str, Any] | None) -> None:
    """Write intent before publishing the matching JSONL; reads finish a crash."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update((json.dumps(row, ensure_ascii=False) + "\n").encode())
    target = directory / PAUSE_PENDING_FILE
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"format": "tracking-pause-publication-v1", "publicationId": uuid.uuid4().hex, "resultSha256": digest.hexdigest(), "pause": pause}, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        from .tracking_restart import _sync_directory
        _sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def recover_pause_publication(directory: Path) -> None:
    if not (directory / PAUSE_PENDING_FILE).is_file():
        return
    from .review_source_lock import source_write
    @source_write
    def recover():
        marker = directory / PAUSE_PENDING_FILE
        if not marker.is_file():
            return
        try:
            intent = json.loads(marker.read_text(encoding="utf-8"))
            if intent.get("format") != "tracking-pause-publication-v1" or not isinstance(intent.get("publicationId"), str) or not intent["publicationId"]:
                raise ValueError("invalid pause publication intent")
            state = read_state(directory, recover_pause=False)
            if state.get("_pausePublicationId") != intent["publicationId"]:
                result = directory / "tracker_results.json"
                digest = hashlib.sha256()
                if result.is_file():
                    with result.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                if result.is_file() and digest.hexdigest() == intent["resultSha256"]:
                    publish_tracking_pause(directory, directory.name, intent["pause"], intent["publicationId"])
                    log.warning("tracking.pause_publication_recovered media=%s", directory.name)
            # Even an already-applied ID must have its rename made durable
            # before cleanup, including a prior directory-sync failure.
            from .tracking_restart import _sync_directory
            _sync_directory(directory)
            try:
                marker.unlink()
                _sync_directory(directory)
            except OSError:
                # Applied publication IDs make cleanup retries harmless after
                # a later normal/corrected feedback or a branch restart.
                log.exception("tracking.pause_cleanup_pending media=%s", directory.name)
        except Exception as exc:
            log.exception("tracking.pause_publication_failed media=%s", directory.name)
            raise HTTPException(503, "追踪暂停状态尚未保存，请保留当前编辑并重试读取") from exc
    recover()
