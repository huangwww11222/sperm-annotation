"""Statistics export contract and failure recovery on disposable data only."""

import csv
import hashlib
import io
import json
import sqlite3
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.auth import sign_jwt
from app import statistics_export as export
from app import statistics_export_jobs as jobs
from app import training_export

from .test_review_workflow import task as task
from .test_confirmation_workflow import confirmation as confirmation, finish


@pytest.fixture
def statistics_client(confirmation, tmp_path, monkeypatch):
    finish(confirmation)
    with db.connect() as conn:
        training_export.migrate(conn)
        conn.execute("UPDATE users SET password_hash='never-export-this-password-hash'")
    manager = jobs.ExportManager(tmp_path / "statistics-runtime", db.DB_FILE)
    monkeypatch.setattr(jobs, "_manager", manager)
    monkeypatch.setattr(manager, "enqueue", lambda eid: None)
    yield TestClient(main.app), {"Authorization": "Bearer " + sign_jwt({"uid": 1})}, manager
    manager.shutdown()


def create(fixture, key="first", auth=None):
    client, default_auth, _ = fixture
    return client.post("/api/statistics/exports", json={}, headers={**(auth or default_auth), "Idempotency-Key": key})


def ready(fixture, key="first"):
    response = create(fixture, key)
    assert response.status_code == 202, response.text
    eid = response.json()["exportId"]
    fixture[2].run(eid)
    assert fixture[2].status(eid, 1)["state"] == "ready"
    return eid


def zip_data(fixture, eid):
    client, auth, _ = fixture
    response = client.get(f"/api/statistics/exports/{eid}/download", headers=auth)
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def csv_rows(archive, filename):
    return list(csv.DictReader(io.StringIO(archive.read(filename).decode("utf-8-sig"))))


def test_anonymous_statistics_export_is_rejected(statistics_client):
    client, _, _ = statistics_client
    response = client.post("/api/statistics/exports", json={}, headers={"Idempotency-Key": "first"})
    assert response.status_code == 401
    assert client.get("/api/statistics/exports/unknown").status_code == 401
    assert client.get("/api/statistics/exports/unknown/download").status_code == 401


def test_authenticated_statistics_export_creates_background_job(statistics_client):
    client, auth, manager = statistics_client
    response = client.post("/api/statistics/exports", json={}, headers={**auth, "Idempotency-Key": "first"})
    assert response.status_code == 202, response.text
    assert response.json()["exportId"].startswith("stats_")
    eid = response.json()["exportId"]
    assert response.json()["state"] == "queued"
    assert client.get(f"/api/statistics/exports/{eid}", headers=auth).json()["progress"]["completedTables"] == 0
    assert client.get(f"/api/statistics/exports/{eid}/download", headers=auth).status_code == 409
    assert not manager.database.name.endswith(".zip")


def test_zip_matches_script_schema_read_only_and_excludes_private_data(statistics_client):
    manager = statistics_client[2]
    original = hashlib.sha256(manager.database.read_bytes()).hexdigest()
    eid = ready(statistics_client)
    assert hashlib.sha256(manager.database.read_bytes()).hexdigest() == original
    with zip_data(statistics_client, eid) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schemaVersion"] == "annotation-confirmation-statistics-v1"
        assert manifest["exportId"] == eid and manifest["exportMode"] == "full"
        assert manifest["sourceDatabaseName"] == manager.database.name
        with db.connect() as conn:
            assert manifest["sourceSystemId"] == conn.execute("SELECT source_system_id FROM audit_source_identity").fetchone()[0]
        assert set(archive.namelist()) == {spec.filename for spec in export.EXPORT_SPECS} | {"manifest.json"}
        assert len(manifest["files"]) == 17
        for item in manifest["files"]:
            content = archive.read(item["name"])
            assert hashlib.sha256(content).hexdigest() == item["sha256"]
            assert len(content) == item["size"]
            rows = [json.loads(row) for row in content.decode("utf-8").splitlines()] if item["format"] == "jsonl" else csv_rows(archive, item["name"])
            assert len(rows) == item["rowCount"]
        users = csv_rows(archive, "users.csv")
        assert set(users[0]) == {"id", "username", "created_at"}
        assert len(users) == 3  # All accounts, including other workflow actors.
        assert all(b"never-export-this-password-hash" not in archive.read(name) for name in archive.namelist())
        final_frames = [json.loads(x) for x in archive.read("final_version_frames.jsonl").decode().splitlines()]
        assert [f["frame_index"] for f in final_frames] == [0, 1, 2]
        assert final_frames[2]["objects_json"] == []
        assert csv_rows(archive, "final_versions.csv")[0]["is_current"] == "1"
    assert not list(manager.runtime.glob(".work-*"))
    assert not list(manager.runtime.glob(".snapshot-*"))


