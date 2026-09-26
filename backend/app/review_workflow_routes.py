"""Versioned B routes and shared authenticated request logging for B/C."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from .auth import current_user
from . import review_workflow as workflow
from .review_repository import get_baseline, list_baselines

log = logging.getLogger("review.api")


class LoggedRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def invoke(request: Request):
            request_id = "req_" + uuid.uuid4().hex[:16]
            try:
                response = await handler(request)
            except workflow.ReviewError as e:
                log.warning(
                    "review.request_rejected request=%s path=%s code=%s",
                    request_id,
                    request.url.path,
                    e.code,
                )
                response = JSONResponse(
                    status_code=e.status,
                    content={
                        "code": e.code,
                        "message": e.message,
                        "requestId": request_id,
                    },
                )
            except HTTPException as e:
                response = JSONResponse(
                    status_code=e.status_code,
                    content={
                        "code": "AUTH_REQUIRED"
                        if e.status_code == 401
                        else "REQUEST_REJECTED",
                        "message": str(e.detail),
                        "requestId": request_id,
                    },
                    headers=e.headers,
                )
            except RequestValidationError:
                response = JSONResponse(
                    status_code=422,
                    content={
                        "code": "INVALID_REQUEST",
                        "message": "请求格式或数值无效，请刷新页面后重试",
                        "requestId": request_id,
                    },
                )
            except Exception:
                log.exception(
                    "review.request_failed request=%s path=%s",
                    request_id,
                    request.url.path,
                )
                response = JSONResponse(
                    status_code=500,
                    content={
                        "code": "INTERNAL_ERROR",
                        "message": "操作失败，请保留当前编辑并联系管理员",
                        "requestId": request_id,
                    },
                )
            response.headers["X-Request-ID"] = request_id
            log.info(
                "review.request request=%s method=%s path=%s status=%s key=%s",
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                request.headers.get("Idempotency-Key", "-")[:128],
            )
            return response

        return invoke


router = APIRouter(prefix="/api/review", tags=["review"], route_class=LoggedRoute)
User = Annotated[dict, Depends(current_user)]


def protocol(
    x_review_contract: str = Header(default=""),
    idempotency_key: str = Header(default=""),
):
    if x_review_contract != "2":
        raise workflow.ReviewError(
            "CONTRACT_UPGRADE_REQUIRED", "请刷新页面使用新的审查版本", 428
        )
    if not idempotency_key or len(idempotency_key) > 128:
        raise workflow.ReviewError(
            "IDEMPOTENCY_KEY_REQUIRED", "请求缺少有效的重试标识", 428
        )
    return idempotency_key


Key = Annotated[str, Depends(protocol)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Patch(Model):
    objectId: int = Field(strict=True, gt=0)
    bbox: list[float] = Field(min_length=4, max_length=4)


class FrameVersion(Model):
    expectedFrameRevision: int = Field(ge=0)


class Edit(FrameVersion):
    patch: list[Patch]


class Complete(Model):
    expectedSessionRevision: int = Field(ge=0)


class Cursor(Model):
    sessionId: str
    frameIndex: int = Field(ge=0)
    expectedCursorRevision: int = Field(ge=0)


class SessionIn(Model):
    baselineId: str


@router.get("/sessions")
def sessions(user: User):
    return {"items": workflow.list_sessions(user["uid"])}


@router.get("/sessions/{sid}")
def session(sid: str, user: User):
    return workflow.get_session(sid, user["uid"])


@router.post("/sessions/{sid}/claim")
def claim(sid: str, user: User, key: Key):
    return workflow.write("claim", sid, user["uid"], key, {})


@router.get("/sessions/{sid}/frames/{fi}")
def frame(sid: str, fi: int, user: User):
    return workflow.get_frame(sid, fi, user["uid"])


@router.put("/sessions/{sid}/frames/{fi}/draft")
def draft(sid: str, fi: int, body: Edit, user: User, key: Key):
    return workflow.write("draft", sid, user["uid"], key, body.model_dump(), fi)


@router.post("/sessions/{sid}/frames/{fi}/submit")
def submit(sid: str, fi: int, body: Edit, user: User, key: Key):
    return workflow.write("submit", sid, user["uid"], key, body.model_dump(), fi)


@router.post("/sessions/{sid}/frames/{fi}/discard")
def discard(sid: str, fi: int, body: FrameVersion, user: User, key: Key):
    return workflow.write("discard", sid, user["uid"], key, body.model_dump(), fi)


@router.post("/sessions/{sid}/freeze")
def complete(sid: str, body: Complete, user: User, key: Key):
    return workflow.write("finish", sid, user["uid"], key, body.model_dump())


@router.put("/cursors")
def cursor(body: Cursor, user: User, key: Key):
    return workflow.write(
        "cursor", body.sessionId, user["uid"], key, body.model_dump(), body.frameIndex
    )


@router.get("/baselines")
def baselines(user: User):
    return {"items": list_baselines()}


@router.get("/baselines/{bid}")
def baseline(bid: str, user: User):
    data = get_baseline(bid)
    if not data:
        raise workflow.ReviewError("BASELINE_NOT_FOUND", "原始标注版本不存在", 404)
    return data


@router.post("/sessions")
def ensure_session(body: SessionIn, user: User, key: Key):
    return workflow.ensure_session(body.baselineId, user["uid"], key)


class EmptyRange(Model):
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=0)


class AnnotationComplete(Model):
    expectedSourceRevision: str
    confirmComplete: bool = Field(strict=True)
    explicitEmptyFrameRanges: list[EmptyRange] = Field(default_factory=list)


def completion_context(media_id):
    from .main import media_dir, find_video, tracking_is_busy
    from .tracker import _probe_video

    if tracking_is_busy():
        raise workflow.ReviewError(
            "SOURCE_BUSY", "Tracking 正在运行，请等待保存完成后送审"
        )
    directory = media_dir(media_id)
    video = find_video(directory)
    if not video:
        raise workflow.ReviewError("MEDIA_NOT_FOUND", "原始视频不存在", 404)
    return directory, video, _probe_video(video)


@router.get("/media/{media_id}/completion-preview")
def completion_preview(media_id: str, user: User):
    from .annotation_completion import preview
    from .review_source_lock import completion_read

    with completion_read():
        directory, video, info = completion_context(media_id)
        return preview(directory, user["uid"], info)


@router.post("/media/{media_id}/complete")
def annotation_complete(media_id: str, body: AnnotationComplete, user: User, key: Key):
    from .annotation_completion import complete
    from .review_source_lock import completion_read

    with completion_read():
        directory, video, info = completion_context(media_id)
        return complete(
            directory, video, media_id, user["uid"], key, body.model_dump(), info
        )
