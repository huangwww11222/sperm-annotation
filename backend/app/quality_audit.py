"""Immutable evidence captured in the dataset creation transaction.

No credentials, write receipts, cursor-based attention claims or inferred work
hours enter this contract. Readers can use this module without importing main.
"""

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

log = logging.getLogger("review.audit")
AUDIT_SCHEMA = 1
BUNDLE_FORMAT = "annotation-quality-audit"
BUNDLE_SCHEMA = 1


def packed(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha(value):
    return hashlib.sha256(
        value.encode("utf-8") if isinstance(value, str) else value
    ).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS audit_source_identity (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), source_system_id TEXT NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS training_export_audits (
      export_id TEXT PRIMARY KEY REFERENCES training_exports(id),
      audit_id TEXT NOT NULL UNIQUE, schema_version INTEGER NOT NULL,
      captured_at_utc TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, snapshot_json TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS audit_snapshot_no_update BEFORE UPDATE ON training_export_audits
      BEGIN SELECT RAISE(ABORT, 'audit snapshots are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS audit_snapshot_no_delete BEFORE DELETE ON training_export_audits
      BEGIN SELECT RAISE(ABORT, 'audit snapshots are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS audit_identity_no_update BEFORE UPDATE ON audit_source_identity
      BEGIN SELECT RAISE(ABORT, 'audit identity is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS audit_identity_no_delete BEFORE DELETE ON audit_source_identity
      BEGIN SELECT RAISE(ABORT, 'audit identity is immutable'); END;
    """)
    c.execute(
        "INSERT OR IGNORE INTO audit_source_identity VALUES (1,?)",
        ("site_" + uuid.uuid4().hex,),
    )
    c.commit()


def rows(c, table, column, ids, columns="*"):
    """Table/column identifiers are internal constants; values are parameters."""
    if not ids:
        return []
    result = []
    # Keep below SQLite's variable limit for exports containing long videos.
    ids = sorted(set(ids), key=str)
    for start in range(0, len(ids), 400):
        part = ids[start : start + 400]
        result.extend(
            dict(r)
            for r in c.execute(
                f"SELECT {columns} FROM {table} WHERE {column} IN ({','.join('?' for _ in part)}) ORDER BY rowid",
                part,
            )
        )
    return result


def capture(c, eid, actor, body, versions):
    """Caller owns BEGIN IMMEDIATE; audit and job either both commit or neither."""
    source = c.execute(
        "SELECT source_system_id FROM audit_source_identity WHERE singleton=1"
    ).fetchone()[0]
    tables = {}
    sources = []
    for v in versions:
        f, m = v["final"], v["media"]
        rv = c.execute(
            "SELECT * FROM review_versions WHERE id=?", (f["review_version_id"],)
        ).fetchone()
        sources.append(
            dict(
                mediaRevisionId=m["id"],
                mediaId=m["media_id"],
                sourceSha256=m["sha256"],
                width=m["width"],
                height=m["height"],
                frameCount=m["frame_count"],
                baselineId=f["baseline_id"],
                reviewSessionId=rv["session_id"],
                reviewVersionId=f["review_version_id"],
                confirmationId=f["confirmation_id"],
                finalVersionId=f["id"],
                snapshotHash=f["snapshot_hash"],
            )
        )
    selectors = [
        ("media_revisions", "id", [s["mediaRevisionId"] for s in sources]),
        ("annotation_baselines", "id", [s["baselineId"] for s in sources]),
        ("baseline_frames", "baseline_id", [s["baselineId"] for s in sources]),
        ("review_sessions", "id", [s["reviewSessionId"] for s in sources]),
        ("review_frames", "session_id", [s["reviewSessionId"] for s in sources]),
        ("frame_submissions", "session_id", [s["reviewSessionId"] for s in sources]),
        ("review_versions", "id", [s["reviewVersionId"] for s in sources]),
        (
            "review_version_frames",
            "version_id",
            [s["reviewVersionId"] for s in sources],
        ),
        (
            "review_changes",
            "review_version_id",
            [s["reviewVersionId"] for s in sources],
        ),
        ("confirmation_sessions", "id", [s["confirmationId"] for s in sources]),
        ("decision_events", "confirmation_id", [s["confirmationId"] for s in sources]),
        ("decision_heads", "confirmation_id", [s["confirmationId"] for s in sources]),
        (
            "confirmation_actions",
            "confirmation_id",
            [s["confirmationId"] for s in sources],
        ),
        (
            "confirmation_reopen_events",
            "confirmation_id",
            [s["confirmationId"] for s in sources],
        ),
        # Keep historical final headers/decision records, but image objects only for selected F.
        ("final_versions", "confirmation_id", [s["confirmationId"] for s in sources]),
        (
            "final_version_frames",
            "final_version_id",
            [s["finalVersionId"] for s in sources],
        ),
        (
            "final_frame_objects",
            "final_version_id",
            [s["finalVersionId"] for s in sources],
        ),
    ]
    for table, column, ids in selectors:
        tables[table] = rows(c, table, column, ids)
    tables["review_submission_payloads"] = rows(
        c,
        "review_submission_payloads",
        "submission_id",
        [r["id"] for r in tables["frame_submissions"]],
    )
    tables["confirmation_final_records"] = rows(
        c,
        "confirmation_final_records",
        "final_version_id",
        [r["id"] for r in tables["final_versions"]],
    )
    users = {actor}
    for table, field in [
        ("annotation_baselines", "submitted_by"),
        ("review_sessions", "reviewer_id"),
        ("frame_submissions", "reviewer_id"),
        ("confirmation_sessions", "confirmer_id"),
        ("decision_events", "actor_id"),
        ("confirmation_actions", "actor_id"),
        ("confirmation_reopen_events", "actor_id"),
        ("final_versions", "confirmed_by"),
    ]:
        users.update(r[field] for r in tables[table] if r.get(field) is not None)
    tables["users"] = rows(c, "users", "id", users, "id,username")
    available = {r["submission_id"] for r in tables["review_submission_payloads"]}
    missing = [r["id"] for r in tables["frame_submissions"] if r["id"] not in available]
    snapshot = dict(
        schemaVersion=AUDIT_SCHEMA,
        auditId="audit_" + eid,
        exportId=eid,
        sourceSystemId=source,
        capturedAtUtc=now(),
        sourceAppRevision=os.getenv("APP_RELEASE_REVISION") or None,
        dataset=dict(actorId=actor, settings=body),
        sources=sources,
        tables=tables,
        completeness=dict(
            backfilled=False,
            annotationOperationHistory="not-collected",
            viewingTelemetry="not-collected",
            missingSubmissionPayloadIds=missing,
        ),
        conventions=dict(
            frameIndexBase=0,
            bbox="original-pixel-xyxy",
            workflowTimestampTimezone="UTC",
            annotationResponsibility="baseline-submitter; not per-box manual authorship",
        ),
    )
    payload = packed(snapshot)
    checksum = sha(payload)
    c.execute(
        "INSERT INTO training_export_audits VALUES (?,?,?,?,?,?)",
        (
            eid,
            snapshot["auditId"],
            AUDIT_SCHEMA,
            snapshot["capturedAtUtc"],
            checksum,
            payload,
        ),
    )
    log.info(
        "audit.captured export=%s audit=%s sha256=%s",
        eid,
        snapshot["auditId"],
        checksum,
    )
    return snapshot


def load(c, eid):
    row = c.execute(
        "SELECT * FROM training_export_audits WHERE export_id=?", (eid,)
    ).fetchone()
    if not row:
        raise ValueError("该导出没有固定审计快照（可能是升级前的历史任务）")
    if sha(row["snapshot_json"]) != row["snapshot_sha256"]:
        raise ValueError("审计快照摘要不匹配")
    snapshot = json.loads(row["snapshot_json"])
    if (
        snapshot["schemaVersion"] != AUDIT_SCHEMA
        or snapshot["exportId"] != eid
        or snapshot["auditId"] != row["audit_id"]
    ):
        raise ValueError("审计快照身份或格式不匹配")
    return snapshot, row["snapshot_sha256"]


def reference(snapshot, checksum):
    return dict(
        auditId=snapshot["auditId"],
        sha256=checksum,
        schemaVersion=AUDIT_SCHEMA,
        capturedAtUtc=snapshot["capturedAtUtc"],
    )
