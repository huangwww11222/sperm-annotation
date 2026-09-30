"""Authenticated C API. No legacy mutation can bypass version/idempotency checks."""

from typing import Literal

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import Field

from . import confirmation_workflow as flow
from .review_repository import list_final_versions
from .review_workflow import packed
from .review_workflow_routes import Key, LoggedRoute, Model, User

router = APIRouter(
    prefix="/api/confirmation", tags=["confirmation"], route_class=LoggedRoute
)
final_router = APIRouter(
    prefix="/api/final-versions", tags=["final-versions"], route_class=LoggedRoute
)


class Decision(Model):
    choice: Literal["A", "B"]
    expectedDecisionRevision: int = Field(strict=True, ge=0)


class Version(Model):
    expectedSessionRevision: int = Field(strict=True, ge=0)


class Undo(Version):
    actionId: str


class ReturnFrame(Version):
    changeId: str
    reason: str = Field(min_length=1, max_length=1000)


class Cursor(Model):
    changeId: str | None = None
    expectedCursorRevision: int = Field(strict=True, ge=0)


@router.get("/sessions")
def sessions(user: User):
    return {"items": flow.list_sessions(user["uid"])}


@router.get("/sessions/{sid}")
def session(sid: str, user: User):
    return flow.get_session(sid, user["uid"])


@router.get("/sessions/{sid}/changes")
def changes(sid: str, user: User):
    return {"items": flow.list_changes(sid, user["uid"])}


@router.get("/sessions/{sid}/frames/{fi}")
def frame(sid: str, fi: int, user: User):
    return flow.get_frame(sid, fi, user["uid"])


@router.post("/sessions/{sid}/claim")
def claim(sid: str, user: User, key: Key):
    return flow.write("claim", sid, user["uid"], key, {})


@router.put("/sessions/{sid}/changes/{change_id}/decision")
def decide(sid: str, change_id: str, body: Decision, user: User, key: Key):
    return flow.write("decide", sid, user["uid"], key, body.model_dump(), change_id)


@router.post("/sessions/{sid}/undo")
def undo(sid: str, body: Undo, user: User, key: Key):
    return flow.write("undo", sid, user["uid"], key, body.model_dump())


@router.post("/sessions/{sid}/finalize")
def finish(sid: str, body: Version, user: User, key: Key):
    return flow.write("finish", sid, user["uid"], key, body.model_dump())


@router.post("/sessions/{sid}/reopen")
def reopen(sid: str, body: Version, user: User, key: Key):
    return flow.write("reopen", sid, user["uid"], key, body.model_dump())


@router.post("/sessions/{sid}/return")
def return_frame(sid: str, body: ReturnFrame, user: User, key: Key):
    return flow.write("return", sid, user["uid"], key, body.model_dump())


@router.put("/sessions/{sid}/cursor")
def cursor(sid: str, body: Cursor, user: User, key: Key):
    return flow.write("cursor", sid, user["uid"], key, body.model_dump(), body.changeId)


@final_router.get("")
@final_router.get("/")
def finals(user: User):
    return {"items": list_final_versions()}


@final_router.get("/{vid}")
def final(vid: str, user: User):
    return flow.final_data(vid)


@final_router.get("/{vid}/download")
def download(vid: str, user: User):
    data = flow.final_data(vid)
    flow.log.info("confirmation.export version=%s actor=%s", vid, user["uid"])
    return Response(
        content=packed(data),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{vid}.json"'},
    )