def test_idempotency_replays_same_task_and_isolated_accounts(statistics_client):
    client, auth, manager = statistics_client
    first = create(statistics_client).json()
    assert create(statistics_client).json() == first
    other = {"Authorization": "Bearer " + sign_jwt({"uid": 2})}
    assert create(statistics_client, auth=other).status_code == 409
    for tail in ("", "/download"):
        assert client.get(f"/api/statistics/exports/{first['exportId']}{tail}", headers=other).status_code == 404
    manager.run(first["exportId"])
    assert create(statistics_client).json()["exportId"] == first["exportId"]
    next_job = create(statistics_client, auth=other)
    assert next_job.status_code == 202 and next_job.json()["exportId"] != first["exportId"]


def test_concurrent_same_key_allocates_one_job_and_other_intent_is_busy(statistics_client):
    manager = statistics_client[2]
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: manager.create(1, "same"), range(30)))
    assert len({item["exportId"] for item in answers}) == 1
    assert len(manager.jobs) == 1
    response = create(statistics_client, "different")
    assert response.status_code == 409 and response.json()["code"] == "STATISTICS_EXPORT_BUSY"


def test_key_body_and_unknown_expired_requests(statistics_client):
    client, auth, _ = statistics_client
    assert client.post("/api/statistics/exports", json={}, headers=auth).status_code == 428
    assert client.post("/api/statistics/exports", json={}, headers={**auth, "Idempotency-Key": " "}).status_code == 428
    assert client.post("/api/statistics/exports", json={"database": "arbitrary.db"}, headers={**auth, "Idempotency-Key": "first"}).status_code == 422
    for tail in ("", "/download"):
        response = client.get("/api/statistics/exports/unknown" + tail, headers=auth)
        assert response.status_code == 410 and response.json()["code"] == "STATISTICS_EXPORT_EXPIRED"


@pytest.mark.parametrize("fault", ["missing_table", "bad_json", "publication"])
def test_failed_generation_publishes_no_partial_and_new_task_can_recover(statistics_client, monkeypatch, caplog, fault):
    manager = statistics_client[2]
    if fault == "missing_table":
        with db.connect() as conn:
            conn.execute("ALTER TABLE baseline_frames RENAME TO saved_baseline_frames")
    elif fault == "bad_json":
        with db.connect() as conn:
            saved = conn.execute("SELECT objects_json FROM baseline_frames WHERE frame_index=0").fetchone()[0]
            conn.execute("UPDATE baseline_frames SET objects_json='invalid' WHERE frame_index=0")
    else:
        original_link = export.os.link
        monkeypatch.setattr(export.os, "link", lambda *args: (_ for _ in ()).throw(OSError("injected disk publication failure")))
    eid = create(statistics_client).json()["exportId"]
    manager.run(eid)
    status = manager.status(eid, 1)
    assert status["state"] == "failed" and not status["downloadAllowed"]
    assert status["error"]["code"] == "STATISTICS_EXPORT_FAILED"
    assert list(manager.runtime.iterdir()) == [manager.runtime / ".owner"]
    assert "statistics.failed" in caplog.text
    client, auth, _ = statistics_client
    assert client.get(f"/api/statistics/exports/{eid}/download", headers=auth).status_code == 409
    if fault == "missing_table":
        with db.connect() as conn:
            conn.execute("ALTER TABLE saved_baseline_frames RENAME TO baseline_frames")
    elif fault == "bad_json":
        with db.connect() as conn:
            conn.execute("UPDATE baseline_frames SET objects_json=? WHERE frame_index=0", (saved,))
    else:
        monkeypatch.setattr(export.os, "link", original_link)
    assert ready(statistics_client, "recovered") != eid


