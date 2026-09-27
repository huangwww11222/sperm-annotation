"""Real-video package checks and workflow bypass/failure regression tests."""

import json
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml
from app import confirmation_workflow as confirm
from app import db, main
from app import review_workflow as review
from app import training_export as export
from app.auth import sign_jwt
from app.review_repository import (
    compute_file_sha256,
    create_review_session,
    freeze_baseline,
    upsert_media_revision,
)
from app.training_export_routes import legacy_router, router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .test_confirmation_workflow import confirmation as confirmation
from .test_confirmation_workflow import finish, write
from .test_review_workflow import task as task


def video_file(directory, w=640, h=480, n=3, seed=50):
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / "original.avi"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
    for i in range(n):
        out.write(np.full((h, w, 3), seed + 30 * i, dtype=np.uint8))
    out.release()
    (directory / "media.json").write_text(json.dumps({"videoName": video.name}))
    return video


@pytest.fixture
def frozen(confirmation, tmp_path, monkeypatch):
    monkeypatch.setattr(export, "DATASET_EXPORT_DIR", tmp_path / "datasets")
    monkeypatch.setattr(main, "TRACK_DATA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "LEGACY_TRACK_DATA_DIR", tmp_path / "legacy")
    monkeypatch.setattr(export, "enqueue", lambda eid: None)
    video = video_file(main.TRACK_DATA_DIR / "test-video")
    s = confirm.get_session(confirmation, 3)
    with db.connect() as c:
        export.migrate(c)
        c.execute(
            "UPDATE media_revisions SET sha256=?,file_size=?",
            (compute_file_sha256(video), video.stat().st_size),
        )
        for table, col, key in [
            ("baseline_frames", "baseline_id", s["baselineId"]),
            ("review_version_frames", "version_id", s["reviewVersionId"]),
        ]:
            for fi in [0, 1]:
                row = c.execute(
                    f"SELECT objects_json FROM {table} WHERE {col}=? AND frame_index=?",
                    (key, fi),
                ).fetchone()
                objs = json.loads(row[0])
                if fi == 1:
                    objs[0]["classKey"] = "CD4"
                else:
                    objs.append(
                        {
                            "objectId": 2,
                            "annotationId": "stable-unchanged",
                            "bbox": [100.125, 110.25, 130.5, 140.625],
                            "classKey": "rare: 'sperm' #α",
                        }
                    )
                c.execute(
                    f"UPDATE {table} SET objects_json=? WHERE {col}=? AND frame_index=?",
                    (json.dumps(objs), key, fi),
                )
    final = finish(confirmation)["session"]["finalVersionId"]
    return {"cid": confirmation, "vid": final, "video": video, "root": tmp_path}


def create(frozen, fmt="yolo", key=None, ids=None):
    return export.create(
        3,
        key or uuid.uuid4().hex,
        {"finalVersionIds": ids or [frozen["vid"]], "format": fmt, "splitRatio": 0.8},
    )


def ready(frozen, fmt="yolo", ids=None):
    job = create(frozen, fmt, ids=ids)
    export.run(job["exportId"])
    state = export.status(job["exportId"], 3)
    assert state["state"] == "ready", state
    return state


@pytest.mark.parametrize("fmt", ["yolo", "coco", "both"])
def test_zip_contains_real_frames_and_final_labels(frozen, fmt):
    job = ready(frozen, fmt)
    with zipfile.ZipFile(export.download(job["exportId"], 3)) as z:
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json"))
        images = [x for x in names if x.endswith(".jpg")]
        samples = {s["frameIndex"]: s for s in manifest["samples"]}
        assert (
            len(images) == 3
            and manifest["counts"]["train"] == 2
            and manifest["counts"]["val"] == 1
        )
        assert (
            manifest["counts"]["emptyFrames"] == 1
            and manifest["counts"]["objectCount"] == 3
        )
        assert len(set(images)) == 3
        decoded = cv2.imdecode(
            np.frombuffer(z.read("images/train/v000_frame_000001.jpg"), np.uint8),
            cv2.IMREAD_COLOR,
        )
        assert decoded.shape[:2] == (480, 640) and 75 <= decoded.mean() <= 85
        assert (
            len(json.loads(z.read("provenance/" + frozen["vid"] + ".json"))["frames"])
            == 3
        )
        if fmt != "coco":
            config = yaml.safe_load(z.read("data.yaml"))
            assert "path" not in config
            assert config["train"] == "images/train" and config["val"] == "images/val"
            assert config["names"] == {0: "CD4", 1: "rare: 'sperm' #α", 2: "sperm"}
            assert z.read(samples[2]["label"]) == b""
            row = list(map(float, z.read("labels/train/v000_frame_000001.txt").split()))
            assert row == pytest.approx(
                [0, 23.333 / 640, 30 / 480, 20 / 640, 20 / 480], abs=1e-10
            )
            lines = z.read(samples[0]["label"]).decode().splitlines()
            assert list(map(float, lines[0].split())) == pytest.approx(
                [2, 20 / 640, 30 / 480, 20 / 640, 20 / 480], abs=1e-10
            )
            assert (
                len(lines) == 2
            )  # includes unchanged object, preserving subpixel geometry
            assert len(
                [x for x in names if x.startswith("labels/") and x.endswith(".txt")]
            ) == len(images)
        else:
            assert "data.yaml" not in names
        if fmt != "yolo":
            co = json.loads(z.read("annotations/instances_train.json"))
            val = json.loads(z.read("annotations/instances_val.json"))
            co["images"] += val["images"]
            co["annotations"] += val["annotations"]
            co["annotations"].sort(key=lambda obj: obj["id"])
            assert len(co["images"]) == 3 and len(co["annotations"]) == 3
            assert co["annotations"][2]["bbox"] == pytest.approx([13.333, 20, 20, 20])
            for item in co["images"]:
                assert (
                    (Path("annotations") / item["file_name"]).as_posix().replace(
                        "annotations/../", ""
                    )
                    in names
                )
        else:
            assert "annotations/instances_train.json" not in names
    assert not list((export.DATASET_EXPORT_DIR / ".work").iterdir())


def test_current_head_and_completed_review_are_required(frozen):
    cid = frozen["cid"]
    job = ready(frozen)
    write(cid, "reopen")
    for action in [
        lambda: create(frozen),
        lambda: export.preview([frozen["vid"]]),
        lambda: export.download(job["exportId"], 3),
    ]:
        with pytest.raises(review.ReviewError) as e:
            action()
        assert e.value.code == "CONFIRMATION_REQUIRED"
    assert export.status(job["exportId"], 3)["state"] == "invalidated"
    new = write(cid, "finish")["session"]["finalVersionId"]
    with pytest.raises(review.ReviewError):
        create(frozen)
    assert export.preview([new])["frameCount"] == 3
    with db.connect() as c:
        c.execute("UPDATE review_sessions SET state='in_progress'")
    with pytest.raises(review.ReviewError) as e:
        export.preview([new])
    assert e.value.code == "REVIEW_REQUIRED"


def test_unconfirmed_changes_and_incomplete_snapshots_cannot_export(frozen):
    with db.connect() as c:
        c.execute(
            "DELETE FROM decision_heads WHERE confirmation_id=?", (frozen["cid"],)
        )
    with pytest.raises(review.ReviewError) as e:
        create(frozen)
    assert e.value.code == "UNDECIDED_CHANGES"


def test_snapshot_hash_detects_tampered_coordinates(frozen):
    with db.connect() as c:
        c.execute(
            "UPDATE final_version_frames SET objects_json='[]' WHERE frame_index=0"
        )
    with pytest.raises(review.ReviewError) as e:
        create(frozen)
    assert e.value.code == "FINAL_SNAPSHOT_CHANGED"


def test_migration_does_not_accept_legacy_final_without_full_frames(frozen):
    with db.connect() as c:
        c.execute("DELETE FROM final_version_frames WHERE frame_index=2")
    with pytest.raises(review.ReviewError) as e:
        create(frozen)
    assert e.value.code == "INCOMPLETE_FINAL_VERSION"


def test_idempotent_create_queues_once_and_rejects_changed_settings(
    frozen, monkeypatch
):
    enqueued = []
    monkeypatch.setattr(export, "enqueue", enqueued.append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: create(frozen, key="lost-response"), range(2))
        )
    assert results[0]["exportId"] == results[1]["exportId"] and len(enqueued) == 1
    with pytest.raises(review.ReviewError) as e:
        create(frozen, "both", key="lost-response")
    assert e.value.code == "IDEMPOTENCY_KEY_REUSED"
    export.run(results[0]["exportId"])
    assert create(frozen, key="lost-response")["state"] == "ready"


