from __future__ import annotations

from app import db


def _row(user_id: int, object_name: str, object_id: str = "1") -> dict:
    return {
        "user_id": user_id,
        "batch_id": f"batch-{object_name}",
        "object_id": object_id,
        "media_id": "server-video",
        "media_name": "same-video.avi",
        "media_type": "video",
        "media_width": 100,
        "media_height": 50,
        "frame_index": 3,
        "timestamp_ms": 300,
        "object_name": object_name,
        "source": "manual",
        "confidence": None,
        "shape_type": "bbox",
        "pct_x": 1,
        "pct_y": 2,
        "pct_w": 3,
        "pct_h": 4,
        "px_x1": 1,
        "px_y1": 1,
        "px_x2": 4,
        "px_y2": 5,
        "px_point_x": None,
        "px_point_y": None,
        "annotation_version": "test",
        "raw_json": "{}",
    }


def test_results_keep_only_latest_logical_annotation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "app.db")
    db.init_db()
    user_id = db.create_user("tester", "hash")
    db.insert_annotations([_row(user_id, "old-name")])
    db.insert_annotations([_row(user_id, "new-name")])

    rows = db.list_annotations(None, source="manual")

    assert len(rows) == 1
    assert rows[0]["object_name"] == "new-name"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 1


def test_results_keep_distinct_media_with_same_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_FILE', tmp_path / 'app.db')
    db.init_db()
    uid = db.create_user('tester', 'hash')
    db.insert_annotations([_row(uid, 'first'), {**_row(uid, 'second'), 'media_id': 'server-video-2'}])
    rows = db.list_annotations(None, source='manual')
    assert {r['media_id'] for r in rows} == {'server-video', 'server-video-2'}
    assert len(db.list_annotations(uid, 'server-video-2', source='manual')) == 1
    assert db.list_annotations(uid, source='sam3') == []
    assert db.list_annotations(uid + 1) == []


def test_old_database_adds_batch_before_creating_index(monkeypatch, tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE annotations (id INTEGER PRIMARY KEY, user_id INTEGER, media_id TEXT)')
        conn.execute("INSERT INTO annotations VALUES (1, 1, 'preserved')")
    monkeypatch.setattr(db, 'DB_FILE', path)
    db.init_db()
    db.init_db()
    with db.connect() as conn:
        assert 'batch_id' in {r[1] for r in conn.execute('PRAGMA table_info(annotations)')}
        assert conn.execute('SELECT media_id FROM annotations WHERE id=1').fetchone()[0] == 'preserved'
        assert 'idx_ann_batch' in {r[1] for r in conn.execute('PRAGMA index_list(annotations)')}


def test_results_api_accepts_source_filter_and_keeps_shared_manual_records(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import current_user
    monkeypatch.setattr(db, 'DB_FILE', tmp_path / 'api.db')
    db.init_db()
    first = db.create_user('first', 'hash')
    second = db.create_user('second', 'hash')
    db.insert_annotations([_row(first, 'first'), _row(second, 'second'), {**_row(second, 'ai'), 'source': 'sam3'}])
    app.dependency_overrides[current_user] = lambda: {'uid': first, 'username': 'first'}
    try:
        client = TestClient(app)
        response = client.get('/api/annotation/projects/default/results')
        assert response.status_code == 200
        assert response.json()['total'] == 2
        assert {r['user_id'] for r in response.json()['items']} == {first, second}
        assert client.get('/api/annotation/media/server-video').json()['total'] == 2
    finally:
        app.dependency_overrides.pop(current_user, None)
