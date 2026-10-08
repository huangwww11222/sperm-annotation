from __future__ import annotations

import math
import os
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

import cv2
import numpy as np
import torch
from PIL import Image


@dataclass
class TrackDetection:
    object_id: int
    bbox: list[float]
    score: float | None
    source: str = "sam3"
    mask_area: int | None = None
    sam3_object_id: int | None = None


def _as_numpy(value: Any) -> np.ndarray | None:
    if value is None:
        return None
    try:
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        if hasattr(value, "numpy"):
            value = value.numpy()
        return np.asarray(value)
    except Exception:
        return None


class SourceVideoWindow:
    """Single-pass native frames. The caller retains only its current batch."""

    def __init__(self, video_path, max_frames=None, target_fps=None, start_frame=0):
        if start_frame < 0 or (max_frames is not None and max_frames < 1):
            raise ValueError('Invalid source-frame window')
        self.path = Path(video_path)
        self.max_frames, self.target_fps, self.start_frame = max_frames, target_fps, start_frame
        self.cap = None
        self.meta = {}
        self.consumed = False

    def __enter__(self):
        if not self.path.is_file():
            raise FileNotFoundError(f'Video not found: {self.path}')
        from ..video_frames import open_capture_at
        self.cap = open_capture_at(self.path, self.start_frame)
        fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0)
        requested_fps = float(self.target_fps or 0)
        interval = max(1, int(round(fps / requested_fps))) if 0 < requested_fps < fps else 1
        self.meta = dict(name=self.path.name, width=int(self.cap.get(3)), height=int(self.cap.get(4)),
                         fps=fps / interval if fps else requested_fps, source_fps=fps,
                         frameCount=int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
                         sample_interval=interval, source_frame_indices=[])
        return self

    def __exit__(self, *args):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __len__(self):
        n = self.meta['frameCount']
        interval = self.meta['sample_interval']
        first = self.start_frame + (-self.start_frame % interval)
        remaining = max(0, (n - first + interval - 1) // interval)
        return min(remaining, self.max_frames) if self.max_frames else remaining

    def __iter__(self):
        if self.cap is None or self.consumed:
            raise ValueError('Source window must be opened and consumed once')
        self.consumed = True
        index, count = self.start_frame, 0
        while self.max_frames is None or count < self.max_frames:
            ok, frame = self.cap.read()
            if not ok:
                break
            if frame.shape[:2] != (self.meta['height'], self.meta['width']):
                raise ValueError(f'Source image size changed at frame {index}')
            if index % self.meta['sample_interval'] == 0:
                self.meta['source_frame_indices'].append(index)
                count += 1
                yield Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            index += 1
        if not count:
            raise ValueError(f'Video contains no readable frames: {self.path}')
        if self.max_frames is not None and self.meta['frameCount'] and count != len(self):
            raise ValueError(f'Cannot decode source window: expected {len(self)} frames from {self.start_frame}, got {count}')
        if not self.meta['frameCount']:
            self.meta['frameCount'] = index


def read_video(video_path, max_frames=None, target_fps=None, start_frame=0):
    """Compatibility materializer; production tracking consumes SourceVideoWindow."""
    with SourceVideoWindow(video_path, max_frames, target_fps, start_frame) as window:
        frames = list(window)
        return frames, window.meta


class Sam3Engine:
    """The SAM3 engine used by the validated 01_test pipeline.

    The tracker model and processor are loaded only once per FastAPI process.
    Every tracking request gets a fresh inference session, just like 01_test.
    """

    def __init__(self, model_id: str, device: str, dtype: str):
        self.model_id = self.resolve_model_id(model_id)
        self.device = self._resolve_device(device)
        self.torch_dtype = self._resolve_dtype(dtype)
        self.tracker_model = None
        self.tracker_processor = None
        self._load_lock = Lock()

    @staticmethod
    def resolve_model_id(model_id: str | None) -> str:
        requested = (model_id or os.getenv("SAM3_MODEL_ID") or "").strip()
        project_root = Path(__file__).resolve().parents[2]
        candidates: list[Path] = []
        if requested and Path(requested).is_dir():
            candidates.append(Path(requested).expanduser())
        candidates.extend(
            [
                project_root / "track_modul" / "facebook--sam3" / "snapshots" / "master",
                project_root / "track_modul" / "facebook--sam3",
                project_root / "models" / "sam3",
            ]
        )
        for candidate in candidates:
            if candidate.is_dir() and (candidate / "config.json").exists():
                return str(candidate.resolve())
        return requested or "facebook/sam3"

    @staticmethod
    def _resolve_device(device: str) -> torch.device:
        value = str(device).lower().strip()
        if value in {"gpu", "cuda", "cuda:0"}:
            if not torch.cuda.is_available():
                raise RuntimeError("SAM3_DEVICE=cuda, but CUDA is not available")
            return torch.device("cuda")
        return torch.device(value)

    @staticmethod
    def _resolve_dtype(dtype: str) -> torch.dtype:
        value = str(dtype).lower().replace("torch.", "")
        if value in {"bfloat16", "bf16"}:
            if torch.cuda.is_available() and hasattr(torch.cuda, "is_bf16_supported"):
                if not torch.cuda.is_bf16_supported():
                    print("[sam3] bfloat16 not supported; falling back to float16")
                    return torch.float16
            return torch.bfloat16
        if value in {"float16", "fp16", "half"}:
            return torch.float16
        return torch.float32

    def _pretrained_kwargs(self) -> dict[str, Any]:
        if Path(str(self.model_id)).is_dir():
            return {"local_files_only": True}
        token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN")
        return {"token": token} if token else {}

    def load_tracker(self) -> None:
        if self.tracker_model is not None and self.tracker_processor is not None:
            return
        with self._load_lock:
            if self.tracker_model is not None and self.tracker_processor is not None:
                return
            from transformers import Sam3TrackerVideoModel, Sam3TrackerVideoProcessor

            kwargs = self._pretrained_kwargs()
            started = time.perf_counter()
            print(f"[sam3] loading tracker from: {self.model_id}")
            print(f"[sam3] device={self.device}, dtype={self.torch_dtype}")
            self.tracker_model = Sam3TrackerVideoModel.from_pretrained(self.model_id, dtype=self.torch_dtype, **kwargs).to(
                self.device, dtype=self.torch_dtype
            )
            self.tracker_processor = Sam3TrackerVideoProcessor.from_pretrained(self.model_id, **kwargs)
            self.tracker_model.eval()
            logging.getLogger('review.tracking').info('tracking.model_ready device=%s dtype=%s elapsed_ms=%.1f', self.device, self.torch_dtype, (time.perf_counter()-started)*1000)
            print("[sam3] tracker loaded once and cached.")

    @property
    def model(self):
        return self.tracker_model

    @property
    def processor(self):
        return self.tracker_processor

    def make_tracker_session(self, frames):
        self.load_tracker()
        from transformers import Sam3TrackerVideoInferenceSession
        # Use exactly the standard video processor, only in small batches.
        # Avoid stacking/resizing all native 4K frames in a single allocation.
        started = time.perf_counter()
        count, written, batch, storage, original_size = len(frames), 0, [], None, None
        if count < 1:
            raise ValueError('No source frames available')
        def process():
            nonlocal written, storage, original_size
            inputs = self.tracker_processor.video_processor(videos=batch, device='cpu', return_tensors='pt')
            size = tuple(int(x) for x in inputs.original_sizes[0])
            if original_size is not None and size != original_size:
                raise ValueError('Source image size changed during preprocessing')
            original_size = size
            pixels = inputs.pixel_values_videos[0]
            if pixels.shape[0] != len(batch) or written + len(batch) > count:
                raise ValueError('Video processor changed the source-frame sequence')
            if storage is None:
                storage = torch.empty((count, *pixels.shape[1:]), device='cpu', dtype=self.torch_dtype)
            storage[written:written + len(batch)].copy_(pixels)
            written += len(batch)
            batch.clear()
        with torch.inference_mode():
            for frame in frames:
                batch.append(frame)
                if len(batch) == 2:
                    process()
            if batch:
                process()
        if written != count or storage is None:
            raise ValueError(f'Incomplete video preprocessing: expected {count}, got {written}')
        logging.getLogger('review.tracking').info('tracking.preprocessed frames=%s raw_batch_frames=2 storage_bytes=%s elapsed_ms=%.1f',
            written, storage.numel() * storage.element_size(), (time.perf_counter() - started) * 1000)
        return Sam3TrackerVideoInferenceSession(video=storage, video_height=original_size[0], video_width=original_size[1],
            inference_device=self.device, inference_state_device='cpu', video_storage_device='cpu',
            dtype=self.torch_dtype, max_vision_features_cache_size=1)

    def add_manual_boxes(self, session, frame_index: int, objects: list[dict[str, Any]]) -> None:
        self.load_tracker()
        if not objects:
            raise ValueError("No manual objects were supplied")
        ids = [int(o["object_id"]) for o in objects]
        boxes = [[float(v) for v in o["bbox"]] for o in objects]
        self.tracker_processor.add_inputs_to_inference_session(
            inference_session=session,
            frame_idx=int(frame_index),
            obj_ids=ids,
            input_boxes=[boxes],
        )

    def propagate_manual(self, session, max_frames: int, start_frame_idx: int):
        self.load_tracker()
        yield from self.tracker_model.propagate_in_video_iterator(
            inference_session=session,
            start_frame_idx=int(start_frame_idx),
            max_frame_num_to_track=int(max_frames),
            show_progress_bar=False,
        )

    def decode_tracker_output(self, session, output, keep_masks=False) -> tuple[list[TrackDetection], dict[int, np.ndarray]]:
        self.load_tracker()
        ids = [int(x) for x in getattr(session, "obj_ids", [])]
        scores_np = _as_numpy(getattr(output, "object_score_logits", None))
        if scores_np is not None:
            scores_np = np.asarray(scores_np).reshape(-1)
            scores_np = 1.0 / (1.0 + np.exp(-scores_np))

        detections: list[TrackDetection] = []
        mask_map: dict[int, np.ndarray] = {}
        for i, object_id in enumerate(ids):
            if i >= output.pred_masks.shape[0]:
                continue
            # Standard interpolation/binarization on the original device, one
            # object at a time. Tracking consumes geometry, not retained masks.
            masks = self.tracker_processor.post_process_masks([output.pred_masks[i:i+1]],
                original_sizes=[[session.video_height, session.video_width]], binarize=True)[0]
            masks_np = _as_numpy(masks)
            if masks_np is None:
                continue
            mask = np.asarray(masks_np)[0]
            if mask.ndim == 3 and mask.shape[0] == 1:
                mask = mask[0]
            occupied = mask > .5
            ys = np.flatnonzero(occupied.any(axis=1))
            xs = np.flatnonzero(occupied.any(axis=0))
            if not len(xs) or not len(ys):
                continue
            bbox = [float(xs[0]), float(ys[0]), float(xs[-1] + 1), float(ys[-1] + 1)]
            score = float(scores_np[i]) if scores_np is not None and i < len(scores_np) else None
            detections.append(
                TrackDetection(
                    object_id=object_id,
                    bbox=bbox,
                    score=score,
                    mask_area=int(np.count_nonzero(occupied)),
                    sam3_object_id=object_id,
                )
            )
            if keep_masks:
                mask_map[object_id] = mask
        return detections, mask_map

    @staticmethod
    def source_to_sampled_index(source_frame: int, source_indices: list[int]) -> int:
        if not source_indices:
            return source_frame
        return min(range(len(source_indices)), key=lambda i: abs(int(source_indices[i]) - source_frame))


_ENGINE: Sam3Engine | None = None
_ENGINE_LOCK = Lock()


def get_sam3_engine(model_id: str, device: str, dtype: str) -> Sam3Engine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = Sam3Engine(model_id, device, dtype)
        return _ENGINE
