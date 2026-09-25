from pathlib import Path
import json

from app.tracker import _read_jsonl, rewind_tracking_results


def test_rewind_tracking_keeps_current_and_removes_future_rows(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    result = media_dir / "tracker_results.json"
    rows = [
        {"frame_index": 1, "source_frame_index": 1, "objects": []},
        {"frame_index": 9, "source_frame_index": 9, "objects": []},
        {"frame_index": 10, "source_frame_index": 10, "objects": []},
        {"frame_index": 15, "source_frame_index": 15, "objects": []},
    ]
    result.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (media_dir / "annotations_frame_000010.json").write_text("{}", encoding="utf-8")
    (media_dir / "annotations_frame_000015.json").write_text("{}", encoding="utf-8")
    video = media_dir / "video.mp4"
    info = rewind_tracking_results(result, video, 10)
    assert info["removedRows"] == 1
    assert info["deletedFutureSeedFiles"] == 0
    assert (media_dir / "annotations_frame_000010.json").exists()
    # Rewinding tracking must not delete durable user annotations/manual baselines.
    assert (media_dir / "annotations_frame_000015.json").exists()
    kept = _read_jsonl(result)
    assert [r["source_frame_index"] for r in kept] == [1, 9, 10]
