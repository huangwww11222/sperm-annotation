"""Independent behavior probes. Failures are audit findings, not fix requests.

Use disposable APP_* paths before importing app. No production source is patched;
the only injected failure is a test-side filesystem write/read error.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from app import annotation_state, db, main, tracker
from .test_media_reimport import imports, upload
from .test_review_workflow import task  # fixture dependency


def evidence(name, **values):
    record = {"case": name, **values}
    print(json.dumps(record, ensure_ascii=False))
    if target := os.environ.get("INDEPENDENT_AUDIT_EVIDENCE"):
        with Path(target).open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


def manual(**overrides):
    return {"id": "manual-0-7", "source": "manual", "objectId": 7,
            "frameIndex": 0, "name": "audit object",
            "bbox": {"x": 10, "y": 10, "width": 20, "height": 20}, **overrides}


def save(client, mid, objects, revision=0, key="audit-save", **extra):
    return client.put("/api/track/workspace/" + mid,
                      json={"manualAnnotations": objects, "expectedRevision": revision, **extra},
                      headers={"Idempotency-Key": key})


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-token"])
def test_raw_source_video_obeys_the_same_login_boundary_as_other_media_reads(imports, authorization):
    client, content = imports
    mid = upload(client, content).json()["mediaId"]
    assert client.get("/api/track/video/"+mid).content == content
    anonymous = TestClient(main.app)
    if authorization is not None:
        anonymous.headers["Authorization"] = authorization
    protected = {route: anonymous.get("/api/track/"+route+"/"+mid).status_code
                 for route in ("workspace", "result")}
    source = anonymous.get("/api/track/video/"+mid)
    evidence("raw-video-login-boundary", supplied_invalid_token=authorization is not None,
             protected_statuses=protected, video_status=source.status_code,
             source_bytes_equal=source.content==content, returned_bytes=len(source.content))
    assert set(protected.values()) == {401}
    assert source.status_code == 401, "knowing a mediaId must not grant anonymous access to the original video"
    assert source.content != content


@pytest.mark.parametrize("identity", ["object_id", "objectId", "sam3_object_id"])
def test_accepted_tracking_identity_is_preserved_and_deleted_everywhere(imports, identity):
    client, content = imports
    mid = upload(client, content).json()["mediaId"]
    root = main.media_dir(mid)
    tracker._write_jsonl(root / "tracker_results.json", [{"frame_index": 0,
        "objects": [{identity: oid, "bbox": [1, 2, 10, 12]} for oid in (7, 17)]}])
    assert save(client, mid, []).status_code == 200
    first = client.get("/api/track/result/" + mid)
    assert first.status_code == 200, first.text
    assert save(client, mid, [], 1, "delete-17", deletedObjectIds=[17]).status_code == 200
    restored = TestClient(main.app)
    restored.headers.update(client.headers)
    after = restored.get("/api/track/result/" + mid).json()
    raw = restored.get("/api/track/result-file/" + mid)
    first_ids = [o["objectId"] for o in first.json()["frames"][0]["annotations"]]
    after_ids = [o["objectId"] for o in after["frames"][0]["annotations"]]
    raw_ids = [o.get(identity) for o in json.loads(raw.text)["objects"]]
    evidence("identity-" + identity, first_ids=first_ids, after_ids=after_ids,
             raw_ids=raw_ids, tombstone=restored.get("/api/track/workspace/"+mid).json()["deletedObjectIds"])
    assert first_ids == [7, 17], "an accepted identity must not become zero or collapse"
    assert after_ids == raw_ids == [7], "a deleted stable identity must stay deleted in every reader"


def test_original_frame_number_agrees_between_result_reader_and_review_source(imports):
    client, content = imports
    mid = upload(client, content).json()["mediaId"]
    tracker._write_jsonl(main.media_dir(mid) / "tracker_results.json", [
        {"frame_index": 0, "source_frame_index": 2,
         "objects": [{"object_id": 7, "bbox": [1, 2, 10, 12]}]}])
    assert save(client, mid, []).status_code == 200
    result = client.get("/api/track/result/"+mid).json()
    preview = client.get("/api/review/media/"+mid+"/completion-preview").json()
    evidence("original-frame", result_frames=[f["frameIndex"] for f in result["frames"]],
             unknown_ranges=preview["unknownFrameRanges"])
    assert [f["frameIndex"] for f in result["frames"]] == [2], "source_frame_index is the original frame identity"
    assert preview["unknownFrameRanges"] == [{"start": 0, "end": 1}]


BAD_OBJECTS = [
    pytest.param([manual(objectId=0)], id="zero-identity"),
    pytest.param([manual(), manual(id="other-ui-id")], id="duplicate-stable-identity"),
    pytest.param([manual(frameIndex=-1)], id="negative-frame"),
    pytest.param([manual(frameIndex=3)], id="past-final-frame"),
    pytest.param([manual(bbox={"x": 10, "y": 10, "width": -1, "height": 20})], id="negative-extent"),
    pytest.param([manual(bbox={"x": 95, "y": 10, "width": 20, "height": 20})], id="outside-source"),
]


@pytest.mark.parametrize("objects", BAD_OBJECTS)
def test_invalid_workspace_write_is_rejected_without_poisoning_durable_state(imports, objects):
    client, content = imports
    mid = upload(client, content).json()["mediaId"]
    assert save(client, mid, [manual()]).status_code == 200
    root = main.media_dir(mid)
    before = (root / "workspace_state.json").read_bytes()
    response = save(client, mid, objects, 1, "invalid-input")
    fresh = TestClient(main.app)
    fresh.headers.update(client.headers)
    persisted = fresh.get("/api/track/workspace/"+mid).json()
    preview = fresh.get("/api/review/media/"+mid+"/completion-preview")
    preserved = (root/"workspace_state.json").read_bytes() == before
    # Verify a healthy repair of test data without changing the invalid-write
    # expectation or concealing the already-recorded persistence failure.
    recovery = save(client, mid, [manual()], persisted["revision"], "valid-input-after-fault")
    recovered = fresh.get("/api/track/workspace/"+mid).json()
    assert recovery.status_code == 200 and recovered["manualAnnotations"] == [manual()]
    assert fresh.get("/api/review/media/"+mid+"/completion-preview").status_code == 200
    evidence("invalid-workspace", input=objects, status=response.status_code,
             persisted_objects=persisted.get("manualAnnotations"), revision=persisted["revision"],
             bytes_unchanged=preserved, completion_status=preview.status_code, completion_body=preview.json(),
             recovery_status=recovery.status_code, recovered_objects=recovered["manualAnnotations"])
    assert response.status_code == 422, "invalid geometry/identity must fail before a successful save receipt"
    assert preserved
    assert persisted["revision"] == 1 and persisted["manualAnnotations"] == [manual()]
    assert preview.status_code == 200


def seed(mid, x=5):
    return {"mediaId": mid, "frameIndex": 0, "mediaWidth": 64, "mediaHeight": 48,
            "annotations": [{"object_id": 7, "source": "manual", "bbox": [x, 5, x+10, 15]}]}


def test_failed_seed_write_keeps_legacy_manual_source_and_recovers(imports, monkeypatch):
    client, content = imports
    mid = upload(client, content).json()["mediaId"]
    assert client.post("/api/track/annotations", json=seed(mid)).status_code == 201
    root = main.media_dir(mid)
    path = root / "annotations_frame_000000.json"
    before = path.read_bytes()
    restored_before = client.get("/api/track/workspace/"+mid).json()["manualAnnotations"]
    original = Path.write_text
    def disk_full(file, *args, **kwargs):
        if file == path or (file.parent == path.parent and file.name.startswith('.'+path.name+'.')):
            # Simulate the actual open/truncate/partial-write behavior of ENOSPC.
            original(file, '{"partial":', encoding="utf-8")
            raise OSError("injected disk full after partial seed write")
        return original(file, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", disk_full)
        failing = TestClient(main.app, raise_server_exceptions=False)
        failing.headers.update(client.headers)
        response = failing.post("/api/track/annotations", json=seed(mid, 8))
    preserved = path.read_bytes() == before
    during_failure = client.get("/api/track/workspace/"+mid).json()
    # Complete the recovery side even if the preservation expectation fails.
    retry = client.post("/api/track/annotations", json=seed(mid, 8))
    recovered = client.get("/api/track/workspace/"+mid).json()
    evidence("partial-seed-write", failure_status=response.status_code,
             prior_manual=restored_before, old_bytes_preserved=preserved,
             state_after_failure=during_failure, retry_status=retry.status_code,
             recovered_manual=recovered.get("manualAnnotations"))
    assert retry.status_code == 201 and recovered.get("manualAnnotations"), "healthy retry must save and reload real boxes"
    assert response.status_code >= 500
    assert preserved, "a failed seed write must not truncate the previous durable manual source"
    assert during_failure.get("manualAnnotations") == restored_before


@pytest.mark.parametrize("mid", [".", ".."])
def test_seed_rejects_noncanonical_media_without_writing_storage_root(imports, mid):
    client, content = imports
    valid_mid = upload(client, content).json()["mediaId"]
    assert client.post("/api/track/annotations", json=seed(valid_mid)).status_code == 201
    valid_seed = main.media_dir(valid_mid)/"annotations_frame_000000.json"
    valid_bytes = valid_seed.read_bytes()
    # Prepare the unsafe target without going through the now-validating resolver.
    target = main.TRACK_DATA_DIR / mid / "annotations_frame_000000.json"
    assert not target.exists()
    response = client.post("/api/track/annotations", json=seed(mid))
    evidence("noncanonical-seed", input=mid, status=response.status_code,
             outside_media_write=target.exists())
    assert valid_seed.read_bytes() == valid_bytes
    assert response.status_code in (400, 404, 422), "a media ID must select one existing media directory"
    assert not target.exists()


def test_late_seed_request_does_not_recreate_deleted_media(imports):
    client, content = imports
    mid = upload(client, content).json()["mediaId"]
    assert client.post("/api/track/annotations", json=seed(mid)).status_code == 201
    assert client.delete("/api/track/media/"+mid).status_code == 200
    root = main.media_dir(mid)
    assert not root.exists()
    delayed = client.post("/api/track/annotations", json=seed(mid))
    # Capture before a legitimate reupload may reuse the old media directory.
    orphan_recreated = root.exists()
    new_upload = upload(client, content)
    assert new_upload.status_code == 201
    reuploaded = new_upload.json()["mediaId"]
    assert client.get("/api/track/result/"+reuploaded).json()["state"] == "not_generated"
    assert client.post("/api/track/annotations", json=seed(reuploaded)).status_code == 201
    assert client.get("/api/track/workspace/"+reuploaded).json().get("manualAnnotations")
    evidence("delete-late-seed", status=delayed.status_code, orphan_recreated=orphan_recreated,
             reupload_same_identity=reuploaded==mid)
    assert delayed.status_code == 404, "a request sent before deletion cannot recreate a seed-only media"
    assert not orphan_recreated


def test_workspace_read_fault_retains_bytes_and_allows_a_real_edit_after_recovery(imports, monkeypatch):
    client, content = imports
    mid = upload(client, content).json()["mediaId"]
    assert save(client, mid, [manual()]).status_code == 200
    path = main.media_dir(mid) / "workspace_state.json"
    before = path.read_bytes()
    original = Path.read_text
    def unreadable(file, *args, **kwargs):
        if file == path:
            raise PermissionError("injected read failure")
        return original(file, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", unreadable)
        assert client.get("/api/track/workspace/"+mid).status_code == 500
        assert save(client, mid, [], 1, "blocked-by-read-fault").status_code == 500
    assert path.read_bytes() == before
    assert client.get("/api/track/workspace/"+mid).json()["manualAnnotations"] == [manual()]
    changed = manual(name="changed after recovery")
    assert save(client, mid, [changed], 1, "after-read-recovery").status_code == 200
    fresh = TestClient(main.app)
    fresh.headers.update(client.headers)
    assert fresh.get("/api/track/workspace/"+mid).json()["manualAnnotations"] == [changed]


def test_lost_workspace_response_replays_after_a_new_python_process(imports, tmp_path):
    client, content = imports
    mid = upload(client, content).json()["mediaId"]
    body = {"expectedRevision": 0, "manualAnnotations": [manual()]}
    first = client.put("/api/track/workspace/"+mid, json=body,
                       headers={"Idempotency-Key": "process-replay"})
    assert first.status_code == 200
    # Do not copy live globals or receipts; the child imports app from disk.
    code = """
