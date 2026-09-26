"""Create isolated real-video fixtures; refuses business database paths."""

import json
import uuid
from pathlib import Path

import cv2
import numpy as np
from app import confirmation_workflow as confirmation
from app import db
from app import review_workflow as review
from app.auth import hash_password, sign_jwt
from app.config import DB_FILE, TRACK_DATA_DIR
from app.review_repository import (
    compute_file_sha256,
    create_review_session,
    freeze_baseline,
    upsert_media_revision,
)
from app.review_schema import apply_review_schema

assert "/work/" in str(DB_FILE) and "/e2e-confirm" in str(DB_FILE), DB_FILE
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
db.init_db()
for name in ["confirm-A", "confirm-B", "confirm-C"]:
    if not db.get_user(name):
        db.create_user(name, hash_password("confirmation-test"))
with db.connect() as c:
    apply_review_schema(c)
    review.migrate(c)
    confirmation.migrate(c)
result = {"token": sign_jwt({"uid": 3}), "authorToken": sign_jwt({"uid": 1})}
for mode in ["main", "zero", "failure"]:
    mid = "confirmation-" + mode + "-" + uuid.uuid4().hex[:6]
    directory = TRACK_DATA_DIR / mid
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / (mode + ".avi")
    writer = cv2.VideoWriter(
        str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (800, 450)
    )
    frames = []
    for fi in range(4):
        image = np.random.default_rng(42 + fi).integers(
            80, 125, (450, 800, 3), dtype=np.uint8
        )
        boxes = [[200, 140, 260, 200], [400, 210, 470, 290]] if fi < 3 else []
        for x1, y1, x2, y2 in boxes:
            cv2.ellipse(
                image,
                ((x1 + x2) // 2, (y1 + y2) // 2),
                ((x2 - x1) // 3, (y2 - y1) // 3),
                30,
                0,
                360,
                (210, 210, 210),
                -1,
            )
            cv2.line(
                image, ((x1 + x2) // 2, y2 - 10), (x2 + 30, y2 + 20), (155, 155, 155), 2
            )
        cv2.putText(
            image,
            f"{mode} Frame {fi + 1}",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (255, 255, 255),
            2,
        )
        writer.write(image)
        frames.append(
            dict(
                frameIndex=fi,
                coverage="objects" if boxes else "empty",
                objects=[
                    dict(
                        objectId=i + 1, bbox=b, classKey="sperm" if i == 0 else "debris"
                    )
                    for i, b in enumerate(boxes)
                ],
            )
        )
    writer.release()
    (directory / "media.json").write_text(
        json.dumps(
            dict(videoName=video.name, width=800, height=450, fps=10, frameCount=4)
        )
    )
    m = upsert_media_revision(
        mid, compute_file_sha256(video), video.stat().st_size, 800, 450, 10, 4
    )
    a = freeze_baseline(m, mid, 1, frames)
    sid = create_review_session(a["id"])["id"]
    review.write("claim", sid, 2, uuid.uuid4().hex, {})
    for fi in range(4):
        patch = []
        if mode != "zero" and fi < 2:
            patch = [dict(objectId=1, bbox=[207.125, 146, 269.625, 207])]
            if fi == 0:
                patch.append(dict(objectId=2, bbox=[400, 210, 481.25, 299.5]))
        review.write(
            "submit",
            sid,
            2,
            uuid.uuid4().hex,
            dict(
                expectedFrameRevision=review.get_frame(sid, fi, 2)["frameRevision"],
                patch=patch,
            ),
            fi,
        )
    r = review.write(
        "finish",
        sid,
        2,
        uuid.uuid4().hex,
        dict(expectedSessionRevision=review.get_session(sid, 2)["revision"]),
    )
    result[mode] = {"sid": r["confirmationSessionId"], "mediaId": mid}
Path("work/confirmation-browser-fixture.json").write_text(json.dumps(result))
print("Created three isolated C tasks with 4 source frames each.")
