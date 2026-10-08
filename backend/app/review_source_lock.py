"""Source publication lock for this application's single-worker tracking server."""

from contextlib import contextmanager
from functools import wraps
from threading import RLock
from inspect import signature
import logging
import time
from .review_workflow import ReviewError

_lock = RLock()


def source_write(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        started = time.perf_counter()
        with _lock:
            acquired = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                wait_ms = (acquired - started) * 1000
                work_ms = (time.perf_counter() - acquired) * 1000
                if wait_ms >= 500 or work_ms >= 1000:
                    logging.getLogger('review.source').warning('source.lock_slow operation=%s wait_ms=%.1f work_ms=%.1f', fn.__name__, wait_ms, work_ms)

    wrapped.__signature__ = signature(fn, eval_str=True)
    return wrapped


@contextmanager
def completion_read():
    if not _lock.acquire(blocking=False):
        raise ReviewError("SOURCE_BUSY", "标注或 Tracking 正在保存，请稍后重新预览")
    try:
        yield
    finally:
        _lock.release()
