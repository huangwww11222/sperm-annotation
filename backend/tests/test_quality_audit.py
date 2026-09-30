"""Frozen evidence and read-only extraction of real workflow/video fixtures."""

import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from app import audit_export as cli, db, quality_audit as audit
from app import training_export as export
from .test_training_export import frozen, create, ready
from .test_confirmation_workflow import confirmation, choose, finish, write
from .test_review_workflow import task


def test_capture_atomic_and_idempotent(frozen):
    job = create(frozen, key="one-audit")
    with db.connect() as c:
        snapshot, checksum = audit.load(c, job["exportId"])
        assert snapshot["exportId"] == job["exportId"]
        assert snapshot["sources"][0]["finalVersionId"] == frozen["vid"]
        assert len(snapshot["tables"]["review_frames"]) == 3
        assert len(snapshot["tables"]["frame_submissions"]) == 3
        assert all(set(u) == {"id", "username"} for u in snapshot["tables"]["users"])
        assert snapshot["completeness"]["viewingTelemetry"] == "not-collected"
        assert audit.sha(audit.packed(snapshot)) == checksum
    assert create(frozen, key="one-audit")["exportId"] == job["exportId"]
    with db.connect() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM training_export_audits").fetchone()[0] == 1
        )
        c.execute(
            "CREATE TRIGGER inject_audit_failure BEFORE INSERT ON training_export_audits BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        create(frozen, key="broken-audit")
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM training_exports").fetchone()[0] == 1


@pytest.mark.parametrize(
    "table,operation",
    [
        ("training_export_audits", "UPDATE"),
        ("training_export_audits", "DELETE"),
        ("audit_source_identity", "UPDATE"),
        ("audit_source_identity", "DELETE"),
    ],
)
def test_immutable_records(frozen, table, operation):
    create(frozen)
    column = (
        "snapshot_json" if table == "training_export_audits" else "source_system_id"
    )
    with db.connect() as c, pytest.raises(sqlite3.IntegrityError):
        c.execute(
            f"UPDATE {table} SET {column}={column}"
            if operation == "UPDATE"
            else f"DELETE FROM {table}"
        )


def test_identity_survives_migration(frozen):
    first = create(frozen)
    with db.connect() as c:
        source = audit.load(c, first["exportId"])[0]["sourceSystemId"]
        audit.migrate(c)
    second = create(frozen)
    with db.connect() as c:
        assert audit.load(c, second["exportId"])[0]["sourceSystemId"] == source


def test_frozen_history_after_reconfirmation(frozen):
    old = ready(frozen, "both")
    with db.connect() as c:
        before = audit.load(c, old["exportId"])
    write(frozen["cid"], "reopen")
    with db.connect() as c:
        change = c.execute(
            "SELECT id FROM review_changes ORDER BY frame_index"
        ).fetchone()[0]
    choose(frozen["cid"], 0, "B")
    new_vid = write(frozen["cid"], "finish")["session"]["finalVersionId"]
    new = ready(frozen, ids=[new_vid])
    with db.connect() as c:
        assert audit.load(c, old["exportId"]) == before
        s, _ = audit.load(c, new["exportId"])
        assert s["tables"]["confirmation_reopen_events"]
    assert export.status(old["exportId"], 3)["state"] == "invalidated"
    assert new["manifest"]["auditReference"] != old["manifest"]["auditReference"]


@pytest.mark.parametrize("fmt", ["yolo", "coco", "both"])
def test_sample_indexes_match_labels_and_images(frozen, fmt):
    r = ready(frozen, fmt)
    m = r["manifest"]
    assert m["schemaVersion"] == 2
    with zipfile.ZipFile(export.download(r["exportId"], 3)) as z:
        for sample in m["samples"]:
            assert (
                r["exportId"] in sample["sampleId"]
                and sample["sampleId"] in sample["image"]
            )
            assert audit.sha(z.read(sample["image"])) == sample["imageSha256"]
            if fmt != "coco":
                assert len(z.read(sample["label"]).splitlines()) == len(
                    sample["objects"]
                )
                assert [o["yoloLine"] for o in sample["objects"]] == list(
                    range(1, len(sample["objects"]) + 1)
                )
        assert not any(n.startswith("audits/") for n in z.namelist())
    with cli.connect_readonly(db.DB_FILE) as c:
        found = cli.lookup(c, cli.jobs(c), m["samples"][0]["sampleId"])
        assert found["source"]["mediaRevisionId"] == m["sources"][0]["mediaRevisionId"]
        assert found["sample"]["frameIndex"] == 0


def test_cli_bundle_is_consistent_and_database_readonly(frozen, tmp_path):
    r = ready(frozen)
    before = Path(db.DB_FILE).read_bytes()
    output = tmp_path / "audit.zip"
    assert (
        cli.main(
            [
                "--db",
                str(db.DB_FILE),
                "--dataset-id",
                r["exportId"],
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert Path(db.DB_FILE).read_bytes() == before
    with zipfile.ZipFile(output) as z:
        m = json.loads(z.read("manifest.json"))
        assert m["format"] == audit.BUNDLE_FORMAT
        for f in m["files"]:
            assert audit.sha(z.read(f["name"])) == f["sha256"]
        ds = json.loads(z.read("datasets.jsonl"))
        s = json.loads(z.read(ds["snapshotFile"]))
        assert ds["currentFinalAtRead"] and ds["state"] == "ready"
        assert audit.sha(audit.packed(s)) == ds["auditReference"]["sha256"]
    assert cli.main(["--db", str(db.DB_FILE), "--output", str(output)]) == 1
    with cli.connect_readonly(db.DB_FILE) as c, pytest.raises(sqlite3.OperationalError):
        c.execute("UPDATE training_exports SET state='failed'")


def test_failed_attempts_and_legacy_coverage(frozen, tmp_path):
    r = create(frozen)
    export.mark_failed(r["exportId"], "TEST_FAILED", "test")
    assert (
        cli.main(["--db", str(db.DB_FILE), "--output", str(tmp_path / "none.zip")]) == 1
    )
    assert (
        cli.main(
            [
                "--db",
                str(db.DB_FILE),
                "--include-failed",
                "--output",
                str(tmp_path / "failed.zip"),
            ]
        )
        == 0
    )
    with db.connect() as c:
        c.execute("DROP TRIGGER audit_snapshot_no_delete")
        c.execute("DELETE FROM training_export_audits")
    with cli.connect_readonly(db.DB_FILE) as c:
        assert not cli.summary(cli.jobs(c)[0])["audited"]
    assert (
        cli.main(
            [
                "--db",
                str(db.DB_FILE),
                "--dataset-id",
                r["exportId"],
                "--output",
                str(tmp_path / "legacy.zip"),
            ]
        )
        == 1
    )


def test_nonexistent_db_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    assert cli.main(["--db", str(path), "--list"]) == 1
    assert not path.exists()


def test_date_filters_accept_timezones_and_media(frozen):
    r = ready(frozen)
    with cli.connect_readonly(db.DB_FILE) as c:
        assert (
            cli.jobs(
                c,
                since=cli.utc_bound("2000-01-01T00:00:00+08:00"),
                media_id="test-video",
            )[0]["id"]
            == r["exportId"]
        )
        assert not cli.jobs(c, until=cli.utc_bound("2000-01-01T00:00:00Z"))
    with pytest.raises(Exception):
        cli.utc_bound("2026-09-30")


def test_long_submission_payload_query_is_chunked(frozen):
    with db.connect() as c:
        result = audit.rows(c, "users", "id", list(range(2500)), "id,username")
    assert len(result) == 3


def test_interoperability_artifacts(frozen):
    """Optional artifact handoff to the independent statistics test process."""
    import os, shutil

    output = os.getenv("AUDIT_INTEROP_DIR")
    if not output:
        pytest.skip("设置 AUDIT_INTEROP_DIR 生成跨仓库联调夹具")
    directory = Path(output).resolve()
    assert directory.is_relative_to(Path(__file__).resolve().parents[2] / "work")
    directory.mkdir(parents=True, exist_ok=True)
    for fmt in ("yolo", "coco", "both"):
        ready(frozen, fmt)
    from .test_training_export import (
        test_multiple_video_versions_are_all_exported_without_filename_collisions,
    )

    test_multiple_video_versions_are_all_exported_without_filename_collisions(frozen)
    write(frozen["cid"], "reopen")
    choose(frozen["cid"], 0, "B")
    vid = write(frozen["cid"], "finish")["session"]["finalVersionId"]
    ready(frozen, "both", ids=[vid])
    with cli.connect_readonly(db.DB_FILE) as c:
        selected = cli.jobs(c)
        for r in selected:
            shutil.copyfile(
                export.DATASET_EXPORT_DIR / (r["id"] + ".zip"),
                directory / (r["id"] + ".zip"),
            )
        with (directory / "audit.zip").open("wb") as output:
            cli.bundle(c, selected, output)
        (directory / "manifest-list.json").write_text(
            json.dumps([json.loads(r["manifest_json"]) for r in selected]),
            encoding="utf-8",
        )