@pytest.mark.parametrize(
    "failure",
    ["missing", "hash", "decode", "dimensions", "image_write", "reopen", "publish"],
)
def test_export_fails_without_publishing_partial_training_zip(
    frozen, monkeypatch, failure, caplog
):
    job = create(frozen)
    eid = job["exportId"]
    if failure == "missing":
        frozen["video"].unlink()
    if failure == "hash":
        frozen["video"].write_bytes(b"changed video")
    if failure in ("decode", "dimensions"):
        real = cv2.VideoCapture

        class BrokenCapture:
            def __init__(self, path):
                self.cap = real(path)
                self.n = 0

            def isOpened(self):
                return self.cap.isOpened()

            def read(self):
                self.n += 1
                if failure == "decode" and self.n == 2:
                    return False, None
                ok, img = self.cap.read()
                return ok, img[:400] if failure == "dimensions" else img

            def release(self):
                self.cap.release()

        monkeypatch.setattr(cv2, "VideoCapture", BrokenCapture)
    if failure == "image_write":
        monkeypatch.setattr(cv2, "imencode", lambda *a, **kw: (False, None))
    if failure == "reopen":
        original = cv2.imencode
        called = False

        def save(*args, **kwargs):
            nonlocal called
            if not called:
                called = True
                write(frozen["cid"], "reopen")
            return original(*args, **kwargs)

        monkeypatch.setattr(cv2, "imencode", save)
    if failure == "publish":
        with db.connect() as c:
            c.execute(
                "CREATE TRIGGER publish_fail BEFORE UPDATE ON training_exports WHEN NEW.state='ready' BEGIN SELECT RAISE(ABORT,'injected publication failure'); END"
            )
    export.run(eid)
    result = export.status(eid, 3)
    assert result["state"] in ("failed", "invalidated") and result["errorCode"]
    assert "dataset.failed" in caplog.text
    assert not (export.DATASET_EXPORT_DIR / (eid + ".zip")).exists()
    assert not (export.DATASET_EXPORT_DIR / (eid + ".zip.partial")).exists()
    assert not (export.DATASET_EXPORT_DIR / ".work" / eid).exists()
    with pytest.raises(review.ReviewError):
        export.download(eid, 3)