import json,sys
from fastapi.testclient import TestClient
from app import main
from app.auth import sign_jwt
main.LEGACY_TRACK_DATA_DIR=main.TRACK_DATA_DIR.parent/'unused-legacy'
client=TestClient(main.app)
client.headers.update({'Authorization':'Bearer '+sign_jwt({'uid':1})})
mid,body=json.loads(sys.stdin.read())
r=client.put('/api/track/workspace/'+mid,json=body,headers={'Idempotency-Key':'process-replay'})
print(json.dumps({'status':r.status_code,'receipt':r.json(),'state':client.get('/api/track/workspace/'+mid).json()}))
"""
    env = dict(os.environ, APP_DB_FILE=str(db.DB_FILE), APP_DATA_DIR=str(tmp_path/'child-data'),
               APP_STORAGE_DIR=str(main.TRACK_DATA_DIR.parent), PYTHONPATH="backend", SAM3_ENABLED="false")
    # config derives media/ from APP_STORAGE_DIR; this fixture already uses it.
    child = subprocess.run([sys.executable, "-c", code], input=json.dumps([mid, body]),
                           text=True, capture_output=True, env=env, timeout=30, check=True)
    replay = json.loads(child.stdout.strip().splitlines()[-1])
    evidence("process-replay", status=replay["status"], identical_receipt=replay["receipt"]==first.json(),
             recovered_count=len(replay["state"].get("manualAnnotations", [])), revision=replay["state"]["revision"])
    assert replay["status"] == 200 and replay["receipt"] == first.json()
    assert replay["state"]["revision"] == 1 and replay["state"]["manualAnnotations"] == [manual()]
