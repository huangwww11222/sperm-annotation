from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from app.services.frame_difference import find_next_new_object_frame


def _write_video(path: Path, frame_count: int = 14) -> None:
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        20.0,
        (320, 240),
    )
    assert writer.isOpened()
    for frame_index in range(frame_count):
        frame = np.zeros((240, 320, 3), dtype=np.uint8)

        # Known seed object: remains static, so it should not generate motion cues.
        cv2.rectangle(frame, (30, 80), (60, 100), (220, 220, 220), -1)

        # New object: appears at frame 4 and then keeps moving.
        if frame_index >= 4:
            x = 180 + min(frame_index - 4, 5) * 3
            cv2.rectangle(frame, (x, 150), (x + 30, 171), (230, 230, 230), -1)

        writer.write(frame)
    writer.release()


def test_frame_difference_finds_persistent_new_object(tmp_path: Path):
    video = tmp_path / "synthetic.mp4"
    seed = tmp_path / "seed.json"
    _write_video(video)

    seed.write_text(
        json.dumps(
            {
                "version": "1.0",
                "format": "sam3-annotations",
                "media": {"id": "synthetic", "name": "synthetic.mp4", "type": "video", "width": 320, "height": 240},
                "frame": {"frameIndex": 0, "timestampMs": 0},
                "coordinateSystem": {
                    "source": "frontend-pixel",
                    "target": "pixel",
                    "bbox": "[x1, y1, x2, y2]",
                },
                "annotations": [
                    {
                        "id": "manual-known-1",
                        "object_id": 1,
                        "name": "known sperm",
                        "source": "manual",
                        "frameIndex": 0,
                        "timestampMs": 0,
                        "bbox": [30, 80, 60, 100],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = find_next_new_object_frame(
        video,
        seed,
        start_frame=0,
        diff_threshold=5,
        min_area=10,
        min_area_ratio=0.2,
        min_w_ratio=0.2,
        min_h_ratio=0.2,
        max_aspect=4.0,
        confirm_frames=3,
        max_candidate_step=12,
        template_match_threshold=0.2,
        max_confirm_miss=1,
        max_search_frames=12,
    )

    assert result.status == "new_object_found"
    assert result.frame_index == 4
    assert result.frame_offset == 4
    assert result.recommended_track_frames == 5
    assert result.bbox is not None


def test_seed_json_can_be_used_at_nonzero_frame(tmp_path: Path):
    video = tmp_path / "seed_at_3.mp4"
    seed = tmp_path / "seed.json"
    _write_video(video)

    seed.write_text(
        json.dumps(
            {
                "media": {"id": "synthetic", "name": "synthetic.mp4", "type": "video", "width": 320, "height": 240},
                "frame": {"frameIndex": 3, "timestampMs": 150},
                "coordinateSystem": {"bbox": "[x1, y1, x2, y2]"},
                "annotations": [
                    {
                        "id": "manual-known-1",
                        "object_id": 21,
                        "name": "known sperm",
                        "source": "manual",
                        "frameIndex": 3,
                        "timestampMs": 150,
                        "bbox": [30, 80, 60, 100],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = find_next_new_object_frame(
        video,
        seed,
        start_frame=3,
        diff_threshold=5,
        min_area=10,
        min_area_ratio=0.2,
        min_w_ratio=0.2,
        min_h_ratio=0.2,
        max_aspect=4.0,
        confirm_frames=2,
        max_candidate_step=12,
        template_match_threshold=0.2,
        max_confirm_miss=1,
        max_search_frames=8,
    )

    assert result.status == "new_object_found"
    assert result.frame_index == 4
    assert result.frame_offset == 1
    assert result.recommended_track_frames == 2
