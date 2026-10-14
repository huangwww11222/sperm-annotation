"""One owned native-frame decoder for compact tracking image evidence.

No frame cache, JPEG, model input resize, or implicit AI seed reference is used.
The Probe retains compact human optical references, never this reader's frames.
An unavailable or changed source remains unknown and cannot approve geometry.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Sequence, TYPE_CHECKING

import cv2
import numpy as np

from app.video_frames import FrameReadError, open_capture_at, source_signature
from .tracking_evidence import MaskRegion, TrackingEvidenceProbe

if TYPE_CHECKING:
    from .anomaly_detector import ManualBaseline

log = logging.getLogger('review.tracking')


class NativeImageEvidence:
    """Prime explicit human frames, then observe exact native BGR frames.

    Baselines at the same source frame share one decoded image. Their readers
    are released before observation starts. A forward observation stream owns
    one decoder; rewinds and larger jumps reopen through ``open_capture_at``.
    ``close`` is idempotent, including when a caller or Probe raises.
    """
    _MAX_FORWARD_GRABS = 8

    def __init__(self, video_path: str | Path, *, fps: float, width: int,
                 height: int, manual_baselines: Mapping[int, ManualBaseline],
                 seed_frame: int):
        self.video_path = Path(video_path)
        self.width, self.height = int(width), int(height)
        self.seed_frame = int(seed_frame)
        if self.width <= 0 or self.height <= 0 or self.seed_frame < 0:
            raise ValueError('native evidence dimensions/seed frame are invalid')
        self.probe = TrackingEvidenceProbe(fps=fps, frame_width=self.width,
                                           frame_height=self.height)
        self._baselines = dict(manual_baselines)
        self._reader = None
        self._next_frame: int | None = None
        self._primed = False
        self._closed = False
        self._source_problem: str | None = None
        try:
            self._signature = tuple(source_signature(self.video_path))
        except (OSError, ValueError) as exc:
            self._signature = None
            self._source_problem = 'source_unavailable'
            self._warn(self.seed_frame, 'initialization', exc)

    def __enter__(self) -> NativeImageEvidence:
        if self._closed:
            raise RuntimeError('native image evidence reader is closed')
        self._prime()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def _warn(self, frame: int, phase: str, exc: Exception):
        log.warning('tracking.image_read_failed media=%s frame=%s phase=%s reason=%s error=%s',
                    self.video_path.parent.name, frame, phase,
                    self._source_problem or 'decode_failed', exc)

    def _release_reader(self):
        reader, self._reader = self._reader, None
        self._next_frame = None
        if reader is not None:
            reader.release()

    def _check_source(self):
        if self._source_problem or self._signature is None:
            raise FrameReadError(self._source_problem or 'source_unavailable')
        try:
            current = tuple(source_signature(self.video_path))
        except OSError:
            self._source_problem = 'source_unavailable'
            self._release_reader()
            raise
        if current != self._signature:
            # References from this context can never be reused for a new file,
            # even if its name/dimensions match and a fresh decoder could open.
            self._source_problem = 'source_changed'
            self._release_reader()
            raise FrameReadError('source_changed: 原视频已变化，停止使用旧图像参考', 409)

    def _validate_image(self, image, frame: int):
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                or image.shape != (self.height, self.width, 3)):
            raise FrameReadError(f'第 {frame} 帧不是预期的原生 BGR 图像')

    def _read_once(self, frame: int):
        reader = None
        try:
            self._check_source()
            reader = open_capture_at(self.video_path, frame)
            ok, image = reader.read()
            if not ok or image is None:
                raise FrameReadError(f'无法解码第 {frame} 帧', 404)
            self._validate_image(image, frame)
            if int(round(reader.get(cv2.CAP_PROP_POS_FRAMES))) != frame + 1:
                raise FrameReadError(f'第 {frame} 帧原始位置校验失败')
            self._check_source()
            return image
        except (OSError, ValueError, cv2.error) as exc:
            self._warn(frame, 'reference', exc)
            return None
        finally:
            if reader is not None:
                reader.release()

    def _prime(self):
        if self._primed:
            return
        self._primed = True
        groups = {}
        for oid, baseline in self._baselines.items():
            if (type(oid) is not int or oid <= 0
                    or baseline.source not in ('manual', 'confirmed-normal')
                    or type(baseline.frame_index) is not int
                    or not 0 <= baseline.frame_index <= self.seed_frame):
                continue
            groups.setdefault(baseline.frame_index, {})[oid] = list(baseline.bbox)
        for frame, boxes in sorted(groups.items()):
            image = self._read_once(frame)
            if image is None:
                continue
            try:
                results = self.probe.prime(frame, image, boxes)
                for oid in boxes:
                    result = results.get(oid, {})
                    log.info('tracking.image_reference media=%s frame=%s object=%s source=%s status=%s reasons=%s',
                             self.video_path.parent.name, frame, oid,
                             self._baselines[oid].source,
                             result.get('head', {}).get('status', 'unknown'),
                             ','.join(result.get('reasons', [])))
            finally:
                # Assignment on the next loop must not transiently retain the
                # previous 4K frame while decoding the next baseline group.
                del image
        self._baselines.clear()

    def _read_observation(self, frame: int):
        try:
            if type(frame) is not int or frame < 0:
                raise FrameReadError('图像证据帧号必须为非负整数')
            self._check_source()
            if self._reader is not None:
                gap = frame - self._next_frame
                if gap < 0 or gap > self._MAX_FORWARD_GRABS:
                    self._release_reader()
                else:
                    for _ in range(gap):
                        if not self._reader.grab():
                            raise FrameReadError(f'无法抵达第 {frame} 帧', 404)
                        self._next_frame += 1
            if self._reader is None:
                self._reader = open_capture_at(self.video_path, frame)
                self._next_frame = frame
            ok, image = self._reader.read()
            if not ok or image is None:
                raise FrameReadError(f'无法解码第 {frame} 帧', 404)
            self._next_frame = frame + 1
            self._validate_image(image, frame)
            if int(round(self._reader.get(cv2.CAP_PROP_POS_FRAMES))) != self._next_frame:
                raise FrameReadError(f'第 {frame} 帧原始位置校验失败')
            self._check_source()
            return image
        except (OSError, ValueError, cv2.error) as exc:
            self._release_reader()
            self._warn(frame, 'observation', exc)
            return None

    def observe(self, frame_index: int, boxes: Mapping[int, Sequence[float]],
                masks: Mapping[int, MaskRegion] | None = None) -> dict[int, dict]:
        if self._closed:
            raise RuntimeError('native image evidence reader is closed')
        self._prime()
        image = self._read_observation(frame_index)
        return self.probe.observe(frame_index, image, boxes, masks)

    def close(self):
        self._closed = True
        self._release_reader()
