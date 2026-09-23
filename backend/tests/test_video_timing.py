"""Regression coverage for raw-frame timing across common source FPS values."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.sam3_engine import read_video


def _write_video(path: Path, fps: float, frame_count: int = 8) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (64, 48))
    if not writer.isOpened():
        pytest.skip("OpenCV MJPG writer is unavailable in this environment")
    for index in range(frame_count):
        writer.write(np.full((48, 64, 3), index, dtype=np.uint8))
    writer.release()


@pytest.mark.parametrize("fps", [15.0, 25.0, 29.97, 60.0])
def test_raw_frame_reader_preserves_source_indices_and_fps(tmp_path: Path, fps: float) -> None:
    video = tmp_path / f"fps_{fps}.avi"
    _write_video(video, fps)

    frames, meta = read_video(video, target_fps=None)

    assert len(frames) == 8
    assert meta["source_frame_indices"] == list(range(8))
    assert meta["sample_interval"] == 1
    assert meta["source_fps"] == pytest.approx(fps, rel=0.02)
    assert meta["fps"] == pytest.approx(fps, rel=0.02)
