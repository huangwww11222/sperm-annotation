"""Review-only workflow: immutable submissions, draft CAS and atomic completion.

Never writes A or mutable annotation files. All business mutations use one SQLite
transaction, including their idempotency receipt. C continues to consume existing
review_versions/review_changes tables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import uuid
from datetime import datetime, timezone

from contextlib import closing
from .config import MEDIA_STORAGE_DIR, LEGACY_TRACK_DATA_DIR
from .db import connect

log = logging.getLogger("review.workflow")


class ReviewError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def packed(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return "sha256:" + hashlib.sha256(packed(value).encode()).hexdigest()


def migrate(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS review_submission_payloads (
      submission_id TEXT PRIMARY KEY REFERENCES frame_submissions(id),
      patch_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS review_version_frames (
      version_id TEXT NOT NULL REFERENCES review_versions(id),
      frame_index INTEGER NOT NULL,
      submission_id TEXT NOT NULL REFERENCES frame_submissions(id),
      objects_json TEXT NOT NULL,
      PRIMARY KEY(version_id, frame_index)
    );
    CREATE TABLE IF NOT EXISTS review_write_receipts (
      user_id INTEGER NOT NULL REFERENCES users(id), request_key TEXT NOT NULL,
      request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
      created_at TEXT NOT NULL, PRIMARY KEY(user_id, request_key)
    );
    CREATE TABLE IF NOT EXISTS review_bookmarks (
      user_id INTEGER NOT NULL REFERENCES users(id),
      session_id TEXT NOT NULL REFERENCES review_sessions(id),
      frame_index INTEGER NOT NULL, revision INTEGER NOT NULL,
      updated_at TEXT NOT NULL, PRIMARY KEY(user_id, session_id)
    );
    CREATE TABLE IF NOT EXISTS review_withdrawals (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES review_sessions(id),
      actor_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS review_annotation_returns (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES review_sessions(id),
      actor_id INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS review_object_catalog (
      session_id TEXT NOT NULL REFERENCES review_sessions(id), object_id INTEGER NOT NULL,
      class_key TEXT NOT NULL, origin TEXT NOT NULL,
      actor_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
      PRIMARY KEY(session_id, object_id)
    );
    CREATE TABLE IF NOT EXISTS review_object_allocators (
      session_id TEXT PRIMARY KEY REFERENCES review_sessions(id),
      next_object_id INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS review_return_events (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES review_sessions(id),
      confirmation_id TEXT NOT NULL REFERENCES confirmation_sessions(id),
      change_id TEXT NOT NULL REFERENCES review_changes(id), frame_index INTEGER NOT NULL,
      actor_id INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL, created_at TEXT NOT NULL,
      resolved_review_version_id TEXT REFERENCES review_versions(id),
      next_confirmation_id TEXT REFERENCES confirmation_sessions(id)
    );
    CREATE TABLE IF NOT EXISTS confirmation_decision_carries (
      event_id TEXT PRIMARY KEY REFERENCES decision_events(id),
      source_event_id TEXT NOT NULL REFERENCES decision_events(id),
      return_event_id TEXT NOT NULL REFERENCES review_return_events(id)
    );
    """)
    # Only a current submitted legacy row is reconstructable. Never invent an
    # older committed baseline from an already edited draft.
    conn.execute("""INSERT OR IGNORE INTO review_submission_payloads
       SELECT f.submission_id, COALESCE(f.patch_json,'[]') FROM review_frames f
       JOIN frame_submissions s ON s.id=f.submission_id
       AND s.session_id=f.session_id AND s.frame_index=f.frame_index
       WHERE f.state='submitted' """)
    from .concentrated_review import migrate as migrate_concentrated
    migrate_concentrated(conn)
    conn.commit()


def session_row(c, sid):
    r = c.execute(
        """SELECT s.*, a.submitted_by, a.media_revision_id,
        m.media_id,m.width,m.height,m.fps,m.frame_count AS media_frames
        FROM review_sessions s JOIN annotation_baselines a ON a.id=s.baseline_id
        JOIN media_revisions m ON m.id=a.media_revision_id WHERE s.id=?""",
        (sid,),
    ).fetchone()
    if not r:
        raise ReviewError("SESSION_NOT_FOUND", "审查任务不存在", 404)
    return r


def baseline(c, s, fi):
    r = c.execute(
        "SELECT * FROM baseline_frames WHERE baseline_id=? AND frame_index=?",
        (s["baseline_id"], fi),
    ).fetchone()
    if not r:
        raise ReviewError("FRAME_NOT_FOUND", "原始标注帧不存在", 404)
    objects = json.loads(r["objects_json"])
    return [
        {**o, "annotationId": f"{s['baseline_id']}:f{fi}:o{o['objectId']}"}
        for o in objects
    ]


