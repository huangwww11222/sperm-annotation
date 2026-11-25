"""Microfluidic mode over HTTP. Judging never returns scores, where a target came from, or which ones are planted."""

import json
import logging
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import Field

from . import flow_export, flow_recheck, flow_store as store
from .api_common import Key, LoggedRoute, Model, User
from .auth import current_download_user
from .review_workflow import ReviewError

log = logging.getLogger('review.flow')
router = APIRouter(prefix='/api/flow', tags=['flow'], route_class=LoggedRoute)
# Under the path of the existing download cookie, so the browser can save a ZIP without a token in the link.
download_router = APIRouter(prefix='/api/datasets/exports/flow', tags=['flow'], route_class=LoggedRoute)

MediaId = Annotated[str, Field(min_length=1, max_length=255)]
TargetKey = Annotated[str, Field(min_length=1, max_length=32)]
View = Literal['e', 'r']


class AddMedia(Model):
    mediaIds: list[MediaId] = Field(min_length=1, max_length=2000)
    sample: str = Field(default='', max_length=200)


class MediaList(Model):
    mediaIds: list[MediaId] = Field(min_length=1, max_length=2000)


class Find(Model):
    force: bool = False


class Page(Model):
    runId: str = Field(min_length=1, max_length=64)
    picked: list[TargetKey] = Field(max_length=store.PAGE)
    ms: int = Field(ge=0)
    revision: int = Field(ge=0)


class Answer(Model):
    runId: str = Field(min_length=1, max_length=64)
    answer: Literal['yes', 'no', 'unsure']
    dropped: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list, max_length=64)
    ms: int = Field(ge=0)
    revision: int = Field(ge=0)


class Scan(Model):
    runId: str = Field(min_length=1, max_length=64)
    points: list[list[float]] = Field(max_length=store.MAX_POINTS)
    ms: int = Field(ge=0)


class Settle(Model):
    answer: Literal['yes', 'no', 'unsure']
    dropped: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list, max_length=64)
    revision: int = Field(ge=0)


class ExportBody(Model):
    mediaIds: list[MediaId] = Field(min_length=1, max_length=500)
    step: int = Field(default=4, ge=1, le=50)
    trainRatio: float = Field(default=.8, gt=0, le=1)
    includeSourceVideos: bool = Field(default=False, strict=True)


def _latest_tasks() -> dict[str, dict]:
    from . import main
    with main.TASK_LOCK:
        return {item['mediaId']: item for item in main.TASKS.values() if item.get('kind') == 'flow'}


def _public(task: dict | None) -> dict | None:
    return {k: task.get(k) for k in ('taskId', 'status', 'stage', 'message', 'processedFrames', 'totalFrames')} if task else None


