"""Authenticated training export jobs; clients cannot supply annotation geometry."""

from typing import Annotated, Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import Field

from . import training_export as export
from .review_workflow import ReviewError
from .review_workflow_routes import Key, LoggedRoute, Model, User

router = APIRouter(prefix="/api/datasets", tags=["datasets"], route_class=LoggedRoute)
legacy_router = APIRouter(tags=["datasets"], route_class=LoggedRoute)


class Selection(Model):
    finalVersionIds: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=100
    )


class Export(Selection):
    format: Literal["yolo", "coco", "both"] = "yolo"
    splitRatio: float = Field(default=0.8, gt=0, lt=1, strict=True)


@router.post("/exports/preview")
def preview(body: Selection, user: User):
    return export.preview(body.finalVersionIds)


@router.post("/exports")
def create(body: Export, user: User, key: Key):
    return export.create(user["uid"], key, body.model_dump())


@router.get("/exports/{eid}")
def status(eid: str, user: User):
    return export.status(eid, user["uid"])


@router.get("/exports/{eid}/download")
def download(eid: str, user: User):
    path = export.download(eid, user["uid"])
    return FileResponse(path, media_type="application/zip", filename=f"{eid}.zip")


@legacy_router.post("/api/export/dataset")
def retired(user: User):
    raise ReviewError(
        "FINAL_CONFIRMATION_REQUIRED",
        "人工标注阶段不能导出训练数据集。请完成审查和对比确认后，在对比确认页导出。",
        410,
    )
