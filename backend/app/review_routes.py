"""FastAPI router for review / confirmation / final-export workflow.

Mounted by main.py under ``/api/review`` and ``/api/confirmation``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import current_user
from .config import DATASET_EXPORT_DIR
from .db import connect
from .review_schema import apply_review_schema
from .review_repository import (
    _camelize,
    claim_review_session,
    compute_file_sha256,
    compute_snapshot_hash,
    create_review_session,
    create_review_issue,
    finalize_confirmation,
    freeze_baseline,
    freeze_review_version,
    get_confirmation_session,
    get_final_version,
    get_media_revision,
    get_review_change,
    get_review_frame,
    get_review_session,
    get_baseline,
    list_baselines,
    list_confirmation_sessions,
    list_final_versions,
    list_review_changes,
    list_review_sessions,
    load_cursor,
    reopen_frame,
    save_cursor,
    save_decision,
    save_frame_draft,
    submit_frame,
    upsert_media_revision,
)

review_router = APIRouter(prefix="/api/review", tags=["review"])
confirm_router = APIRouter(prefix="/api/confirmation", tags=["confirmation"])
final_router = APIRouter(prefix="/api/final-versions", tags=["final-versions"])
dataset_router = APIRouter(prefix="/api/datasets", tags=["datasets"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class FrameObjectIn(BaseModel):
    objectId: int
    bbox: list[int] = Field(min_length=4, max_length=4)
    classKey: str = "sperm"


class FramePatchItem(BaseModel):
    objectId: int
    bbox: list[int] = Field(min_length=4, max_length=4)


class FrameIn(BaseModel):
    frameIndex: int
    coverage: str = Field(default="objects")  # objects | empty
    objects: list[FrameObjectIn] = Field(default_factory=list)


class FreezeBaselineRequest(BaseModel):
    mediaId: str
    frames: list[FrameIn]


class CreateReviewRequest(BaseModel):
    baselineId: str


class SaveDraftRequest(BaseModel):
    patch: list[FramePatchItem] = Field(default_factory=list)
    expectedRevision: int | None = None


class SubmitFrameRequest(BaseModel):
    patch: list[FramePatchItem] = Field(default_factory=list)


class SaveCursorRequest(BaseModel):
    stage: str   # "review" | "confirmation"
    sessionId: str
    frameIndex: int | None = None
    changeId: str | None = None


class SaveDecisionRequest(BaseModel):
    choice: str          # "A" | "B"
    note: str | None = None
    expectedRevision: int | None = None


class FinalizeRequest(BaseModel):
    expectedRevision: int | None = None


class CreateIssueRequest(BaseModel):
    sessionId: str | None = None
    confirmationId: str | None = None
    frameIndex: int | None = None
    annotationId: str | None = None
    issueType: str
    description: str


# ---------------------------------------------------------------------------
# Review endpoints
# ---------------------------------------------------------------------------

@review_router.get("/media/{mediaId}/baselines")
def list_baselines_for_media(mediaId: str):
    """Return every frozen AnnotationBaseline for a media."""
    items = list_baselines(media_id=mediaId)
    return {"items": items}


@review_router.get("/baselines/{baselineId}")
def get_baseline_detail(baselineId: str):
    """Return a single baseline with all its frame objects (A version)."""
    bl = get_baseline(baselineId)
    if not bl:
        raise HTTPException(404, "baseline not found")
    return _camelize(bl)


@review_router.post("/baselines/freeze")
def post_freeze_baseline(
    req: FreezeBaselineRequest,
    user: dict[str, Any] = Depends(current_user),
):
    """Freeze original annotations → version A (AnnotationBaseline)."""
    media_id = req.mediaId

    # Need a media_revision — try existing; otherwise probe from existing track_data
    rev = get_media_revision(media_id)
    if not rev:
        # Try to find existing video to probe
        candidate = _probe_existing_video(media_id)
        if candidate:
            sha = compute_file_sha256(candidate["path"])
            rev_id = upsert_media_revision(
                media_id, sha, candidate["size"],
                candidate["width"], candidate["height"],
                candidate["fps"], candidate["frame_count"],
            )
            rev = get_media_revision(media_id)
        if not rev:
            raise HTTPException(400, f"no media record for {media_id}; upload video first")

    frames_dicts = [fr.model_dump() for fr in req.frames]
    bl = freeze_baseline(
        media_revision_id=rev["id"],
        media_id=media_id,
        submitted_by=user["uid"],
        frames=frames_dicts,
    )
    return {"baseline": bl}


@review_router.post("/sessions")
def post_review_session(
    req: CreateReviewRequest,
    user: dict[str, Any] = Depends(current_user),
):
    try:
        sess = create_review_session(req.baselineId)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"session": sess}


@review_router.get("/sessions")
def get_review_sessions(
    state: str | None = None,
    mediaId: str | None = None,
):
    return {"items": list_review_sessions(state=state, media_id=mediaId)}


@review_router.get("/sessions/{sessionId}")
def get_review_session_detail(sessionId: str):
    sess = get_review_session(sessionId)
    if not sess:
        raise HTTPException(404, "session not found")
    # Attach cursor if present
    return sess


@review_router.post("/sessions/{sessionId}/claim")
def claim_session(
    sessionId: str,
    user: dict[str, Any] = Depends(current_user),
):
    try:
        sess = claim_review_session(sessionId, user["uid"])
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"session": sess}


@review_router.get("/sessions/{sessionId}/frames/{frameIndex}")
def get_frame(sessionId: str, frameIndex: int):
    fr = get_review_frame(sessionId, frameIndex)
    if not fr:
        raise HTTPException(404, "frame not found")
    return fr


@review_router.put("/sessions/{sessionId}/frames/{frameIndex}/draft")
def put_frame_draft(
    sessionId: str,
    frameIndex: int,
    req: SaveDraftRequest,
    user: dict[str, Any] = Depends(current_user),
):
    try:
        return save_frame_draft(
            sessionId, frameIndex,
            [p.model_dump() for p in req.patch],
            req.expectedRevision,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))


@review_router.post("/sessions/{sessionId}/frames/{frameIndex}/submit")
def post_frame_submit(
    sessionId: str,
    frameIndex: int,
    req: SubmitFrameRequest,
    user: dict[str, Any] = Depends(current_user),
):
    try:
        res = submit_frame(
            sessionId, frameIndex, user["uid"],
            [p.model_dump() for p in req.patch],
        )
        sess = get_review_session(sessionId)
        # Auto-freeze when all frames are submitted (for convenience)
        if sess and sess["submittedFrames"] == sess["frameCount"]:
            frozen = _auto_freeze_review(sessionId, user["uid"])
            res["autoFrozen"] = frozen
        return res
    except ValueError as e:
        raise HTTPException(409, str(e))


@review_router.post("/sessions/{sessionId}/frames/{frameIndex}/reopen")
def post_frame_reopen(sessionId: str, frameIndex: int):
    try:
        return reopen_frame(sessionId, frameIndex)
    except ValueError as e:
        raise HTTPException(409, str(e))


@review_router.post("/sessions/{sessionId}/freeze")
def post_freeze_session(
    sessionId: str,
    user: dict[str, Any] = Depends(current_user),
):
    try:
        return freeze_review_version(sessionId, user["uid"])
    except ValueError as e:
        raise HTTPException(409, str(e))


@review_router.put("/cursors", response_model=dict)
def put_cursor(
    req: SaveCursorRequest,
    user: dict[str, Any] = Depends(current_user),
):
    save_cursor(user["uid"], req.stage, req.sessionId, req.frameIndex, req.changeId)
    curs = load_cursor(user["uid"], req.stage, req.sessionId)
    return {"cursor": curs}


@review_router.post("/issues")
def post_create_issue(
    req: CreateIssueRequest,
    user: dict[str, Any] = Depends(current_user),
):
    if not req.sessionId and not req.confirmationId:
        raise HTTPException(400, "need either sessionId or confirmationId")
    issue_id = create_review_issue(
        req.sessionId, req.confirmationId, user["uid"],
        req.issueType, req.description, req.frameIndex, req.annotationId,
    )
    return {"issueId": issue_id}


# ---------------------------------------------------------------------------
# Confirmation endpoints
# ---------------------------------------------------------------------------

@confirm_router.get("/sessions")
def get_confirmation_sessions(
    state: str | None = None,
    mediaId: str | None = None,
):
    return {"items": list_confirmation_sessions(state=state, media_id=mediaId)}


@confirm_router.get("/sessions/{confirmationId}")
def get_confirmation_detail(confirmationId: str):
    sess = get_confirmation_session(confirmationId)
    if not sess:
        raise HTTPException(404, "session not found")
    return sess


@confirm_router.get("/sessions/{confirmationId}/changes")
def get_changes(
    confirmationId: str,
    pending: bool = Query(default=False),
):
    return {"items": list_review_changes(confirmationId, only_pending=pending)}


@confirm_router.get("/sessions/{confirmationId}/changes/{changeId}")
def get_change_detail(confirmationId: str, changeId: str):
    ch = get_review_change(changeId)
    if not ch:
        raise HTTPException(404, "change not found")
    if ch["confirmationId"] != confirmationId:
        raise HTTPException(404, "change does not belong to this session")
    return ch


@confirm_router.put("/sessions/{confirmationId}/changes/{changeId}/decision")
def put_decision(
    confirmationId: str,
    changeId: str,
    req: SaveDecisionRequest,
    user: dict[str, Any] = Depends(current_user),
):
    try:
        return save_decision(
            confirmationId, changeId, req.choice,
            user["uid"], req.note, req.expectedRevision,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))


@confirm_router.post("/sessions/{confirmationId}/finalize")
def post_finalize(
    confirmationId: str,
    req: FinalizeRequest,
    user: dict[str, Any] = Depends(current_user),
):
    try:
        return finalize_confirmation(confirmationId, user["uid"])
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------------------
# FinalVersion + COCO / YOLO export
# ---------------------------------------------------------------------------

@final_router.get("/")
def list_final_versions():
    return {"items": list_final_versions()}


@final_router.get("/{finalVersionId}")
def get_final_version_detail(finalVersionId: str):
    fv = get_final_version(finalVersionId)
    if not fv:
        raise HTTPException(404, "final version not found")
    return fv


@dataset_router.post("/exports")
def post_export(
    body: dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(current_user),
):
    """Generate COCO + YOLO export for one or more final versions."""
    ids = body.get("finalVersionIds") or body.get("final_version_ids") or []
    if not ids:
        raise HTTPException(400, "finalVersionIds required")
    fmt = (body.get("format") or "both").lower()
    fv = get_final_version(ids[0])
    if not fv:
        raise HTTPException(404, "final version not found")

    export_id = f"exp_{uuid.uuid4().hex[:8]}"
    export_dir = DATASET_EXPORT_DIR / export_id
    export_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "exportId": export_id,
        "createdAt": _utc_now(),
        "createdBy": user["uid"],
        "format": fmt,
        "finalVersions": [fv["id"]],
        "mediaRevision": None,
        "classKey": "sperm",
        "classId": {"coco": 1, "yolo": 0},
        "samples": 0,
        "annotations": 0,
    }

    # Collect frames and annotations
    coco_images: list[dict[str, Any]] = []
    coco_annotations: list[dict[str, Any]] = []
    categories = [{"id": 1, "name": "sperm", "supercategory": "cell"}]
    img_id = 0
    ann_id = 0

    # Determine media revision from baseline
    bl = get_baseline(fv["baseline_id"])
    media_revision_id = None
    if bl:
        media_revision_id = bl["media_revision_id"]
    manifest["mediaRevision"] = media_revision_id

    yolo_labels_dir = export_dir / "labels"
    yolo_images_dir = export_dir / "images"
    yolo_labels_dir.mkdir(exist_ok=True)
    yolo_images_dir.mkdir(exist_ok=True)

    for fr in fv["frames"]:
        frame_index = fr["frameIndex"]
        img_id += 1
        img_info = {
            "id": img_id,
            "file_name": f"frame_{frame_index:06d}.png",
            "width": 640,   # filled from media when available
            "height": 432,
            "mediaRevisionId": media_revision_id,
            "frameIndex": frame_index,
        }
        coco_images.append(img_info)

        # YOLO per frame
        yolo_lines: list[str] = []
        for obj in fr["objects"]:
            ann_id += 1
            x1, y1, x2, y2 = obj["bbox"]
            w = x2 - x1
            h = y2 - y1
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            area = w * h
            coco_annotations.append({
                "id": ann_id,
                "image_id": img_id,
                "category_id": 1,
                "bbox": [x1, y1, w, h],
                "area": area,
                "iscrowd": 0,
                "annotationId": obj["annotationId"],
                "resolution": obj["resolution"],
                "changeId": obj.get("changeId"),
            })
            if fmt in ("yolo", "both"):
                # YOLO: class cx cy w h (normalized)
                dw = img_info["width"] or 640
                dh_n = img_info["height"] or 432
                yolo_lines.append(
                    f"0 {cx/dw:.6f} {cy/dh_n:.6f} {w/dw:.6f} {h/dh_n:.6f}"
                )
            manifest["annotations"] += 1

        if fmt in ("yolo", "both") and yolo_lines:
            (yolo_labels_dir / f"frame_{frame_index:06d}.txt").write_text("\n".join(yolo_lines) + "\n")
            # Copy placeholder image path (actual PNGs should be extracted by the frame endpoint)

        manifest["samples"] += 1

    if fmt in ("coco", "both"):
        coco = {
            "info": {
                "description": f"Sperm detection dataset — export {export_id}",
                "version": "1.0",
                "created": _utc_now(),
                "finalVersionId": fv["id"],
            },
            "images": coco_images,
            "annotations": coco_annotations,
            "categories": categories,
        }
        (export_dir / "instances_default.json").write_text(
            json.dumps(coco, indent=2, ensure_ascii=False)
        )

    if fmt in ("yolo", "both"):
        (export_dir / "dataset.yaml").write_text(
            f"path: .\ntrain: images\nval: images\nnc: 1\nnames: ['sperm']\n"
        )

    (export_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False)
    )

    # ZIP
    zip_path = DATASET_EXPORT_DIR / f"{export_id}.zip"
    _zip_dir(export_dir, zip_path)

    return {
        "exportId": export_id,
        "status": "succeeded",
        "zipPath": str(zip_path),
        "downloadUrl": f"/api/datasets/exports/{export_id}/download",
        "manifest": manifest,
    }


@dataset_router.get("/exports/{exportId}/download")
def get_export_download(exportId: str):
    zip_path = DATASET_EXPORT_DIR / f"{exportId}.zip"
    if not zip_path.exists():
        raise HTTPException(404, "export not found")
    from fastapi.responses import FileResponse
    return FileResponse(zip_path, media_type="application/zip", filename=f"{exportId}.zip")


# ---------------------------------------------------------------------------
# Helpers (module scope so main.py imports work)
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _probe_existing_video(media_id: str) -> dict[str, Any] | None:
    """Best-effort probe: look up video file by media_id in track_data dir."""
    try:
        # Import late to avoid circular import at module load
        from .config import MEDIA_STORAGE_DIR, LEGACY_TRACK_DATA_DIR
        base_dirs = [MEDIA_STORAGE_DIR, LEGACY_TRACK_DATA_DIR]
        for base in base_dirs:
            candidate_dir = Path(base) / media_id
            if not candidate_dir.is_dir():
                continue
            mp4s = list(candidate_dir.glob("*.mp4")) + list(candidate_dir.glob("*.mov")) + list(candidate_dir.glob("*.avi"))
            if not mp4s:
                continue
            path = mp4s[0]
            # Read media.json if present
            meta: dict[str, Any] = {}
            meta_file = candidate_dir / "media.json"
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            import cv2  # type: ignore
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                continue
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
            cap.release()
            return {
                "path": str(path),
                "size": path.stat().st_size,
                "width": w, "height": h,
                "fps": fps, "frame_count": fc,
            }
    except Exception:
        pass
    return None


def _auto_freeze_review(session_id: str, user_id: int) -> dict[str, Any]:
    """All frames submitted → freeze B and create ConfirmationSession."""
    try:
        return freeze_review_version(session_id, user_id)
    except Exception as e:
        print(f"[review] auto-freeze failed: {e}")
        return {"error": str(e)}


def _zip_dir(src: Path, dst: Path) -> None:
    import zipfile
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src))


def register_review_routers(app) -> None:
    """Install review, confirmation, final-versions and dataset routers."""
    app.include_router(review_router)
    app.include_router(confirm_router)
    app.include_router(final_router)
    app.include_router(dataset_router)
    # Ensure review schema applied (idempotent)
    from .db import _DB_LOCK
    with _DB_LOCK, connect() as conn:
        apply_review_schema(conn)
        conn.commit()
