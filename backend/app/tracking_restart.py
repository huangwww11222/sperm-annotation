"""Explicit earlier-frame restarts, with recoverable file publication and receipts."""
from contextlib import closing, nullcontext
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException

from . import annotation_state, media_reimport
from .db import connect
from .review_source_lock import source_write
from .review_workflow import now, packed

log = logging.getLogger("review.tracking")
JOURNAL_ROOT = "_tracking_restarts"
FILES = ("tracker_results.json", annotation_state.FILE_NAME)


def _sync_directory(directory):
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path, data):
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_replace(source, target):
    temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".restore")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        temporary.replace(target)
        _sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_file(staged, target):
    staged.replace(target)
    _sync_directory(target.parent)


def _invalidate_old_preview(directory, fingerprint):
    if fingerprint is None:
        return
    path = directory / 'tracker_overlay.mp4'
    try:
        if not path.is_file():
            return
        stat = path.stat()
        if [stat.st_size, stat.st_mtime_ns, stat.st_ino] == fingerprint:
            path.unlink()
            _sync_directory(directory)
    except OSError:
        # Results are already committed. Cached preview reads still validate
        # their result signature and must regenerate or fail, never serve this
        # obsolete file. Do not turn cache cleanup into a failed source edit.
        log.exception('tracking.preview_cleanup_pending media=%s', directory.name)