def test_optional_tables_can_be_omitted_explicitly(statistics_client):
    with db.connect() as conn:
        conn.execute("DROP TABLE confirmation_actions")
        conn.execute("DROP TABLE confirmation_reopen_events")
    eid = ready(statistics_client)
    with zip_data(statistics_client, eid) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["omittedOptionalTables"] == ["confirmation_actions", "confirmation_reopen_events"]
        assert len(manifest["files"]) == 15


def test_paginated_source_reading_avoids_fetchall(statistics_client, monkeypatch):
    with db.connect() as conn:
        conn.executemany("INSERT INTO users(username,password_hash) VALUES(?,?)", [(f"large-{i}", "private") for i in range(1100)])
    original = export.open_read_only
    batch_sizes = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
            self.description = cursor.description
        def __iter__(self):
            return iter(self.cursor)
        def fetchone(self):
            return self.cursor.fetchone()
        def fetchall(self):
            raise AssertionError("bulk fetchall is forbidden")
        def fetchmany(self, size):
            batch_sizes.append(size)
            return self.cursor.fetchmany(size)

    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, *args):
            return Cursor(self.conn.execute(*args))
        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(export, "open_read_only", lambda database: Connection(original(database)))
    eid = ready(statistics_client)
    with zip_data(statistics_client, eid) as archive:
        assert len(csv_rows(archive, "users.csv")) == 1103
    assert len(batch_sizes) > 17 and set(batch_sizes) == {256}


def test_default_delete_journal_source_writes_continue_during_slow_csv(statistics_client, monkeypatch):
    manager = statistics_client[2]
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        old_revision = conn.execute("SELECT revision FROM review_sessions").fetchone()[0]
    original = export.write_csv
    gate = threading.Event()
    proceed = threading.Event()

    def slow(path, columns, rows):
        if path.name == "users.csv":
            gate.set()
            assert proceed.wait(5), "source writer did not complete"
        return original(path, columns, rows)

    monkeypatch.setattr(export, "write_csv", slow)
    eid = create(statistics_client).json()["exportId"]
    with ThreadPoolExecutor() as pool:
        future = pool.submit(manager.run, eid)
        assert gate.wait(5)
        started = time.monotonic()
        try:
            with sqlite3.connect(manager.database, timeout=0.4) as writer:
                writer.execute("UPDATE users SET username='changed-during-export' WHERE id=1")
                writer.execute("UPDATE review_sessions SET revision=revision+1")
            assert time.monotonic() - started < 1
        finally:
            proceed.set()
        future.result(timeout=5)
    with zip_data(statistics_client, eid) as archive:
        assert csv_rows(archive, "users.csv")[0]["username"] == "A"
        assert int(csv_rows(archive, "review_sessions.csv")[0]["revision"]) == old_revision
    assert not list(manager.runtime.glob(".snapshot-*"))


def test_health_and_status_remain_responsive_during_background_export(statistics_client, monkeypatch):
    client, auth, manager = statistics_client
    entered = threading.Event()
    released = threading.Event()
    original = export.export_statistics_zip

    def blocked(*args, **kwargs):
        entered.set()
        assert released.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(export, "export_statistics_zip", blocked)
    monkeypatch.setattr(manager, "enqueue", lambda eid: jobs.ExportManager.enqueue(manager, eid))
    try:
        response = create(statistics_client)
        assert response.status_code == 202 and entered.wait(2)
        before = time.monotonic()
        assert client.get("/api/health").status_code == 200
        status = client.get("/api/statistics/exports/" + response.json()["exportId"], headers=auth)
        assert status.status_code == 200 and status.json()["state"] == "running"
        assert time.monotonic() - before < 1
    finally:
        released.set()
        manager.shutdown()
    assert manager.status(response.json()["exportId"], 1)["state"] == "ready"


