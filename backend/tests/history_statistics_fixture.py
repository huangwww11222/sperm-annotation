"""Finish retired confirmation tasks directly in the data layer for statistics-export checks.

The confirmation page was removed (2026-11-01), but statistics export still reads the
historical A/B/C/F tables. This gives it a current and a superseded final version.
"""
import json
import uuid
from pathlib import Path

from app import confirmation_workflow as cflow
from app.config import DB_FILE

assert "/work/" in DB_FILE.as_posix() and "/e2e-confirm" in DB_FILE.as_posix(), DB_FILE
fixture = json.loads(Path("work/confirmation-browser-fixture.json").read_text())
UID = 3


def write(cid, action, body=None, change=None):
    if action in ("finish", "reopen"):
        body = {"expectedSessionRevision": cflow.get_session(cid, UID)["revision"], **(body or {})}
    return cflow.write(action, cid, UID, uuid.uuid4().hex, body or {}, change)


def decide_all(cid, choice):
    for item in cflow.list_changes(cid, UID):
        write(cid, "decide", {"choice": choice, "expectedDecisionRevision": item["decisionRevision"]}, item["changeId"])


main = fixture["main"]["sid"]
write(main, "claim")
decide_all(main, "B")
write(main, "finish")
write(main, "reopen")
decide_all(main, "A")
write(main, "finish")
zero = fixture["zero"]["sid"]
write(zero, "claim")
write(zero, "finish")
print("history fixture: main confirmed twice, zero confirmed once")
