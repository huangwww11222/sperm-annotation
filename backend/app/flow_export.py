"""Training set from finished microfluidic videos: every target of an exported frame was judged.

Sperm become labels. "Unclear" targets, frames a person clicked off, frames where a sperm should
be but was not found, and faint things that were not shown become ignore regions: neither a
positive nor background. Everything else is background. The whole-frame check is never exported.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import shutil
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Lock

import cv2
import yaml

from . import flow_recheck, flow_store as store
from .config import DATASET_EXPORT_DIR
from .db import connect
from .review_workflow import ReviewError, digest, now, packed

log = logging.getLogger('review.dataset')
_pool: ThreadPoolExecutor | None = None
_pool_lock = Lock()
OUTSIDE = 6  # A sperm that was not found right after entering or before leaving is masked this many frames.


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS flow_exports (
      id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
      request_json TEXT NOT NULL, state TEXT NOT NULL, completed_frames INTEGER NOT NULL DEFAULT 0,
      total_frames INTEGER NOT NULL DEFAULT 0, manifest_json TEXT, error_message TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(actor_id, request_key)
    );
    """)


def recover_interrupted() -> None:
    with closing(connect()) as c, c:
        c.execute("UPDATE flow_exports SET state='failed',error_message='服务重启中断了导出，请重新生成',updated_at=? WHERE state IN ('queued','running')", (now(),))


def shutdown() -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool:
        pool.shutdown(wait=True)


def _path(eid: str) -> Path:
    return DATASET_EXPORT_DIR / f'{eid}.zip'


def create(actor: int, key: str, body: dict) -> dict:
    global _pool
    body = {**body, 'mediaIds': list(dict.fromkeys(body['mediaIds']))}
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM flow_exports WHERE actor_id=? AND request_key=?', (actor, key)).fetchone()
        if old:
            if old['request_hash'] != digest(body):
                raise ReviewError('IDEMPOTENCY_KEY_REUSED', '同一个重试标识不能用于不同导出设置')
            return _job(old)
        _sources(c, body['mediaIds'])
        eid = 'flow_' + uuid.uuid4().hex
        c.execute('INSERT INTO flow_exports(id,actor_id,request_key,request_hash,request_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                  (eid, actor, key, digest(body), packed(body), 'queued', now(), now()))
        row = c.execute('SELECT * FROM flow_exports WHERE id=?', (eid,)).fetchone()
    log.info('flow.export_queued export=%s actor=%s media=%s', eid, actor, len(body['mediaIds']))
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='flow-export')
        _pool.submit(run, eid)
    return _job(row)


def _job(row) -> dict:
    body, manifest = json.loads(row['request_json']), json.loads(row['manifest_json']) if row['manifest_json'] else None
    return {'exportId': row['id'], 'state': row['state'], 'completedFrames': row['completed_frames'], 'totalFrames': row['total_frames'],
            'videoCount': len(body['mediaIds']), 'step': body['step'], 'counts': manifest['counts'] if manifest else None,
            'errorMessage': row['error_message'], 'createdAt': row['created_at'],
            'downloadUrl': f"/api/datasets/exports/flow/{row['id']}/download" if row['state'] == 'ready' else None}


def jobs(actor: int, limit: int = 20) -> list[dict]:
    with closing(connect()) as c:
        return [_job(row) for row in c.execute("SELECT * FROM flow_exports WHERE actor_id=? AND state<>'deleted' ORDER BY created_at DESC,rowid DESC LIMIT ?", (actor, limit))]


def download(eid: str, actor: int) -> Path:
    with closing(connect()) as c:
        row = c.execute('SELECT state FROM flow_exports WHERE id=? AND actor_id=?', (eid, actor)).fetchone()
    if not row or row['state'] == 'deleted':
        raise ReviewError('EXPORT_NOT_FOUND', '导出任务不存在', 404)
    if row['state'] != 'ready':
        raise ReviewError('EXPORT_NOT_READY', '导出尚未完成')
    if not _path(eid).is_file():
        raise ReviewError('EXPORT_FILE_MISSING', '导出文件已不存在，请重新生成', 404)
    return _path(eid)