def test_retention_expiry_keeps_unknown_retry_from_allocating_new_job(statistics_client):
    client, auth, manager = statistics_client
    eid = ready(statistics_client)
    path = manager.jobs[eid]["path"]
    manager.jobs[eid]["expires"] = time.time() - 1
    assert client.get(f"/api/statistics/exports/{eid}", headers=auth).status_code == 410
    assert not path.exists()
    assert create(statistics_client).status_code == 410
    assert ready(statistics_client, "new-intent") != eid


def test_package_count_limit_and_open_download_lease_are_safe(statistics_client, monkeypatch):
    manager = statistics_client[2]
    monkeypatch.setattr(jobs, "MAX_READY_PACKAGES", 1)
    first = ready(statistics_client)
    stream, _, _ = manager.download(first, 1)
    first_path = manager.jobs[first]["path"]
    second = ready(statistics_client, "second")
    assert first_path.exists() and stream.read(2) == b"PK"
    manager.release(first, stream)
    manager.release(first, stream)  # Stream/background cleanup is idempotent.
    assert manager.status(second, 1)["state"] == "ready"
    assert not first_path.exists()


def test_runtime_task_limits_and_restart_do_not_clear_other_storage(statistics_client, monkeypatch):
    manager = statistics_client[2]
    monkeypatch.setattr(jobs, "MAX_TASKS", 2)
    first = ready(statistics_client)
    ready(statistics_client, "second")
    assert create(statistics_client, "third").status_code == 429
    harmless = manager.root / "runtime_user-owned"
    harmless.mkdir()
    (harmless / "important.txt").write_text("leave this alone")
    restarted = jobs.ExportManager(manager.root, manager.database)
    with pytest.raises(Exception) as expired:
        restarted.status(first, 1)
    assert expired.value.code == "STATISTICS_EXPORT_EXPIRED"
    monkeypatch.setattr(restarted, "enqueue", lambda eid: None)
    restarted.create(1, "after-restart")
    assert (harmless / "important.txt").read_text() == "leave this alone"
    assert not manager.runtime.exists()
    restarted.shutdown()


def test_missing_zip_is_explicit_failure_not_success(statistics_client):
    client, auth, manager = statistics_client
    eid = ready(statistics_client)
    manager.jobs[eid]["path"].unlink()
    response = client.get(f"/api/statistics/exports/{eid}/download", headers=auth)
    assert response.status_code == 410 and response.json()["code"] == "STATISTICS_EXPORT_FILE_MISSING"
    assert manager.status(eid, 1)["state"] == "failed"


def test_existing_zip_is_never_overwritten_and_missing_identity_fails(statistics_client, tmp_path, monkeypatch):
    directory = tmp_path / "manual-output"
    directory.mkdir()
    existing = directory / "fixed.zip"
    existing.write_bytes(b"do not replace")
    with pytest.raises(export.ExportError):
        export.export_statistics_zip(db.DB_FILE, directory, output_name="fixed.zip")
    assert existing.read_bytes() == b"do not replace"
    assert not list(directory.glob(".snapshot-*"))
    with db.connect() as conn:
        conn.execute("DROP TABLE audit_source_identity")
    monkeypatch.delenv("STATISTICS_SOURCE_SYSTEM_ID", raising=False)
    with pytest.raises(export.ExportError, match="部署身份"):
        export.export_statistics_zip(db.DB_FILE, directory, output_name="missing-identity.zip")
    monkeypatch.setenv("STATISTICS_SOURCE_SYSTEM_ID", "stable-legacy-site")
    recovered = export.export_statistics_zip(db.DB_FILE, directory, output_name="recovered.zip")
    with zipfile.ZipFile(recovered) as archive:
        assert json.loads(archive.read("manifest.json"))["sourceSystemId"] == "stable-legacy-site"


