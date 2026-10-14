from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

# Allow the documented root-level command to work without requiring callers to
# set PYTHONPATH=backend first.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import db
from app.confirmation_workflow import migrate as migrate_confirmation
from app.review_schema import apply_review_schema
from app.review_workflow import migrate as migrate_review
from tests.export_statistics_data import ExportError, export_statistics_zip


def _fixture_database(path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(db.CREATE_SQL)
        apply_review_schema(conn)
        migrate_review(conn)
        migrate_confirmation(conn)

        conn.executemany(
            "INSERT INTO users(id,username,password_hash) VALUES (?,?,?)",
            [
                (1, "标注员", "secret-annotator"),
                (2, "审查员", "secret-reviewer"),
                (3, "确认员", "secret-confirmer"),
            ],
        )
        conn.execute(
            """INSERT INTO media_revisions
               (id,media_id,sha256,file_size,width,height,fps,frame_count)
               VALUES ('mrev_1','sample-video','abc123',100,100,50,25,1)"""
        )
        conn.execute(
            """INSERT INTO annotation_baselines
               (id,media_revision_id,submitted_by,snapshot_hash,frame_count,object_count)
               VALUES ('abl_1','mrev_1',1,'a-hash',1,1)"""
        )
        a_objects = json.dumps(
            [{"objectId": 1, "bbox": [1, 2, 11, 12], "classKey": "sperm"}],
            ensure_ascii=False,
        )
        b_objects = json.dumps(
            [{"objectId": 1, "bbox": [2, 2, 12, 12], "classKey": "sperm"}],
            ensure_ascii=False,
        )
        final_objects = json.dumps(
            [
                {
                    "objectId": 1,
                    "annotationId": "ann_1",
                    "bbox": [2, 2, 12, 12],
                    "baselineBBox": [1, 2, 11, 12],
                    "reviewBBox": [2, 2, 12, 12],
                    "classKey": "sperm",
                    "choice": "B",
                    "resolution": "adopted_b",
                    "changeId": "chg_1",
                    "decisionEventId": "event_1",
                }
            ],
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO baseline_frames VALUES ('abl_1',0,'objects','frame-hash',?)",
            (a_objects,),
        )
        conn.execute(
            """INSERT INTO review_sessions
               (id,baseline_id,reviewer_id,state,revision,frame_count,changed_count)
               VALUES ('rs_1','abl_1',2,'reviewed',1,1,1)"""
        )
        conn.execute(
            """INSERT INTO frame_submissions
               (id,session_id,frame_index,revision,net_change_count,reviewer_id)
               VALUES ('sub_1','rs_1',0,1,1,2)"""
        )
        conn.execute(
            "INSERT INTO review_submission_payloads VALUES ('sub_1','[]')"
        )
        conn.execute(
            """INSERT INTO review_frames
               (session_id,frame_index,state,patch_json,frame_revision,submission_id)
               VALUES ('rs_1',0,'submitted','[]',1,'sub_1')"""
        )
        conn.execute(
            """INSERT INTO review_versions(id,session_id,baseline_id,snapshot_hash)
               VALUES ('rv_1','rs_1','abl_1','b-hash')"""
        )
        conn.execute(
            "INSERT INTO review_version_frames VALUES ('rv_1',0,'sub_1',?)",
            (b_objects,),
        )
        conn.execute(
            """INSERT INTO review_changes
               (id,review_version_id,frame_index,object_id,annotation_id,
                before_bbox,after_bbox,before_center,after_center,iou,
                center_shift,dx,dy,dw,dh,reviewer_id,frame_submission_id)
               VALUES
               ('chg_1','rv_1',0,1,'ann_1','[1,2,11,12]','[2,2,12,12]',
                '[6,7]','[7,7]',0.818,1,1,0,0,0,2,'sub_1')"""
        )
        conn.execute(
            """INSERT INTO confirmation_sessions
               (id,review_version_id,baseline_id,confirmer_id,state,revision,
                total_changes,decided_changes,adopted_b,kept_a)
               VALUES ('cs_1','rv_1','abl_1',3,'confirmed',2,1,1,1,0)"""
        )
        conn.execute(
            """INSERT INTO decision_events
               (id,confirmation_id,change_id,choice,note,actor_id,revision)
               VALUES ('event_1','cs_1','chg_1','B',NULL,3,1)"""
        )
        conn.execute(
            "INSERT INTO decision_heads VALUES ('cs_1','chg_1','event_1','B')"
        )
        conn.execute(
            """INSERT INTO final_versions
               (id,baseline_id,review_version_id,confirmation_id,snapshot_hash,
                content_hash,frame_count,confirmed_by)
               VALUES ('fv_1','abl_1','rv_1','cs_1','f-hash','f-content',1,3)"""
        )
        conn.execute(
            "INSERT INTO final_version_frames VALUES ('fv_1',0,?)",
            (final_objects,),
        )
        conn.execute(
            """INSERT INTO final_frame_objects
               (final_version_id,frame_index,object_id,bbox,annotation_id,resolution,
                change_id,baseline_bbox,adopted_bbox)
               VALUES ('fv_1',0,1,'[2,2,12,12]','ann_1','adopted_b',
                       'chg_1','[1,2,11,12]','[2,2,12,12]')"""
        )
        conn.execute(
            "INSERT INTO confirmation_final_records VALUES ('fv_1',?,NULL)",
            (json.dumps([{"changeId": "chg_1", "choice": "B"}]),),
        )
        conn.commit()


def _csv_rows(raw: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def test_export_statistics_zip_contains_safe_complete_package(tmp_path) -> None:
    database = tmp_path / "app.db"
    _fixture_database(database)
    before_counts: dict[str, int] = {}
    with closing(sqlite3.connect(database)) as conn:
        for table in ("users", "annotation_baselines", "review_changes", "final_versions"):
            before_counts[table] = conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]

    output = export_statistics_zip(
        database,
        tmp_path / "标注确认数据",
        source_system_id="test-source",
        output_name="statistics.zip",
    )

    assert output == tmp_path / "标注确认数据" / "statistics.zip"
    assert output.is_file()
    assert not output.with_name("statistics.zip.partial").exists()

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert {
            "manifest.json",
            "users.csv",
            "media_revisions.csv",
            "baseline_frames.jsonl",
            "review_changes.csv",
            "decision_heads.csv",
            "final_versions.csv",
            "final_version_frames.jsonl",
            "confirmation_final_records.jsonl",
        } <= names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["sourceSystemId"] == "test-source"
        assert manifest["exportMode"] == "full"
        assert manifest["schemaVersion"] == "annotation-confirmation-statistics-v1"

        entries = {entry["name"]: entry for entry in manifest["files"]}
        for name, entry in entries.items():
            payload = archive.read(name)
            assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
            assert entry["size"] == len(payload)

        users = _csv_rows(archive.read("users.csv"))
        assert [row["username"] for row in users] == ["标注员", "审查员", "确认员"]
        assert "password_hash" not in users[0]
        assert b"secret-annotator" not in archive.read("users.csv")

        baselines = [
            json.loads(line)
            for line in archive.read("baseline_frames.jsonl")
            .decode("utf-8")
            .splitlines()
        ]
        assert baselines[0]["objects_json"][0]["objectId"] == 1
        finals = _csv_rows(archive.read("final_versions.csv"))
        assert finals[0]["is_current"] == "1"

    with closing(sqlite3.connect(database)) as conn:
        after_counts = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in before_counts
        }
    assert after_counts == before_counts


def test_export_rejects_database_without_required_workflow_tables(tmp_path) -> None:
    database = tmp_path / "incomplete.db"
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, created_at TEXT)"
        )
        conn.commit()

    with pytest.raises(ExportError, match="缺少统计导出所需表"):
        export_statistics_zip(database, tmp_path / "output")

    assert not list((tmp_path / "output").glob("*.zip"))


def test_export_rejects_broken_json_without_publishing_partial_zip(tmp_path) -> None:
    database = tmp_path / "broken.db"
    _fixture_database(database)
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "UPDATE baseline_frames SET objects_json='not-json' WHERE baseline_id='abl_1'"
        )
        conn.commit()

    output_directory = tmp_path / "output"
    with pytest.raises(ExportError, match="不是合法 JSON"):
        export_statistics_zip(
            database,
            output_directory,
            output_name="should-not-exist.zip",
        )

    assert not (output_directory / "should-not-exist.zip").exists()
    assert not (output_directory / "should-not-exist.zip.partial").exists()
