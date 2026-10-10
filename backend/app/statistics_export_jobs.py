"""Bounded, authenticated runtime jobs; no mutations to the source database."""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import db, statistics_export
from .config import STORAGE_ROOT
from .review_workflow import ReviewError

log = logging.getLogger("review.statistics")
RETENTION_SECONDS = 24 * 60 * 60
MAX_READY_PACKAGES = 20
MAX_TASKS = 128
_manager = None
_manager_lock = threading.Lock()
MARKER = "annotation-statistics-export-runtime-v1\n"


def utc(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


class ExportManager:
    def __init__(self, root: Path, database: Path):
        self.root = Path(root)
        self.database = Path(database)
        self.lock = threading.RLock()
        self.jobs = {}
        self.receipts = {}
        self.pool = None
        self.runtime = None
        self.closed = False

    def _prepare(self):
        if self.runtime is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        # Only previous directories explicitly marked as our transient export
        # runtime are removed. Never traverse media, datasets or user files.
        for old in self.root.glob("runtime_*"):
            marker = old / ".owner"
            if old.is_dir() and not old.is_symlink() and marker.is_file() and marker.read_text(encoding="utf-8") == MARKER:
                shutil.rmtree(old)
        self.runtime = self.root / ("runtime_" + uuid.uuid4().hex)
        self.runtime.mkdir(mode=0o700)
        (self.runtime / ".owner").write_text(MARKER, encoding="utf-8")

    def _expire(self, now):
        ready = sorted((j for j in self.jobs.values() if j["state"] == "ready"), key=lambda j: j["created"])
        excess = {j["exportId"] for j in ready[:-MAX_READY_PACKAGES]}
        for eid, job in list(self.jobs.items()):
            if job["state"] in {"queued", "running"} or job["leases"]:
                continue
            if job["expires"] <= now or eid in excess:
                if job.get("path"):
                    Path(job["path"]).unlink(missing_ok=True)
                self.jobs.pop(eid)
                # Receipts live until their own TTL, so an expired output cannot
                # silently become a new operation when its old key is retried.
                log.info("statistics.expired export=%s", eid)
        for pair, receipt in list(self.receipts.items()):
            if receipt[1] <= now:
                self.receipts.pop(pair)

    def create(self, actor: int, key: str):
        with self.lock:
            now = time.time()
            self._expire(now)
            pair = (actor, key)
            receipt = self.receipts.get(pair)
            if receipt:
                return self._status(receipt[0], actor)
            if self.closed:
                raise ReviewError("STATISTICS_EXPORT_UNAVAILABLE", "服务正在停止，请稍后重新导出统计数据", 503)
            if any(j["state"] in {"queued", "running"} for j in self.jobs.values()):
                raise ReviewError("STATISTICS_EXPORT_BUSY", "已有统计数据正在生成，请稍后再试", 409)
            if len(self.jobs) >= MAX_TASKS or len(self.receipts) >= MAX_TASKS:
                raise ReviewError("STATISTICS_EXPORT_LIMIT", "本次服务的统计导出任务已达到保留上限，请等待旧任务过期后再试", 429)
            self._prepare()
            eid = "stats_" + uuid.uuid4().hex
            job = {
                "exportId": eid, "actor": actor, "state": "queued", "stage": "queued",
                "created": now, "updated": now, "expires": now + RETENTION_SECONDS,
                "progress": {"completedTables": 0, "totalTables": len(statistics_export.EXPORT_SPECS), "rowsWritten": 0, "currentTable": None},
                "path": None, "sizeBytes": None, "error": None, "leases": set(),
                "fileName": "标注确认数据_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + eid[-8:] + ".zip",
            }
            self.jobs[eid] = job
            self.receipts[pair] = (eid, job["expires"])
            try:
                self.enqueue(eid)
            except Exception:
                self._fail(eid, "STATISTICS_EXPORT_START_FAILED", "统计导出任务未能启动，请新建任务重试")
                log.exception("statistics.enqueue_failed export=%s actor=%s", eid, actor)
            log.info("statistics.created export=%s actor=%s scope=instance", eid, actor)
            return self._status(eid, actor)

    def enqueue(self, eid):
        if self.pool is None:
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="statistics-export")
        self.pool.submit(self.run, eid)

    def _status(self, eid, actor):
        job = self.jobs.get(eid)
        if job is None:
            raise ReviewError("STATISTICS_EXPORT_EXPIRED", "统计导出任务已过期或服务已重启，请重新生成", 410)
        if job["actor"] != actor:
            raise ReviewError("STATISTICS_EXPORT_NOT_FOUND", "统计导出任务不存在", 404)
        return {
            "exportId": eid, "state": job["state"], "stage": job["stage"],
            "scope": "instance", "schemaVersion": statistics_export.EXPORT_SCHEMA_VERSION,
            "progress": dict(job["progress"]), "createdAt": utc(job["created"]),
            "updatedAt": utc(job["updated"]), "expiresAt": utc(job["expires"]),
            "fileName": job["fileName"], "sizeBytes": job["sizeBytes"],
            "downloadAllowed": job["state"] == "ready", "error": job["error"],
        }

    def status(self, eid, actor):
        with self.lock:
            self._expire(time.time())
            return self._status(eid, actor)

    def _progress(self, eid, **fields):
        with self.lock:
            job = self.jobs[eid]
            job["stage"] = fields.pop("stage")
            job["progress"].update(fields)
            job["updated"] = time.time()

    def _fail(self, eid, code, message):
        job = self.jobs[eid]
        job.update(state="failed", stage="failed", updated=time.time(), error={"code": code, "message": message})

    def run(self, eid):
        with self.lock:
            job = self.jobs.get(eid)
            if not job or job["state"] != "queued":
                return
            job.update(state="running", stage="reading", updated=time.time())
            filename, directory = job["fileName"], self.runtime
        started = time.monotonic()
        try:
            output = statistics_export.export_statistics_zip(self.database, directory, output_name=filename, export_id=eid, progress=lambda **fields: self._progress(eid, **fields))
            with self.lock:
                job.update(state="ready", stage="ready", updated=time.time(), path=output, sizeBytes=output.stat().st_size)
                self._expire(time.time())
            log.info("statistics.ready export=%s rows=%s size=%s duration=%.3f", eid, job["progress"]["rowsWritten"], job["sizeBytes"], time.monotonic() - started)
        except Exception as exc:
            with self.lock:
                message = str(exc) if isinstance(exc, statistics_export.ExportError) else "生成统计数据失败，请查看服务器日志后重新生成"
                self._fail(eid, "STATISTICS_EXPORT_FAILED", message)
            log.exception("statistics.failed export=%s duration=%.3f", eid, time.monotonic() - started)

    def download(self, eid, actor):
        with self.lock:
            self._expire(time.time())
            state = self._status(eid, actor)
            if state["state"] == "failed":
                raise ReviewError("STATISTICS_EXPORT_FAILED", state["error"]["message"], 409)
            if state["state"] != "ready":
                raise ReviewError("STATISTICS_EXPORT_NOT_READY", "统计数据尚未生成完成，请稍候", 409)
            job = self.jobs[eid]
            try:
                stream = Path(job["path"]).open("rb")
            except OSError:
                self._fail(eid, "STATISTICS_EXPORT_FILE_MISSING", "统计数据文件无法读取，请重新生成")
                log.exception("statistics.download_failed export=%s", eid)
                raise ReviewError("STATISTICS_EXPORT_FILE_MISSING", "统计数据文件无法读取，请重新生成", 410)
            job["leases"].add(stream)
        return stream, state["fileName"], state["sizeBytes"]

    def release(self, eid, stream):
        stream.close()
        with self.lock:
            job = self.jobs.get(eid)
            if job:
                job["leases"].discard(stream)

    def shutdown(self):
        with self.lock:
            self.closed = True
            pool = self.pool
        if pool:
            pool.shutdown(wait=True)


def manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ExportManager(STORAGE_ROOT / ".statistics-exports", db.DB_FILE)
        return _manager


def shutdown():
    global _manager
    with _manager_lock:
        previous, _manager = _manager, None
    if previous:
        previous.shutdown()