def integrity(c, s):
    n = s["frame_count"]
    rows = c.execute(
        "SELECT frame_index FROM baseline_frames WHERE baseline_id=? ORDER BY frame_index",
        (s["baseline_id"],),
    ).fetchall()
    if n <= 0 or n != s["media_frames"] or [r[0] for r in rows] != list(range(n)):
        return "原始标注未覆盖全部视频帧，请由标注端完成全视频标注后重新送审。"
    frames = c.execute(
        "SELECT * FROM review_frames WHERE session_id=? ORDER BY frame_index",
        (s["id"],),
    ).fetchall()
    if [r["frame_index"] for r in frames] != list(range(n)):
        return "历史任务帧集合不完整，已设为只读。"
    invalid = c.execute(
        """SELECT 1 FROM review_frames f
        LEFT JOIN frame_submissions x ON x.id=f.submission_id AND x.session_id=f.session_id AND x.frame_index=f.frame_index
        LEFT JOIN review_submission_payloads p ON p.submission_id=x.id
        WHERE f.session_id=? AND ((f.submission_id IS NOT NULL AND p.submission_id IS NULL)
          OR (f.state='submitted' AND f.submission_id IS NULL)
          OR (f.state='draft' AND f.submission_id IS NULL AND EXISTS
             (SELECT 1 FROM frame_submissions h WHERE h.session_id=f.session_id AND h.frame_index=f.frame_index)))
        LIMIT 1""",
        (s["id"],),
    ).fetchone()
    if invalid:
        return "历史提交缺少可恢复的快照，已设为只读。"
    return None


def editable(c, s, uid):
    if s["state"] not in ("pending", "in_progress"):
        raise ReviewError("SESSION_READ_ONLY", "该任务已完成或不可编辑")
    if s["reviewer_id"] != uid:
        raise ReviewError("NOT_ASSIGNEE", "请先领取任务；只有当前审查员可以编辑", 403)
    reason = integrity(c, s)
    if reason:
        raise ReviewError("LEGACY_TASK_READ_ONLY", reason)


def object_catalog(c, s):
    """Initialize one immutable A identity directory, then retain every allocated ID.

    Creating a directory is metadata only: it cannot submit a frame or create a
    change. An abandoned new object remains reserved, so stale clients cannot
    silently attach its identity to a different sperm later.
    """
    if not c.execute("SELECT 1 FROM review_object_allocators WHERE session_id=?", (s['id'],)).fetchone():
        identities = {}
        for frame in c.execute("SELECT objects_json FROM baseline_frames WHERE baseline_id=? ORDER BY frame_index", (s['baseline_id'],)):
            for obj in json.loads(frame[0]):
                oid, label = obj['objectId'], obj.get('classKey', 'sperm')
                if oid in identities and identities[oid] != label:
                    raise ReviewError('INVALID_BASELINE', '原标注同一对象的类别不一致，不能补标', 422)
                identities[oid] = label
        c.executemany("INSERT OR IGNORE INTO review_object_catalog VALUES (?,?,?,?,?,?)",
                      [(s['id'], oid, label, 'baseline', None, now()) for oid, label in identities.items()])
        c.execute("INSERT INTO review_object_allocators VALUES (?,?)", (s['id'], max(identities, default=0) + 1))
    return {r['object_id']: dict(objectId=r['object_id'], classKey=r['class_key'], origin=r['origin'])
            for r in c.execute("SELECT * FROM review_object_catalog WHERE session_id=? ORDER BY object_id", (s['id'],))}


def get_objects(sid, uid):
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        s = session_row(c, sid)
        return {'items': list(object_catalog(c, s).values())}


def normalize_patch(objects, patch, width, height, catalog=None):
    a = {o["objectId"]: o for o in objects}
    if len(a) != len(objects):
        raise ReviewError("INVALID_BASELINE", "原标注存在重复对象 ID", 422)
    seen, result = set(), []
    for p in patch:
        if not isinstance(p, dict):
            raise ReviewError('INVALID_PATCH', '标注修改格式无效', 422)
        oid = p.get("objectId")
        op = p.get('op', 'update')
        if type(oid) is not int or oid <= 0 or oid in seen:
            raise ReviewError("UNKNOWN_OBJECT", "对象 ID 必须有效且不能重复", 422)
        if op not in ('update', 'add', 'delete'):
            raise ReviewError('INVALID_OPERATION', '不支持的审查操作', 422)
        seen.add(oid)
        reference = (catalog or a).get(oid)
        if reference is None or (op == 'add') == (oid in a):
            raise ReviewError('UNKNOWN_OBJECT', '补标必须选择视频已有对象或先分配新对象，已有框请使用调整或删除', 422)
        if p.get('classKey') is not None and p['classKey'] != reference.get('classKey', 'sperm'):
            raise ReviewError('CLASS_MISMATCH', '不能更改已有对象类别', 422)
        if op == 'delete':
            if p.get('bbox') is not None:
                raise ReviewError('INVALID_PATCH', '删除操作不能附带替代标注框', 422)
            result.append({'objectId': oid, 'op': 'delete'})
            continue
        if op == 'add' and (catalog is None or not p.get('classKey')):
            raise ReviewError('CLASS_REQUIRED', '补标必须提供目录中的对象类别', 422)
        values = p.get("bbox")
        if not isinstance(values, list) or len(values) != 4 or any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in values):
            raise ReviewError("INVALID_BBOX", "框坐标必须是四个有限数值", 422)
        b = [round(float(v), 3) for v in values]
        if not (0 <= b[0] < b[2] <= width and 0 <= b[1] < b[3] <= height):
            raise ReviewError("INVALID_BBOX", "标注框必须位于图像内且宽高大于零", 422)
        if op == 'add':
            result.append({'objectId': oid, 'op': 'add', 'bbox': b, 'classKey': reference['classKey']})
        elif b != [round(float(v), 3) for v in a[oid]["bbox"]]:
            result.append({"objectId": oid, "bbox": b})
    return sorted(result, key=lambda p: p["objectId"])


