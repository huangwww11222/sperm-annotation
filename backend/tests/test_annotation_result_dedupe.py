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