def test_stream_error_releases_open_download_lease(statistics_client, monkeypatch):
    from app import statistics_export_routes as routes
    manager = statistics_client[2]
    eid = ready(statistics_client)
    released = []

    class BrokenStream:
        def read(self, _):
            raise OSError("injected interrupted download")
        def close(self):
            released.append(True)

    broken = BrokenStream()
    monkeypatch.setattr(manager, "download", lambda *args: (broken, "statistics.zip", 1))
    response = routes.download(eid, {"uid": 1})
    iterator = response.body_iterator

    async def consume():
        with pytest.raises(OSError):
            await anext(iterator)

    import asyncio
    asyncio.run(consume())
    assert released


def test_client_disconnect_releases_lease_without_waiting_for_gc(statistics_client):
    import asyncio
    from starlette.requests import ClientDisconnect
    from app import statistics_export_routes as routes

    manager = statistics_client[2]
    eid = ready(statistics_client)
    response = routes.download(eid, {"uid": 1})
    assert manager.jobs[eid]["leases"]

    async def closed_client(message):
        if message["type"] == "http.response.body":
            raise OSError("injected disconnected client")

    async def receive():
        return {"type": "http.disconnect"}

    async def exercise():
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, closed_client)
        assert not manager.jobs[eid]["leases"]

    asyncio.run(exercise())


def test_current_final_query_aggregates_once_and_preserves_historical_rules(statistics_client):
    spec = next(s for s in export.EXPORT_SPECS if s.table == "final_versions")
    with db.connect() as conn:
        original = dict(conn.execute("SELECT * FROM final_versions").fetchone())
        session = dict(conn.execute("SELECT * FROM confirmation_sessions").fetchone())
        session_columns = list(session)
        final_columns = list(original)
        rows = []
        # Many independently confirmed sessions produce the original expensive
        # reverse scan once per row; three historical F versions per session.
        for index in range(80):
            extra_session = {**session, "id": f"statistics-session-{index}", "state": "in_progress" if index % 2 else "confirmed"}
            conn.execute(
                "INSERT INTO confirmation_sessions (" + ",".join(session_columns) + ") VALUES (" + ",".join("?" for _ in session_columns) + ")",
                [extra_session[c] for c in session_columns],
            )
            for historical in range(3):
                extra_final = {**original, "id": f"statistics-final-{index}-{historical}", "confirmation_id": extra_session["id"]}
                rows.append([extra_final[c] for c in final_columns])
        conn.executemany(
            "INSERT INTO final_versions (" + ",".join(final_columns) + ") VALUES (" + ",".join("?" for _ in final_columns) + ")", rows,
        )
        _, selected = export.select_rows(conn, spec)
        results = list(selected)
        assert len(results) == 241
        current_ids = {row["id"] for row in results if row["is_current"]}
        assert current_ids == {original["id"]} | {f"statistics-final-{i}-2" for i in range(0, 80, 2)}
        plan = [str(row[3]).upper() for row in conn.execute("EXPLAIN QUERY PLAN " + spec.query)]
        assert not any("CORRELATED" in step for step in plan), plan
        assert not any("SCAN NEWEST" in step for step in plan), plan


def test_job_and_receipt_ttl_expired_allows_same_key_new_snapshot(statistics_client):
    client, auth, manager = statistics_client
    old = ready(statistics_client)
    old_path = manager.jobs[old]["path"]
    manager.jobs[old]["expires"] = time.time() - 1
    manager.receipts[(1, "first")] = (old, time.time() - 1)
    response = create(statistics_client)
    assert response.status_code == 202
    new = response.json()["exportId"]
    assert new != old and not old_path.exists()
    assert client.get(f"/api/statistics/exports/{old}", headers=auth).status_code == 410
    assert client.get(f"/api/statistics/exports/{new}", headers=auth).json()["state"] == "queued"
    manager.run(new)
    with zip_data(statistics_client, new) as archive:
        assert json.loads(archive.read("manifest.json"))["exportId"] == new