def delete(eid: str, actor: int) -> dict:
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT state FROM flow_exports WHERE id=? AND actor_id=?', (eid, actor)).fetchone()
        if not row or row['state'] == 'deleted':
            raise ReviewError('EXPORT_NOT_FOUND', '导出任务不存在', 404)
        if row['state'] in ('queued', 'running'):
            raise ReviewError('EXPORT_RUNNING', '任务还在生成，完成后才能删除')
        c.execute("UPDATE flow_exports SET state='deleted',updated_at=? WHERE id=?", (now(), eid))
    _path(eid).unlink(missing_ok=True)
    return {'deleted': True, 'exportId': eid}


def _sources(c, media_ids: list[str]) -> list[dict]:
    """Only videos whose two passes are finished, with everything needed to label their frames."""
    out = []
    for media_id in media_ids:
        media, run = store._run(c, media_id)
        progress = store._progress(c, media_id, run)
        if progress['state'] != 'done':
            raise ReviewError('FLOW_NOT_DONE', f'「{media_id}」还没判完，不能导出')
        recheck = flow_recheck.active(c, media_id, run['run_id'])
        scan = c.execute('SELECT 1 FROM flow_scans WHERE media_id=? AND run_id=?', (media_id, run['run_id'])).fetchone() is not None
        out.append({'mediaId': media_id, 'runId': run['run_id'], 'sample': media['sample'], 'scanned': scan,
                    'picked': store._picked(c, media_id, run['run_id']),
                    'answers': flow_recheck.final_answers(c, media_id, run['run_id']), 'imported': media['imported_json'] is not None,
                    'recheck': flow_recheck.stats(c, recheck) if recheck and recheck['state'] == 'done' else None})
    return out


def _box(x: float, y: float, w: float, h: float, width: int, height: int) -> list[float] | None:
    box = [max(0.0, x - w / 2), max(0.0, y - h / 2), min(float(width), x + w / 2), min(float(height), y + h / 2)]
    return box if box[2] - box[0] >= 2 and box[3] - box[1] >= 2 else None


