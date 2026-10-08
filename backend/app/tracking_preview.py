"""Derived full-video preview, generated on request outside the source lock."""
import hashlib
import json
import logging
from pathlib import Path
from threading import Lock
import time
import uuid

from fastapi import HTTPException
from .annotation_state import read_state
from .review_source_lock import source_write
from .tracker import OVERLAY_FILE_NAME, _probe_video, _read_jsonl, _render_overlay_video

log = logging.getLogger('review.tracking')
_locks = {}
_guard = Lock()


def signature(video, result):
    state = read_state(video.parent)
    parts = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in (video, result)]
    parts += [state.get(k, []) for k in ('deletedObjectIds', 'deletedFrameObjects')]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def prepare(video: Path, result: Path, busy):
    with _guard:
        lock = _locks.setdefault(str(video.parent.resolve()), Lock())
    with lock:
        output = video.parent / OVERLAY_FILE_NAME
        stamp = video.parent / 'tracker_overlay.meta.json'
        @source_write
        def snapshot():
            if busy():
                raise HTTPException(409, 'AI Tracking 正在运行，请完成后再读取带框视频预览')
            key = signature(video, result)
            try:
                if output.is_file() and json.loads(stamp.read_text()).get('signature') == key:
                    return key, None
            except (OSError, ValueError):
                pass
            return key, _read_jsonl(result)
        key, rows = snapshot()
        if rows is None:
            return output
        temporary = video.parent / '.frame_cache' / ('overlay-' + uuid.uuid4().hex + '.mp4')
        temporary.parent.mkdir(exist_ok=True)
        began = time.perf_counter()
        log.info('tracking.preview_started media=%s', video.parent.name)
        try:
            _render_overlay_video(video, temporary, _probe_video(video), rows)
            @source_write
            def publish():
                if busy() or signature(video, result) != key:
                    raise HTTPException(409, '生成预览期间追踪或删除状态已变化，请重试读取预览')
                temporary.replace(output)
                stamp.write_text(json.dumps({'signature': key}))
            publish()
            log.info('tracking.preview_completed media=%s elapsed_ms=%.1f', video.parent.name, (time.perf_counter() - began) * 1000)
            return output
        except HTTPException:
            raise
        except Exception as exc:
            log.exception('tracking.preview_failed media=%s', video.parent.name)
            raise HTTPException(503, '带框预览生成失败；此操作不修改追踪和标注，请按日志检查后重试') from exc
        finally:
            temporary.unlink(missing_ok=True)