def apply_patch(objects, patch):
    by_id = {o['objectId']: dict(o) for o in objects}
    for p in patch:
        oid, op = p['objectId'], p.get('op', 'update')
        if op == 'delete':
            by_id.pop(oid, None)
        elif op == 'add':
            by_id[oid] = dict(objectId=oid, bbox=p['bbox'], classKey=p['classKey'])
        else:
            by_id[oid] = {**by_id[oid], 'bbox': p['bbox']}
    return [by_id[oid] for oid in sorted(by_id)]


def materialize(s, fi, objects, patch):
    return [{**o, 'annotationId': f"{s['baseline_id']}:f{fi}:o{o['objectId']}"}
            for o in apply_patch(objects, patch)]


def box_metrics(a, b):
    if a == b:
        return None
    inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    cx, cy = (b[0] + b[2] - a[0] - a[2]) / 2, (b[1] + b[3] - a[1] - a[3]) / 2
    dw, dh = b[2] - b[0] - a[2] + a[0], b[3] - b[1] - a[3] + a[1]
    pos, size = a[:2] != b[:2], bool(dw or dh)
    return dict(
        iou=inter / union,
        centerShiftPx=math.hypot(cx, cy),
        deltaCenterX=cx,
        deltaCenterY=cy,
        deltaWidth=dw,
        deltaHeight=dh,
        changeType="位置 + 尺寸调整"
        if pos and size
        else "位置调整"
        if pos
        else "尺寸调整",
    )


def progress(c, s):
    rows = c.execute(
        """SELECT f.frame_index,f.state,f.submission_id,p.patch_json FROM review_frames f
       LEFT JOIN review_submission_payloads p ON p.submission_id=f.submission_id WHERE f.session_id=? ORDER BY f.frame_index""",
        (s["id"],),
    ).fetchall()
    submitted = [r for r in rows if r["state"] == "submitted"]
    patches = [json.loads(r["patch_json"] or "[]") for r in submitted]
    return dict(
        submittedFrames=len(submitted),
        unsubmittedFrames=s["frame_count"] - len(submitted),
        draftFrames=sum(r["state"] == "draft" for r in rows),
        modifiedFrames=sum(bool(p) for p in patches),
        modifiedBoxes=sum(len(p) for p in patches),
        percent=round(100 * len(submitted) / s["frame_count"], 1)
        if s["frame_count"]
        else 0,
    )


def media_name(media_id):
    from pathlib import Path

    for root in (MEDIA_STORAGE_DIR, LEGACY_TRACK_DATA_DIR):
        path = root / Path(media_id).name / "media.json"
        if path.is_file():
            try:
                return (
                    json.loads(path.read_text(encoding="utf-8")).get("videoName")
                    or media_id
                )
            except (OSError, ValueError):
                pass
    return media_id


