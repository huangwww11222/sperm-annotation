"""Shared request plumbing: logged JSON errors, auth, idempotency and media context."""

import logging
import uuid
from typing import Annotated

from fastapi import Depends, Header, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from .auth import current_user
from . import review_workflow as workflow

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


User = Annotated[dict, Depends(current_user)]


def protocol(
    x_review_contract: str = Header(default=""),
    idempotency_key: str = Header(default=""),
):
    if x_review_contract not in ("2", "3"):
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


class EmptyRange(Model):
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=0)


class AnnotationComplete(Model):
    expectedSourceRevision: str
    confirmComplete: bool = Field(strict=True)
    explicitEmptyFrameRanges: list[EmptyRange] = Field(default_factory=list)


def media_context(media_id, allow_tracking=False):
    """Directory, original video and probed info; writes refuse while tracking runs."""
    from .main import media_dir, find_video, tracking_is_busy
    from .tracker import _probe_video

    if not allow_tracking and tracking_is_busy(media_id):
        raise workflow.ReviewError(
            "SOURCE_BUSY", "该视频正在 AI 追踪，请等待完成后再操作"
        )
    directory = media_dir(media_id)
    video = find_video(directory)
    if not video:
        raise workflow.ReviewError("MEDIA_NOT_FOUND", "原始视频不存在", 404)
    return directory, video, _probe_video(video)


def completion_context(media_id):
    return media_context(media_id)
