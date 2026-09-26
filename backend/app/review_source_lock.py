"""Source publication lock for this application's single-worker tracking server."""

from contextlib import contextmanager
from functools import wraps
from threading import RLock
from inspect import signature
from .review_workflow import ReviewError

_lock = RLock()


def source_write(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)

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