def test_an_already_claimed_job_cannot_remove_another_workers_files(frozen):
    job = create(frozen)
    eid = job["exportId"]
    work = export.DATASET_EXPORT_DIR / ".work" / eid
    work.mkdir(parents=True)
    (work / "in-progress.txt").write_text("active")
    with db.connect() as c:
        c.execute("UPDATE training_exports SET state='running' WHERE id=?", (eid,))
    export.run(eid)
    assert (work / "in-progress.txt").exists()


def test_restart_marks_interrupted_job_failed(frozen):
    job = create(frozen)
    export.recover_interrupted()
    assert export.status(job["exportId"], 3)["errorCode"] == "EXPORT_INTERRUPTED"


def test_source_metadata_and_raw_workspace_do_not_override_final(frozen):
    (frozen["video"].parent / "workspace_state.json").write_text(
        '{"manualAnnotations":[]}'
    )
    (frozen["video"].parent / "tracker_results.json").write_text(
        "invalid mutable tracking data"
    )
    assert ready(frozen)["manifest"]["counts"]["objectCount"] == 3


def test_multiple_video_versions_are_all_exported_without_filename_collisions(frozen):
    directory = main.TRACK_DATA_DIR / "second"
    video = video_file(directory, w=120, h=90, n=2, seed=80)
    media = upsert_media_revision(
        "second", compute_file_sha256(video), video.stat().st_size, 120, 90, 30, 2
    )
    frames = [
        {
            "frameIndex": i,
            "coverage": "objects",
            "objects": [{"objectId": 1, "bbox": [1, 2, 11, 12], "classKey": "sperm"}],
        }
        for i in range(2)
    ]
    a = freeze_baseline(media, "second", 1, frames)
    sid = create_review_session(a["id"])["id"]
    review.write("claim", sid, 2, "second-claim", {})
    for fi in range(2):
        review.write(
            "submit", sid, 2, str(fi), {"expectedFrameRevision": 0, "patch": []}, fi
        )
    r = review.write(
        "finish",
        sid,
        2,
        "finish-second",
        {"expectedSessionRevision": review.get_session(sid, 2)["revision"]},
    )
    cid = r["confirmationSessionId"]
    write(cid, "claim")
    vid = write(cid, "finish")["session"]["finalVersionId"]
    job = ready(frozen, ids=[frozen["vid"], vid])
    assert (
        job["manifest"]["counts"]["frameCount"] == 5
        and len(job["manifest"]["sources"]) == 2
    )
    with zipfile.ZipFile(export.download(job["exportId"], 3)) as z:
        assert len([x for x in z.namelist() if x.endswith(".jpg")]) == 5
        row = list(map(float, z.read("labels/val/v001_frame_000001.txt").split()))
        assert row[1:] == pytest.approx([6 / 120, 7 / 90, 10 / 120, 10 / 90], abs=1e-10)