@router.get('/media')
def list_media(user: User):
    from . import main, media_library
    folders, tasks, items = media_library.paths(), _latest_tasks(), []
    overview = store.overview()
    names = main._user_names({p['lastActorId'] for o in overview.values() if (p := o['progress']) and p['lastActorId']})
    for media_id, entry in overview.items():
        directory = main.media_dir(media_id)
        video = main.find_video(directory)
        if not video:
            continue
        try:
            meta = json.loads((directory / 'media.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            meta = {}
        task, progress = tasks.get(media_id), entry['progress']
        busy = bool(task and task.get('status') in ('queued', 'running'))
        if progress:
            progress['lastActorName'] = names.get(progress['lastActorId'])
        items.append({
            'mediaId': media_id, 'videoName': meta.get('videoName') or video.name, 'folderPath': folders.get(media_id, ''),
            'sample': entry['sample'], 'frameCount': meta.get('frameCount') or 0, 'fps': meta.get('fps') or 0,
            'width': meta.get('width') or 0, 'height': meta.get('height') or 0, 'sizeBytes': video.stat().st_size,
            'task': _public(task) if task and (busy or task.get('status') == 'failed') else None,
            'state': 'finding' if busy else progress['state'] if progress else 'failed' if task and task.get('status') == 'failed' else 'new',
            'progress': progress, 'recheck': entry['recheck'], 'imported': entry['imported'],
        })
    items.sort(key=lambda item: (item['folderPath'], item['videoName'], item['mediaId']))
    return {'items': items}


@router.put('/media')
def add_media(body: AddMedia, user: User):
    from . import main
    for media_id in body.mediaIds:
        if main.tracking_is_busy(media_id):
            raise ReviewError('SOURCE_BUSY', '视频正在自动预标或追踪，请等待完成后再改成微流控')
    added = store.add(list(dict.fromkeys(body.mediaIds)), body.sample, user['uid'])
    queued = [media_id for media_id in added if _queue(media_id, user['uid'], '', only_new=True)]
    return {'added': added, 'queued': queued}


@router.post('/media/remove')
def remove_media(body: MediaList, user: User):
    from . import main
    for media_id in body.mediaIds:
        if main.tracking_is_busy(media_id):
            raise ReviewError('SOURCE_BUSY', '正在找目标，请等待完成')
    return {'removed': store.remove(list(dict.fromkeys(body.mediaIds)), user['uid'])}


@router.post('/import-package', status_code=201)
async def import_package(user: User, file: UploadFile = File(...), folderPath: str = Form('')):
    from . import flow_import, main, media_library
    folder = media_library.normalize(folderPath)
    main.TRACK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='_import-', dir=main.TRACK_DATA_DIR) as tmp:
            package = Path(tmp) / 'package.zip'
            if await run_in_threadpool(main._save_upload_stream, file.file, package, main.MAX_PACKAGE_BYTES) <= 0:
                raise HTTPException(400, '文件为空')
            report = await run_in_threadpool(flow_import.import_package, package, int(user['uid']), Path(tmp), folder)
            for item in report['imported']:
                await run_in_threadpool(_queue, item['mediaId'], int(user['uid']), '', True)
            return report
    finally:
        await file.close()


# ------------------------------------------------------------------ finding targets

def _detect(images):
    from . import head_click
    return head_click.detect_batch(images, caller='flow')


def _run_find(task_id: str, media_id: str) -> None:
    from . import flow_import, flow_targets, main
    from .media_reimport import source_sha
    began = time.perf_counter()
    main._set_task(task_id, status='running', stage='detecting', message='正在找画面里的目标', processedFrames=0)
    try:
        directory = main.media_dir(media_id)
        video = main.find_video(directory)
        if not video:
            raise RuntimeError('视频不存在')
        flow_targets.clean_leftovers(directory)
        info = main.source_media_info(directory)
        report = lambda **patch: main._set_task(task_id, elapsedSeconds=round(time.perf_counter() - began, 1), **patch)
        sha256, waiting = source_sha(directory, video), flow_import.pending(directory)
        if waiting:
            record = flow_import.finish(media_id, directory, video, waiting, sha256, report)
        else:
            record = flow_targets.find(directory, video, info, sha256, _detect, progress=report)
            store.publish_run(media_id, record)
        main._set_task(task_id, status='success', stage='completed', message='completed', processedFrames=record['frameCount'],
                       lastProcessedFrame=record['frameCount'] - 1, reachedVideoEnd=True, targets=len(record['targets']))
        log.info('flow.task_finished task=%s media=%s elapsed_ms=%.0f', task_id, media_id, (time.perf_counter() - began) * 1000)
    except Exception as exc:
        log.exception('flow.task_failed task=%s media=%s', task_id, media_id)
        message = exc.message if isinstance(exc, ReviewError) else str(exc) if isinstance(exc, (RuntimeError, ValueError)) else '找目标失败，请重试'
        main._set_task(task_id, status='failed', stage='failed', message=message)


def _queue(media_id: str, actor: int, request_key: str, only_new: bool = False) -> dict | None:
    from . import main
    if main.tracking_is_busy(media_id):
        if only_new:
            return None
        raise ReviewError('SOURCE_BUSY', '已在排队或正在找目标，请等待完成')
    if only_new and store.overview()[media_id]['progress']:
        return None
    directory = main.media_dir(media_id)
    if not main.find_video(directory):
        raise ReviewError('MEDIA_NOT_FOUND', '视频不存在', 404)
    frames = int(main.source_media_info(directory)['frameCount'])
    task_id = uuid.uuid4().hex
    main._set_task(task_id, taskId=task_id, kind='flow', status='queued', stage='queued', message='queued', mediaId=media_id, startFrame=0,
                   maxFrames=frames, totalFrames=frames, userId=actor, requestKey=request_key, requestHash='flow')
    main.TRACK_EXECUTOR.submit(_run_find, task_id, media_id)
    log.info('flow.queued task=%s media=%s frames=%s actor=%s', task_id, media_id, frames, actor)
    return {'taskId': task_id, 'status': 'queued'}


@router.post('/media/{media_id}/find', status_code=202)
def find(media_id: str, body: Find, user: User, idempotency_key: str = Header(default='')):
    from . import main
    request_key = idempotency_key if isinstance(idempotency_key, str) else ''
    if len(request_key) > 128:
        raise ReviewError('INVALID_REQUEST', '请求重试标识过长', 422)
    if request_key:
        with main.TASK_LOCK:
            prior = next((t for t in main.TASKS.values() if t.get('userId') == user['uid'] and t.get('requestKey') == request_key), None)
        if prior:
            if prior.get('kind') != 'flow' or prior.get('mediaId') != media_id:
                raise ReviewError('IDEMPOTENCY_KEY_REUSED', '同一重试标识不能用于不同请求')
            return {'taskId': prior['taskId'], 'status': prior['status']}
    if not store.is_flow(media_id):
        raise ReviewError('FLOW_NOT_FOUND', '这个视频不是微流控模式', 404)
    entry = store.overview()[media_id]
    progress = entry['progress']
    if entry['recheck']:
        raise ReviewError('FLOW_RECHECKING', '这段有复查，不能重新找目标；请先作废复查')
    if progress and not body.force and (progress['pagesDone'] or progress['secondDone'] or progress['scanDone']):
        raise ReviewError('FLOW_HAS_ANSWERS', '这段已经有判定；重新找目标可能要从头判')
    return _queue(media_id, user['uid'], request_key)


# ------------------------------------------------------------------ judging

@router.get('/media/{media_id}')
def detail(media_id: str, user: User, recheck: bool = False):
    return flow_recheck.detail(media_id) if recheck else store.detail(media_id)


@router.post('/media/{media_id}/recheck')
def start_recheck(media_id: str, user: User):
    return flow_recheck.start(media_id, user['uid'])


@router.delete('/media/{media_id}/recheck/{recheck_id}')
def cancel_recheck(media_id: str, recheck_id: str, user: User):
    return flow_recheck.cancel(media_id, recheck_id, user['uid'])


@router.post('/media/{media_id}/recheck/{recheck_id}/compare')
def compare_recheck(media_id: str, recheck_id: str, user: User):
    return flow_recheck.compare(media_id, recheck_id, user['uid'])


@router.put('/media/{media_id}/recheck/{recheck_id}/settles/{key}')
def put_settle(media_id: str, recheck_id: str, key: str, body: Settle, user: User):
    return flow_recheck.settle(media_id, recheck_id, key, body.answer, body.dropped, body.revision, user['uid'])


def _image(path):
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=31536000, immutable'})


@router.get('/media/{media_id}/strips/{key}')
def strip(media_id: str, key: str, run: str, user: User, view: View = 'e'):
    return _image(store.strip_path(media_id, key, view, run))


@router.get('/media/{media_id}/scan-image')
def scan_image(media_id: str, run: str, user: User, view: View = 'e'):
    return _image(store.scan_path(media_id, view, run))


@router.put('/media/{media_id}/pages/{page}')
def put_page(media_id: str, page: int, body: Page, user: User):
    return store.save_page(media_id, body.runId, page, body.picked, body.ms, body.revision, user['uid'])


@router.put('/media/{media_id}/answers/{key}')
def put_answer(media_id: str, key: str, body: Answer, user: User):
    return store.save_answer(media_id, body.runId, key, body.answer, body.dropped, body.ms, body.revision, user['uid'])


@router.put('/media/{media_id}/scan')
def put_scan(media_id: str, body: Scan, user: User):
    return store.save_scan(media_id, body.runId, body.points, body.ms, user['uid'])


@router.get('/misses')
def misses(user: User):
    return store.misses()


# ------------------------------------------------------------------ export

@router.post('/exports')
def create_export(body: ExportBody, user: User, key: Key):
    return flow_export.create(user['uid'], key, body.model_dump())


@router.get('/exports')
def list_exports(user: User):
    return {'items': flow_export.jobs(user['uid'])}


@router.delete('/exports/{eid}')
def delete_export(eid: str, user: User):
    return flow_export.delete(eid, user['uid'])


@download_router.get('/{eid}/download')
def download(eid: str, user: Annotated[dict, Depends(current_download_user)]):
    return FileResponse(flow_export.download(eid, user['uid']), media_type='application/zip')
