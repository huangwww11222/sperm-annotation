"""Disposable source videos and durable paused results for annotation controls."""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from app import db
from app.auth import hash_password, sign_jwt
from app.config import DATA_DIR, DB_FILE, STORAGE_ROOT, TRACK_DATA_DIR


for path in (DATA_DIR, DB_FILE, STORAGE_ROOT):
    assert "/work/e2e-annotation-controls" in str(path) or "/work/e2e-confirm-ux" in str(path), path
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
db.init_db()
user = db.get_user("annotation-controls-tester")
uid = int(user["id"]) if user else db.create_user("annotation-controls-tester", hash_password("annotation-controls-fixture"))
media_ids = ["controls-delete", "controls-feedback", "controls-feedback-failure", "controls-feedback-once", "controls-feedback-corrected"]
for media_id in media_ids:
    directory = TRACK_DATA_DIR / media_id
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / f"{media_id}.avi"
    width, height, count, fps = 480, 270, 24, 30
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height))
    assert writer.isOpened(), "MJPG is required for the disposable browser fixture"
    rows = []
    paused = "feedback" in media_id
    for frame in range(count):
        image = np.full((height, width, 3), 55, np.uint8)
        x = 65 + min(frame, 3) * 45 if paused else 65 + frame * 2
        objects = [
            {"object_id": 7, "name": "示例对象", "bbox": [x, 80, x + 40, 92], "score": 0.96, "source": "manual_sam3_tracker"},
            {"object_id": 12, "name": "示例对象", "bbox": [260 + frame, 170, 300 + frame, 182], "score": 0.95, "source": "manual_sam3_tracker"},
        ]
        for obj in objects:
            x1, y1, x2, y2 = obj["bbox"]
            cv2.ellipse(image, ((x1 + x2) // 2, (y1 + y2) // 2), (16, 5), 0, 0, 360, (220, 220, 220), -1)
        cv2.putText(image, f"Frame {frame + 1}", (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 1)
        writer.write(image)
        row = {"frame_index": frame, "source_frame_index": frame, "objects": objects}
        if paused and frame == 3:
            details = {"current_bbox": objects[0]["bbox"], "manual_baseline_bbox": [65, 80, 105, 92], "manual_baseline_frame": 0, "motion_reference_bbox": [155, 80, 195, 92], "motion_reference_frame": 2, "center_shift": 45, "motion_normalized": 45 / (40 ** 2 + 12 ** 2) ** 0.5, "motion_pause_threshold": 1, "motion_streak": 3, "feedback_sample_count": 0, "frame_gap": 1, "source_fps": 30, "review_start_frame": 0, "review_end_frame": 2}
            reasons = ["adjacent_center_shift=45.0px normalized=1.078 sustained_motion HARD"]
            objects[0].update(anomaly_level="anomaly", anomaly_reasons=reasons, anomaly_details=details)
            row["anomalies"] = [{"object_id": 7, "level": "anomaly", "reasons": reasons, "details": details}]
        rows.append(row)
    writer.release()
    (directory / "media.json").write_text(json.dumps({"videoName": video.name, "width": width, "height": height, "fps": fps, "frameCount": count}), encoding="utf-8")
    (directory / "tracker_results.json").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    manual = {"id": "manual-0-7", "objectId": 7, "frameIndex": 0, "name": "示例对象", "source": "manual", "type": "bbox", "bbox": {"x": 65 / width * 100, "y": 80 / height * 100, "width": 40 / width * 100, "height": 12 / height * 100}}
    workspace = {"format": "annotation-workspace-v1", "mediaId": media_id, "updatedBy": uid, "revision": 1, "currentFrame": 3 if paused else 0, "manualAnnotations": [manual], "manualBaselines": [{"objectId": 7, "frameIndex": 0, "name": "示例对象", "source": "manual", "bbox": [65, 80, 105, 92]}], "deletedTrackingIds": [], "deletedObjectIds": [], "deletedFrameObjects": [], "normalMotionSamples": [], "trackingFeedbackEvents": [], "anomalyFrames": [], "pausedAnomalies": [], "lastPausedContext": None, "display": {"brightness": 100, "contrast": 100, "zoom": 1}, "editor": {"activeTool": "select", "selectedObjectId": "ai-3-7" if paused else "manual-0-7", "objectNameInput": "示例对象"}}
    if paused:
        workspace.update(anomalyFrames=[{"frame_index": 3, "level": "anomaly", "reasons": ["运动持续超范围"]}], pausedAnomalies=[{"objectId": 7, "displayName": "示例对象", "title": "追踪运动异常", "summary": "连续运动超出初始范围，请人工核对。", "metrics": ["中心偏移 45.00 px"], "baselineFrame": 0, "reviewRange": "第 1～3 帧", "suggestion": "核对对象身份与位置后明确确认。", "canLearn": True, "resolved": False}], lastPausedContext={"mediaId": media_id, "frameIndex": 3})
    (directory / "workspace_state.json").write_text(json.dumps(workspace, ensure_ascii=False), encoding="utf-8")
    # Rerunning this dedicated fixture resets only its own old seed files.
    for path in directory.glob("annotations_frame_*.json"):
        path.unlink()
    for path in directory.glob("seed_frame_*.json"):
        path.unlink()

Path("work/annotation-controls-browser-fixture.json").write_text(json.dumps({"token": sign_jwt({"uid": uid}), "userId": uid, "mediaIds": media_ids, "frameCount": 24, "pausedFrame": 3}), encoding="utf-8")
print("Prepared 5 isolated annotation control videos; credentials saved only in work fixture.")