def session_data(c, s, uid):
    p = progress(c, s)
    reason = integrity(c, s)
    cursor = c.execute(
        "SELECT * FROM review_bookmarks WHERE user_id=? AND session_id=?",
        (uid, s["id"]),
    ).fetchone()
    pending = c.execute(
        "SELECT MIN(frame_index) FROM review_frames WHERE session_id=? AND state!='submitted'",
        (s["id"],),
    ).fetchone()[0]
    drafts = [
        r[0]
        for r in c.execute(
            "SELECT frame_index FROM review_frames WHERE session_id=? AND state='draft' ORDER BY frame_index",
            (s["id"],),
        )
    ]
    submitted = [
        r[0]
        for r in c.execute(
            "SELECT frame_index FROM review_frames WHERE session_id=? AND state='submitted' ORDER BY frame_index",
            (s["id"],),
        )
    ]
    rv = c.execute(
        "SELECT id FROM review_versions WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
        (s["id"],),
    ).fetchone()
    active = s["state"] in ("pending", "in_progress") and not reason
    own = s["reviewer_id"] == uid
    can_claim = active and s["reviewer_id"] is None
    returned = c.execute('SELECT * FROM review_annotation_returns WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (s['id'],)).fetchone()
    withdraw_allowed = can_withdraw(c, s, uid)
    withdrawal_reason = None
    if not withdraw_allowed:
        if s['state'] == 'returned_to_annotation':
            withdrawal_reason = '任务已退回人工标注，可修改后重新送审。'
        elif s['state'] == 'withdrawn':
            withdrawal_reason = '任务已撤回。'
        elif s['state'] == 'reviewed':
            withdrawal_reason = '审查已完成，不能撤回；需修改时请在对比确认中退回本帧重审。'
        elif s['submitted_by'] != uid:
            withdrawal_reason = '只有原送审者可以撤回送审。'
        else:
            withdrawal_reason = '审查已被领取或已完成，不能直接撤回；大范围问题请联系当前审查员退回重新标注。'
    from .concentrated_review import info as concentrated_info
    concentrated = concentrated_info(c, s["id"])
    if concentrated and s["state"] == "reviewed":
        withdrawal_reason = "审查已完成，不能撤回；请由审查员在审查页重新审查本帧。"
    return dict(
        workflow="concentrated" if concentrated else "legacy",
        finalVersionId=concentrated["final_version_id"] if concentrated else None,
        id=s["id"],
        baselineId=s["baseline_id"],
        state=s["state"],
        revision=s["revision"],
        reviewerId=s["reviewer_id"],
        submittedBy=s['submitted_by'],
        withdrawalBlockedReason=withdrawal_reason,
        returnAnnotation=dict(reason=returned['reason'], actorId=returned['actor_id'], createdAt=returned['created_at']) if returned else None,
        mediaId=s["media_id"],
        frameCount=s["frame_count"],
        submittedFrames=p["submittedFrames"],
        progress=p,
        media=dict(
            mediaId=s["media_id"],
            name=media_name(s["media_id"]),
            width=s["width"],
            height=s["height"],
            fps=s["fps"],
            frameCount=s["frame_count"],
        ),
        resume=dict(
            lastViewedFrameIndex=cursor["frame_index"] if cursor else None,
            cursorRevision=cursor["revision"] if cursor else 0,
            firstUnsubmittedFrameIndex=pending,
            draftFrameIndexes=drafts,
            submittedFrameIndexes=submitted,
        ),
        permissions=dict(
            canClaim=can_claim,
            canEdit=active and own,
            canComplete=active and own and p["unsubmittedFrames"] == 0,
            canWithdraw=withdraw_allowed,
            canReturnAnnotation=active and own,
        ),
        returnRequests=[dict(frameIndex=r["frame_index"], reason=r["reason"], confirmationId=r["confirmation_id"])
                        for r in c.execute("SELECT * FROM review_return_events WHERE session_id=? AND resolved_review_version_id IS NULL", (s["id"],))],
        readOnlyReason=reason,
        completedReviewVersionId=rv["id"] if rv else None,
    )


def can_withdraw(c, s, uid):
    # Claiming, saving even a no-change draft, or a successful submission is a
    # start of review. A later discard cannot erase that fact.
    return (s["submitted_by"] == uid and s["state"] == "pending"
            and s["reviewer_id"] is None
            and not c.execute("SELECT 1 FROM review_frames WHERE session_id=? AND frame_revision>0 LIMIT 1", (s["id"],)).fetchone())


def submission_status(media_id, uid):
    with closing(connect()) as c:
        ids = [r[0] for r in c.execute("""SELECT s.id FROM review_sessions s
            JOIN annotation_baselines a ON a.id=s.baseline_id
            JOIN media_revisions m ON m.id=a.media_revision_id
            WHERE m.media_id=? AND s.state NOT IN ('withdrawn','returned_to_annotation') ORDER BY s.rowid DESC""", (media_id,))]
        history_ids = [r[0] for r in c.execute('''SELECT s.id FROM review_sessions s
            JOIN annotation_baselines a ON a.id=s.baseline_id
            JOIN media_revisions m ON m.id=a.media_revision_id
            WHERE m.media_id=? AND s.state IN ('withdrawn','returned_to_annotation') ORDER BY s.rowid DESC''', (media_id,))]
        return {"items": [session_data(c, session_row(c, sid), uid) for sid in ids],
                'history': [session_data(c, session_row(c, sid), uid) for sid in history_ids]}


def get_session(sid, uid):
    with closing(connect()) as c, c:
        return session_data(c, session_row(c, sid), uid)


def list_sessions(uid):
    with closing(connect()) as c, c:
        ids = [
            r[0]
            for r in c.execute(
                "SELECT id FROM review_sessions ORDER BY created_at DESC,id DESC"
            )
        ]
        return [session_data(c, session_row(c, sid), uid) for sid in ids]


def frame_data(c, s, fi, uid):
    f = c.execute(
        "SELECT * FROM review_frames WHERE session_id=? AND frame_index=?",
        (s["id"], fi),
    ).fetchone()
    if not f:
        raise ReviewError("FRAME_NOT_FOUND", "帧号超出当前任务", 404)
    a = baseline(c, s, fi)
    sub = (
        c.execute(
            """SELECT x.*,p.patch_json FROM frame_submissions x JOIN review_submission_payloads p ON p.submission_id=x.id
        WHERE x.id=?""",
            (f["submission_id"],),
        ).fetchone()
        if f["submission_id"]
        else None
    )
    committed = json.loads(sub["patch_json"]) if sub else []
    draft = json.loads(f["patch_json"] or "[]") if f["state"] == "draft" else None
    b = materialize(s, fi, a, draft if draft is not None else committed)
    # Completed sessions read immutable version frames when available.
    if s["state"] == "reviewed":
        frozen = c.execute(
            """SELECT vf.objects_json FROM review_version_frames vf JOIN review_versions v ON v.id=vf.version_id
            WHERE v.session_id=? AND vf.frame_index=? ORDER BY v.rowid DESC LIMIT 1""",
            (s["id"], fi),
        ).fetchone()
        if frozen:
            b = json.loads(frozen[0])
    a_by_id, b_by_id = {o['objectId']: o for o in a}, {o['objectId']: o for o in b}
    metrics = [box_metrics(a_by_id[o['objectId']]['bbox'], o['bbox']) if o['objectId'] in a_by_id else None for o in b]
    changed = [m for m in metrics if m]
    added = len(b_by_id.keys() - a_by_id.keys())
    deleted = len(a_by_id.keys() - b_by_id.keys())
    permissions = session_data(c, s, uid)["permissions"]
    return dict(
        sessionId=s["id"],
        frameIndex=fi,
        frameRevision=f["frame_revision"],
        state=f["state"],
        baselineObjects=a,
        effectiveObjects=[{**o, "metrics": m} for o, m in zip(b, metrics)],
        patch=draft if draft is not None else committed,
        hasDraft=draft is not None,
        lastSubmission=dict(
            id=sub["id"], patch=committed, submittedAt=sub["submitted_at"]
        )
        if sub
        else None,
        metrics=dict(
            totalBoxes=len(b),
            modifiedBoxes=len(changed) + added + deleted,
            updatedBoxes=len(changed), addedBoxes=added, deletedBoxes=deleted,
            unmodifiedBoxes=len(b) - len(changed) - added,
            meanModifiedIou=sum(m["iou"] for m in changed) / len(changed)
            if changed
            else None,
            maxCenterShiftPx=max(m["centerShiftPx"] for m in changed)
            if changed
            else None,
        ),
        permissions=dict(
            canEdit=permissions["canEdit"],
            canSubmit=permissions["canEdit"] and f["state"] != "submitted",
            canDiscard=permissions["canEdit"] and draft is not None,
        ),
    )


def get_frame(sid, fi, uid):
    with closing(connect()) as c, c:
        return frame_data(c, session_row(c, sid), fi, uid)


def require_contract(c, s, action, fi, body, contract):
    if str(contract) == '3':
        return
    structural = action in ('add_object', 'return_annotation')
    if action in ('draft', 'submit', 'discard', 'finish'):
        payloads = [body.get('patch', [])]
        query = 'SELECT patch_json,submission_id FROM review_frames WHERE session_id=?'
        args = [s['id']]
        if action != 'finish':
            query += ' AND frame_index=?'; args.append(fi)
        for row in c.execute(query, args):
            payloads.append(json.loads(row['patch_json'] or '[]'))
            if row['submission_id']:
                committed = c.execute('SELECT patch_json FROM review_submission_payloads WHERE submission_id=?', (row['submission_id'],)).fetchone()
                if committed:
                    payloads.append(json.loads(committed[0]))
        structural = structural or any(p.get('op') in ('add', 'delete') for patch in payloads for p in patch)
    if structural:
        raise ReviewError('CONTRACT_UPGRADE_REQUIRED', '此任务包含补标或删除，请刷新页面使用新版审查界面', 428)


def write(action, sid, uid, key, body, fi=None, contract='3'):
    """Replay before CAS: a lost success response can be retried safely."""
    h = digest([action, sid, fi, body])
    try:
        with closing(connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            prior = c.execute(
                "SELECT * FROM review_write_receipts WHERE user_id=? AND request_key=?",
                (uid, key),
            ).fetchone()
            if prior:
                if prior["request_hash"] != h:
                    raise ReviewError(
                        "IDEMPOTENCY_KEY_REUSED", "重试标识不能用于不同操作"
                    )
                log.info(
                    "review.replay action=%s session=%s frame=%s actor=%s key=%s",
                    action,
                    sid,
                    fi,
                    uid,
                    key,
                )
                return json.loads(prior["response_json"])
            s = session_row(c, sid)
            require_contract(c, s, action, fi, body, contract)
            if action == "withdraw":
                if body["expectedSessionRevision"] != s["revision"]:
                    raise ReviewError("SESSION_REVISION_CONFLICT", "任务已更新，请刷新后撤回")
                if s["submitted_by"] != uid:
                    raise ReviewError("NOT_SUBMITTER", "只有送审者可以撤回", 403)
                if not can_withdraw(c, s, uid):
                    raise ReviewError("REVIEW_ALREADY_STARTED", "审查已开始，无法撤回送审")
                c.execute("INSERT INTO review_withdrawals VALUES (?,?,?,?)", ("withdraw_" + uuid.uuid4().hex, sid, uid, now()))
                c.execute("UPDATE review_sessions SET state='withdrawn',revision=revision+1 WHERE id=?", (sid,))
                result = {"session": session_data(c, session_row(c, sid), uid)}
            elif action == "reopen_review":
                from .concentrated_review import reopen
                result = reopen(c, s, uid, body)
            elif action == "claim":
                if s["reviewer_id"] not in (None, uid):
                    raise ReviewError(
                        "SESSION_ALREADY_CLAIMED", "任务已由其他审查员领取"
                    )
                if s["state"] not in ("pending", "in_progress"):
                    raise ReviewError("SESSION_READ_ONLY", "任务已完成")
                if integrity(c, s):
                    raise ReviewError("LEGACY_TASK_READ_ONLY", integrity(c, s))
                if s["reviewer_id"] is None:
                    c.execute(
                        "UPDATE review_sessions SET reviewer_id=?, revision=revision+1 WHERE id=?",
                        (uid, sid),
                    )
                result = {"session": session_data(c, session_row(c, sid), uid)}
            elif action == "cursor":
                if fi is None or fi < 0 or fi >= s["frame_count"]:
                    raise ReviewError("INVALID_FRAME_INDEX", "帧号无效", 422)
                r = c.execute(
                    "SELECT revision FROM review_bookmarks WHERE user_id=? AND session_id=?",
                    (uid, sid),
                ).fetchone()
                rev = r[0] if r else 0
                if body["expectedCursorRevision"] != rev:
                    raise ReviewError(
                        "CURSOR_REVISION_CONFLICT", "浏览位置已在其他窗口更新"
                    )
                c.execute(
                    """INSERT INTO review_bookmarks VALUES (?,?,?,?,?) ON CONFLICT(user_id,session_id)
                    DO UPDATE SET frame_index=excluded.frame_index,revision=excluded.revision,updated_at=excluded.updated_at""",
                    (uid, sid, fi, rev + 1, now()),
                )
                result = {"revision": rev + 1, "frameIndex": fi}
            else:
                editable(c, s, uid)
                if action in ('add_object', 'return_annotation'):
                    if body.get('expectedSessionRevision') != s['revision']:
                        raise ReviewError('SESSION_REVISION_CONFLICT', '任务已更新，请刷新后重试')
                    if action == 'add_object':
                        catalog = object_catalog(c, s)
                        classes = {obj['classKey'] for obj in catalog.values()} or {'rare sperm'}
                        category = body.get('classKey')
                        if category is None:
                            if len(classes) != 1:
                                raise ReviewError('CLASS_REQUIRED', '该视频包含多个类别，请先选择新增对象的类别', 422)
                            category = next(iter(classes))
                        if not isinstance(category, str) or category not in classes:
                            raise ReviewError('INVALID_CLASS', '只能选择本视频已有类别，不能新增或改写类别', 422)
                        oid = c.execute('SELECT next_object_id FROM review_object_allocators WHERE session_id=?', (sid,)).fetchone()[0]
                        c.execute('UPDATE review_object_allocators SET next_object_id=next_object_id+1 WHERE session_id=?', (sid,))
                        c.execute('INSERT INTO review_object_catalog VALUES (?,?,?,?,?,?)', (sid, oid, category, 'review', uid, now()))
                        c.execute('UPDATE review_sessions SET revision=revision+1 WHERE id=?', (sid,))
                        result = {'session': session_data(c, session_row(c, sid), uid), 'object': dict(objectId=oid, classKey=category, origin='review')}
                    else:
                        reason = body.get('reason')
                        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 2000:
                            raise ReviewError('RETURN_REASON_REQUIRED', '请填写不超过2000字的退回原因', 422)
                        c.execute('INSERT INTO review_annotation_returns VALUES (?,?,?,?,?)', ('rta_' + uuid.uuid4().hex, sid, uid, reason.strip(), now()))
                        c.execute("UPDATE review_sessions SET state='returned_to_annotation',revision=revision+1 WHERE id=?", (sid,))
                        result = {'session': session_data(c, session_row(c, sid), uid)}
                elif action == "finish":
                    if body["expectedSessionRevision"] != s["revision"]:
                        raise ReviewError(
                            "SESSION_REVISION_CONFLICT", "审查任务已更新，请刷新后完成"
                        )
                    result = finish(c, s, uid)
                else:
                    f = c.execute(
                        "SELECT * FROM review_frames WHERE session_id=? AND frame_index=?",
                        (sid, fi),
                    ).fetchone()
                    if not f:
                        raise ReviewError("FRAME_NOT_FOUND", "帧不存在", 404)
                    if body["expectedFrameRevision"] != f["frame_revision"]:
                        raise ReviewError(
                            "FRAME_REVISION_CONFLICT",
                            "本帧已在其他窗口更新；已保留您的本地编辑，请先处理冲突",
                        )
                    a = baseline(c, s, fi)
                    prior_payload = c.execute(
                        "SELECT patch_json FROM review_submission_payloads WHERE submission_id=?",
                        (f["submission_id"],),
                    ).fetchone()
                    committed = json.loads(prior_payload[0]) if prior_payload else []
                    if action == "discard":
                        patch = committed
                        new_state = "submitted" if f["submission_id"] else "unreviewed"
                    else:
                        patch = normalize_patch(
                            a, body["patch"], s["width"], s["height"], object_catalog(c, s)
                        )
                        new_state = (
                            "submitted"
                            if action == "submit"
                            else ("submitted" if f["submission_id"] else "unreviewed")
                            if patch == committed
                            else "draft"
                        )
                    recheck = c.execute("SELECT 1 FROM review_return_events WHERE session_id=? AND frame_index=? AND resolved_review_version_id IS NULL", (sid, fi)).fetchone()
                    direct_recheck = c.execute("SELECT 1 FROM concentrated_reopens WHERE session_id=? AND frame_index=? AND invalidated_submission_id=?", (sid, fi, f["submission_id"])).fetchone()
                    if (recheck or direct_recheck) and action != "submit" and f["state"] != "submitted":
                        # Restoring prior geometry still requires explicit submission.
                        new_state = "draft" if patch != committed else "unreviewed"
                    sub_id = f["submission_id"]
                    if action == "submit" and not (
                        f["state"] == "submitted" and patch == committed
                    ):
                        sub_id = "sub_" + uuid.uuid4().hex
                        c.execute(
                            """INSERT INTO frame_submissions(id,session_id,frame_index,revision,net_change_count,reviewer_id)
                            VALUES (?,?,?,?,?,?)""",
                            (sub_id, sid, fi, f["frame_revision"] + 1, len(patch), uid),
                        )
                        c.execute(
                            "INSERT INTO review_submission_payloads VALUES (?,?)",
                            (sub_id, packed(patch)),
                        )
                    changed = (
                        new_state != f["state"]
                        or patch != json.loads(f["patch_json"] or "[]")
                        or sub_id != f["submission_id"]
                    )
                    if changed:
                        c.execute(
                            """UPDATE review_frames SET state=?,patch_json=?,submission_id=?,frame_revision=frame_revision+1
                            WHERE session_id=? AND frame_index=?""",
                            (new_state, packed(patch), sub_id, sid, fi),
                        )
                        c.execute(
                            "UPDATE review_sessions SET revision=revision+1 WHERE id=?",
                            (sid,),
                        )
                    if action == "submit":
                        c.execute(
                            "UPDATE review_sessions SET state='in_progress' WHERE id=?",
                            (sid,),
                        )
                    s = session_row(c, sid)
                    pending = [
                        r[0]
                        for r in c.execute(
                            "SELECT frame_index FROM review_frames WHERE session_id=? AND state!='submitted' ORDER BY frame_index",
                            (sid,),
                        )
                    ]
                    nxt = next(
                        (i for i in pending if i > fi), pending[0] if pending else None
                    )
                    result = {
                        "frame": frame_data(c, s, fi, uid),
                        "session": session_data(c, s, uid),
                        "nextUnsubmittedFrameIndex": nxt,
                        "savedAt": now(),
                    }
            c.execute(
                "INSERT INTO review_write_receipts VALUES (?,?,?,?,?)",
                (uid, key, h, packed(result), now()),
            )
            c.commit()
        log.info(
            "review.committed action=%s session=%s frame=%s actor=%s key=%s",
            action,
            sid,
            fi,
            uid,
            key,
        )
        return result
    except ReviewError as e:
        log.warning(
            "review.rejected action=%s session=%s frame=%s actor=%s code=%s key=%s",
            action,
            sid,
            fi,
            uid,
            e.code,
            key,
        )
        raise
    except sqlite3.Error:
        log.exception(
            "review.storage_failed action=%s session=%s frame=%s actor=%s key=%s",
            action,
            sid,
            fi,
            uid,
            key,
        )
        raise ReviewError(
            "STORAGE_UNAVAILABLE", "保存失败，编辑已保留，请使用原请求重试", 503
        )


def finish(c, s, uid):
    sid = s["id"]
    fs = c.execute(
        "SELECT * FROM review_frames WHERE session_id=? ORDER BY frame_index", (sid,)
    ).fetchall()
    if (
        not fs
        or len(fs) != s["frame_count"]
        or any(f["state"] != "submitted" or not f["submission_id"] for f in fs)
    ):
        raise ReviewError("FRAMES_NOT_SUBMITTED", "仍有未提交帧，不能完成视频审查")
    vid = "rv_" + uuid.uuid4().hex
    frames = []
    changes = []
    for f in fs:
        a = baseline(c, s, f["frame_index"])
        patch = json.loads(
            c.execute(
                "SELECT patch_json FROM review_submission_payloads WHERE submission_id=?",
                (f["submission_id"],),
            ).fetchone()[0]
        )
        # Validate the frozen payload again rather than trusting an old draft or
        # array position. Removed and added objects have no counterpart geometry.
        normalized = normalize_patch(a, patch, s['width'], s['height'], object_catalog(c, s))
        b = materialize(s, f['frame_index'], a, normalized)
        frames.append({"frameIndex": f["frame_index"], "objects": b})
        before_ids, after_ids = {o['objectId']: o for o in a}, {o['objectId']: o for o in b}
        for oid in sorted(before_ids.keys() | after_ids.keys()):
            before, after = before_ids.get(oid), after_ids.get(oid)
            aa, bb = before['bbox'] if before else None, after['bbox'] if after else None
            m = box_metrics(aa, bb) if aa is not None and bb is not None else None
            if aa != bb:
                changes.append(
                    (
                        "chg_" + uuid.uuid4().hex,
                        vid,
                        f["frame_index"],
                        oid,
                        (before or after)["annotationId"],
                        packed(aa),
                        packed(bb),
                        packed([(aa[0] + aa[2]) / 2, (aa[1] + aa[3]) / 2]) if aa else None,
                        packed([(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]) if bb else None,
                        m["iou"] if m else None,
                        m["centerShiftPx"] if m else None,
                        bb[0] - aa[0] if m else None,
                        bb[1] - aa[1] if m else None,
                        m["deltaWidth"] if m else None,
                        m["deltaHeight"] if m else None,
                        uid,
                        f["submission_id"],
                    )
                )
    sh = digest(frames)
    c.execute(
        "INSERT INTO review_versions(id,session_id,baseline_id,snapshot_hash) VALUES (?,?,?,?)",
        (vid, sid, s["baseline_id"], sh),
    )
    c.executemany(
        "INSERT INTO review_version_frames VALUES (?,?,?,?)",
        [
            (vid, f["frame_index"], f["submission_id"], packed(b["objects"]))
            for f, b in zip(fs, frames)
        ],
    )
    c.executemany(
        """INSERT INTO review_changes(id,review_version_id,frame_index,object_id,annotation_id,before_bbox,after_bbox,
        before_center,after_center,iou,center_shift,dx,dy,dw,dh,reviewer_id,frame_submission_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        changes,
    )
    cid = "cs_" + uuid.uuid4().hex
    c.execute(
        "INSERT INTO confirmation_sessions(id,review_version_id,baseline_id,total_changes) VALUES (?,?,?,?)",
        (cid, vid, s["baseline_id"], len(changes)),
    )
    c.execute(
        "UPDATE review_sessions SET state='reviewed',revision=revision+1,changed_count=? WHERE id=?",
        (len({x[2] for x in changes}), sid),
    )
    from .concentrated_review import info as concentrated_info, finalize as accept_review
    if concentrated_info(c, sid):
        accept_review(c, s, cid, uid)
    else:
        carry_confirmations(c, sid, vid, cid)
    return dict(
        session=session_data(c, session_row(c, sid), uid),
        reviewVersionId=vid,
        confirmationSessionId=cid,
        totalChanges=len(changes),
        snapshotHash=sh,
    )


def carry_confirmations(c, sid, vid, cid):
    requests = c.execute("SELECT * FROM review_return_events WHERE session_id=? AND resolved_review_version_id IS NULL ORDER BY rowid", (sid,)).fetchall()
    if not requests:
        return
    from . import confirmation_workflow as confirmation
    previous = confirmation.session_row(c, requests[-1]["confirmation_id"])
    returned_frames = {r["frame_index"] for r in requests}
    c.execute("UPDATE confirmation_sessions SET confirmer_id=? WHERE id=?", (previous["confirmer_id"], cid))
    new_session = confirmation.session_row(c, cid)
    candidates = c.execute("""SELECT n.id, e.id AS source_event_id, e.actor_id,h.choice
        FROM review_changes n JOIN review_changes old ON old.review_version_id=?
        AND old.frame_index=n.frame_index AND old.object_id=n.object_id
        AND old.before_bbox=n.before_bbox AND old.after_bbox=n.after_bbox
        JOIN decision_heads h ON h.change_id=old.id AND h.confirmation_id=?
        JOIN decision_events e ON e.id=h.event_id WHERE n.review_version_id=?""",
        (previous["review_version_id"], previous["id"], vid)).fetchall()
    for row in candidates:
        fi = c.execute("SELECT frame_index FROM review_changes WHERE id=?", (row["id"],)).fetchone()[0]
        if fi in returned_frames:
            continue
        event = confirmation.append_decision(c, new_session, row["id"], row["choice"], row["actor_id"], "carried:" + row["source_event_id"])
        c.execute("INSERT INTO confirmation_decision_carries VALUES (?,?,?)", (event, row["source_event_id"], requests[-1]["id"]))
    c.execute("UPDATE review_return_events SET resolved_review_version_id=?,next_confirmation_id=? WHERE session_id=? AND resolved_review_version_id IS NULL", (vid, cid, sid))


def ensure_session(bid, uid, key):
    h = digest(["ensure-session", bid])
    with closing(connect()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute(
            "SELECT * FROM review_write_receipts WHERE user_id=? AND request_key=?",
            (uid, key),
        ).fetchone()
        if old:
            if old["request_hash"] != h:
                raise ReviewError("IDEMPOTENCY_KEY_REUSED", "重试标识不能用于不同操作")
            return json.loads(old["response_json"])
        a = c.execute(
            "SELECT a.*,m.frame_count AS media_frames FROM annotation_baselines a JOIN media_revisions m ON m.id=a.media_revision_id WHERE a.id=?",
            (bid,),
        ).fetchone()
        if not a:
            raise ReviewError("BASELINE_NOT_FOUND", "原始标注版本不存在", 404)
        frames = [
            r[0]
            for r in c.execute(
                "SELECT frame_index FROM baseline_frames WHERE baseline_id=? ORDER BY frame_index",
                (bid,),
            )
        ]
        if (
            not frames
            or len(frames) != a["frame_count"]
            or frames != list(range(a["media_frames"]))
        ):
            raise ReviewError(
                "INCOMPLETE_ANNOTATION",
                "原始标注未覆盖全部视频帧，不能创建审查任务",
                422,
            )
        existing = c.execute(
            "SELECT id FROM review_sessions WHERE baseline_id=? ORDER BY created_at LIMIT 1",
            (bid,),
        ).fetchone()
        sid = existing[0] if existing else "rs_" + uuid.uuid4().hex
        if not existing:
            c.execute(
                "INSERT INTO review_sessions(id,baseline_id,frame_count) VALUES (?,?,?)",
                (sid, bid, len(frames)),
            )
            c.executemany(
                "INSERT INTO review_frames(session_id,frame_index) VALUES (?,?)",
                [(sid, i) for i in frames],
            )
        result = {"session": session_data(c, session_row(c, sid), uid)}
        c.execute(
            "INSERT INTO review_write_receipts VALUES (?,?,?,?,?)",
            (uid, key, h, packed(result), now()),
        )
        c.commit()
        return result
