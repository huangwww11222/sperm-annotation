"""C workflow: audited choices, reversible decisions, immutable final versions.

B is read exclusively from frozen version frames. Every write, including its
idempotency receipt, commits in one SQLite transaction with optimistic versions.
"""

import json
import logging
import sqlite3
import uuid
from contextlib import closing

from .db import connect
from .review_workflow import ReviewError, box_metrics, digest, media_name, now, packed

log = logging.getLogger("review.confirmation")


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS confirmation_change_versions (
      confirmation_id TEXT NOT NULL REFERENCES confirmation_sessions(id),
      change_id TEXT NOT NULL REFERENCES review_changes(id), revision INTEGER NOT NULL,
      PRIMARY KEY(confirmation_id,change_id)
    );
    CREATE TABLE IF NOT EXISTS confirmation_actions (
      id TEXT PRIMARY KEY, confirmation_id TEXT NOT NULL REFERENCES confirmation_sessions(id),
      change_id TEXT NOT NULL REFERENCES review_changes(id), actor_id INTEGER NOT NULL REFERENCES users(id),
      before_choice TEXT, after_choice TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES decision_events(id),
      undone_by TEXT REFERENCES decision_events(id), created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_confirmation_action ON confirmation_actions(confirmation_id,created_at);
    CREATE TABLE IF NOT EXISTS confirmation_bookmarks (
      user_id INTEGER NOT NULL REFERENCES users(id), confirmation_id TEXT NOT NULL REFERENCES confirmation_sessions(id),
      change_id TEXT REFERENCES review_changes(id), revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(user_id,confirmation_id)
    );
    CREATE TABLE IF NOT EXISTS final_version_frames (
      final_version_id TEXT NOT NULL REFERENCES final_versions(id), frame_index INTEGER NOT NULL,
      objects_json TEXT NOT NULL, PRIMARY KEY(final_version_id,frame_index)
    );
    CREATE TABLE IF NOT EXISTS confirmation_final_records (
      final_version_id TEXT PRIMARY KEY REFERENCES final_versions(id), decision_snapshot_json TEXT NOT NULL,
      previous_final_version_id TEXT REFERENCES final_versions(id)
    );
    CREATE TABLE IF NOT EXISTS confirmation_reopen_events (
      id TEXT PRIMARY KEY, confirmation_id TEXT NOT NULL REFERENCES confirmation_sessions(id),
      final_version_id TEXT NOT NULL REFERENCES final_versions(id), actor_id INTEGER NOT NULL REFERENCES users(id),
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_changes_navigation ON review_changes(review_version_id,frame_index,object_id,id);
    """)
    c.commit()


def session_row(c, sid):
    s = c.execute(
        """SELECT cs.*,m.media_id,m.width,m.height,m.fps,m.frame_count,a.frame_count AS baseline_count,
       rv.baseline_id AS review_baseline_id,rv.session_id AS review_session_id
       FROM confirmation_sessions cs JOIN annotation_baselines a ON a.id=cs.baseline_id
       JOIN media_revisions m ON m.id=a.media_revision_id JOIN review_versions rv ON rv.id=cs.review_version_id
       WHERE cs.id=?""",
        (sid,),
    ).fetchone()
    if not s:
        raise ReviewError("CONFIRMATION_NOT_FOUND", "对比确认任务不存在", 404)
    return s


def integrity(c, s):
    n = s["frame_count"]
    if (
        n <= 0
        or s["baseline_count"] != n
        or s["baseline_id"] != s["review_baseline_id"]
    ):
        return "原始标注版本与视频不一致，任务仅可查看。"
    for table, column, value in [
        ("baseline_frames", "baseline_id", s["baseline_id"]),
        ("review_version_frames", "version_id", s["review_version_id"]),
    ]:
        row = c.execute(
            f"SELECT COUNT(*),MIN(frame_index),MAX(frame_index) FROM {table} WHERE {column}=?",
            (value,),
        ).fetchone()
        if row[0] != n or row[1] != 0 or row[2] != n - 1:
            return (
                "历史任务缺少完整的固定 A/B 快照，不能生成可靠的最终版本。请重新送审。"
            )
    count = c.execute(
        "SELECT COUNT(*) FROM review_changes WHERE review_version_id=?",
        (s["review_version_id"],),
    ).fetchone()[0]
    if count != s["total_changes"]:
        return "修改项数量与固定 B 版本不一致，任务仅可查看。"
    if c.execute(
        "SELECT 1 FROM review_issues WHERE confirmation_id=? AND status='open' LIMIT 1",
        (s["id"],),
    ).fetchone():
        return "此历史任务仍有未解决的问题记录，任务仅可查看。"
    return None


def change_metrics(a, b):
    metrics = box_metrics(a, b)
    if metrics:
        # Prototype classifies position by the center, so a concentric resize is size-only.
        moved = (
            abs(metrics["deltaCenterX"]) > 1e-9 or abs(metrics["deltaCenterY"]) > 1e-9
        )
        resized = (
            abs(metrics["deltaWidth"]) > 1e-9 or abs(metrics["deltaHeight"]) > 1e-9
        )
        metrics["changeType"] = (
            "位置 + 尺寸调整"
            if moved and resized
            else "位置调整"
            if moved
            else "尺寸调整"
        )
    return metrics


def changes(c, s, change_id=None):
    rows = c.execute(
        """SELECT x.*,h.choice,h.event_id
       FROM review_changes x LEFT JOIN decision_heads h ON h.change_id=x.id AND h.confirmation_id=?
       WHERE x.review_version_id=?""" + (' AND x.id=?' if change_id is not None else '') + ' ORDER BY x.frame_index,x.object_id,x.id',
        (s["id"], s["review_version_id"], *([change_id] if change_id is not None else [])),
    ).fetchall()
    revisions = {
        r["change_id"]: r["revision"]
        for r in c.execute(
            "SELECT * FROM confirmation_change_versions WHERE confirmation_id=?" + (' AND change_id=?' if change_id is not None else ''),
            (s["id"], *([change_id] if change_id is not None else [])),
        )
    }
    result = []
    for r in rows:
        a, b = json.loads(r["before_bbox"]), json.loads(r["after_bbox"])
        result.append(
            dict(
                changeId=r["id"],
                frameIndex=r["frame_index"],
                objectId=r["object_id"],
                annotationId=r["annotation_id"],
                beforeBbox=a,
                afterBbox=b,
                metrics=change_metrics(a, b),
                decisionRevision=revisions.get(r["id"], 0),
                decision=dict(choice=r["choice"], eventId=r["event_id"])
                if r["choice"] in ("A", "B")
                else None,
            )
        )
    return result


def last_action(c, s):
    return c.execute(
        "SELECT * FROM confirmation_actions WHERE confirmation_id=? AND undone_by IS NULL ORDER BY rowid DESC LIMIT 1",
        (s["id"],),
    ).fetchone()


def session_data(c, s, uid):
    total, kept, adopted = c.execute('''SELECT COUNT(*),COALESCE(SUM(h.choice='A'),0),COALESCE(SUM(h.choice='B'),0)
        FROM review_changes x LEFT JOIN decision_heads h ON h.confirmation_id=? AND h.change_id=x.id
        WHERE x.review_version_id=?''', (s['id'],s['review_version_id'])).fetchone()
    decided = kept + adopted
    first_pending = next_pending(c, s)
    reason = integrity(c, s)
    active = s["state"] in ("pending", "in_progress") and not reason
    own = s["confirmer_id"] == uid
    bookmark = c.execute(
        "SELECT * FROM confirmation_bookmarks WHERE user_id=? AND confirmation_id=?",
        (uid, s["id"]),
    ).fetchone()
    fv = c.execute(
        "SELECT * FROM final_versions WHERE confirmation_id=? ORDER BY rowid DESC LIMIT 1",
        (s["id"],),
    ).fetchone()
    action = last_action(c, s)
    returned = c.execute("SELECT * FROM review_return_events WHERE confirmation_id=? ORDER BY rowid DESC LIMIT 1", (s["id"],)).fetchone()
    return dict(
        id=s["id"],
        baselineId=s["baseline_id"],
        reviewVersionId=s["review_version_id"],
        state=s["state"],
        revision=s["revision"],
        confirmerId=s["confirmer_id"],
        media=dict(
            mediaId=s["media_id"],
            name=media_name(s["media_id"]),
            width=s["width"],
            height=s["height"],
            fps=s["fps"],
            frameCount=s["frame_count"],
        ),
        progress=dict(
            totalChanges=total,
            decided=decided,
            pending=total - decided,
            keptA=kept,
            adoptedB=adopted,
            percent=round(decided / total * 100, 1) if total else 100,
        ),
        permissions=dict(
            canClaim=active and s["confirmer_id"] is None,
            canEdit=active and own,
            canUndo=active and own and action is not None,
            canComplete=active and own and decided == total,
            canReopen=s["state"] == "confirmed" and own and not reason,
            canExport=fv is not None and s["state"] == "confirmed",
            canReturn=active and own and bool(total),
        ),
        returnedReview=dict(frameIndex=returned["frame_index"], reason=returned["reason"],
                           reviewSessionId=returned["session_id"], nextConfirmationId=returned["next_confirmation_id"]) if returned else None,
        resume=dict(
            lastViewedChangeId=bookmark["change_id"] if bookmark else None,
            cursorRevision=bookmark["revision"] if bookmark else 0,
            firstPendingChangeId=first_pending,
        ),
        undo=dict(actionId=action["id"], changeId=action["change_id"])
        if action
        else None,
        finalVersionId=fv["id"] if fv else None,
        readOnlyReason=reason,
    )


def get_session(sid, uid):
    with closing(connect()) as c:
        return session_data(c, session_row(c, sid), uid)


def list_sessions(uid):
    with closing(connect()) as c:
        return [
            session_data(c, session_row(c, r[0]), uid)
            for r in c.execute(
                "SELECT id FROM confirmation_sessions ORDER BY created_at DESC,id DESC"
            )
        ]


def list_changes(sid, uid):
    with closing(connect()) as c:
        return changes(c, session_row(c, sid))


def frame_context(c, s, fi):
    a = c.execute(
        "SELECT objects_json FROM baseline_frames WHERE baseline_id=? AND frame_index=?",
        (s["baseline_id"], fi),
    ).fetchone()
    b = c.execute(
        "SELECT objects_json FROM review_version_frames WHERE version_id=? AND frame_index=?",
        (s["review_version_id"], fi),
    ).fetchone()
    if not a or not b:
        raise ReviewError(
            "FROZEN_FRAME_UNAVAILABLE", "该帧缺少完整固定版本，无法对比", 409
        )
    return dict(
        frameIndex=fi, baselineObjects=json.loads(a[0]), reviewObjects=json.loads(b[0])
    )


def get_frame(sid, fi, uid):
    with closing(connect()) as c:
        return frame_context(c, session_row(c, sid), fi)


def editable(c, s, uid, completed=False):
    if s["confirmer_id"] != uid:
        raise ReviewError(
            "NOT_CONFIRMATION_ASSIGNEE",
            "任务由已领取的确认员编辑，请先领取未分配的任务",
            403,
        )
    if s["state"] not in (("confirmed",) if completed else ("pending", "in_progress")):
        raise ReviewError(
            "CONFIRMATION_READ_ONLY", "任务已完成或不可编辑，请先选择重新确认"
        )
    reason = integrity(c, s)
    if reason:
        raise ReviewError("INVALID_FROZEN_VERSION", reason)


def append_decision(c, s, change_id, choice, uid, note=None):
    ver = c.execute(
        "SELECT revision FROM confirmation_change_versions WHERE confirmation_id=? AND change_id=?",
        (s["id"], change_id),
    ).fetchone()
    revision = (ver[0] if ver else 0) + 1
    event = "decision_" + uuid.uuid4().hex
    c.execute(
        "INSERT INTO decision_events(id,confirmation_id,change_id,choice,note,actor_id,revision) VALUES (?,?,?,?,?,?,?)",
        (event, s["id"], change_id, choice or "unset", note, uid, revision),
    )
    c.execute(
        "INSERT INTO confirmation_change_versions VALUES (?,?,?) ON CONFLICT(confirmation_id,change_id) DO UPDATE SET revision=excluded.revision",
        (s["id"], change_id, revision),
    )
    if choice:
        c.execute(
            "INSERT INTO decision_heads VALUES (?,?,?,?) ON CONFLICT(confirmation_id,change_id) DO UPDATE SET event_id=excluded.event_id,choice=excluded.choice",
            (s["id"], change_id, event, choice),
        )
    else:
        c.execute(
            "DELETE FROM decision_heads WHERE confirmation_id=? AND change_id=?",
            (s["id"], change_id),
        )
    c.execute(
        "UPDATE confirmation_sessions SET revision=revision+1,state='in_progress' WHERE id=?",
        (s["id"],),
    )
    return event


def next_pending(c, s, selected=None):
    base = '''SELECT x.id FROM review_changes x LEFT JOIN decision_heads h
        ON h.confirmation_id=? AND h.change_id=x.id WHERE x.review_version_id=?
        AND (h.choice IS NULL OR h.choice NOT IN ('A','B'))'''
    order = ' ORDER BY x.frame_index,x.object_id,x.id LIMIT 1'
    if selected:
        row = c.execute('SELECT frame_index,object_id,id FROM review_changes WHERE id=? AND review_version_id=?',
                        (selected,s['review_version_id'])).fetchone()
        if row:
            following = c.execute(base+' AND (x.frame_index,x.object_id,x.id)>(?,?,?)'+order,
                                  (s['id'],s['review_version_id'],*tuple(row))).fetchone()
            if following:
                return following[0]
    first = c.execute(base+order,(s['id'],s['review_version_id'])).fetchone()
    return first[0] if first else None


def write(action, sid, uid, key, body, change_id=None, response_mode='full'):
    h = digest(["confirmation", action, sid, change_id, body])
    try:
        with closing(connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT * FROM review_write_receipts WHERE user_id=? AND request_key=?",
                (uid, key),
            ).fetchone()
            if old:
                if old["request_hash"] != h:
                    raise ReviewError(
                        "IDEMPOTENCY_KEY_REUSED", "重试标识不能用于不同操作"
                    )
                log.info(
                    "confirmation.replay action=%s session=%s change=%s actor=%s key=%s",
                    action,
                    sid,
                    change_id,
                    uid,
                    key,
                )
                return json.loads(old["response_json"])
            s = session_row(c, sid)
            selected = change_id
            if action == "claim":
                if s["confirmer_id"] not in (None, uid):
                    raise ReviewError(
                        "CONFIRMATION_ALREADY_CLAIMED", "任务已由其他确认员领取"
                    )
                if s["state"] not in ("pending", "in_progress"):
                    raise ReviewError("CONFIRMATION_READ_ONLY", "任务已完成或不可领取")
                reason = integrity(c, s)
                if reason:
                    raise ReviewError("INVALID_FROZEN_VERSION", reason)
                if s["confirmer_id"] is None:
                    c.execute(
                        "UPDATE confirmation_sessions SET confirmer_id=?,revision=revision+1 WHERE id=?",
                        (uid, sid),
                    )
            elif action == "cursor":
                if (
                    change_id is not None
                    and not c.execute(
                        "SELECT 1 FROM review_changes WHERE id=? AND review_version_id=?",
                        (change_id, s["review_version_id"]),
                    ).fetchone()
                ):
                    raise ReviewError("CHANGE_NOT_FOUND", "修改项不属于此任务", 404)
                r = c.execute(
                    "SELECT revision FROM confirmation_bookmarks WHERE user_id=? AND confirmation_id=?",
                    (uid, sid),
                ).fetchone()
                rev = r[0] if r else 0
                if body["expectedCursorRevision"] != rev:
                    raise ReviewError(
                        "CURSOR_REVISION_CONFLICT", "浏览位置已在其他窗口更新"
                    )
                c.execute(
                    "INSERT INTO confirmation_bookmarks VALUES (?,?,?,?,?) ON CONFLICT(user_id,confirmation_id) DO UPDATE SET change_id=excluded.change_id,revision=excluded.revision,updated_at=excluded.updated_at",
                    (uid, sid, change_id, rev + 1, now()),
                )
            else:
                editable(c, s, uid, completed=action == "reopen")
                if (
                    action != "decide"
                    and body["expectedSessionRevision"] != s["revision"]
                ):
                    raise ReviewError(
                        "CONFIRMATION_REVISION_CONFLICT",
                        "确认进度已在其他窗口更新，请重新读取",
                    )
                if action == "decide":
                    found = changes(c, s, change_id)
                    item = found[0] if found else None
                    if not item:
                        raise ReviewError("CHANGE_NOT_FOUND", "修改项不属于此任务", 404)
                    if body["expectedDecisionRevision"] != item["decisionRevision"]:
                        raise ReviewError(
                            "DECISION_REVISION_CONFLICT",
                            "本项已在其他窗口修改；已保留您的选择，请处理冲突",
                        )
                    choice = body["choice"]
                    before = item["decision"]["choice"] if item["decision"] else None
                    if choice not in ("A", "B"):
                        raise ReviewError(
                            "INVALID_CHOICE", "只能选择保留 A 或采用 B", 422
                        )
                    if choice != before:
                        event = append_decision(c, s, change_id, choice, uid)
                        c.execute(
                            "INSERT INTO confirmation_actions VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                "action_" + uuid.uuid4().hex,
                                sid,
                                change_id,
                                uid,
                                before,
                                choice,
                                event,
                                None,
                                now(),
                            ),
                        )
                elif action == "undo":
                    last = last_action(c, s)
                    if not last or last["id"] != body["actionId"]:
                        raise ReviewError(
                            "UNDO_CONFLICT", "可撤销的选择已变化，请重新读取"
                        )
                    selected = last["change_id"]
                    event = append_decision(
                        c, s, selected, last["before_choice"], uid, "undo:" + last["id"]
                    )
                    c.execute(
                        "UPDATE confirmation_actions SET undone_by=? WHERE id=?",
                        (event, last["id"]),
                    )
                elif action == "finish":
                    finalize(c, s, uid)
                elif action == "return":
                    return_frame(c, s, uid, body)
                elif action == "reopen":
                    fv = c.execute(
                        "SELECT id FROM final_versions WHERE confirmation_id=? ORDER BY rowid DESC LIMIT 1",
                        (sid,),
                    ).fetchone()
                    if not fv:
                        raise ReviewError(
                            "FINAL_VERSION_MISSING", "历史最终版本不存在，无法重新确认"
                        )
                    c.execute(
                        "INSERT INTO confirmation_reopen_events VALUES (?,?,?,?,?)",
                        ("reopen_" + uuid.uuid4().hex, sid, fv[0], uid, now()),
                    )
                    c.execute(
                        "UPDATE confirmation_sessions SET state='in_progress',revision=revision+1 WHERE id=?",
                        (sid,),
                    )
                else:
                    raise ReviewError("INVALID_ACTION", "操作不支持", 422)
            s = session_row(c, sid)
            view = session_data(c, s, uid)
            p = view['progress']
            if action != "cursor":
                c.execute(
                    "UPDATE confirmation_sessions SET decided_changes=?,kept_a=?,adopted_b=? WHERE id=?",
                    (p["decided"], p["keptA"], p["adoptedB"], sid),
                )
            if response_mode == 'delta':
                items = changes(c, s, selected) if action in ('decide','undo') else []
            else:
                items = changes(c, s)
            nxt = next_pending(c, s, selected)
            result = dict(
                session=view,
                items=items,
                selectedChangeId=selected,
                nextPendingChangeId=nxt,
                savedAt=now(),
            )
            if response_mode == 'delta': result['itemsScope'] = 'changed'
            c.execute(
                "INSERT INTO review_write_receipts VALUES (?,?,?,?,?)",
                (uid, key, h, packed(result), now()),
            )
            c.commit()
        log.info(
            "confirmation.committed action=%s session=%s change=%s actor=%s key=%s",
            action,
            sid,
            selected,
            uid,
            key,
        )
        return result
    except ReviewError as e:
        log.warning(
            "confirmation.rejected action=%s session=%s change=%s actor=%s code=%s key=%s",
            action,
            sid,
            change_id,
            uid,
            e.code,
            key,
        )
        raise
    except sqlite3.Error as e:
        log.exception(
            "confirmation.storage_failed action=%s session=%s change=%s actor=%s key=%s",
            action,
            sid,
            change_id,
            uid,
            key,
        )
        raise ReviewError(
            "STORAGE_UNAVAILABLE", "保存失败，当前选择已保留，请使用原请求重试", 503
        ) from e


def return_frame(c, s, uid, body):
    item = c.execute("SELECT * FROM review_changes WHERE id=? AND review_version_id=?", (body["changeId"], s["review_version_id"])).fetchone()
    if not item:
        raise ReviewError("CHANGE_NOT_FOUND", "修改项不属于此任务", 404)
    reason = body["reason"].strip()
    if not reason or len(reason) > 1000:
        raise ReviewError("INVALID_RETURN_REASON", "请填写退回原因（最多 1000 字）", 422)
    rs = c.execute("SELECT * FROM review_sessions WHERE id=?", (s["review_session_id"],)).fetchone()
    latest = c.execute("SELECT id FROM review_versions WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (rs["id"],)).fetchone()
    if rs["state"] != "reviewed" or not latest or latest[0] != s["review_version_id"]:
        raise ReviewError("REVIEW_VERSION_CHANGED", "审查版本已变化，请刷新后重试")
    c.execute("INSERT INTO review_return_events(id,session_id,confirmation_id,change_id,frame_index,actor_id,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
              ("return_" + uuid.uuid4().hex, rs["id"], s["id"], item["id"], item["frame_index"], uid, reason, now()))
    # Retain immutable evidence and the last B geometry as the starting point.
    c.execute("UPDATE review_frames SET state='unreviewed',frame_revision=frame_revision+1 WHERE session_id=? AND frame_index=?", (rs["id"], item["frame_index"]))
    c.execute("UPDATE review_sessions SET state='in_progress',revision=revision+1 WHERE id=?", (rs["id"],))
    c.execute("UPDATE confirmation_sessions SET state='returned',revision=revision+1 WHERE id=?", (s["id"],))
    log.info("confirmation.frame_returned confirmation=%s review=%s frame=%s actor=%s", s["id"], rs["id"], item["frame_index"], uid)


def finalize(c, s, uid):
    items = changes(c, s)
    if any(x["decision"] is None for x in items):
        raise ReviewError("UNDECIDED_CHANGES", "仍有未选择的修改项，不能完成确认")
    by_key = {(x["frameIndex"], x["objectId"]): x for x in items}
    if len(by_key) != len(items):
        raise ReviewError("INVALID_CHANGES", "固定 B 包含重复修改项")
    output = []
    matched = set()
    for fi in range(s["frame_count"]):
        context = frame_context(c, s, fi)
        a = context["baselineObjects"]
        b = {o["objectId"]: o for o in context["reviewObjects"]}
        if len(a) != len(b) or {o["objectId"] for o in a} != set(b):
            raise ReviewError("INVALID_FROZEN_VERSION", "A/B 对象集合不一致")
        objects = []
        for obj in a:
            other = b[obj["objectId"]]
            key = (fi, obj["objectId"])
            change = by_key.get(key)
            if obj.get("classKey") != other.get("classKey"):
                raise ReviewError("INVALID_FROZEN_VERSION", "A/B 类别不一致")
            if change:
                if (
                    obj["bbox"] != change["beforeBbox"]
                    or other["bbox"] != change["afterBbox"]
                    or obj["bbox"] == other["bbox"]
                ):
                    raise ReviewError(
                        "INVALID_FROZEN_VERSION", "修改项与固定 A/B 几何不一致"
                    )
                choice = change["decision"]["choice"]
                matched.add(key)
            else:
                if obj["bbox"] != other["bbox"]:
                    raise ReviewError("INVALID_CHANGES", "固定 B 存在未列出的修改项")
                choice = None
            objects.append(
                dict(
                    objectId=obj["objectId"],
                    annotationId=other.get("annotationId")
                    or f"{s['baseline_id']}:f{fi}:o{obj['objectId']}",
                    classKey=obj.get("classKey", "sperm"),
                    bbox=other["bbox"] if choice == "B" else obj["bbox"],
                    baselineBBox=obj["bbox"],
                    reviewBBox=other["bbox"],
                    choice=choice,
                    resolution="adopted_b"
                    if choice == "B"
                    else "kept_a"
                    if choice == "A"
                    else "unchanged",
                    changeId=change["changeId"] if change else None,
                    decisionEventId=change["decision"]["eventId"] if change else None,
                )
            )
        output.append(dict(frameIndex=fi, objects=objects))
    if matched != set(by_key):
        raise ReviewError("INVALID_CHANGES", "修改项引用了不存在的帧或对象")
    vid = "fv_" + uuid.uuid4().hex
    content = digest(output)
    prior = c.execute(
        "SELECT id FROM final_versions WHERE confirmation_id=? ORDER BY rowid DESC LIMIT 1",
        (s["id"],),
    ).fetchone()
    c.execute(
        "INSERT INTO final_versions(id,baseline_id,review_version_id,confirmation_id,snapshot_hash,content_hash,frame_count,confirmed_by) VALUES (?,?,?,?,?,?,?,?)",
        (
            vid,
            s["baseline_id"],
            s["review_version_id"],
            s["id"],
            content,
            content,
            len(output),
            uid,
        ),
    )
    c.executemany(
        "INSERT INTO final_version_frames VALUES (?,?,?)",
        [(vid, f["frameIndex"], packed(f["objects"])) for f in output],
    )
    c.executemany(
        """INSERT INTO final_frame_objects(final_version_id,frame_index,object_id,bbox,annotation_id,resolution,change_id,baseline_bbox,adopted_bbox)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (
                vid,
                f["frameIndex"],
                o["objectId"],
                packed(o["bbox"]),
                o["annotationId"],
                o["resolution"],
                o["changeId"],
                packed(o["baselineBBox"]),
                packed(o["reviewBBox"]) if o["choice"] == "B" else None,
            )
            for f in output
            for o in f["objects"]
        ],
    )
    c.execute(
        "INSERT INTO confirmation_final_records VALUES (?,?,?)",
        (vid, packed(items), prior[0] if prior else None),
    )
    c.execute(
        "UPDATE confirmation_sessions SET state='confirmed',revision=revision+1 WHERE id=?",
        (s["id"],),
    )