def test_split_has_no_overlap_and_moves_positive_sample_into_training():
    versions = [
        {
            "frames": [
                {"frameIndex": i, "objects": [1] if i == 4 else []} for i in range(5)
            ]
        }
    ]
    splits = export.split_samples(versions, 0.8)
    assert (
        len(splits) == 5
        and splits[0, 4] == "train"
        and list(splits.values()).count("val") == 1
    )


def test_authenticated_routes_block_old_export_and_annotation_payloads(frozen):
    app = FastAPI()
    app.include_router(router)
    app.include_router(legacy_router)
    client = TestClient(app)
    assert client.post("/api/export/dataset", json={}).status_code == 401
    client.headers["Authorization"] = "Bearer " + sign_jwt({"uid": 3})
    legacy = client.post(
        "/api/export/dataset", json={"annotations": [{"bbox": [0, 0, 10, 10]}]}
    )
    assert (
        legacy.status_code == 410
        and legacy.json()["code"] == "FINAL_CONFIRMATION_REQUIRED"
    )
    url = "/api/datasets/exports"
    body = {"finalVersionIds": [frozen["vid"]], "format": "yolo", "splitRatio": 0.8}
    assert client.post(url, json=body).status_code == 428
    client.headers.update({"X-Review-Contract": "2", "Idempotency-Key": "api-create"})
    for patch in [
        {"annotations": []},
        {"mediaId": "raw-video"},
        {"format": "invalid"},
        {"splitRatio": 0},
        {"splitRatio": 1},
        {"splitRatio": True},
    ]:
        assert client.post(url, json={**body, **patch}).status_code == 422
    created = client.post(url, json=body)
    assert created.status_code == 200
    eid = created.json()["exportId"]
    assert client.get(url + "/" + eid + "/download").status_code == 409
    export.run(eid)
    download = client.get(url + "/" + eid + "/download")
    assert (
        download.status_code == 200
        and download.headers["content-type"] == "application/zip"
    )
    assert download.headers["X-Request-ID"]
    assert client.get(url + "/old-ungated-package/download").status_code == 404
    client.headers["Authorization"] = "Bearer " + sign_jwt({"uid": 1})
    assert (
        client.get(url + "/" + eid).status_code == 404
        and client.get(url + "/" + eid + "/download").status_code == 404
    )
    client.headers.pop("Authorization")
    assert client.get(url + "/" + eid + "/download").status_code == 401


def test_split_keeps_validation_positive_when_two_positive_frames_exist():
    versions = [
        {
            "frames": [
                {"frameIndex": i, "objects": [1] if i < 2 else []} for i in range(5)
            ]
        }
    ]
    splits = export.split_samples(versions, 0.8)
    assert splits[0, 0] != splits[0, 1]
    assert sum(s == "train" for s in splits.values()) == 4
