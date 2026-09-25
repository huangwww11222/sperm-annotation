"""Database schema for review / confirmation / final-export workflow.

V1 baseline: tables added in this module are CREATE TABLE IF NOT EXISTS and
executed from init_review_db() during app startup.
"""

REVIEW_SCHEMA = """
-- --------------------------------------------------------------------------
-- MediaRevision: immutable source-video identity (sha256 + size + frame count)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_revisions (
  id              TEXT PRIMARY KEY,            -- sha256-based stable id
  media_id        TEXT NOT NULL,               -- friendly id used in /api/track endpoints
  sha256          TEXT NOT NULL,               -- full file digest
  file_size       INTEGER NOT NULL,
  width           INTEGER NOT NULL,
  height          INTEGER NOT NULL,
  fps             REAL NOT NULL DEFAULT 30.0,
  frame_count     INTEGER NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_mrev_media ON media_revisions(media_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mrev_sha ON media_revisions(sha256);

-- --------------------------------------------------------------------------
-- AnnotationBaseline (version A): frozen original annotations
-- Frame coverage = objects (explicit objects) | empty (explicit no-sperm)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS annotation_baselines (
  id              TEXT PRIMARY KEY,
  media_revision_id TEXT NOT NULL REFERENCES media_revisions(id),
  submitted_by    INTEGER NOT NULL REFERENCES users(id),
  snapshot_hash   TEXT NOT NULL,               -- sha256 of canonical JSON
  frame_count     INTEGER NOT NULL,
  object_count    INTEGER NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_abl_media ON annotation_baselines(media_revision_id);

CREATE TABLE IF NOT EXISTS baseline_frames (
  baseline_id     TEXT NOT NULL REFERENCES annotation_baselines(id) ON DELETE CASCADE,
  frame_index     INTEGER NOT NULL,
  coverage        TEXT NOT NULL,               -- 'objects' | 'empty'
  frame_hash      TEXT,                        -- sha256 of packed bbox tuples
  objects_json    TEXT NOT NULL,               -- canonical JSON array
  PRIMARY KEY (baseline_id, frame_index)
);
CREATE INDEX IF NOT EXISTS idx_bfrm_idx ON baseline_frames(frame_index);

-- --------------------------------------------------------------------------
-- Review session + per-frame draft
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_sessions (
  id              TEXT PRIMARY KEY,
  baseline_id     TEXT NOT NULL REFERENCES annotation_baselines(id) ON DELETE CASCADE,
  reviewer_id     INTEGER REFERENCES users(id),
  state           TEXT NOT NULL DEFAULT 'pending',  -- pending | in_progress | reviewed | blocked | returned
  revision        INTEGER NOT NULL DEFAULT 0,
  frame_count     INTEGER NOT NULL,
  changed_count   INTEGER NOT NULL DEFAULT 0,       -- net changed frames (submitted)
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_rs_baseline ON review_sessions(baseline_id);

CREATE TABLE IF NOT EXISTS review_frames (
  session_id      TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  frame_index     INTEGER NOT NULL,
  state           TEXT NOT NULL DEFAULT 'unreviewed', -- unreviewed | draft | submitted
  patch_json      TEXT,                         -- list of {objectId, newBBox, annotationId}
  frame_revision  INTEGER NOT NULL DEFAULT 0,
  submission_id   TEXT,
  PRIMARY KEY (session_id, frame_index)
);
CREATE INDEX IF NOT EXISTS idx_rfrm_state ON review_frames(state);

CREATE TABLE IF NOT EXISTS frame_submissions (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  frame_index     INTEGER NOT NULL,
  revision        INTEGER NOT NULL,
  net_change_count INTEGER NOT NULL DEFAULT 0,
  reviewer_id     INTEGER NOT NULL REFERENCES users(id),
  submitted_at    TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_subm_sess ON frame_submissions(session_id);

CREATE TABLE IF NOT EXISTS review_versions (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  baseline_id     TEXT NOT NULL REFERENCES annotation_baselines(id),
  snapshot_hash   TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS review_changes (
  id              TEXT PRIMARY KEY,
  review_version_id TEXT NOT NULL REFERENCES review_versions(id) ON DELETE CASCADE,
  frame_index     INTEGER NOT NULL,
  object_id       INTEGER NOT NULL,
  annotation_id   TEXT NOT NULL,
  before_bbox     TEXT NOT NULL,               -- JSON [x1,y1,x2,y2]
  after_bbox      TEXT NOT NULL,
  before_center   TEXT,                        -- JSON [cx, cy]
  after_center    TEXT,
  iou             REAL,
  center_shift    REAL,
  dx              REAL,
  dy              REAL,
  dw              REAL,
  dh              REAL,
  reviewer_id     INTEGER NOT NULL REFERENCES users(id),
  frame_submission_id TEXT REFERENCES frame_submissions(id),
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_rchg_rev ON review_changes(review_version_id);
CREATE INDEX IF NOT EXISTS idx_rchg_frame ON review_changes(frame_index);

-- --------------------------------------------------------------------------
-- Confirmation session + per-change decision
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS confirmation_sessions (
  id              TEXT PRIMARY KEY,
  review_version_id TEXT NOT NULL REFERENCES review_versions(id) ON DELETE CASCADE,
  baseline_id     TEXT NOT NULL REFERENCES annotation_baselines(id),
  confirmer_id    INTEGER REFERENCES users(id),
  state           TEXT NOT NULL DEFAULT 'pending', -- pending | in_progress | confirmed | blocked | returned
  revision        INTEGER NOT NULL DEFAULT 0,
  total_changes   INTEGER NOT NULL DEFAULT 0,
  decided_changes INTEGER NOT NULL DEFAULT 0,
  adopted_b       INTEGER NOT NULL DEFAULT 0,
  kept_a          INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_cs_rev ON confirmation_sessions(review_version_id);

CREATE TABLE IF NOT EXISTS decision_events (
  id              TEXT PRIMARY KEY,
  confirmation_id TEXT NOT NULL REFERENCES confirmation_sessions(id) ON DELETE CASCADE,
  change_id       TEXT NOT NULL REFERENCES review_changes(id) ON DELETE CASCADE,
  choice          TEXT NOT NULL,                -- 'A' | 'B'
  note            TEXT,
  actor_id        INTEGER NOT NULL REFERENCES users(id),
  revision        INTEGER NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_decision_cs ON decision_events(confirmation_id);
CREATE INDEX IF NOT EXISTS idx_decision_change ON decision_events(change_id);

CREATE TABLE IF NOT EXISTS decision_heads (
  confirmation_id TEXT NOT NULL REFERENCES confirmation_sessions(id) ON DELETE CASCADE,
  change_id       TEXT NOT NULL REFERENCES review_changes(id) ON DELETE CASCADE,
  event_id        TEXT NOT NULL REFERENCES decision_events(id) ON DELETE CASCADE,
  choice          TEXT NOT NULL,                -- 'A' | 'B'
  PRIMARY KEY (confirmation_id, change_id)
);

-- --------------------------------------------------------------------------
-- Review issues: problem frame (block session)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_issues (
  id              TEXT PRIMARY KEY,
  session_id      TEXT REFERENCES review_sessions(id) ON DELETE CASCADE,
  confirmation_id TEXT REFERENCES confirmation_sessions(id) ON DELETE CASCADE,
  frame_index     INTEGER,
  annotation_id   TEXT,
  issue_type      TEXT NOT NULL,                -- 'missing' | 'wrong' | 'return'
  description     TEXT NOT NULL,
  reporter_id     INTEGER NOT NULL REFERENCES users(id),
  status          TEXT NOT NULL DEFAULT 'open',  -- open | resolved | returned
  resolution      TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_issue_sess ON review_issues(session_id);

-- --------------------------------------------------------------------------
-- Final version (F): A + adopted-B modifications
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS final_versions (
  id              TEXT PRIMARY KEY,
  baseline_id     TEXT NOT NULL REFERENCES annotation_baselines(id),
  review_version_id TEXT NOT NULL REFERENCES review_versions(id),
  confirmation_id TEXT NOT NULL REFERENCES confirmation_sessions(id) ON DELETE CASCADE,
  snapshot_hash   TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  frame_count     INTEGER NOT NULL,
  confirmed_by    INTEGER NOT NULL REFERENCES users(id),
  created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS final_frame_objects (
  final_version_id TEXT NOT NULL REFERENCES final_versions(id) ON DELETE CASCADE,
  frame_index      INTEGER NOT NULL,
  object_id        INTEGER NOT NULL,
  bbox             TEXT NOT NULL,              -- final [x1,y1,x2,y2]
  annotation_id    TEXT NOT NULL,
  resolution       TEXT NOT NULL,              -- 'unchanged' | 'kept_a' | 'adopted_b'
  change_id        TEXT,                       -- null when unchanged
  baseline_bbox    TEXT NOT NULL,
  adopted_bbox     TEXT,                       -- null when kept_a
  PRIMARY KEY (final_version_id, frame_index, object_id)
);

-- --------------------------------------------------------------------------
-- Resume cursor (bookmark)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resume_cursors (
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id      TEXT NOT NULL,
  stage           TEXT NOT NULL,               -- 'review' | 'confirmation'
  frame_index     INTEGER,
  change_id       TEXT,
  updated_at      TEXT NOT NULL DEFAULT (datetime('now','utc')),
  PRIMARY KEY (user_id, stage, session_id)
);

-- --------------------------------------------------------------------------
-- Migration bookkeeping
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  version         TEXT PRIMARY KEY,
  applied_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
"""

REVIEW_SCHEMA_VERSION = "review-workflow-v1"


def apply_review_schema(conn) -> None:
    """Idempotently apply the review schema on the given connection."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    )
    already = {row[0] for row in cur.fetchall()}
    if "schema_migrations" in already:
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version=?",
            (REVIEW_SCHEMA_VERSION,),
        ).fetchone()
        if row:
            return  # already applied

    conn.executescript(REVIEW_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
        (REVIEW_SCHEMA_VERSION,),
    )
    conn.commit()
