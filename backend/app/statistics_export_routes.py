"""Authenticated creation, progress and download of instance statistics packages."""

from typing import Annotated
from urllib.parse import quote
import logging

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .statistics_export_jobs import manager
from .review_workflow import ReviewError
from .review_workflow_routes import LoggedRoute, Model, User

router = APIRouter(prefix="/api/statistics", tags=["statistics"], route_class=LoggedRoute)
log = logging.getLogger("review.statistics")


class LeasedStreamingResponse(StreamingResponse):
    def __init__(self, *args, release, export_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release
        self.export_id = export_id

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            log.warning("statistics.download_interrupted export=%s", self.export_id, exc_info=True)
            raise
        finally:
            # ASGI send failures/cancellation can bypass BackgroundTask and
            # leave a suspended iterator alive. Release immediately on exit.
            self.release()


def retry_key(idempotency_key: str = Header(default="")):
    if not idempotency_key.strip() or len(idempotency_key) > 128:
        raise ReviewError("IDEMPOTENCY_KEY_REQUIRED", "请求缺少有效的重试标识", 428)
    return idempotency_key


class ExportRequest(Model):
    pass


@router.post("/exports", status_code=202)
def create(body: ExportRequest, user: User, key: Annotated[str, Depends(retry_key)]):
    return manager().create(user["uid"], key)


@router.get("/exports/{eid}")
def status(eid: str, user: User):
    return manager().status(eid, user["uid"])


@router.get("/exports/{eid}/download")
def download(eid: str, user: User):
    jobs = manager()
    stream, filename, size = jobs.download(eid, user["uid"])

    def chunks():
        try:
            while chunk := stream.read(1024 * 1024):
                yield chunk
        finally:
            jobs.release(eid, stream)

    return LeasedStreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": "attachment; filename=statistics.zip; filename*=UTF-8''" + quote(filename),
        "Content-Length": str(size), "Cache-Control": "no-store",
    }, background=BackgroundTask(jobs.release, eid, stream),
       release=lambda: jobs.release(eid, stream), export_id=eid)