def final_data(vid):
    with closing(connect()) as c:
        f = c.execute("SELECT * FROM final_versions WHERE id=?", (vid,)).fetchone()
        if not f:
            raise ReviewError("FINAL_VERSION_NOT_FOUND", "最终版本不存在", 404)
        s = session_row(c, f["confirmation_id"])
        frames = [
            dict(frameIndex=r["frame_index"], objects=json.loads(r["objects_json"]))
            for r in c.execute(
                "SELECT * FROM final_version_frames WHERE final_version_id=? ORDER BY frame_index",
                (vid,),
            )
        ]
        if len(frames) != f["frame_count"]:
            raise ReviewError(
                "LEGACY_FINAL_VERSION", "历史最终版本缺少完整快照，请重新确认后导出"
            )
        record = c.execute(
            "SELECT * FROM confirmation_final_records WHERE final_version_id=?", (vid,)
        ).fetchone()
        return dict(
            finalVersionId=vid,
            confirmationId=s["id"],
            baselineId=f["baseline_id"],
            reviewVersionId=f["review_version_id"],
            media=dict(
                mediaId=s["media_id"],
                name=media_name(s["media_id"]),
                width=s["width"],
                height=s["height"],
                fps=s["fps"],
                frameCount=f["frame_count"],
            ),
            coordinates="original-image pixels; bbox=[x1,y1,x2,y2]; frameIndex is zero-based",
            snapshotHash=f["snapshot_hash"],
            confirmedBy=f["confirmed_by"],
            confirmedAt=f["created_at"],
            previousFinalVersionId=record["previous_final_version_id"]
            if record
            else None,
            frames=frames,
        )
