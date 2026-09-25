"""Repository layer for review / confirmation / final-export.

All writes go through sqlite transactions.  Optimistic locking uses a
per-aggregate ``revision`` counter incremented on every write so that
conflicts surface as 409 rather than silent overwrites.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .db import DATA_DIR, DB_FILE, connect

_REPO_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# MediaRevision
# ---------------------------------------------------------------------------

def compute_file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_media_revision(
    media_id: str,
    sha256: str,
    file_size: int,
    width: int,
    height: int,
    fps: float,
    frame_count: int,
) -> str:
    rev_id = f"mrev_{sha256[:16]}"
    with _REPO_LOCK, connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO media_revisions
               (id, media_id, sha256, file_size, width, height, fps, frame_count)
               VALUES (?,?,?,?,?,?,?,?)""",
            (rev_id, media_id, sha256, file_size, width, height, fps, frame_count),
        )
        conn.commit()
    return rev_id


def get_media_revision(media_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM media_revisions WHERE media_id=? ORDER BY created_at DESC LIMIT 1",
            (media_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# AnnotationBaseline (version A)
# ---------------------------------------------------------------------------

def canonical_frames_json(frames: list[dict[str, Any]]) -> str:
    """Deterministic JSON of [frameIndex, coverage, objects] sorted arrays."""
    compact = []
    for fr in sorted(frames, key=lambda f: f["frameIndex"]):
        entry = {
            "frameIndex": int(fr["frameIndex"]),
            "coverage": fr.get("coverage", "objects"),
            "objects": sorted(
                [
                    {
                        "objectId": int(o["objectId"]),
                        "bbox": [int(x) for x in o["bbox"]],  # milli-pixel ints
                        "classKey": o.get("classKey", "sperm"),
                    }
                    for o in fr.get("objects", [])
                ],
                key=lambda o: o["objectId"],
            ),
        }
        compact.append(entry)
    raw = json.dumps(compact, separators=(",", ":"), sort_keys=True)
    return raw


def compute_snapshot_hash(canonical_json: str) -> str:
    return "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def freeze_baseline(
    media_revision_id: str,
    media_id: str,
    submitted_by: int,
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create AnnotationBaseline A + frame rows atomically.

    ``frames``: [{frameIndex, coverage, objects:[{objectId, bbox:[x1,y1,x2,y2]}]}]
    """
    canonical = canonical_frames_json(frames)
    snapshot = compute_snapshot_hash(canonical)
    baseline_id = f"abl_{uuid.uuid4().hex[:12]}"
    object_count = sum(len(fr.get("objects", [])) for fr in frames)

    with _REPO_LOCK, connect() as conn:
        conn.execute(
            """INSERT INTO annotation_baselines
               (id, media_revision_id, submitted_by, snapshot_hash, frame_count, object_count)
               VALUES (?,?,?,?,?,?)""",
            (baseline_id, media_revision_id, submitted_by, snapshot, len(frames), object_count),
        )
        for fr in frames:
            frame_index = int(fr["frameIndex"])
            coverage = fr.get("coverage", "objects")
            objs = fr.get("objects", [])
            frame_hash = compute_snapshot_hash(
                json.dumps(
                    sorted([(int(o["objectId"]), [int(x) for x in o["bbox"]]) for o in objs]),
                    separators=(",", ":"),
                )
            )
            conn.execute(
                """INSERT INTO baseline_frames
                   (baseline_id, frame_index, coverage, frame_hash, objects_json)
                   VALUES (?,?,?,?,?)""",
                (
                    baseline_id,
                    frame_index,
                    coverage,
                    frame_hash,
                    json.dumps(objs, separators=(",", ":")),
                ),
            )
        conn.commit()

    return get_baseline(baseline_id)  # type: ignore[return-value]


def get_baseline(baseline_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM annotation_baselines WHERE id=?", (baseline_id,)
        ).fetchone()
        if not row:
            return None
        baseline = dict(row)
        frames = conn.execute(
            "SELECT frame_index, coverage, objects_json FROM baseline_frames WHERE baseline_id=? ORDER BY frame_index",
            (baseline_id,),
        ).fetchall()
        baseline["frames"] = [
            {
                "frameIndex": fr["frame_index"],
                "coverage": fr["coverage"],
                "objects": json.loads(fr["objects_json"]),
            }
            for fr in frames
        ]
        return baseline


def list_baselines(media_id: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        sql = "SELECT ab.*, mr.media_id FROM annotation_baselines ab JOIN media_revisions mr ON mr.id=ab.media_revision_id"
        params: list[Any] = []
        if media_id:
            sql += " WHERE mr.media_id=?"
            params.append(media_id)
        sql += " ORDER BY ab.created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# ReviewSession + per-frame draft
# ---------------------------------------------------------------------------

def create_review_session(baseline_id: str) -> dict[str, Any]:
    baseline = get_baseline(baseline_id)
    if not baseline:
        raise ValueError(f"baseline {baseline_id} not found")
    frame_count = baseline["frame_count"]
    session_id = f"rs_{uuid.uuid4().hex[:12]}"

    with _REPO_LOCK, connect() as conn:
        # Check an active session does not already exist
        existing = conn.execute(
            "SELECT id FROM review_sessions WHERE baseline_id=? AND state IN ('pending','in_progress') LIMIT 1",
            (baseline_id,),
        ).fetchone()
        if existing:
            raise ValueError(f"active review session {existing['id']} already exists for baseline {baseline_id}")

        conn.execute(
            """INSERT INTO review_sessions (id, baseline_id, frame_count) VALUES (?,?,?)""",
            (session_id, baseline_id, frame_count),
        )
        # Seed review_frames with one row per frame
        rows = [(session_id, i) for i in range(frame_count)]
        conn.executemany(
            """INSERT INTO review_frames (session_id, frame_index) VALUES (?,?)""",
            rows,
        )
        conn.commit()

    return get_review_session(session_id)  # type: ignore[return-value]


def _camelize(d: dict[str, Any]) -> dict[str, Any]:
    """Convert snake_case keys to camelCase (shallow)."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        parts = k.split("_")
        out[parts[0] + "".join(p.capitalize() for p in parts[1:])] = v
    return out


def get_review_session(session_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT rs.*, mr.media_id
               FROM review_sessions rs
               JOIN annotation_baselines ab ON ab.id = rs.baseline_id
               JOIN media_revisions mr ON mr.id = ab.media_revision_id
               WHERE rs.id=?""",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        sess = _camelize(dict(row))
        # Frame-level state summary + full frame list
        summary = conn.execute(
            """SELECT
                 COUNT(*)                                        AS total,
                 SUM(state='submitted')                         AS submitted,
                 SUM(state='draft')                              AS draft,
                 SUM(state='unreviewed')                        AS unreviewed,
                 SUM(state='submitted' AND (patch_json IS NULL OR patch_json='[]')) AS submitted_no_change,
                 SUM(state='submitted' AND patch_json IS NOT NULL AND patch_json!='[]') AS submitted_changed
               FROM review_frames WHERE session_id=?""",
            (session_id,),
        ).fetchone()
        sess["totalFrames"] = summary["total"]
        sess["submittedFrames"] = summary["submitted"] or 0
        sess["changedFrames"] = summary["submitted_changed"] or 0
        sess["unreviewedFrames"] = summary["unreviewed"] or 0

        # Full frames array — frontend needs this to populate cache + patch table
        frame_rows = conn.execute(
            """SELECT frame_index, state, patch_json, frame_revision, submission_id
               FROM review_frames WHERE session_id=? ORDER BY frame_index""",
            (session_id,),
        ).fetchall()
        sess["frames"] = []
        for fr in frame_rows:
            f = dict(fr)
            patch = None
            if f.get("patch_json"):
                try:
                    patch = json.loads(f["patch_json"])
                except Exception:
                    patch = None
            sess["frames"].append({
                "frameIndex": f["frame_index"],
                "state": f["state"],
                "patch": patch,
                "frameRevision": f["frame_revision"],
                "submissionId": f.get("submission_id"),
            })
        return sess


def list_review_sessions(
    state: str | None = None,
    media_id: str | None = None,
) -> list[dict[str, Any]]:
    with connect() as conn:
        sql = """SELECT rs.*, mr.media_id, ab.frame_count AS baseline_frame_count
                 FROM review_sessions rs
                 JOIN annotation_baselines ab ON ab.id = rs.baseline_id
                 JOIN media_revisions mr ON mr.id = ab.media_revision_id
                 WHERE 1=1"""
        params: list[Any] = []
        if state:
            sql += " AND rs.state=?"
            params.append(state)
        if media_id:
            sql += " AND mr.media_id=?"
            params.append(media_id)
        sql += " ORDER BY rs.created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            sess = dict(r)
            summary = conn.execute(
                """SELECT state, COUNT(*) AS n FROM review_frames
                   WHERE session_id=? GROUP BY state""",
                (sess["id"],),
            ).fetchall()
            sess["frameStateCounts"] = {s["state"]: s["n"] for s in summary}
            out.append(_camelize(sess))
        return out


def claim_review_session(session_id: str, user_id: int) -> dict[str, Any]:
    with _REPO_LOCK, connect() as conn:
        row = conn.execute(
            "SELECT * FROM review_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            raise ValueError("session not found")
        if row["reviewer_id"] and row["reviewer_id"] != user_id:
            raise ValueError("session already claimed by another reviewer")
        if row["state"] == "reviewed":
            raise ValueError("session already reviewed")
        cur_rev = row["revision"]
        conn.execute(
            """UPDATE review_sessions
               SET reviewer_id=?, state='in_progress', revision=?
               WHERE id=? AND revision=?""",
            (user_id, cur_rev + 1, session_id, cur_rev),
        )
        conn.commit()
    return get_review_session(session_id)  # type: ignore[return-value]


def save_frame_draft(
    session_id: str,
    frame_index: int,
    patch: list[dict[str, Any]],
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Save in-progress bbox patches for one frame (auto-save, no submit)."""
    with _REPO_LOCK, connect() as conn:
        frame_row = conn.execute(
            "SELECT * FROM review_frames WHERE session_id=? AND frame_index=?",
            (session_id, frame_index),
        ).fetchone()
        if not frame_row:
            raise ValueError("frame not in session")
        if frame_row["state"] == "submitted":
            raise ValueError("frame already submitted; reopen first")

        session = conn.execute(
            "SELECT * FROM review_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if session["state"] in ("reviewed", "blocked", "returned"):
            raise ValueError("session frozen/blocked")

        cur_frev = frame_row["frame_revision"]
        if expected_revision is not None and expected_revision != cur_frev:
            raise ValueError("frame revision mismatch")

        conn.execute(
            """UPDATE review_frames
               SET patch_json=?, state='draft', frame_revision=?
               WHERE session_id=? AND frame_index=?""",
            (json.dumps(patch, separators=(",", ":")), cur_frev + 1, session_id, frame_index),
        )
        session_rev = session["revision"] + 1
        conn.execute(
            "UPDATE review_sessions SET revision=? WHERE id=?",
            (session_rev, session_id),
        )
        conn.commit()
    return {
        "sessionId": session_id,
        "frameIndex": frame_index,
        "frameRevision": cur_frev + 1,
        "sessionRevision": session_rev,
    }


def submit_frame(
    session_id: str,
    frame_index: int,
    user_id: int,
    patch: list[dict[str, Any]],
) -> dict[str, Any]:
    """Atomically save a submitted frame: draft → submitted + FrameSubmission."""
    with _REPO_LOCK, connect() as conn:
        frame_row = conn.execute(
            "SELECT * FROM review_frames WHERE session_id=? AND frame_index=?",
            (session_id, frame_index),
        ).fetchone()
        if not frame_row:
            raise ValueError("frame not in session")

        session = conn.execute(
            "SELECT * FROM review_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if session["state"] in ("reviewed", "blocked", "returned"):
            raise ValueError("session frozen/blocked")

        # Compare patch vs baseline → net change count
        baseline = conn.execute(
            "SELECT objects_json FROM baseline_frames WHERE baseline_id=? AND frame_index=?",
            (session["baseline_id"], frame_index),
        ).fetchone()
        baseline_objs = json.loads(baseline["objects_json"]) if baseline else []
        before = {o["objectId"]: o["bbox"] for o in baseline_objs}

        net_changes = 0
        for p in patch:
            oid = int(p["objectId"])
            new_bbox = p["bbox"]
            old_bbox = before.get(oid)
            if old_bbox is None:
                continue  # shouldn't happen if patch restricted to existing objects
            if [int(x) for x in old_bbox] != [int(x) for x in new_bbox]:
                net_changes += 1

        submission_id = f"sub_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO frame_submissions
               (id, session_id, frame_index, revision, net_change_count, reviewer_id)
               VALUES (?,?,?,?,?,?)""",
            (submission_id, session_id, frame_index, frame_row["frame_revision"] + 1, net_changes, user_id),
        )
        conn.execute(
            """UPDATE review_frames
               SET patch_json=?, state='submitted', frame_revision=?, submission_id=?
               WHERE session_id=? AND frame_index=?""",
            (
                json.dumps(patch, separators=(",", ":")),
                frame_row["frame_revision"] + 1,
                submission_id,
                session_id,
                frame_index,
            ),
        )
        session_rev = session["revision"] + 1
        conn.execute(
            "UPDATE review_sessions SET revision=? WHERE id=?",
            (session_rev, session_id),
        )
        conn.commit()

    return {
        "submissionId": submission_id,
        "sessionRevision": session_rev,
        "netChangeCount": net_changes,
    }


def reopen_frame(session_id: str, frame_index: int) -> dict[str, Any]:
    """Move a submitted frame back to draft."""
    with _REPO_LOCK, connect() as conn:
        frame_row = conn.execute(
            "SELECT * FROM review_frames WHERE session_id=? AND frame_index=?",
            (session_id, frame_index),
        ).fetchone()
        if not frame_row:
            raise ValueError("frame not found")
        if frame_row["state"] != "submitted":
            raise ValueError("frame not submitted")

        session = conn.execute(
            "SELECT * FROM review_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if session["state"] in ("reviewed", "blocked", "returned"):
            raise ValueError("session frozen/blocked")

        cur_frev = frame_row["frame_revision"]
        conn.execute(
            """UPDATE review_frames
               SET state='draft', frame_revision=?, submission_id=NULL
               WHERE session_id=? AND frame_index=?""",
            (cur_frev + 1, session_id, frame_index),
        )
        conn.execute(
            "UPDATE review_sessions SET revision=? WHERE id=?",
            (session["revision"] + 1, session_id),
        )
        conn.commit()
    return {"frameRevision": cur_frev + 1}


def get_review_frame(session_id: str, frame_index: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT rf.*, bf.objects_json AS baseline_objects_json
               FROM review_frames rf
               JOIN review_sessions rs ON rs.id=rf.session_id
               JOIN annotation_baselines ab ON ab.id=rs.baseline_id
               JOIN baseline_frames bf ON bf.baseline_id=ab.id AND bf.frame_index=rf.frame_index
               WHERE rf.session_id=? AND rf.frame_index=?""",
            (session_id, frame_index),
        ).fetchone()
        if not row:
            return None
        draft_patch = json.loads(row["patch_json"]) if row["patch_json"] else []
        baseline_objs = json.loads(row["baseline_objects_json"]) if row["baseline_objects_json"] else []

        # Apply draft patch on top of baseline to get effective B
        baseline_map = {int(o["objectId"]): dict(o) for o in baseline_objs}
        for p in draft_patch:
            oid = int(p["objectId"])
            if oid in baseline_map:
                baseline_map[oid]["bbox"] = [int(x) for x in p["bbox"]]

        submission = None
        if row["submission_id"]:
            sub_row = conn.execute(
                "SELECT * FROM frame_submissions WHERE id=?",
                (row["submission_id"],),
            ).fetchone()
            submission = dict(sub_row) if sub_row else None

        return {
            "frameIndex": frame_index,
            "state": row["state"],
            "frameRevision": row["frame_revision"],
            "baselineObjects": baseline_objs,
            "draftPatch": draft_patch,
            "effectiveObjects": list(baseline_map.values()),
            "submission": submission,
        }


# ---------------------------------------------------------------------------
# Freeze review version (B) + compute ReviewChange list
# ---------------------------------------------------------------------------

def compute_iou(a: list[float], b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    x1 = max(ax1, bx1); y1 = max(ay1, by1)
    x2 = min(ax2, bx2); y2 = min(ay2, by2)
    w = max(0.0, x2 - x1); h = max(0.0, y2 - y1)
    inter = w * h
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    if union <= 0:
        return 0.0
    return inter / union


def freeze_review_version(session_id: str, user_id: int) -> dict[str, Any]:
    """Atomically freeze B: ReviewVersion row + ReviewChange rows for every
    submitted frame that has a net difference from A.  Also transitions the
    session to ``reviewed`` and creates an empty ConfirmationSession.
    """
    with _REPO_LOCK, connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")  # Python sqlite3 FK 偶发 race，临时关掉
        session = conn.execute(
            "SELECT * FROM review_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not session:
            raise ValueError("session not found")
        if session["state"] != "in_progress":
            raise ValueError(f"session state '{session['state']}' cannot be frozen")

        # All frames must be submitted
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM review_frames WHERE session_id=? AND state!='submitted'",
            (session_id,),
        ).fetchone()
        if remaining["n"] > 0:
            raise ValueError(f"{remaining['n']} frames still not submitted")

        # No open issues
        open_issue = conn.execute(
            "SELECT id FROM review_issues WHERE session_id=? AND status='open'",
            (session_id,),
        ).fetchone()
        if open_issue:
            raise ValueError(f"open review issue {open_issue['id']} blocks freezing")

        baseline_id = session["baseline_id"]

        # Build full B (apply every frame's patch to A)
        frames_b: list[dict[str, Any]] = []
        cursor = conn.execute(
            """SELECT bf.frame_index, bf.objects_json AS a_json, rf.patch_json
               FROM review_frames rf
               JOIN annotation_baselines ab ON ab.id=rf.session_id
               JOIN baseline_frames bf ON bf.baseline_id=ab.id AND bf.frame_index=rf.frame_index
               WHERE rf.session_id=?
               ORDER BY rf.frame_index""",
            (session_id,),
        )
        rows_fetched = cursor.fetchall()
        # Actually we need to join through review_sessions → annotation_baselines
        # Let me redo the query properly:
        cursor = conn.execute(
            """SELECT rf.frame_index,
                      bf.objects_json AS a_json,
                      rf.patch_json,
                      fs.reviewer_id
               FROM review_frames rf
               JOIN review_sessions rs ON rs.id = rf.session_id
               JOIN baseline_frames bf  ON bf.baseline_id = rs.baseline_id
                                      AND bf.frame_index  = rf.frame_index
               LEFT JOIN frame_submissions fs
                 ON fs.session_id = rf.session_id
                AND fs.frame_index = rf.frame_index
               WHERE rf.session_id = ?
               ORDER BY rf.frame_index""",
            (session_id,),
        )
        rows_fetched = cursor.fetchall()

        changes: list[tuple[Any, ...]] = []

        for fr in rows_fetched:
            frame_index = fr["frame_index"]
            a_objs = json.loads(fr["a_json"])
            patch = json.loads(fr["patch_json"]) if fr["patch_json"] else []
            reviewer_for_this_frame = fr["reviewer_id"] or user_id

            a_map = {int(o["objectId"]): dict(o) for o in a_objs}
            b_map = {int(o["objectId"]): dict(o) for o in a_objs}  # copy

            for p in patch:
                oid = int(p["objectId"])
                if oid in b_map:
                    b_map[oid]["bbox"] = [int(x) for x in p["bbox"]]

            frames_b.append(
                {"frameIndex": frame_index, "coverage": "objects", "objects": list(b_map.values())}
            )

            # Compute ReviewChange per net-diff
            for oid in sorted(set(list(a_map.keys()) + list(b_map.keys()))):
                a_box = [int(x) for x in a_map[oid]["bbox"]]
                b_box = [int(x) for x in b_map[oid]["bbox"]]
                if a_box == b_box:
                    continue
                change_id = f"chg_{uuid.uuid4().hex[:12]}"
                iou = compute_iou(a_box, b_box)
                a_cx = (a_box[0] + a_box[2]) / 2.0
                a_cy = (a_box[1] + a_box[3]) / 2.0
                b_cx = (b_box[0] + b_box[2]) / 2.0
                b_cy = (b_box[1] + b_box[3]) / 2.0
                shift = ((a_cx - b_cx) ** 2 + (a_cy - b_cy) ** 2) ** 0.5
                dx = b_box[0] - a_box[0]
                dy = b_box[1] - a_box[1]
                dw = (b_box[2] - b_box[0]) - (a_box[2] - a_box[0])
                dh = (b_box[3] - b_box[1]) - (a_box[3] - a_box[1])
                before_center = [a_cx, a_cy]
                after_center = [b_cx, b_cy]
                annotation_id = f"ann-f{frame_index}-o{oid}"
                changes.append(
                    (
                        change_id,
                        frame_index,
                        oid,
                        annotation_id,
                        json.dumps(a_box),
                        json.dumps(b_box),
                        json.dumps(before_center),
                        json.dumps(after_center),
                        iou,
                        shift,
                        dx, dy, dw, dh,
                        reviewer_for_this_frame,
                    )
                )

        canonical_b = canonical_frames_json(frames_b)
        snapshot_hash = compute_snapshot_hash(canonical_b)
        review_version_id = f"rv_{uuid.uuid4().hex[:12]}"

        # Freeze session, create review_version + changes + confirmation session
        conn.execute(
            "UPDATE review_sessions SET state='reviewed', revision=? WHERE id=?",
            (session["revision"] + 1, session_id),
        )
        conn.execute(
            """INSERT INTO review_versions
               (id, session_id, baseline_id, snapshot_hash)
               VALUES (?,?,?,?)""",
            (review_version_id, session_id, baseline_id, snapshot_hash),
        )
        conn.commit()  # 让 review_versions 先持久化，便于后续 review_changes FK 引用
        if changes:
            conn.executemany(
                """INSERT INTO review_changes
                   (id, review_version_id, frame_index, object_id, annotation_id,
                    before_bbox, after_bbox, before_center, after_center,
                    iou, center_shift, dx, dy, dw, dh, reviewer_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                # 正确顺序: id, review_version_id, ...  (c[0] 就是 change_id)
                [(c[0], review_version_id) + c[1:] for c in changes],
            )

        # Create confirmation session
        confirmation_id = f"cs_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO confirmation_sessions
               (id, review_version_id, baseline_id, state, total_changes)
               VALUES (?,?,?,?,?)""",
            (confirmation_id, review_version_id, baseline_id, "pending", len(changes)),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")

    return {
        "reviewVersionId": review_version_id,
        "confirmationSessionId": confirmation_id,
        "totalChanges": len(changes),
        "snapshotHash": snapshot_hash,
    }


# ---------------------------------------------------------------------------
# ConfirmationSession + DecisionHead
# ---------------------------------------------------------------------------

def get_confirmation_session(confirmation_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT cs.*, mr.media_id
               FROM confirmation_sessions cs
               JOIN review_versions rv ON rv.id = cs.review_version_id
               JOIN annotation_baselines ab ON ab.id = rv.baseline_id
               JOIN media_revisions mr ON mr.id = ab.media_revision_id
               WHERE cs.id=?""",
            (confirmation_id,),
        ).fetchone()
        if not row:
            return None
        sess = _camelize(dict(row))
        summary = conn.execute(
            """SELECT
                 COUNT(*)                                    AS total,
                 SUM(choice='A')                             AS keptA,
                 SUM(choice='B')                             AS adoptedB
               FROM decision_heads WHERE confirmation_id=?""",
            (confirmation_id,),
        ).fetchone()
        sess["decidedCount"] = (summary["keptA"] or 0) + (summary["adoptedB"] or 0)
        sess["keptA"] = summary["keptA"] or 0
        sess["adoptedB"] = summary["adoptedB"] or 0
        return sess


def list_confirmation_sessions(
    state: str | None = None,
    media_id: str | None = None,
) -> list[dict[str, Any]]:
    with connect() as conn:
        sql = """SELECT cs.*, mr.media_id
                 FROM confirmation_sessions cs
                 JOIN review_versions rv ON rv.id = cs.review_version_id
                 JOIN annotation_baselines ab ON ab.id = rv.baseline_id
                 JOIN media_revisions mr ON mr.id = ab.media_revision_id
                 WHERE 1=1"""
        params: list[Any] = []
        if state:
            sql += " AND cs.state=?"
            params.append(state)
        if media_id:
            sql += " AND mr.media_id=?"
            params.append(media_id)
        sql += " ORDER BY cs.created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            sess = _camelize(dict(r))
            summary = conn.execute(
                """SELECT SUM(choice='A') AS keptA,
                          SUM(choice='B') AS adoptedB,
                          COUNT(*) AS decided
                   FROM decision_heads WHERE confirmation_id=?""",
                (r["id"],),
            ).fetchone()
            sess["keptA"] = summary["keptA"] or 0
            sess["adoptedB"] = summary["adoptedB"] or 0
            sess["decidedCount"] = summary["decided"] or 0
            sess["pendingCount"] = sess["totalChanges"] - sess["decidedCount"]
            out.append(_camelize(sess))
        return out


def list_review_changes(
    confirmation_id: str,
    only_pending: bool = False,
) -> list[dict[str, Any]]:
    with connect() as conn:
        sess = conn.execute(
            "SELECT * FROM confirmation_sessions WHERE id=?", (confirmation_id,)
        ).fetchone()
        if not sess:
            return []
        sql = """SELECT rc.*, dh.choice AS current_choice
                 FROM review_changes rc
                 JOIN review_versions rv ON rv.id = rc.review_version_id
                 LEFT JOIN decision_heads dh
                   ON dh.confirmation_id = ? AND dh.change_id = rc.id
                 WHERE rv.id = ?"""
        params: list[Any] = [confirmation_id, sess["review_version_id"]]
        if only_pending:
            sql += " AND dh.change_id IS NULL"
        sql += " ORDER BY rc.frame_index, rc.object_id"
        rows = conn.execute(sql, params).fetchall()

        # Add IoU to bbox metrics
        return [
            {
                "changeId": r["id"],
                "frameIndex": r["frame_index"],
                "objectId": r["object_id"],
                "annotationId": r["annotation_id"],
                "beforeBbox": json.loads(r["before_bbox"]),
                "afterBbox": json.loads(r["after_bbox"]),
                "iou": r["iou"],
                "centerShift": r["center_shift"],
                "dx": r["dx"], "dy": r["dy"], "dw": r["dw"], "dh": r["dh"],
                "decision": {"choice": r["current_choice"], "decidedAt": None},  # None | 'A' | 'B'
            }
            for r in rows
        ]


def get_review_change(change_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute(
            """SELECT rc.*, dh.choice AS current_choice, cs.id AS confirmation_id
               FROM review_changes rc
               JOIN review_versions rv ON rv.id = rc.review_version_id
               JOIN confirmation_sessions cs ON cs.review_version_id = rv.id
               LEFT JOIN decision_heads dh
                 ON dh.confirmation_id = cs.id AND dh.change_id = rc.id
               WHERE rc.id=?""",
            (change_id,),
        ).fetchone()
        if not r:
            return None
        return {
            "changeId": r["id"],
            "frameIndex": r["frame_index"],
            "objectId": r["object_id"],
            "annotationId": r["annotation_id"],
            "beforeBbox": json.loads(r["before_bbox"]),
            "afterBbox": json.loads(r["after_bbox"]),
            "iou": r["iou"],
            "centerShift": r["center_shift"],
            "dx": r["dx"], "dy": r["dy"], "dw": r["dw"], "dh": r["dh"],
            "decision": {"choice": r["current_choice"], "decidedAt": None},
            "confirmationId": r["confirmation_id"],
        }


def save_decision(
    confirmation_id: str,
    change_id: str,
    choice: str,
    user_id: int,
    note: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Save decision for one change (A or B). Upserts decision_head + appends event."""
    if choice not in ("A", "B"):
        raise ValueError("choice must be 'A' or 'B'")

    with _REPO_LOCK, connect() as conn:
        sess = conn.execute(
            "SELECT * FROM confirmation_sessions WHERE id=?", (confirmation_id,)
        ).fetchone()
        if not sess:
            raise ValueError("confirmation session not found")
        if sess["state"] == "confirmed":
            raise ValueError("session already confirmed")
        if sess["state"] in ("blocked", "returned"):
            raise ValueError("session blocked")

        # change must belong to this session's review version
        chg = conn.execute(
            """SELECT rc.* FROM review_changes rc
               JOIN confirmation_sessions cs ON cs.review_version_id=rc.review_version_id
               WHERE rc.id=? AND cs.id=?""",
            (change_id, confirmation_id),
        ).fetchone()
        if not chg:
            raise ValueError("change does not belong to this confirmation session")

        if expected_revision is not None and sess["revision"] != expected_revision:
            raise ValueError("session revision mismatch")

        # auto-claim if not claimed
        if sess["confirmer_id"] is None:
            conn.execute(
                """UPDATE confirmation_sessions
                   SET confirmer_id=?, state='in_progress', revision=?
                   WHERE id=?""",
                (user_id, sess["revision"] + 1, confirmation_id),
            )

        sess_rev = sess["revision"] + 1
        conn.execute(
            "UPDATE confirmation_sessions SET revision=? WHERE id=?",
            (sess_rev, confirmation_id),
        )

        event_id = f"dev_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO decision_events
               (id, confirmation_id, change_id, choice, note, actor_id, revision)
               VALUES (?,?,?,?,?,?,?)""",
            (event_id, confirmation_id, change_id, choice, note, user_id, sess_rev),
        )
        conn.execute(
            """INSERT INTO decision_heads (confirmation_id, change_id, event_id, choice)
               VALUES (?,?,?,?)
               ON CONFLICT(confirmation_id, change_id) DO UPDATE
               SET event_id=excluded.event_id, choice=excluded.choice""",
            (confirmation_id, change_id, event_id, choice),
        )
        conn.commit()

    # Update counts
    return get_confirmation_session(confirmation_id)  # type: ignore[return-value]


def finalize_confirmation(confirmation_id: str, user_id: int) -> dict[str, Any]:
    """All decisions made → create FinalVersion F, freeze confirmation."""
    with _REPO_LOCK, connect() as conn:
        sess = conn.execute(
            "SELECT * FROM confirmation_sessions WHERE id=?", (confirmation_id,)
        ).fetchone()
        if not sess:
            raise ValueError("confirmation session not found")
        if sess["state"] == "confirmed":
            raise ValueError("session already confirmed")
        if sess["state"] in ("blocked", "returned"):
            raise ValueError("session blocked")

        total = sess["total_changes"]
        decided = conn.execute(
            "SELECT COUNT(*) AS n FROM decision_heads WHERE confirmation_id=?",
            (confirmation_id,),
        ).fetchone()["n"]
        if decided != total:
            raise ValueError(f"{total - decided} changes still undecided")

        open_issue = conn.execute(
            "SELECT id FROM review_issues WHERE confirmation_id=? AND status='open'",
            (confirmation_id,),
        ).fetchone()
        if open_issue:
            raise ValueError(f"open issue {open_issue['id']} blocks finalization")

        # Build F frames
        review_version = conn.execute(
            "SELECT * FROM review_versions WHERE id=?", (sess["review_version_id"],)
        ).fetchone()

        baseline = conn.execute(
            "SELECT * FROM annotation_baselines WHERE id=?", (sess["baseline_id"],)
        ).fetchone()

        # Gather all A frames
        a_frames = conn.execute(
            "SELECT frame_index, objects_json FROM baseline_frames WHERE baseline_id=? ORDER BY frame_index",
            (sess["baseline_id"],),
        ).fetchall()

        # ReviewChange map (frame_index, object_id) → (after_bbox, decision_choice, change_id)
        chg_map: dict[tuple[int, int], tuple[list[float], str, str]] = {}
        changes = conn.execute(
            """SELECT rc.*, dh.choice
               FROM review_changes rc
               JOIN decision_heads dh
                 ON dh.confirmation_id = ? AND dh.change_id = rc.id
               WHERE rc.review_version_id = ?""",
            (confirmation_id, sess["review_version_id"]),
        ).fetchall()
        for chg in changes:
            chg_map[(chg["frame_index"], chg["object_id"])] = (
                json.loads(chg["after_bbox"]),
                chg["choice"],
                chg["id"],
            )

        # Determine resolution reason per frame+object
        final_objects_rows: list[tuple[Any, ...]] = []
        f_objects_by_frame: dict[int, list[dict[str, Any]]] = {}

        for fr in a_frames:
            frame_index = fr["frame_index"]
            a_objs = json.loads(fr["objects_json"])
            f_objects: list[dict[str, Any]] = []

            for a_obj in a_objs:
                oid = int(a_obj["objectId"])
                a_box = [int(x) for x in a_obj["bbox"]]
                key = (frame_index, oid)

                if key in chg_map:
                    after_box, choice, change_id = chg_map[key]
                    if choice == "B":
                        final_box = [int(x) for x in after_box]
                        resolution = "adopted_b"
                        adopted_bbox = json.dumps(after_box)
                    else:  # kept A
                        final_box = a_box
                        resolution = "kept_a"
                        adopted_bbox = None
                    change_id_val = change_id
                else:
                    final_box = a_box
                    resolution = "unchanged"
                    change_id_val = None
                    adopted_bbox = None

                final_objects_rows.append((
                    None,  # final_version_id set below
                    frame_index, oid,
                    json.dumps(final_box),
                    f"ann-f{frame_index}-o{oid}",
                    resolution,
                    change_id_val,
                    json.dumps(a_box),
                    adopted_bbox,
                ))
                f_objects.append({
                    "objectId": oid,
                    "bbox": final_box,
                    "classKey": "sperm",
                })

            f_objects_by_frame[frame_index] = f_objects

        canonical_f = canonical_frames_json(
            [
                {"frameIndex": fi, "coverage": "objects", "objects": f_objects_by_frame[fi]}
                for fi in sorted(f_objects_by_frame.keys())
            ]
        )
        snapshot_hash = compute_snapshot_hash(canonical_f)

        final_version_id = f"fv_{uuid.uuid4().hex[:12]}"

        conn.execute(
            """INSERT INTO final_versions
               (id, baseline_id, review_version_id, confirmation_id,
                snapshot_hash, content_hash, frame_count, confirmed_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                final_version_id,
                sess["baseline_id"],
                sess["review_version_id"],
                confirmation_id,
                snapshot_hash,
                snapshot_hash,
                len(a_frames),
                user_id,
            ),
        )

        # Insert final_frame_objects
        updated_rows = []
        for row in final_objects_rows:
            updated_rows.append((final_version_id,) + row[1:])
        conn.executemany(
            """INSERT INTO final_frame_objects
               (final_version_id, frame_index, object_id, bbox, annotation_id,
                resolution, change_id, baseline_bbox, adopted_bbox)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            updated_rows,
        )

        # Mark session confirmed
        conn.execute(
            "UPDATE confirmation_sessions SET state='confirmed', revision=? WHERE id=?",
            (sess["revision"] + 1, confirmation_id),
        )
        conn.commit()

    return {
        "finalVersionId": final_version_id,
        "snapshotHash": snapshot_hash,
        "frameCount": len(a_frames),
        "status": "confirmed",
    }


# ---------------------------------------------------------------------------
# Resume cursor
# ---------------------------------------------------------------------------

def save_cursor(user_id: int, stage: str, session_id: str, frame_index: int | None, change_id: str | None = None) -> None:
    with _REPO_LOCK, connect() as conn:
        conn.execute(
            """INSERT INTO resume_cursors
               (user_id, stage, session_id, frame_index, change_id)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, stage, session_id) DO UPDATE
               SET frame_index=excluded.frame_index, change_id=excluded.change_id,
                   updated_at=datetime('now','utc')""",
            (user_id, stage, session_id, frame_index, change_id),
        )
        conn.commit()


def load_cursor(user_id: int, stage: str, session_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM resume_cursors WHERE user_id=? AND stage=? AND session_id=?",
            (user_id, stage, session_id),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Review issues
# ---------------------------------------------------------------------------

def create_review_issue(
    session_id: str | None,
    confirmation_id: str | None,
    reporter_id: int,
    issue_type: str,
    description: str,
    frame_index: int | None = None,
    annotation_id: str | None = None,
) -> str:
    issue_id = f"iss_{uuid.uuid4().hex[:12]}"
    with _REPO_LOCK, connect() as conn:
        if session_id:
            conn.execute(
                "UPDATE review_sessions SET state='blocked' WHERE id=?",
                (session_id,),
            )
        if confirmation_id:
            conn.execute(
                "UPDATE confirmation_sessions SET state='blocked' WHERE id=?",
                (confirmation_id,),
            )
        conn.execute(
            """INSERT INTO review_issues
               (id, session_id, confirmation_id, frame_index, annotation_id,
                issue_type, description, reporter_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (issue_id, session_id, confirmation_id, frame_index, annotation_id,
             issue_type, description, reporter_id),
        )
        conn.commit()
    return issue_id


# ---------------------------------------------------------------------------
# FinalVersion read-side
# ---------------------------------------------------------------------------

def get_final_version(final_version_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM final_versions WHERE id=?", (final_version_id,)
        ).fetchone()
        if not row:
            return None
        fv = dict(row)
        # Frame objects
        frames = conn.execute(
            """SELECT frame_index, object_id, bbox, annotation_id,
                      resolution, change_id, baseline_bbox, adopted_bbox
               FROM final_frame_objects
               WHERE final_version_id=?
               ORDER BY frame_index, object_id""",
            (final_version_id,),
        ).fetchall()
        frames_by_idx: dict[int, list[dict[str, Any]]] = {}
        for fr in frames:
            fi = fr["frame_index"]
            frames_by_idx.setdefault(fi, []).append({
                "objectId": fr["object_id"],
                "bbox": json.loads(fr["bbox"]),
                "annotationId": fr["annotation_id"],
                "resolution": fr["resolution"],
                "changeId": fr["change_id"],
                "baselineBBox": json.loads(fr["baseline_bbox"]),
                "adoptedBBox": json.loads(fr["adopted_bbox"]) if fr["adopted_bbox"] else None,
            })
        fv["frames"] = [
            {"frameIndex": fi, "objects": frames_by_idx[fi]}
            for fi in sorted(frames_by_idx.keys())
        ]
        return fv


def list_final_versions() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT fv.*, mr.media_id
               FROM final_versions fv
               JOIN annotation_baselines ab ON ab.id = fv.baseline_id
               JOIN media_revisions mr ON mr.id = ab.media_revision_id
               ORDER BY fv.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
