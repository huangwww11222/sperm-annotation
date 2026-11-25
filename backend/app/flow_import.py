"""Import a microfluidic ZIP exported with its original videos: judged videos arrive judged.

Targets and answers come from the package, so what the sender saw is what is shown here. Everything
read from it is checked: the video against its SHA-256, the targets against their run id, and every
number against the video. The film strips are drawn again from the video. A re-check is optional.
"""
from __future__ import annotations

import json
import logging
import math
import re
import shutil
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import flow_store as store, flow_targets
from .db import connect
from .package_import import MANIFEST_LIMIT, PROVENANCE_LIMIT, SAFE_ID, VIDEO_PATH, Skip, _discard, _extract_video, _read_json
from .review_workflow import now, packed

log = logging.getLogger('review.media')
PENDING = 'flow-import.json'
MAX_TARGETS = 20000
KEY = re.compile(r'^T\d{4,6}$')
HEADS = {'dark', 'bright', 'clear', 'pale'}


def _number(value: Any, low: float, high: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _need(ok: bool) -> None:
    if not ok:
        raise Skip('目标记录无效或已损坏')


def checked_record(data: Any, source: dict) -> dict:
    """Only known fields, only values this program could have written."""
    _need(isinstance(data, dict))
    if data.get('policy') != flow_targets.POLICY:
        raise Skip('找目标的版本不同，请用新版工具重新导出')
    n, w, h = (data.get(k) for k in ('frameCount', 'width', 'height'))
    _need(all(type(v) is int and v > 0 for v in (n, w, h)) and (n, w, h) == tuple(source.get(k) for k in ('frameCount', 'width', 'height')))
    _need(data.get('sha256') == source.get('sourceSha256') and data.get('cell') == flow_targets.STRIP_CELL)
    _need(type(data.get('scanFrame')) is int and 0 <= data['scanFrame'] < n)
    intervals = data.get('intervals')
    _need(isinstance(intervals, list) and len(intervals) == max(0, n - 1) and all(type(v) is int and 1 <= v <= 100 for v in intervals))

    def rows_of(value):
        _need(isinstance(value, list) and 0 < len(value) <= n)
        for row in value:
            _need(isinstance(row, list) and len(row) == 5 and type(row[0]) is int and 0 <= row[0] < n)
            _need(_number(row[1], -w, 2 * w) and _number(row[2], -h, 2 * h) and _number(row[3], 0, w) and _number(row[4], 0, h))
        frames = [row[0] for row in value]
        _need(frames == sorted(set(frames)))
        return value, frames
    targets, seen = data.get('targets'), set()
    _need(isinstance(targets, list) and len(targets) <= MAX_TARGETS)
    clean = []
    for item in targets:
        _need(isinstance(item, dict) and isinstance(item.get('id'), str) and bool(KEY.match(item['id'])) and item['id'] not in seen)
        seen.add(item['id'])
        rows, frames = rows_of(item.get('frames'))
        shown, best = item.get('shown'), item.get('best')
        _need(isinstance(shown, list) and 0 < len(shown) <= flow_targets.STRIP_FRAMES and shown == sorted(set(shown)) and set(shown) <= set(frames))
        _need(type(best) is int and 0 <= best < len(shown) and (item.get('first'), item.get('last')) == (frames[0], frames[-1]))
        _need(_number(item.get('score'), 0, 1) and _number(item.get('energy'), 0, 1e6) and item.get('head') in HEADS)
        clean.append({k: item[k] for k in ('id', 'first', 'last', 'frames', 'shown', 'best', 'score', 'energy', 'head')})
    faint = data.get('faint')
    _need(isinstance(faint, list) and len(faint) <= MAX_TARGETS)
    for track in faint:
        rows_of(track)
    run_id = flow_targets.run_id_of(data['sha256'], n, clean)
    _need(data.get('runId') == run_id == source.get('runId'))
    number = lambda key: data[key] if _number(data.get(key), -1e6, 1e6) else 0
    return {'policy': flow_targets.POLICY, 'runId': run_id, 'sha256': data['sha256'], 'width': w, 'height': h, 'frameCount': n, 'angle': number('angle'),
            'slow': data.get('slow') is True, 'intervals': intervals, 'lostFrames': sum(v - 1 for v in intervals), 'intervalFixes': number('intervalFixes'),
            'linkedShare': number('linkedShare'), 'unsteady': data.get('unsteady') is True,
            'detector': data['detector'] if data.get('detector') in ('ok', 'none', 'failed') else 'none', 'detectorError': None,
            'scanFrame': data['scanFrame'], 'cell': flow_targets.STRIP_CELL, 'targets': clean, 'faint': faint}


def checked_judgements(data: Any, record: dict) -> dict:
    by_id = {item['id']: item for item in record['targets']}
    if not isinstance(data, dict) or not isinstance(data.get('picked'), list) or not isinstance(data.get('answers'), dict):
        raise Skip('判定记录无效')
    picked, answers = data['picked'], {}
    if not all(isinstance(key, str) and key in by_id for key in picked) or len(set(picked)) != len(picked):
        raise Skip('判定记录和目标对不上')
    for key, value in data['answers'].items():
        dropped = value.get('dropped') if isinstance(value, dict) else None
        if key not in by_id or not isinstance(dropped, list) or value.get('answer') not in store.ANSWERS \
                or not all(type(f) is int and f in by_id[key]['shown'] for f in dropped) or len(set(dropped)) >= len(by_id[key]['shown']):
            raise Skip('判定记录和目标对不上')
        answers[key] = {'answer': value['answer'], 'dropped': sorted(set(dropped))}
    # An answer settled in a re-check may belong to a target nobody picked at first.
    picked = sorted(set(picked) | set(answers))
    if set(store.second_list(record, set(picked), {})) != set(answers):
        raise Skip('判定没有判完')
    return {'picked': picked, 'answers': answers, 'scanned': data.get('scanned') is True}


def pending(directory: Path) -> tuple[dict, dict] | None:
    path = directory / PENDING
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    return data['record'], data['judged']


def finish(media_id: str, directory: Path, video: Path, waiting: tuple[dict, dict], sha256: str, progress) -> dict:
    """The slow half, in the background: draw the strips, then publish targets and answers together."""
    record, judged = waiting
    if sha256 != record['sha256']:
        raise RuntimeError('原始视频和标注包里的不一致')
    flow_targets.redraw(directory, video, record, progress)
    store.restore_run(media_id, record, judged)
    (directory / PENDING).unlink(missing_ok=True)
    return record


def _enroll(media_id: str, directory: Path, sample: str, uid: int, record: dict, judged: dict, origin: dict) -> None:
    part = directory / f'.{PENDING}.{uuid.uuid4().hex}.tmp'
    part.write_text(packed({'record': record, 'judged': judged}), encoding='utf-8')
    part.replace(directory / PENDING)
    with closing(connect()) as c, c:
        c.execute('INSERT INTO flow_media(media_id,sample,imported_json,added_by,added_at) VALUES (?,?,?,?,?)', (media_id, sample, packed(origin), uid, now()))


def import_package(package: Path, uid: int, work: Path, folder: str) -> dict[str, Any]:
    from . import main, media_library
    try:
        archive = zipfile.ZipFile(package)
    except (zipfile.BadZipFile, OSError) as exc:
        raise HTTPException(422, '不是有效的 ZIP 文件') from exc
    with archive:
        try:
            manifest = _read_json(archive, 'manifest.json', MANIFEST_LIMIT)
        except Skip as exc:
            raise HTTPException(422, f'这不是微流控导出的数据集：{exc}') from exc
        sources = manifest.get('sources') if isinstance(manifest, dict) else None
        if not isinstance(sources, list) or not sources or manifest.get('kind') != 'microfluidic' or manifest.get('schemaVersion') != 1:
            raise HTTPException(422, '这不是微流控导出的数据集；湿片的标注包请在“标注”页导入')
        imported, skipped = [], []
        for index, source in enumerate(sources):
            label = f'第 {index + 1} 个视频'
            try:
                if not isinstance(source, dict):
                    raise Skip('来源记录无效')
                video_file = source.get('videoFile')
                if video_file is None:
                    raise Skip('导出时没有勾选“包含原视频”，无法导入')
                match = VIDEO_PATH.match(video_file) if isinstance(video_file, str) else None
                if not match or video_file not in archive.namelist():
                    raise Skip('原视频路径无效或缺失')
                filename = label = match.group(1)
                record = checked_record(_read_json(archive, f'targets/v{index:03d}.json', PROVENANCE_LIMIT), source)
                judged = checked_judgements(_read_json(archive, f'judgements/v{index:03d}.json', PROVENANCE_LIMIT), record)
                target = work / f'{index:03d}' / filename
                target.parent.mkdir(parents=True)
                if _extract_video(archive, video_file, target) != record['sha256']:
                    raise Skip('原视频校验失败，文件可能已损坏')
                try:
                    info = main._probe_video(target)
                except Exception as exc:
                    raise Skip(f'无法解码原视频：{exc}') from exc
                if tuple(info.get(k) for k in ('width', 'height', 'frameCount')) != (record['width'], record['height'], record['frameCount']):
                    raise Skip('原视频尺寸或帧数与目标记录不一致')
                try:
                    result = main.publish_upload(target, filename, info, uid)
                except HTTPException as exc:
                    raise Skip(str(exc.detail)) from exc
                if not isinstance(result, dict):
                    existing = json.loads(bytes(result.body))['duplicate']
                    raise Skip(f'服务器已有同一视频「{existing["videoName"]}」，未覆盖；要复查就直接打开它')
                media_id = result['mediaId']
                try:
                    sample = ((source['sample'] if isinstance(source.get('sample'), str) else '') or folder)[:100]
                    export_id = manifest.get('exportId')
                    _enroll(media_id, main.media_dir(media_id), sample, uid, record, judged,
                            {'exportId': export_id if isinstance(export_id, str) and SAFE_ID.match(export_id) else None, 'importedAt': now()})
                    if folder:
                        media_library.place(media_id, folder)
                except Exception as exc:
                    log.exception('flow.package_import_failed media=%s actor=%s', media_id, uid)
                    with closing(connect()) as c, c:
                        c.execute('DELETE FROM flow_media WHERE media_id=?', (media_id,))
                    _discard(main.media_dir(media_id))
                    raise Skip('写入失败，已撤销该视频的导入') from exc
                imported.append({'mediaId': media_id, 'videoName': filename, 'targets': len(record['targets'])})
                log.info('flow.package_imported media=%s actor=%s targets=%s', media_id, uid, len(record['targets']))
            except Skip as exc:
                skipped.append({'videoName': label, 'reason': str(exc)})
                log.warning('flow.package_skipped actor=%s source=%s reason=%s', uid, label, exc)
            finally:
                shutil.rmtree(work / f'{index:03d}', ignore_errors=True)
    return {'imported': imported, 'skipped': skipped}
