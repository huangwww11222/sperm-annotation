from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from app.services.frame_difference import find_next_new_object_frame


def _write_static_video(path: Path, frame_count: int = 10) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (160, 120))
    assert writer.isOpened()
    for _ in range(frame_count):
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        cv2.rectangle(frame, (20, 40), (45, 60), (220, 220, 220), -1)
        writer.write(frame)
    writer.release()


def test_no_new_object_searches_through_video_end(tmp_path: Path):
    video = tmp_path / "static.avi"
    seed = tmp_path / "seed.json"
    _write_static_video(video, frame_count=10)
    seed.write_text(json.dumps({
        "media": {"id": "static", "name": "static.avi", "type": "video", "width": 160, "height": 120},
        "frame": {"frameIndex": 2, "timestampMs": 200},
        "annotations": [{
            "id": "known-1", "object_id": 1, "name": "known", "source": "manual",
            "frameIndex": 2, "bbox": [20, 40, 45, 60],
        }],
    }), encoding="utf-8")
    result = find_next_new_object_frame(
        video, seed, 2, diff_threshold=5, min_area=10, min_area_ratio=0.2,
        min_w_ratio=0.2, min_h_ratio=0.2, max_aspect=4.0,
        confirm_frames=2, max_confirm_miss=1, max_search_frames=None, verbose_log=False,
    )
    assert result.status == "no_new_object"
    assert result.frame_index is None
    assert result.search_frames == 7  # frames 3..9
    assert result.recommended_track_frames == 8  # seed frame 2 + 7 future frames