def frame_labels(record: dict, source: dict, frame: int) -> tuple[list[dict], list[dict]]:
    """(sperm boxes, ignore boxes) of one frame."""
    width, height = record['width'], record['height']
    unit, times = max(width, height) / 4096, store.times_of(record)
    sperm, ignore = [], []

    def mask(reason, x, y, w, h, key=None):
        box = _box(x, y, w, h, width, height)
        if box:
            ignore.append({'targetId': key, 'reason': reason, 'bbox': [round(v, 2) for v in box]})
    for item in record['targets']:
        answer = source['answers'].get(item['id'])
        if answer is None or answer['answer'] == 'no':
            continue
        rows = {row[0]: row for row in item['frames']}
        w, h = (sorted(row[i] for row in item['frames'])[len(rows) // 2] for i in (3, 4))
        if frame in rows:
            _, x, y, bw, bh = rows[frame]
            # A person saw at most nine frames of a long track; a frame next to a clicked-off one is not trusted either.
            if answer['answer'] == 'yes' and not _doubtful(item, answer['dropped'], frame):
                box = _box(x, y, bw, bh, width, height)
                if box:
                    sperm.append({'targetId': item['id'], 'bbox': [round(v, 2) for v in box]})
            else:
                mask('unclear' if answer['answer'] == 'unsure' else 'clicked_off', x, y, bw * 1.3, bh * 1.3, item['id'])
            continue
        # It must be here, but was not found: never let it become background.
        place = store.position_at(item, times, frame)
        if place is None and len(item['frames']) >= 3:
            first, last = item['frames'][0], item['frames'][-1]
            end = first if frame < first[0] else last
            if abs(frame - end[0]) <= OUTSIDE:
                span = times[last[0]] - times[first[0]]
                vx, vy = (last[1] - first[1]) / span, (last[2] - first[2]) / span
                place = end[1] + vx * (times[frame] - times[end[0]]), end[2] + vy * (times[frame] - times[end[0]])
                if not (-w <= place[0] <= width + w and -h <= place[1] <= height + h):
                    place = None
        if place:
            mask('not_found', *place, max(w, 60 * unit) * 2, max(h, 45 * unit) * 2, item['id'])
    for track in record['faint']:
        for row in track:
            if row[0] == frame:
                mask('faint', row[1], row[2], row[3], row[4])
    return sperm, ignore


def _doubtful(item: dict, dropped: list[int], frame: int) -> bool:
    if not dropped:
        return False
    if frame in item['shown']:
        return frame in dropped
    below, above = [f for f in item['shown'] if f < frame], [f for f in item['shown'] if f > frame]
    return bool((below and below[-1] in dropped) or (above and above[0] in dropped))


def _splits(sources: list[dict], ratio: float) -> tuple[dict[str, str], str]:
    """Whole samples stay together; a single sample falls back to whole videos and the manifest says so."""
    groups = {s['mediaId']: s['sample'] or s['mediaId'] for s in sources}
    by_sample = len({g for g in groups.values()}) > 1
    names = groups if by_sample else {m: m for m in groups}
    place = lambda name: int(hashlib.sha256(name.encode()).hexdigest()[:12], 16) / 16 ** 12
    ordered = sorted(set(names.values()), key=place)
    if len(ordered) == 1 or ratio >= 1:
        train = set(ordered)
    else:
        train = {name for name in ordered if place(name) < ratio}
        if not train or len(train) == len(ordered):
            cut = min(len(ordered) - 1, max(1, round(len(ordered) * ratio)))
            train = set(ordered[:cut])
    return {m: 'train' if name in train else 'val' for m, name in names.items()}, 'sample' if by_sample else 'video'


def run(eid: str) -> None:
    from .main import find_video, media_dir
    from .detector_tracks import read_frames
    from .review_repository import compute_file_sha256
    work, partial = DATASET_EXPORT_DIR / '.work' / eid, DATASET_EXPORT_DIR / f'{eid}.zip.partial'
    try:
        with closing(connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM flow_exports WHERE id=?', (eid,)).fetchone()
            if not row or row['state'] != 'queued':
                return
            body = json.loads(row['request_json'])
            sources = _sources(c, body['mediaIds'])
            c.execute("UPDATE flow_exports SET state='running',updated_at=? WHERE id=?", (now(), eid))
        work.mkdir(parents=True, exist_ok=False)
        splits, split_by = _splits(sources, body['trainRatio'])
        plan = []
        for source in sources:
            record = store.record_of(source['mediaId'], source['runId'])
            frames = [f for f in range(0, record['frameCount'], body['step']) if not (source['scanned'] and f == record['scanFrame'])]
            plan.append((source, record, frames))
        total = sum(len(frames) for _, _, frames in plan)
        manifest = {'schemaVersion': 1, 'kind': 'microfluidic', 'exportId': eid, 'createdAt': now(), 'step': body['step'], 'splitBy': split_by,
                    'classNames': ['sperm'], 'counts': {'train': 0, 'val': 0, 'sperm': 0, 'ignore': 0}, 'sources': [], 'samples': []}
        table = io.StringIO()
        writer = csv.writer(table)
        writer.writerow(['mediaId', 'runId', 'sample', 'targetId', 'firstFrame', 'lastFrame', 'detectorScore', 'head', 'pickedOnWall', 'answer', 'droppedFrames', 'actorId', 'ms'])
        done = 0
        for number, (source, record, frames) in enumerate(plan):
            media_id = source['mediaId']
            video = find_video(media_dir(media_id))
            if video is None:
                raise ReviewError('SOURCE_VIDEO_MISSING', f'「{media_id}」的原始视频不存在', 404)
            if compute_file_sha256(video) != record['sha256']:
                raise ReviewError('SOURCE_VIDEO_CHANGED', f'「{media_id}」的原始视频已变化，请重新找目标')
            split, wanted, exported = splits[media_id], set(frames), []
            for index, image in enumerate(read_frames(video, record['frameCount'])):
                if index not in wanted:
                    continue
                if image.shape[1] != record['width'] or image.shape[0] != record['height']:
                    raise ReviewError('SOURCE_DIMENSIONS_CHANGED', f'「{media_id}」的画面尺寸和找目标时不同')
                sperm, ignore = frame_labels(record, source, index)
                stem = f'{eid}__v{number:03d}__frame_{index:06d}'
                for kind, suffix in (('images', 'jpg'), ('labels', 'txt'), ('ignore', 'json')):
                    (work / kind / split).mkdir(parents=True, exist_ok=True)
                ok, data = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
                if not ok:
                    raise ReviewError('IMAGE_WRITE_FAILED', f'第 {index + 1} 帧图片写入失败', 503)
                (work / 'images' / split / f'{stem}.jpg').write_bytes(data.tobytes())
                lines = []
                for item in sperm:
                    x1, y1, x2, y2 = item['bbox']
                    lines.append(f"0 {(x1 + x2) / 2 / record['width']:.10f} {(y1 + y2) / 2 / record['height']:.10f} {(x2 - x1) / record['width']:.10f} {(y2 - y1) / record['height']:.10f}")
                (work / 'labels' / split / f'{stem}.txt').write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
                (work / 'ignore' / split / f'{stem}.json').write_text(packed(ignore), encoding='utf-8')
                manifest['samples'].append({'sampleId': stem, 'mediaId': media_id, 'frameIndex': index, 'split': split, 'image': f'images/{split}/{stem}.jpg',
                                            'label': f'labels/{split}/{stem}.txt', 'objects': sperm, 'ignore': ignore})
                for name, value in ((split, 1), ('sperm', len(sperm)), ('ignore', len(ignore))):
                    manifest['counts'][name] += value
                exported.append(index)
                done += 1
                if done % 10 == 0 or done == total:
                    with closing(connect()) as c, c:
                        c.execute('UPDATE flow_exports SET completed_frames=?,total_frames=?,updated_at=? WHERE id=?', (done, total, now(), eid))
            for item in record['targets']:
                answer = source['answers'].get(item['id'])
                writer.writerow([media_id, source['runId'], source['sample'], item['id'], item['first'], item['last'], item['score'], item['head'], int(item['id'] in source['picked']),
                                 answer['answer'] if answer else 'no', ' '.join(map(str, answer['dropped'])) if answer else '', answer['actor'] if answer else '', answer['ms'] if answer else ''])
            manifest['sources'].append({'mediaId': media_id, 'runId': source['runId'], 'sample': source['sample'], 'sourceSha256': record['sha256'], 'width': record['width'],
                                        'height': record['height'], 'frameCount': record['frameCount'], 'exportedFrames': exported, 'split': split, 'policy': record['policy'],
                                        'targets': len(record['targets']), 'sperm': sum(a['answer'] == 'yes' for a in source['answers'].values()),
                                        'unclear': sum(a['answer'] == 'unsure' for a in source['answers'].values()), 'lostFrames': record['lostFrames'],
                                        'detector': record['detector'], 'unsteady': record['unsteady'],
                                        'imported': source['imported'], 'recheck': source['recheck']})
            if body.get('includeSourceVideos'):
                # The original video and every target found in it: what a later re-check needs to show the same pictures again.
                entry = manifest['sources'][-1]
                entry['videoFile'], entry['targetsFile'] = f'videos/v{number:03d}__{video.name}', f'targets/v{number:03d}.json'
                for name in ('videos', 'targets', 'judgements'):
                    (work / name).mkdir(exist_ok=True)
                try:
                    shutil.copyfile(video, work / entry['videoFile'])
                except OSError as exc:
                    raise ReviewError('VIDEO_COPY_FAILED', '原始视频复制失败', 503) from exc
                if compute_file_sha256(work / entry['videoFile']) != record['sha256']:
                    raise ReviewError('SOURCE_VIDEO_CHANGED', '复制的原始视频校验失败，已停止导出')
                (work / entry['targetsFile']).write_text(packed(record), encoding='utf-8')
                own = {item['id'] for item in record['targets']}
                (work / 'judgements' / f'v{number:03d}.json').write_text(packed({
                    'picked': sorted(source['picked'] & own), 'scanned': source['scanned'],
                    'answers': {key: {'answer': a['answer'], 'dropped': a['dropped']} for key, a in source['answers'].items()}}), encoding='utf-8')
        manifest['misses'] = store.misses(body['mediaIds'])
        (work / 'data.yaml').write_text('# 解压后将 data.yaml 的绝对路径传给 YOLO。\n' + yaml.safe_dump({'train': 'images/train', 'val': 'images/val' if manifest['counts']['val'] else 'images/train', 'nc': 1, 'names': {0: 'sperm'}}, allow_unicode=True, sort_keys=False), encoding='utf-8')
        (work / 'judgements.csv').write_text('﻿' + table.getvalue(), encoding='utf-8')
        (work / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        (work / 'README.md').write_text(README, encoding='utf-8')
        with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(work.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(work), zipfile.ZIP_STORED if path.suffix == '.jpg' or path.parent.name == 'videos' else zipfile.ZIP_DEFLATED)
        summary = {key: manifest[key] for key in ('counts', 'splitBy', 'misses')}
        with closing(connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            fresh = _sources(c, body['mediaIds'])  # Someone may have changed an answer meanwhile.
            if packed([{**s, 'picked': sorted(s['picked'])} for s in fresh]) != packed([{**s, 'picked': sorted(s['picked'])} for s in sources]):
                raise ReviewError('FLOW_CHANGED', '导出过程中判定有改动，请重新导出')
            partial.replace(_path(eid))
            c.execute("UPDATE flow_exports SET state='ready',manifest_json=?,completed_frames=?,total_frames=?,updated_at=? WHERE id=?", (packed(summary), done, total, now(), eid))
        log.info('flow.export_ready export=%s frames=%s sperm=%s ignore=%s', eid, done, manifest['counts']['sperm'], manifest['counts']['ignore'])
    except Exception as exc:
        log.exception('flow.export_failed export=%s', eid)
        with closing(connect()) as c, c:
            c.execute("UPDATE flow_exports SET state='failed',error_message=?,updated_at=? WHERE id=?",
                      (exc.message if isinstance(exc, ReviewError) else '导出失败，请根据导出任务号查看日志后重试', now(), eid))
    finally:
        partial.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


README = """# 微流控训练集

每张图里的目标都有人判过。`labels/` 是 YOLO 格式（0 = 精子），`ignore/` 是每张图要遮盖的框，既不当正样本也不当背景：

- `unclear`：判为“看不清”。
- `clicked_off`：人点掉的、不是这个目标的帧。
- `not_found`：这个精子此刻应该在画面里，但程序没找到，框是按速度推算的。
- `faint`：又淡又小、没有拿给人判的目标。

其余都是背景，包括判为“不是”的目标。整帧查漏用的那一帧不导出。
`manifest.json` 的 `misses` 是实测的漏检：`plantedMissed / planted` 是混进去的已知精子被漏点的比例，`blindUnmatched / blindMarks` 是整帧盲标里程序没有候选的比例。
`sources[].unsteady` 为 true 的视频连轨迹不稳，同一个目标可能出现多次。
勾选“包含原视频”时，`videos/` 是原文件，`targets/` 是程序找到的全部目标，`judgements/` 是完整判定，可在微流控页“标注包”导回；`sources[]` 的 `videoFile`、`targetsFile` 指向它们。
`sources[].recheck` 是盲复查的结果（没复查过为 null）：`firstMissed` 是第一遍漏掉、最后定为精子的个数；复查定稿后标签用定稿的答案。
`judgements.csv` 是每个目标的判定明细。划分按样本号；只有一个样本时按视频划分，`splitBy` 写明是哪种。
"""
