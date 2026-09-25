from __future__ import annotations

import json
from pathlib import Path

from app.main import TASKS, _effective_track_frames, _legacy_workspace_state, _run_tracking_task
from app.schemas import TrackRequest
from app.tracker import _load_latest_manual_baselines


def _write_annotations(path: Path, frame: int, annotations: list[dict]) -> None:
    path.write_text(json.dumps({
        "frame": {"frameIndex": frame},
        "annotations": annotations,
    }), encoding="utf-8")


def test_restore_uses_latest_manual_box_and_ignores_newer_ai_box(tmp_path: Path) -> None:
    _write_annotations(tmp_path / "annotations_frame_000000.json", 0, [
        {"object_id": 1, "name": "sperm1", "source": "manual", "bbox": [10, 10, 50, 22]},
    ])
    _write_annotations(tmp_path / "annotations_frame_000005.json", 5, [
        {"object_id": 1, "name": "sperm1", "source": "ai", "bbox": [20, 10, 45, 22]},
    ])
    current = tmp_path / "annotations_frame_000008.json"
    _write_annotations(current, 8, [
        {"object_id": 1, "name": "sperm1", "source": "manual", "bbox": [30, 15, 72, 29]},
    ])
    _write_annotations(tmp_path / "annotations_frame_000009.json", 9, [
        {"object_id": 1, "name": "sperm1", "source": "ai", "bbox": [35, 15, 60, 29]},
    ])

    restored = _load_latest_manual_baselines(current, 9)

    assert restored[1].frame_index == 8
    assert restored[1].bbox == [30.0, 15.0, 72.0, 29.0]
    assert restored[1].name == "sperm1"


def test_restore_after_restart_is_file_based(tmp_path: Path) -> None:
    seed = tmp_path / "seed_frame_000012.json"
    _write_annotations(tmp_path / "annotations_frame_000003.json", 3, [
        {"object_id": 7, "name": "rare sperm 3", "source": "manual", "bbox": [1, 2, 11, 12]},
    ])
    _write_annotations(seed, 12, [
        {"object_id": 7, "name": "rare sperm 3", "source": "ai", "bbox": [3, 4, 9, 10]},
    ])
    restored = _load_latest_manual_baselines(seed, 12)
    assert restored[7].frame_index == 3


def test_workspace_state_restores_newer_manual_baseline(tmp_path: Path) -> None:
    seed = tmp_path / "seed_frame_000012.json"
    _write_annotations(tmp_path / "annotations_frame_000003.json", 3, [
        {"object_id": 7, "name": "sperm7", "source": "manual", "bbox": [1, 2, 11, 12]},
    ])
    _write_annotations(seed, 12, [
        {"object_id": 7, "name": "sperm7", "source": "ai", "bbox": [3, 4, 9, 10]},
    ])
    (tmp_path / "workspace_state.json").write_text(json.dumps({
        "format": "annotation-workspace-v1",
        "manualBaselines": [
            {"objectId": 7, "name": "sperm7", "source": "manual", "frameIndex": 9, "bbox": [20, 21, 60, 35]},
            {"objectId": 8, "name": "sperm8", "source": "ai", "frameIndex": 10, "bbox": [1, 1, 2, 2]},
        ],
    }), encoding="utf-8")

    restored = _load_latest_manual_baselines(seed, 12)

    assert restored[7].frame_index == 9
    assert restored[7].bbox == [20.0, 21.0, 60.0, 35.0]
    assert 8 not in restored


def test_existing_seed_files_are_migrated_to_workspace_state(tmp_path: Path) -> None:
    (tmp_path / "media.json").write_text(json.dumps({
        "width": 200,
        "height": 100,
        "fps": 20,
    }), encoding="utf-8")
    _write_annotations(tmp_path / "annotations_frame_000004.json", 4, [
        {"object_id": 3, "name": "sperm3", "source": "manual", "bbox": [20, 10, 60, 30]},
        {"object_id": 4, "name": "sperm4", "source": "ai", "bbox": [1, 1, 5, 5]},
    ])

    state = _legacy_workspace_state(tmp_path, "video-a")

    assert state is not None
    assert len(state["manualAnnotations"]) == 1
    assert state["manualAnnotations"][0]["bbox"] == {
        "x": 10.0,
        "y": 10.0,
        "width": 20.0,
        "height": 20.0,
    }
    assert state["manualBaselines"][0]["bbox"] == [20.0, 10.0, 60.0, 30.0]


def test_track_frame_limit_includes_seed_and_respects_video_end(monkeypatch) -> None:
    monkeypatch.setattr("app.main.TRACK_FRAMES", 10)
    assert _effective_track_frames(20, 100, 999) == 10
    assert _effective_track_frames(95, 100, 999) == 5
    assert _effective_track_frames(99, 100, 999) == 1


def test_task_status_returns_actual_last_processed_frame(monkeypatch, tmp_path: Path) -> None:
    def fake_track_video(*_args, **_kwargs):
        return {
            "processedFrames": 4,
            "lastProcessedFrame": 13,
            "reachedVideoEnd": False,
            "anomaly_paused": None,
        }

    monkeypatch.setattr("app.main.track_video", fake_track_video)
    request = TrackRequest(mediaId="m", startFrame=10, maxFrames=4, annotations=[{"object_id": 1}])
    task_id = "test-last-processed"
    TASKS.pop(task_id, None)
    _run_tracking_task(task_id, request, tmp_path / "v.avi", tmp_path / "seed.json", tmp_path / "result.json")
    assert TASKS[task_id]["status"] == "success"
    assert TASKS[task_id]["lastProcessedFrame"] == 13


def test_task_status_returns_paused_frame_and_all_anomalies(monkeypatch, tmp_path: Path) -> None:
    paused_objects = [
        {"object_id": 1, "display_name": "sperm1", "type": "overlap"},
        {"object_id": 2, "display_name": "sperm2", "type": "overlap"},
    ]

    def fake_track_video(*_args, **_kwargs):
        return {
            "processedFrames": 3,
            "lastProcessedFrame": 12,
            "reachedVideoEnd": False,
            "anomaly_paused": {"frame_index": 12, "reasons": paused_objects, "levels": {"1": "anomaly", "2": "anomaly"}},
        }

    monkeypatch.setattr("app.main.track_video", fake_track_video)
    request = TrackRequest(mediaId="m", startFrame=10, maxFrames=10, annotations=[{"object_id": 1}])
    task_id = "test-paused"
    TASKS.pop(task_id, None)
    _run_tracking_task(task_id, request, tmp_path / "v.avi", tmp_path / "seed.json", tmp_path / "result.json")
    assert TASKS[task_id]["status"] == "paused"
    assert TASKS[task_id]["pausedFrame"] == 12
    assert TASKS[task_id]["lastProcessedFrame"] == 12
    assert TASKS[task_id]["pausedObjects"] == paused_objects