@source_write
def recover(root, media_id=None):
    journals = Path(root) / JOURNAL_ROOT
    if not journals.is_dir():
        return
    for folder in list(journals.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            raise RuntimeError("Invalid tracking restart journal directory")
        journal = folder / "journal.json"
        if not journal.is_file():
            # Original files are untouched until the journal is durable.
            try:
                shutil.rmtree(folder)
            except OSError:
                log.exception("tracking.branch_cleanup_pending directory=%s", folder.name)
            continue
        data = json.loads(journal.read_text(encoding="utf-8"))
        mid = data.get("mediaId")
        if data.get("format") != "tracking-restart-v1" or not isinstance(mid, str) or not mid or mid in {".", ".."} or any(c in mid for c in ("/", "\\", "\x00")) or set(data.get("files", {})) != set(FILES):
            raise RuntimeError("Invalid tracking restart journal")
        if media_id is not None and mid != media_id:
            continue
        directory = Path(root) / mid
        if data.get('commitMode') == 'file':
            marker = folder / 'committed.json'
            committed = marker.is_file() and json.loads(marker.read_text(encoding='utf-8')).get('hash') == data['hash']
        elif data.get('commitMode', 'sql') == 'sql':
            with closing(connect()) as c:
                receipt = c.execute("SELECT request_hash FROM review_write_receipts WHERE user_id=? AND request_key=?", (data["uid"], data["key"])).fetchone()
            committed = receipt is not None and receipt["request_hash"] == data["hash"]
        else:
            raise RuntimeError('Invalid tracking restart commit mode')
        if not committed and not data.get("rolledBack"):
            for name, existed in data["files"].items():
                if existed:
                    backup = folder / (name + ".old")
                    if not backup.is_file():
                        raise RuntimeError("Tracking restart backup is missing")
                    _copy_replace(backup, directory / name)
                else:
                    (directory / name).unlink(missing_ok=True)
            _sync_directory(directory)
            data["rolledBack"] = True
            _write(folder / "journal.tmp", packed(data).encode())
            (folder / "journal.tmp").replace(journal)
            _sync_directory(folder)
        if committed:
            _invalidate_old_preview(directory, data.get('previewFingerprint'))
        # A committed operation must never overwrite a later workspace or run.
        try:
            shutil.rmtree(folder)
            _sync_directory(journals)
        except OSError:
            log.exception("tracking.branch_cleanup_pending media=%s committed=%s", mid, committed)
        log.warning("tracking.branch_recovered media=%s committed=%s", mid, committed)


def recover_directory(directory):
    if (directory.parent / JOURNAL_ROOT).is_dir():
        try:
            recover(directory.parent, directory.name)
        except Exception as exc:
            log.exception("tracking.branch_recovery_failed media=%s", directory.name)
            raise HTTPException(503, "追踪分支恢复尚未完成，请保留当前编辑并联系管理员检查存储") from exc


def _object_id(item):
    return item.get("objectId", item.get("object_id")) if isinstance(item, dict) else None


def _stage_branch_files(directory, rows, workspace_bytes, *, uid, key, digest, commit_mode='sql', folder=None, invalidate_preview=False):
    folder = folder or directory.parent / JOURNAL_ROOT / uuid.uuid4().hex
    folder.mkdir(parents=True, mode=0o700)
    existed = {}
    for name in FILES:
        path = directory / name
        existed[name] = path.is_file()
        if existed[name]:
            shutil.copyfile(path, folder / (name + '.old'))
            with (folder / (name + '.old')).open('r+b') as stream:
                os.fsync(stream.fileno())
    with (folder / (FILES[0] + '.new')).open('wb') as stream:
        for row in rows:
            stream.write((packed(row) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    if workspace_bytes is not None:
        _write(folder / (FILES[1] + '.new'), workspace_bytes)
    fingerprint = None
    preview = directory / 'tracker_overlay.mp4'
    if invalidate_preview and preview.is_file():
        stat = preview.stat()
        fingerprint = [stat.st_size, stat.st_mtime_ns, stat.st_ino]
    _write(folder / 'journal.tmp', packed(dict(format='tracking-restart-v1', mediaId=directory.name,
           uid=uid, key=key, hash=digest, files=existed, commitMode=commit_mode,
           previewFingerprint=fingerprint)).encode())
    (folder / 'journal.tmp').replace(folder / 'journal.json')
    _sync_directory(folder)
    _sync_directory(folder.parent)
    _sync_directory(directory.parent)
    return folder


@source_write
def rewind(directory, cutoff, *, uid=None, request_key=None):
    """Trim future rows and retire their relationships in one recoverable edit.

    HTTP callers use their real user's SQL receipt as the commit point. Direct
    standalone tracking has no user account, so the same journal uses a durable
    local commit marker. Neither path advances the editor workspace revision.
    """
    directory = Path(directory)
    if type(cutoff) is not int or cutoff < 0:
        raise HTTPException(422, '追踪回退帧无效')
    key = request_key or 'rewind-internal-' + uuid.uuid4().hex
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise HTTPException(428, '追踪回退需要有效的重试标识')
    recover_directory(directory)
    state = annotation_state.read_state(directory)
    digest = hashlib.sha256(packed(['tracking-rewind', directory.name, cutoff, state.get('generationId')]).encode()).hexdigest()
    folder = None
    try:
        with (closing(connect()) if uid is not None else nullcontext(None)) as c:
            if c is not None:
                c.execute('BEGIN IMMEDIATE')
                prior = c.execute('SELECT request_hash,response_json FROM review_write_receipts WHERE user_id=? AND request_key=?', (uid, key)).fetchone()
                if prior:
                    if prior['request_hash'] != digest:
                        raise HTTPException(409, '同一重试标识不能用于不同追踪回退')
                    log.info('tracking.rewind_replay media=%s actor=%s key=%s', directory.name, uid, key)
                    return json.loads(prior['response_json'])
            rows = annotation_state.read_rows(directory, validate_geometry=True)
            kept = [row for row in rows if row['source_frame_index'] <= cutoff]
            clean = annotation_state.retire_future_relationships(state, cutoff)
            response = dict(cutoffFrame=cutoff, keptRows=len(kept), removedRows=len(rows)-len(kept),
                            deletedFutureSeedFiles=0, overlayRegenerated=False)
            if response['removedRows'] or clean != state:
                # An absent legacy workspace must stay absent: publishing an
                # empty file would hide its durable manual seed annotations.
                workspace_bytes = packed(clean).encode() if (directory / annotation_state.FILE_NAME).is_file() or clean else None
                if workspace_bytes is not None and len(workspace_bytes) > 64 * 1024 * 1024:
                    raise HTTPException(413, '工作区状态超过 64 MiB')
                folder = directory.parent / JOURNAL_ROOT / uuid.uuid4().hex
                _stage_branch_files(directory, kept, workspace_bytes, uid=uid, key=key,
                                    digest=digest, commit_mode='sql' if c is not None else 'file', folder=folder,
                                    invalidate_preview=response['removedRows'] > 0)
                for name in FILES:
                    if name == annotation_state.FILE_NAME and workspace_bytes is None:
                        continue
                    _publish_file(folder / (name + '.new'), directory / name)
            if c is not None:
                c.execute('INSERT INTO review_write_receipts VALUES (?,?,?,?,?)', (uid, key, digest, packed(response), now()))
                c.commit()
            elif folder is not None:
                _write(folder / 'commit.tmp', packed({'hash': digest}).encode())
                (folder / 'commit.tmp').replace(folder / 'committed.json')
                _sync_directory(folder)
        retired_count = len(clean.get('_retiredTrackingRelationshipEventIds', [])) - len(state.get('_retiredTrackingRelationshipEventIds', []))
        log.info('tracking.rewind media=%s cutoff=%s kept=%s removed=%s retired_relationships=%s revision=%s key=%s',
                 directory.name, cutoff, response['keptRows'], response['removedRows'], retired_count, state.get('revision', 0), key)
        return response
    except HTTPException:
        raise
    except Exception as exc:
        log.exception('tracking.rewind_failed media=%s cutoff=%s key=%s', directory.name, cutoff, key)
        raise HTTPException(503, '追踪回退保存失败，旧结果和判断已保留，请重试') from exc
    finally:
        if folder is not None:
            recover_directory(directory)


def restart(directory, uid, key, body, info, tracking_busy):
    """Caller holds source_write. SQL commit decides both-file recovery."""
    media_id = directory.name
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise HTTPException(428, "重建追踪分支需要有效的重试标识")
    if set(body) - {"expectedRevision", "expectedPausedFrame", "startFrame", "generationId", "confirmDiscardFuture"}:
        raise HTTPException(422, "重建追踪分支包含未知字段")
    if any(type(body.get(k)) is not int or body[k] < 0 for k in ("expectedRevision", "expectedPausedFrame", "startFrame")) or body.get("confirmDiscardFuture") is not True:
        raise HTTPException(422, "请明确确认重建分支并提供有效的帧号及版本")
    h = hashlib.sha256(packed(["restart-tracking-branch", media_id, body]).encode()).hexdigest()
    recover_directory(directory)
    state = annotation_state.read_state(directory)
    annotation_state.require_generation(state, body.get("generationId"))
    folder = None
    try:
        with closing(connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            prior = c.execute("SELECT request_hash,response_json FROM review_write_receipts WHERE user_id=? AND request_key=?", (uid, key)).fetchone()
            if prior:
                if prior["request_hash"] != h:
                    raise HTTPException(409, "同一重试标识不能用于不同操作")
                log.info("tracking.branch_replay media=%s actor=%s key=%s", media_id, uid, key)
                return json.loads(prior["response_json"])
            allowed, reason = media_reimport.overwrite_permission(c, media_id)
            if not allowed:
                raise HTTPException(409, reason)
            if tracking_busy():
                raise HTTPException(409, "AI Tracking 正在运行，暂时不能重建追踪分支")
            revision = int(state.get("revision", 0))
            if body["expectedRevision"] != revision:
                raise HTTPException(409, "工作区版本已变化，请保留当前编辑并重新读取")
            start, paused = body["startFrame"], body["expectedPausedFrame"]
            if not 0 <= start < paused < info["frameCount"]:
                raise HTTPException(422, "重建起点必须在暂停帧之前且位于原视频范围内")
            context = state.get("lastPausedContext") or {}
            pending = [item for item in state.get("pausedAnomalies", []) if not item.get("resolved") and not annotation_state.is_deleted(state, paused, _object_id(item))]
            if context.get("frameIndex") != paused or context.get("mediaId", media_id) != media_id or not pending:
                raise HTTPException(409, "当前暂停已变化或已解决，请重新读取工作区")
            if not (directory / "tracker_results.json").is_file():
                raise HTTPException(409, "暂停对应的追踪结果缺失，不能重建分支")
            rows = annotation_state.read_rows(directory, validate_geometry=True)
            if any(row["source_frame_index"] >= info["frameCount"] for row in rows):
                raise HTTPException(500, "追踪结果含超出原视频的帧号，不能重建分支")
            paused_row = next((row for row in rows if row["source_frame_index"] == paused), None)
            evidence = {_object_id(obj) for obj in (paused_row or {}).get("anomalies", [])}
            evidence.update(_object_id(obj) for obj in (paused_row or {}).get("objects", []) if obj.get("anomaly_level") in {"anomaly", "disappeared", "warning"})
            if not paused_row or any(_object_id(item) not in evidence for item in pending):
                raise HTTPException(409, "暂停缺少真实追踪异常依据，请重新读取结果")
            annotation_state.validate_workspace_geometry(state, info)
            manual = [obj for obj in state.get("manualAnnotations", []) if obj.get("frameIndex") == start and obj.get("source") == "manual" and isinstance(obj.get("bbox"), dict)]
            ai = next((row["objects"] for row in rows if row["source_frame_index"] == start), [])
            if not any(not annotation_state.is_deleted(state, start, _object_id(obj)) for obj in [*manual, *ai]):
                raise HTTPException(422, "重建起点没有有效的框，请先修正并保存人工框")
            kept = [row for row in rows if row["source_frame_index"] <= start]
            clean = {**annotation_state.retire_future_relationships(state, start), "revision": revision + 1, "updatedBy": uid, "pausedAnomalies": [], "lastPausedContext": None,
                     "_pauseCandidates": [], "_pauseContext": None,
                     "anomalyFrames": [item for item in state.get("anomalyFrames", []) if isinstance(item, dict) and type(item.get("frame_index")) is int and item["frame_index"] <= start]}
            response = dict(ok=True, mediaId=media_id, revision=revision + 1, pausedAnomalies=[], lastPausedContext=None,
                            cutoffFrame=start, removedRows=len(rows) - len(kept), keptRows=len(kept), deletedFutureSeedFiles=0, overlayRegenerated=False)
            workspace_bytes = packed(clean).encode()
            if len(workspace_bytes) > 64 * 1024 * 1024:
                raise HTTPException(413, "工作区状态超过 64 MiB")
            folder = directory.parent / JOURNAL_ROOT / uuid.uuid4().hex
            _stage_branch_files(directory, kept, workspace_bytes, uid=uid, key=key, digest=h, folder=folder,
                                invalidate_preview=response['removedRows'] > 0)
            for name in FILES:
                _publish_file(folder / (name + ".new"), directory / name)
            c.execute("INSERT INTO review_write_receipts VALUES (?,?,?,?,?)", (uid, key, h, packed(response), now()))
            c.commit()
        log.info("tracking.branch_restarted media=%s actor=%s cutoff=%s paused=%s removed=%s revision=%s key=%s", media_id, uid, start, paused, response["removedRows"], revision + 1, key)
        return response
    except HTTPException as exc:
        log.warning("tracking.branch_rejected media=%s actor=%s status=%s reason=%s", media_id, uid, exc.status_code, exc.detail)
        raise
    except Exception as exc:
        log.exception("tracking.branch_failed media=%s actor=%s key=%s", media_id, uid, key)
        raise HTTPException(503, "重建追踪分支失败，旧数据已保留，请使用原请求重试") from exc
    finally:
        if folder is not None:
            recover_directory(directory)
