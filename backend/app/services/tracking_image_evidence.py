"""One owned native-frame decoder for compact tracking image evidence.

No frame cache, JPEG, model input resize, or implicit AI seed reference is used.
The Probe retains compact human optical references, never this reader's frames.
An unavailable or changed source remains unknown and cannot approve geometry.
"""
from __future__ import annotations

import logging
import time
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
                 seed_frame: int, identity_baselines: Mapping[int, ManualBaseline] | None = None,
                 identity_references: Mapping[int, dict] | None = None):
        self.video_path = Path(video_path)
        self.width, self.height = int(width), int(height)
        self.seed_frame = int(seed_frame)
        if self.width <= 0 or self.height <= 0 or self.seed_frame < 0:
            raise ValueError('native evidence dimensions/seed frame are invalid')
        self.probe = TrackingEvidenceProbe(fps=fps, frame_width=self.width,
                                           frame_height=self.height)
        self._baselines = dict(manual_baselines)
        self._identity_baselines = None if identity_baselines is None else dict(identity_baselines)
        self._identity_references = dict(identity_references or {})
        self._reader = None
        self._next_frame: int | None = None
        self._primed = False
        self._closed = False
        self._source_problem: str | None = None
        try:
            self._signature = tuple(source_signature(self.video_path))
            self.probe.source_signature = list(self._signature)
        except (OSError, ValueError) as exc:
            self._signature = None
            self._source_problem = 'source_unavailable'
            self._warn(self.seed_frame, 'initialization', exc)

    def __enter__(self) -> NativeImageEvidence:
        if self._closed:
            raise RuntimeError('native image evidence reader is closed')
        try:
            self._prime()
        except Exception:
            self.close()
            raise
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
        if self._identity_baselines is not None:
            self._prime_identity_history()
            self._baselines.clear()
            self._identity_baselines.clear()
            self._identity_references.clear()
            return
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

    def _prime_identity_history(self):
        """Bounded legacy migration at tracking startup, never inside save locks.

        Human shape acceptance and identity origins have different lifetimes.
        For old checkpoints without compact reliable identities, recover the
        optical trajectory from the last true manual frame. No AI mask or
        rectangle is used to teach an identity during this recovery.
        """
        started = time.monotonic()
        valid = {oid: baseline for oid, baseline in self._identity_baselines.items()
                 if type(oid) is int and oid > 0 and baseline.source == 'manual'
                 and type(baseline.frame_index) is int and 0 <= baseline.frame_index <= self.seed_frame}
        saved = {oid: ref for oid,ref in self._identity_references.items()
                 if oid in valid and isinstance(ref,dict)
                 and type(ref.get('frameIndex')) is int
                 and ref['frameIndex'] >= valid[oid].frame_index}
        restored = self.probe.restore_references(saved, frame_limit=self.seed_frame)
        groups = {}
        for oid, baseline in valid.items():
            if oid not in restored:
                groups.setdefault(baseline.frame_index,{})[oid] = list(baseline.bbox)
        starts = list(groups) + [self.probe._references[oid].last_frame for oid in restored]
        if starts:
            first = min(starts)
            if self.seed_frame-first > 128:
                # Avoid replaying thousands of 4K images on every restart. A
                # missing legacy identity stays unknown; current geometry does
                # not silently revive the invalid old brightest-core anchor.
                log.warning('tracking.identity_warmup_skipped media=%s first=%s seed=%s reason=legacy_gap_exceeds_128', self.video_path.parent.name, first, self.seed_frame)
                groups = {frame: boxes for frame,boxes in groups.items() if self.seed_frame-frame <= 128}
                starts = list(groups) + [self.probe._references[oid].last_frame for oid in restored if self.seed_frame-self.probe._references[oid].last_frame <= 128]
                for oid in list(restored):
                    if self.seed_frame-self.probe._references[oid].last_frame > 128:
                        self.probe._references.pop(oid, None)
                first = min(starts) if starts else self.seed_frame+1
            log.info('tracking.identity_warmup_started media=%s first=%s seed=%s restored=%s', self.video_path.parent.name, first, self.seed_frame, sorted(restored))
            for frame in range(first,self.seed_frame+1):
                image = self._read_observation(frame)
                if image is None:
                    # Failed intermediate decoding cannot be treated as a
                    # trustworthy continuous identity path.
                    self.probe._references.clear()
                    break
                try:
                    self.probe.advance_references(frame,image)
                    if frame in groups:
                        self.probe.prime(frame,image,groups[frame])
                finally:
                    del image
        for oid, baseline in self._baselines.items():
            self.probe.bind_geometry(oid,baseline.frame_index,baseline.bbox)
        self._release_reader()
        log.info('tracking.identity_warmup_completed media=%s seed=%s reliable=%s elapsedMs=%s',
                 self.video_path.parent.name,self.seed_frame,len(self.probe._references),round((time.monotonic()-started)*1000))

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


def derive_identity_references(video_path, *, fps, width, height, frame_index,
                               boxes, prior_references=None):
    """One current-frame decode for server feedback; no historical warmup.

    Callers supply only server-owned candidate state. This helper revalidates
    its source signature, object, native bounds and current optical ownership.
    Decoder failures propagate instead of being misrepresented as empty data.
    """
    path = Path(video_path)
    signature = source_signature(path)
    probe = TrackingEvidenceProbe(fps=fps,frame_width=width,frame_height=height)
    probe.source_signature = list(signature)
    restored = probe.restore_references(prior_references or {},frame_limit=frame_index)
    reader = None
    try:
        reader = open_capture_at(path,frame_index)
        ok,image = reader.read()
        if not ok or not probe._image_ok(image) or int(round(reader.get(cv2.CAP_PROP_POS_FRAMES))) != frame_index+1:
            raise FrameReadError(f'无法读取第 {frame_index} 帧身份参考')
        if source_signature(path) != signature:
            raise FrameReadError('原视频已变化，未保存身份参考',409)
        probe.advance_references(frame_index,image)
        # Joint priming is a legacy fallback on this one explicit human frame;
        # it cannot replace an existing reliable identity with a new peak.
        missing = {oid: box for oid,box in boxes.items() if oid not in restored}
        if missing:
            # Include restored rectangles as competitors but retain their
            # validated references after the current-frame joint extraction.
            retained = {oid:probe._references[oid] for oid in restored}
            probe.prime(frame_index,image,boxes)
            probe._references.update(retained)
        for oid,box in boxes.items():
            probe.bind_geometry(oid,frame_index,box)
        result = probe.export_references(frame_index,boxes)
        # Never certify two IDs from the same point, even if a legacy single
        # frame happens to provide identical trusted rectangles.
        conflicts = probe._candidate_conflicts({oid:{'point':ref['point'],'radius':ref['radius']} for oid,ref in result.items()})
        return {oid:ref for oid,ref in result.items() if oid in restored and oid not in conflicts}
    finally:
        if reader is not None:
            reader.release()
