from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
import mimetypes

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import Response
from pydantic import BaseModel

from .auth import current_user, hash_password, sign_jwt, verify_password
from .config import (
    DATASET_EXPORT_DIR, DB_FILE, DEVICE, DTYPE, HOST, JWT_SECRET, LEGACY_TRACK_DATA_DIR, MAX_VIDEO_BYTES, MODEL_ID, PORT,
    TRACK_DATA_DIR, TRACK_FRAMES,
)
from .db import create_user, delete_annotation, get_user, get_user_by_id, init_db, insert_annotations, list_annotations
from .schemas import (
    AnomalyFrameOut,
    AnomalyScanRequest,
    AnomalyScanResponse,
    AuthRequest,
    ManualAnnotationRequest,
    TrackRequest,
)
from .tracker import (
    OVERLAY_FILE_NAME,
    RESULT_FILE_NAME,
    WORKSPACE_STATE_FILE_NAME,
    _probe_video,
    get_tracker_engine,
    track_video,
    rewind_tracking_results,
    _load_latest_manual_baselines,
)
from .review_routes import register_review_routers
from .review_source_lock import source_write

app = FastAPI(title="SAM3 Annotation Backend", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

register_review_routers(app)

# GPU tracking strictly serialized for 4GB cards; FastAPI remains responsive.
TRACK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sam3-track")
TASKS: dict[str, dict[str, Any]] = {}
TASK_LOCK = threading.Lock()

def tracking_is_busy() -> bool:
    with TASK_LOCK:
        return any(item.get("status") in {"queued", "running"} for item in TASKS.values())


def allocate_media_id(stem: str) -> str:
    """同名视频不覆盖旧目录：首次为 stem，后续依次 stem_001、stem_002...。"""
    candidate = stem
    index = 1
    while media_dir(candidate).exists():
        candidate = f"{stem}_{index:03d}"
        index += 1
    return candidate


class HealthResponse(BaseModel):
    ok: bool
    service: str
    storage: str
    sam3: dict[str, Any]


@app.on_event("startup")
def startup() -> None:
    init_db()
    from .training_export import recover_interrupted
    recover_interrupted()
    TRACK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATASET_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print("✅ FastAPI backend started")
    print(f"   URL        : http://{HOST}:{PORT}")
    print(f"   SQLite     : {DB_FILE}")
    print(f"   SAM3 model : {MODEL_ID}")
    print(f"   SAM3 device: {DEVICE} / {DTYPE}")
    print("   登录、标注、上传、视频播放、SAM3 Tracking 共用一个 FastAPI 进程")
    print("   SAM3 模型不会在启动时加载；第一次 Tracking 时加载并缓存")
    print("=" * 70)


@app.on_event("shutdown")
def shutdown() -> None:
    from .training_export import shutdown as stop_training_exports
    stop_training_exports()
    TRACK_EXECUTOR.shutdown(wait=False, cancel_futures=True)


@app.get("/api/health", response_model=HealthResponse)
def health() -> dict[str, Any]:
    try:
        engine = get_tracker_engine()
        model_loaded = engine.model is not None and engine.processor is not None
    except Exception:
        model_loaded = False
    with TASK_LOCK:
        running = sum(1 for x in TASKS.values() if x["status"] == "running")
        queued = sum(1 for x in TASKS.values() if x["status"] == "queued")
    return {
        "ok": True,
        "service": "sam3-annotation-backend",
        "storage": "sqlite",
        "sam3": {"ok": True, "modelLoaded": model_loaded, "queued": queued, "running": running, "trackingBusy": (queued + running) > 0, "trackFrames": TRACK_FRAMES},
    }


# ---------------------------- auth ----------------------------
@app.post("/api/auth/register", status_code=201)
def register(req: AuthRequest) -> dict[str, Any]:
    name = req.username.strip()
    if not name or not req.password:
        raise HTTPException(400, "账号和密码不能为空")
    if len(req.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if get_user(name):
        raise HTTPException(409, "账号已存在")
    uid = create_user(name, hash_password(req.password))
    return {"token": sign_jwt({"uid": uid, "username": name}), "user": {"id": uid, "username": name}}


@app.post("/api/auth/login")
def login(req: AuthRequest) -> dict[str, Any]:
    user = get_user(req.username.strip())
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "账号或密码错误")
    uid = int(user["id"])
    return {"token": sign_jwt({"uid": uid, "username": user["username"]}), "user": {"id": uid, "username": user["username"]}}


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    row = get_user_by_id(user["uid"])
    if not row:
        raise HTTPException(404, "用户不存在")
    return {"id": int(row["id"]), "username": row["username"]}


# -------------------------- annotation --------------------------
def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and v == v else None


def _px(obj: dict[str, Any], width: float | None, height: float | None) -> dict[str, float | None]:
    out = {"px_x1": None, "px_y1": None, "px_x2": None, "px_y2": None, "px_point_x": None, "px_point_y": None}
    if not width or not height:
        return out
    if isinstance(obj.get("bbox"), dict):
        b = obj["bbox"]
        out.update({
            "px_x1": round(float(b.get("x", 0)) / 100 * width),
            "px_y1": round(float(b.get("y", 0)) / 100 * height),
            "px_x2": round(float(b.get("x", 0) + b.get("width", 0)) / 100 * width),
            "px_y2": round(float(b.get("y", 0) + b.get("height", 0)) / 100 * height),
        })
    if isinstance(obj.get("point"), dict):
        out["px_point_x"] = round(float(obj["point"].get("x", 0)) / 100 * width)
        out["px_point_y"] = round(float(obj["point"].get("y", 0)) / 100 * height)
    return out


@app.post("/api/annotation/annotations/manual", status_code=201)
def save_manual(req: ManualAnnotationRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if tracking_is_busy():
        raise HTTPException(409, "SAM3 Tracking 正在运行，暂时禁止人工标注")
    if not req.mediaId:
        raise HTTPException(400, "缺少 mediaId")
    if not req.objects:
        raise HTTPException(400, "objects 为空")
    width, height = _num(req.mediaWidth), _num(req.mediaHeight)
    raw = req.model_dump_json()
    batch_id = f"batch-{user['uid']}-{uuid.uuid4().hex[:10]}"
    rows = []
    for obj in req.objects:
        # 数据库只记录人工新增/人工修改后的对象；AI Tracking 结果不进入人工结果库。
        if obj.get("source") != "manual":
            continue
        px = _px(obj, width, height)
        bbox = obj.get("bbox") if isinstance(obj.get("bbox"), dict) else None
        point = obj.get("point") if isinstance(obj.get("point"), dict) else None
        # 逐对象取 frameIndex/timestampMs：一次保存可能包含多个视频帧，不能把请求当前帧覆盖所有对象。
        obj_frame_index = int(obj.get("frameIndex", req.frameIndex) or 0)
        obj_timestamp_ms = int(round(float(obj.get("timestampMs", req.timestampMs) or 0)))
        rows.append({
            "user_id": user["uid"], "batch_id": batch_id,
            "object_id": str(obj.get("objectId") if obj.get("objectId") is not None else obj.get("id", "")),
            "media_id": req.mediaId, "media_name": req.mediaName, "media_type": req.mediaType,
            "media_width": int(width) if width else None, "media_height": int(height) if height else None,
            "frame_index": obj_frame_index, "timestamp_ms": obj_timestamp_ms,
            "object_name": obj.get("name"), "source": obj.get("source", "manual"),
            "confidence": _num(obj.get("confidence")), "shape_type": "bbox" if bbox else "point",
            "pct_x": _num((bbox or point or {}).get("x")), "pct_y": _num((bbox or point or {}).get("y")),
            "pct_w": _num((bbox or {}).get("width")), "pct_h": _num((bbox or {}).get("height")),
            **px, "annotation_version": req.annotationVersion, "raw_json": raw,
        })
    if not rows:
        raise HTTPException(400, "没有可保存的人工标注对象（AI Tracking 结果不会写入人工标注数据库）")
    insert_annotations(rows)
    return {"id": f"annotation-{batch_id}", "batchId": batch_id, "ok": True, "count": len(rows)}


@app.get("/api/annotation/projects/{project_id}/results")
def results(project_id: str, mediaId: str | None = Query(default=None), user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    # 标注结果页查看的是整张人工标注记录表，因此这里不按当前用户限制；
    # 结果仍然只允许已登录用户访问，并且严格只返回 source=manual。
    rows = list_annotations(None, mediaId, source="manual")
    return {"items": rows, "total": len(rows)}


@app.get("/api/annotation/media/{media_id}")
def results_by_media(media_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    rows = list_annotations(None, media_id, source="manual")
    return {"items": rows, "total": len(rows)}


@app.delete("/api/annotation/{annotation_id}")
def remove_annotation(annotation_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, bool]:
    if not delete_annotation(annotation_id, user["uid"]):
        raise HTTPException(404, "记录不存在或无权删除")
    return {"ok": True}


# ----------------------------- files -----------------------------
def safe_stem(filename: str) -> str:
    stem = Path(Path(filename).name).stem
    clean = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in stem).strip()
    return clean or f"video-{uuid.uuid4().hex[:8]}"


def media_dir(media_id: str) -> Path:
    """Return the new storage path, or a matching legacy media directory."""
    safe_id = Path(media_id).name
    current = TRACK_DATA_DIR / safe_id
    legacy = LEGACY_TRACK_DATA_DIR / safe_id
    if not current.is_dir() and legacy.is_dir():
        return legacy
    return current


def iter_media_dirs() -> list[Path]:
    """List new storage first, then legacy directories not shadowed by it."""
    result: list[Path] = []
    seen: set[str] = set()
    for root in (TRACK_DATA_DIR, LEGACY_TRACK_DATA_DIR):
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_") or directory.name in seen:
                continue
            seen.add(directory.name)
            result.append(directory)
    return result


def resolve_media_dir(media_id: str, media_name: str | None = None) -> Path | None:
    """
    查找素材的真实 track_data 目录。
    前端可能传的是 local-xxx / server-xxx 这类 id，
    但后端 track_data 目录名可能是 UUID hash。
    优先直接用 media_id 匹配，找不到就用 media_name 反查所有 media.json。
    """
    # 1. 直接匹配（大多数情况）
    direct = media_dir(media_id)
    if direct.is_dir():
        return direct

    # 2. 用 media_name 反查所有 media.json 的 videoName
    if media_name:
        for d in iter_media_dirs():
            mj = d / "media.json"
            if not mj.is_file():
                continue
            try:
                meta = json.loads(mj.read_text(encoding="utf-8"))
                if meta.get("videoName") == media_name or meta.get("mediaId") == media_id:
                    return d
            except Exception:
                continue

    # 3. 兜底：按文件名（不含路径）匹配
    if media_name:
        stem = Path(media_name).stem  # 去掉扩展名
        for d in iter_media_dirs():
            # 目录名本身就是 stem（790663... 这种）
            if d.name == stem:
                return d
            for f in d.iterdir():
                if f.is_file() and f.name == media_name:
                    return d

    return None


def find_video(directory: Path) -> Path | None:
    """Return the original source video without requiring MP4 or conversion."""
    if not directory.is_dir():
        return None
    meta_file = directory / "media.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            candidate = directory / Path(str(meta.get("videoName", ""))).name
            if candidate.is_file() and candidate.name != OVERLAY_FILE_NAME:
                return candidate
        except Exception:
            pass
    ignored_names = {OVERLAY_FILE_NAME, "media.json", RESULT_FILE_NAME}
    ignored_suffixes = {".json", ".jsonl", ".db", ".txt", ".log", ".jpg", ".jpeg", ".png"}
    for x in sorted(directory.iterdir()):
        if not x.is_file() or x.name in ignored_names:
            continue
        if x.name.startswith(".") or x.suffix.lower() in ignored_suffixes:
            continue
        try:
            import cv2
            cap = cv2.VideoCapture(str(x))
            opened = cap.isOpened()
            cap.release()
            if opened:
                return x
        except Exception:
            continue
    return None


def _video_media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed if guessed and guessed.startswith("video/") else "application/octet-stream"


async def save_upload(upload: UploadFile, target: Path) -> int:
    total = 0
    with target.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_VIDEO_BYTES:
                raise HTTPException(413, f"视频超过大小限制 {MAX_VIDEO_BYTES} bytes")
            out.write(chunk)
    return total


@app.post("/api/track/upload", status_code=201)
async def upload_video(file: UploadFile = File(...), user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    filename = Path(file.filename or "video.bin").name
    if not Path(filename).suffix:
        raise HTTPException(400, "视频文件必须包含扩展名")
    stem = safe_stem(filename)
    media_id = allocate_media_id(stem)
    directory = media_dir(media_id)
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / filename
    try:
        size = await save_upload(file, target)
    except Exception:
        target.unlink(missing_ok=True)
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass
        raise
    finally:
        await file.close()
    if size <= 0:
        target.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise HTTPException(400, "视频文件为空")
    # 用 OpenCV 读取源视频真实元信息；不转换、不改写原始文件。
    try:
        video_meta = _probe_video(target)
    except Exception as exc:
        target.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise HTTPException(400, f"无法解码该视频格式：{exc}") from exc
    if (
        int(video_meta.get("frameCount") or 0) <= 0
        or int(video_meta.get("width") or 0) <= 0
        or int(video_meta.get("height") or 0) <= 0
        or float(video_meta.get("fps") or 0) <= 0
    ):
        target.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise HTTPException(400, "上传文件不可读取，或无法获得有效的帧数、尺寸、FPS 元数据")
    duration = (video_meta["frameCount"] / video_meta["fps"]) if video_meta.get("fps") else 0
    meta = {"mediaId": media_id, "videoName": filename, "videoPath": str(target.resolve()), "videoMimeType": _video_media_type(target), "createdBy": user["uid"], **video_meta, "duration": duration}
    (directory / "media.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"mediaId": media_id, "videoName": filename, "videoUrl": f"/api/track/video/{media_id}", "videoMimeType": meta["videoMimeType"], **video_meta, "duration": duration}


@app.post("/api/track/annotations", status_code=201)
@source_write
def save_frame_annotations(req: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if tracking_is_busy():
        raise HTTPException(409, "SAM3 Tracking 正在运行，暂时禁止人工标注")
    frame = int(req.get("frameIndex", -1))
    annotations = req.get("annotations")
    if not req.get("mediaId") or frame < 0 or not isinstance(annotations, list) or not annotations:
        raise HTTPException(400, "mediaId/frameIndex/annotations 无效")

    # Seed identity 必须由前端显式传递并保持稳定；严禁按数组顺序重新编号。
    normalized_annotations: list[dict[str, Any]] = []
    seen_object_ids: set[int] = set()
    for idx, raw in enumerate(annotations):
        if not isinstance(raw, dict):
            raise HTTPException(400, f"annotations[{idx}] 无效")
        object_id_raw = raw.get("object_id", raw.get("objectId"))
        try:
            object_id = int(object_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"annotations[{idx}] 缺少有效 object_id")
        if object_id <= 0:
            raise HTTPException(400, f"annotations[{idx}] object_id 必须大于 0")
        if object_id in seen_object_ids:
            raise HTTPException(400, f"当前帧存在重复 object_id={object_id}")
        seen_object_ids.add(object_id)
        ann = dict(raw)
        ann["object_id"] = object_id
        ann["objectId"] = object_id
        ann["frameIndex"] = int(ann.get("frameIndex", frame))
        if ann["frameIndex"] != frame:
            raise HTTPException(400, f"annotations[{idx}] frameIndex 与当前 seed frame 不一致")
        normalized_annotations.append(ann)
    directory = media_dir(str(req["mediaId"]))
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "media": {"id": str(req["mediaId"]), "name": req.get("mediaName") or f"{req['mediaId']}.mp4", "type": "video", "width": req.get("mediaWidth"), "height": req.get("mediaHeight")},
        "frame": {"frameIndex": frame, "timestampMs": req.get("timestampMs", 0)},
        "coordinateSystem": {"source": "frontend-pixel", "target": "pixel", "bbox": "[x1, y1, x2, y2]"},
        "annotations": normalized_annotations,
    }
    filename = f"annotations_frame_{frame:06d}.json"
    (directory / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"filename": filename, "frameIndex": frame}


def _legacy_workspace_state(directory: Path, media_id: str) -> dict[str, Any] | None:
    """Build initial workspace state from durable manual seed files."""
    meta: dict[str, Any] = {}
    try:
        meta = json.loads((directory / "media.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    width = float(meta.get("width") or 0)
    height = float(meta.get("height") or 0)
    fps = float(meta.get("fps") or 0)
    if width <= 0 or height <= 0:
        return None

    annotations: dict[tuple[int, int], dict[str, Any]] = {}
    baselines: dict[int, dict[str, Any]] = {}
    for path in sorted(directory.glob("annotations_frame_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            frame_index = int(payload.get("frame", {}).get("frameIndex", -1))
            timestamp_ms = int(payload.get("frame", {}).get("timestampMs") or (round(frame_index * 1000 / fps) if fps > 0 else 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if frame_index < 0:
            continue
        for raw in payload.get("annotations", []):
            if not isinstance(raw, dict) or raw.get("source") != "manual":
                continue
            try:
                object_id = int(raw.get("object_id", raw.get("objectId")))
                x1, y1, x2, y2 = [float(value) for value in raw["bbox"]]
            except (KeyError, TypeError, ValueError):
                continue
            if x2 <= x1 or y2 <= y1:
                continue
            name = str(raw.get("name") or f"object-{object_id}")
            annotations[(frame_index, object_id)] = {
                "id": f"manual-{frame_index}-{object_id}",
                "objectId": object_id,
                "name": name,
                "source": "manual",
                "bbox": {
                    "x": x1 / width * 100,
                    "y": y1 / height * 100,
                    "width": (x2 - x1) / width * 100,
                    "height": (y2 - y1) / height * 100,
                },
                "frameIndex": frame_index,
                "timestampMs": timestamp_ms,
            }
            previous = baselines.get(object_id)
            if previous is None or frame_index >= int(previous["frameIndex"]):
                baselines[object_id] = {
                    "objectId": object_id,
                    "name": name,
                    "source": "manual",
                    "frameIndex": frame_index,
                    "bbox": [x1, y1, x2, y2],
                }
    if not annotations:
        return None
    return {
        "exists": True,
        "format": "annotation-workspace-v1",
        "mediaId": media_id,
        "currentFrame": 0,
        "manualAnnotations": list(annotations.values()),
        "manualBaselines": list(baselines.values()),
        "deletedTrackingIds": [],
        "anomalyFrames": [],
        "pausedAnomalies": [],
        "lastPausedContext": None,
        "display": {"brightness": 100, "contrast": 100, "zoom": 1},
        "migratedFrom": "annotations_frame_*.json",
    }


@app.get("/api/track/workspace/{media_id}")
def get_workspace_state(media_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Return durable per-video editor state without duplicating tracker JSONL."""
    directory = media_dir(media_id)
    if not directory.is_dir() or not find_video(directory):
        raise HTTPException(404, "素材不存在")
    path = directory / WORKSPACE_STATE_FILE_NAME
    if not path.is_file():
        return _legacy_workspace_state(directory, media_id) or {"exists": False, "mediaId": media_id}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"{WORKSPACE_STATE_FILE_NAME} 无法读取") from exc
    if not isinstance(payload, dict):
        raise HTTPException(500, f"{WORKSPACE_STATE_FILE_NAME} 格式无效")
    return {**payload, "exists": True, "mediaId": media_id}


@app.put("/api/track/workspace/{media_id}")
@source_write
def save_workspace_state(media_id: str, payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Atomically persist editor-only state beside the source video."""
    directory = media_dir(media_id)
    if not directory.is_dir() or not find_video(directory):
        raise HTTPException(404, "素材不存在")
    manual_annotations = payload.get("manualAnnotations", [])
    manual_baselines = payload.get("manualBaselines", [])
    if not isinstance(manual_annotations, list) or not isinstance(manual_baselines, list):
        raise HTTPException(400, "manualAnnotations/manualBaselines 必须是数组")
    if len(manual_annotations) > 1_000_000 or len(manual_baselines) > 100_000:
        raise HTTPException(413, "工作区状态过大")

    clean = {
        **payload,
        "format": "annotation-workspace-v1",
        "mediaId": media_id,
        "updatedBy": int(user["uid"]),
    }
    encoded = json.dumps(clean, ensure_ascii=False, indent=2)
    if len(encoded.encode("utf-8")) > 64 * 1024 * 1024:
        raise HTTPException(413, "工作区状态超过 64 MiB")

    path = directory / WORKSPACE_STATE_FILE_NAME
    temporary = directory / f".{WORKSPACE_STATE_FILE_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "ok": True,
        "mediaId": media_id,
        "filename": WORKSPACE_STATE_FILE_NAME,
        "manualAnnotationCount": len(manual_annotations),
        "manualBaselineCount": len(manual_baselines),
    }


# ---------------------------- tracking ----------------------------
def _set_task(task_id: str, **patch: Any) -> None:
    with TASK_LOCK:
        TASKS[task_id] = {**TASKS.get(task_id, {}), **patch}


def _effective_track_frames(start_frame: int, total_frames: int, requested_frames: int) -> int:
    """Return the bounded frame count, including the seed frame."""
    remaining = max(0, int(total_frames) - int(start_frame))
    return min(max(1, int(requested_frames)), TRACK_FRAMES, remaining) if remaining else 0


@source_write
def _run_tracking_task(task_id: str, req: TrackRequest, video: Path, seed_file: Path, output_file: Path) -> None:
    _set_task(task_id, status="running", message="SAM3 tracking running")
    try:
        result = track_video(str(video), str(seed_file), str(output_file), max_frames=req.maxFrames, bbox_mode="pixel", start_frame=req.startFrame)
        anomaly_paused = result.get("anomaly_paused")
        if anomaly_paused:
            _set_task(
                task_id,
                status="paused",
                message=f"Anomaly detected at frame {anomaly_paused['frame_index']}",
                paused=True,
                pausedFrame=anomaly_paused["frame_index"],
                pausedObjects=anomaly_paused.get("reasons", []),
                anomalyLevels=anomaly_paused.get("levels", {}),
                processedFrames=result.get("processedFrames", 0),
                lastProcessedFrame=result.get("lastProcessedFrame"),
                reachedVideoEnd=result.get("reachedVideoEnd", False),
            )
        else:
            _set_task(
                task_id,
                status="success",
                message="completed",
                processedFrames=result.get("processedFrames", 0),
                lastProcessedFrame=result.get("lastProcessedFrame"),
                reachedVideoEnd=result.get("reachedVideoEnd", False),
            )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _set_task(task_id, status="failed", message=str(exc))



@app.post("/api/track/rewind")
@source_write
def rewind_tracking(req: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Start a new tracking branch from the current human-corrected frame.

    Only rows strictly after startFrame in the previous SAM3 JSONL branch are deleted;
    the start frame itself remains as the new authoritative seed anchor. This is deliberately separate
    from /track/annotations so that only an explicit AI Tracking click rewinds the
    previous future branch.
    """
    if tracking_is_busy():
        raise HTTPException(409, "已有 SAM3 Tracking 任务正在运行，请等待完成")
    media_id = str(req.get("mediaId") or "").strip()
    try:
        start_frame = int(req.get("startFrame"))
    except (TypeError, ValueError):
        raise HTTPException(400, "startFrame 无效")
    if not media_id or start_frame < 0:
        raise HTTPException(400, "mediaId/startFrame 无效")

    directory = media_dir(media_id)
    video = find_video(directory)
    if not video:
        raise HTTPException(404, "视频文件不存在，请重新上传")
    result_file = directory / RESULT_FILE_NAME
    if result_file.exists():
        try:
            meta = json.loads((directory / "media.json").read_text(encoding="utf-8")) if (directory / "media.json").is_file() else {}
        except Exception:
            meta = {}
        info = rewind_tracking_results(result_file, video, start_frame, meta)
    else:
        info = {
            "cutoffFrame": start_frame,
            "keptRows": 0,
            "removedRows": 0,
            "deletedFutureSeedFiles": 0,
            "overlayRegenerated": False,
        }
    return {"ok": True, "mediaId": media_id, **info}


@app.post("/api/track", status_code=202)
@source_write
def start_tracking(req: TrackRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if tracking_is_busy():
        raise HTTPException(409, "已有 SAM3 Tracking 任务正在运行，请等待完成")
    if not req.annotations:
        raise HTTPException(400, "没有 Tracking seed bbox")

    # 与 /track/annotations 相同的强校验：直接调用 /track 也不能绕过稳定 ID 约束。
    seen_object_ids: set[int] = set()
    normalized_request_annotations: list[dict[str, Any]] = []
    for idx, raw in enumerate(req.annotations):
        if not isinstance(raw, dict):
            raise HTTPException(400, f"annotations[{idx}] 无效")
        raw_id = raw.get("object_id", raw.get("objectId"))
        try:
            object_id = int(raw_id)
        except (TypeError, ValueError):
            raise HTTPException(400, f"annotations[{idx}] 缺少有效 object_id")
        if object_id <= 0:
            raise HTTPException(400, f"annotations[{idx}] object_id 必须大于 0")
        if object_id in seen_object_ids:
            raise HTTPException(400, f"当前 Tracking seed 存在重复 object_id={object_id}")
        seen_object_ids.add(object_id)
        ann = dict(raw)
        ann["object_id"] = object_id
        ann["objectId"] = object_id
        ann["frameIndex"] = int(ann.get("frameIndex", req.startFrame))
        if ann["frameIndex"] != req.startFrame:
            raise HTTPException(400, f"annotations[{idx}] frameIndex 与 startFrame 不一致")
        normalized_request_annotations.append(ann)

    req.annotations = normalized_request_annotations
    directory = media_dir(req.mediaId)
    video = find_video(directory)
    if not video:
        raise HTTPException(404, "视频文件不存在，请重新上传")
    video_frame_count = int(_probe_video(video).get("frameCount") or 0)
    effective_frames = _effective_track_frames(req.startFrame, video_frame_count, req.maxFrames)
    if effective_frames < 1:
        raise HTTPException(400, "startFrame 已超出视频末尾")
    # maxFrames includes the seed frame.  Environment configuration is always
    # the upper bound, regardless of what a client submits.
    req.maxFrames = effective_frames
    seed_file = directory / f"seed_frame_{req.startFrame:06d}.json"
    output_file = directory / RESULT_FILE_NAME
    payload = {
        "media": {"id": req.mediaId, "name": req.mediaName or video.name, "type": "video", "width": req.mediaWidth, "height": req.mediaHeight},
        "frame": {"frameIndex": req.startFrame, "timestampMs": 0},
        "coordinateSystem": {"source": "frontend-pixel", "target": "pixel", "bbox": "[x1, y1, x2, y2]"},
        "annotations": req.annotations,
    }
    seed_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    task_id = uuid.uuid4().hex
    _set_task(task_id, taskId=task_id, status="queued", message="queued", mediaId=req.mediaId, startFrame=req.startFrame, maxFrames=req.maxFrames, userId=user["uid"])
    TRACK_EXECUTOR.submit(_run_tracking_task, task_id, req, video, seed_file, output_file)
    return {"taskId": task_id, "status": "queued", "maxFrames": req.maxFrames, "trackFrames": req.maxFrames}


@app.get("/api/track/status/{task_id}")
def tracking_status(task_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with TASK_LOCK:
        item = TASKS.get(task_id)
    if not item:
        raise HTTPException(404, "task not found")
    return {k: v for k, v in item.items() if k != "userId"}


@app.post("/api/anomaly/scan", response_model=AnomalyScanResponse)
def scan_anomalies(req: AnomalyScanRequest, user: dict[str, Any] = Depends(current_user)):
    """对已有 tracker_results.json 做事后异常扫描"""
    from .services.anomaly_detector import AnomalyDetector, AnomalyConfig

    directory = resolve_media_dir(req.mediaId, req.mediaName)
    if not directory:
        raise HTTPException(404, f"media {req.mediaId} not found")

    video = find_video(directory)
    if not video:
        raise HTTPException(404, "video file not found")

    cap_meta = _probe_video(video)
    width = cap_meta["width"]
    height = cap_meta["height"]
    fps = cap_meta["fps"]

    tracker_file = directory / RESULT_FILE_NAME
    rows = _read_tracker_jsonl(tracker_file) if tracker_file.is_file() else []

    latest_frame = max(
        (int(row.get("source_frame_index", row.get("frame_index", 0))) for row in rows),
        default=0,
    )
    manual_baselines = _load_latest_manual_baselines(directory / "__anomaly_scan__.json", latest_frame)
    detector = AnomalyDetector(
        config=AnomalyConfig(),
        fps=fps,
        frame_width=width,
        frame_height=height,
        manual_baselines=manual_baselines,
    )
    all_frames: list[AnomalyFrameOut] = []
    summary: dict[str, int] = {"anomaly": 0, "warning": 0, "disappeared": 0}
    pause_at: int | None = None

    for row in rows:
        fi = int(row.get("frame_index", 0))
        frame_objs: dict[int, list[float]] = {}
        for obj in row.get("objects", []):
            frame_objs[int(obj["object_id"])] = list(obj["bbox"])
        report = detector.push(fi, frame_objs)

        for af in report.frames:
            all_frames.append(AnomalyFrameOut(
                frame_index=af.frame_index,
                object_id=af.object_id,
                level=af.level.value,
                reasons=af.reasons,
                details=af.details,
            ))
            if af.level.value in summary:
                summary[af.level.value] += 1

        if report.should_pause and pause_at is None:
            pause_at = fi

    return AnomalyScanResponse(
        mediaId=req.mediaId,
        totalFrames=len(rows),
        anomalyFrames=all_frames,
        summary=summary,
        shouldPauseAt=pause_at,
    )


def _read_tracker_jsonl(file: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not file.is_file():
        return rows
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _tracker_rows_to_frames(rows: list[dict[str, Any]], fps: float = 0.0) -> list[dict[str, Any]]:
    """Convert raw-frame JSONL rows directly to browser-ready frame results.

    Raw-frame mode uses the same frame index as the source video. No interpolation
    or sampled-frame conversion is performed.
    """
    frames: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r.get("frame_index", r.get("source_frame_index", 0)))):
        frame_index = int(row.get("frame_index", row.get("source_frame_index", 0)))
        annotations = []
        for obj in row.get("objects", []):
            bbox = obj.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            object_id = int(obj.get("object_id", obj.get("sam3_object_id", 0)))
            annotations.append({
                "id": f"ai-{frame_index}-{object_id}",
                "objectId": object_id,
                "name": obj.get("name") or f"object-{object_id}",
                "source": "ai",
                "bbox": [float(v) for v in bbox],
                "frameIndex": frame_index,
                "timestampMs": int(round(frame_index * 1000.0 / fps)) if fps > 0 else 0,
                "confidence": obj.get("score"),
                "score": obj.get("score"),
                "sam3ObjectId": obj.get("sam3_object_id", object_id),
                "maskArea": obj.get("mask_area"),
                "anomaly": obj.get("anomaly"),
                "anomaly_level": obj.get("anomaly_level"),
                "anomaly_reasons": obj.get("anomaly_reasons"),
                "anomaly_details": obj.get("anomaly_details"),
            })
        frames.append({
            "frameIndex": frame_index,
            "timestampMs": int(round(frame_index * 1000.0 / fps)) if fps > 0 else 0,
            "annotations": annotations,
        })
    return frames


@app.get("/api/track/result/{media_id}")
def tracking_result(media_id: str, frameIndex: int | None = Query(default=None), user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    directory = media_dir(media_id)
    file = directory / RESULT_FILE_NAME
    if not file.is_file():
        raise HTTPException(404, f"{RESULT_FILE_NAME} 尚未生成")
    rows = _read_tracker_jsonl(file)
    meta = {}
    meta_file = directory / "media.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    source_fps = float(meta.get("fps") or 0.0)
    frames = _tracker_rows_to_frames(rows, source_fps)
    if frameIndex is None:
        return {"format": "sam3-tracking-results-jsonl", "frames": frames, "count": len(frames)}
    return next(
        (f for f in frames if int(f.get("frameIndex", -1)) == frameIndex),
        {"frameIndex": frameIndex, "timestampMs": 0, "annotations": []},
    )


@app.get("/api/track/result-file/{media_id}")
def tracking_result_file(media_id: str, user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    file = media_dir(media_id) / RESULT_FILE_NAME
    if not file.is_file():
        raise HTTPException(404, f"{RESULT_FILE_NAME} 尚未生成")
    return FileResponse(file, media_type="application/x-ndjson", filename=RESULT_FILE_NAME, content_disposition_type="inline")


@app.get("/api/track/media")
def list_media(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """列出后端已有的所有视频素材（刷新页面后可恢复素材列表）。"""
    items: list[dict[str, Any]] = []
    name_count: dict[str, int] = {}
    for entry in iter_media_dirs():
        video_file = find_video(entry)
        if not video_file:
            continue
        meta: dict[str, Any] = {}
        meta_file = entry / "media.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        has_result = (entry / RESULT_FILE_NAME).is_file()
        has_workspace = (entry / WORKSPACE_STATE_FILE_NAME).is_file()
        base_name = meta.get("videoName") or video_file.name
        name_count[base_name] = name_count.get(base_name, 0) + 1
        count = name_count[base_name]
        display_name = base_name if count == 1 else f"{base_name} ({count})"
        items.append({
            "mediaId": entry.name,
            # Stable storage identity and the actual source filename are kept
            # separate from videoName, whose duplicate suffix is UI-only.
            "directoryName": entry.name,
            "sourceVideoName": base_name,
            "videoName": display_name,
            "videoUrl": f"/api/track/video/{entry.name}",
            "videoMimeType": meta.get("videoMimeType") or _video_media_type(video_file),
            "hasTrackingResult": has_result,
            "hasWorkspaceState": has_workspace,
            "fps": meta.get("fps") or 0,
            "width": meta.get("width") or 0,
            "height": meta.get("height") or 0,
            "frameCount": meta.get("frameCount") or 0,
            "duration": meta.get("duration") or ((meta.get("frameCount") or 0) / (meta.get("fps") or 1) if meta.get("fps") else 0),
        })
    return {"items": items}


@app.delete("/api/track/media/{media_id}")
@source_write
def delete_media(media_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """删除一个视频素材及其目录下的所有文件。"""
    if tracking_is_busy():
        raise HTTPException(409, "SAM3 Tracking 正在运行，无法删除素材")
    directory = media_dir(media_id)
    if not directory.is_dir():
        raise HTTPException(404, "素材不存在")
    from .db import connect
    with connect() as conn:
        referenced = conn.execute('SELECT 1 FROM annotation_baselines a JOIN media_revisions m ON m.id=a.media_revision_id WHERE m.media_id=? LIMIT 1',(media_id,)).fetchone()
    if referenced:
        raise HTTPException(409, "视频已被审查基准引用，不能删除原始视频")
    import shutil
    shutil.rmtree(directory, ignore_errors=True)
    return {"deleted": True, "mediaId": media_id}


@app.get("/api/track/video/{media_id}")
def video(media_id: str) -> FileResponse:
    file = find_video(media_dir(media_id))
    if not file:
        raise HTTPException(404, "视频不存在")
    return FileResponse(file, media_type=_video_media_type(file), filename=file.name, content_disposition_type="inline")



@app.get("/api/track/frame/{media_id}/{frame_index}")
def video_frame(media_id: str, frame_index: int, user: dict[str, Any] = Depends(current_user)) -> Response:
    """返回原始视频的精确第 frame_index 帧。\n\n    精确逐帧标注模式不再依赖浏览器 currentTime/seek 的实际呈现帧；\n    后端直接从用户上传的原始源视频解码指定帧，并以 JPEG 返回给前端。\n    """
    if frame_index < 0:
        raise HTTPException(400, "frame_index 不能小于 0")

    directory = media_dir(media_id)
    file = find_video(directory)
    if not file:
        raise HTTPException(404, "视频不存在")

    # 小型磁盘缓存：同一帧反复切换时无需重复解码。
    cache_dir = directory / ".frame_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"frame_{frame_index:08d}.jpg"
    if cache_file.is_file() and cache_file.stat().st_size > 0:
        return FileResponse(cache_file, media_type="image/jpeg", filename=cache_file.name, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    try:
        import cv2
        cap = cv2.VideoCapture(str(file))
        if not cap.isOpened():
            raise HTTPException(500, "无法打开源视频")

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total and frame_index >= total:
            cap.release()
            raise HTTPException(404, f"视频只有 {total} 帧，无法访问第 {frame_index} 帧")

        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            raise HTTPException(404, f"无法解码第 {frame_index} 帧")

        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise HTTPException(500, f"第 {frame_index} 帧 JPEG 编码失败")
        cache_file.write_bytes(encoded.tobytes())
        return Response(content=encoded.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"读取第 {frame_index} 帧失败：{exc}") from exc


@app.get("/api/track/overlay/{media_id}")
def tracking_overlay(media_id: str, user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    file = media_dir(media_id) / OVERLAY_FILE_NAME
    if not file.is_file():
        raise HTTPException(404, f"{OVERLAY_FILE_NAME} 尚未生成")
    return FileResponse(file, media_type="video/mp4", filename=OVERLAY_FILE_NAME, content_disposition_type="inline")


@app.get("/api/track/sam3/health")
def sam3_health(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    engine = get_tracker_engine()
    return {"ok": True, "modelLoaded": engine.model is not None and engine.processor is not None, "model": engine.model_id, "device": engine.device, "dtype": str(engine.dtype)}


# ---------------------------- dataset export ----------------------------

def _dataset_class_name(value: Any) -> str:
    """Map per-object display names to one shared training category.

    UI names include stable tracking instance IDs (for example ``sperm 1``).
    Those IDs distinguish objects during annotation but must not become COCO or
    YOLO categories, so ``sperm1``, ``sperm 2`` and ``sperm_3`` all map to
    ``sperm``.
    """
    name = str(value or "object").strip()
    normalized = re.sub(r"(?:\s*[-_#]?\s*\d+|\s*[（(]\s*\d+\s*[）)])$", "", name).strip()
    return normalized or name


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
