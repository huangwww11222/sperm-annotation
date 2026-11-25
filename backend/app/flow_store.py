"""Microfluidic mode: which videos, what people answered, and what is left to do.

A video judged here never enters the wet-mount workspace, review or export. One published set
of targets is a "run"; answers belong to a run, so finding targets again keeps the old answers
and starts a clean round. Two passes: the photo wall ("looks like a sperm?") and one card per
target that was picked, rated by the detector, or has a pale head the detector does not know.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from threading import Lock

from . import flow_targets
from .db import connect
from .review_workflow import ReviewError, now, packed

log = logging.getLogger('review.flow')
PAGE = 24
SECOND_SCORE = .3
ANSWERS = ('yes', 'no', 'unsure')
PLANTS, PLANT_POOL = 4, 8     # Known sperm mixed into a wall; none until enough are confirmed elsewhere.
SCAN_EVERY = 4                # One video in four starts with a whole-frame check without candidates.
MAX_POINTS = 500


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS flow_media (
      media_id TEXT PRIMARY KEY, sample TEXT NOT NULL DEFAULT '', run_id TEXT, imported_json TEXT,
      added_by INTEGER NOT NULL, added_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS flow_runs (
      media_id TEXT NOT NULL, run_id TEXT NOT NULL, target_count INTEGER NOT NULL,
      order_json TEXT NOT NULL, plants_json TEXT NOT NULL, scan_needed INTEGER NOT NULL,
      second_json TEXT, info_json TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(media_id, run_id)
    );
    CREATE TABLE IF NOT EXISTS flow_pages (
      media_id TEXT NOT NULL, run_id TEXT NOT NULL, page INTEGER NOT NULL, picked_json TEXT NOT NULL,
      actor_id INTEGER NOT NULL, ms INTEGER NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(media_id, run_id, page)
    );
    CREATE TABLE IF NOT EXISTS flow_answers (
      media_id TEXT NOT NULL, run_id TEXT NOT NULL, target_key TEXT NOT NULL,
      answer TEXT NOT NULL CHECK(answer IN ('yes','no','unsure')), dropped_json TEXT NOT NULL,
      actor_id INTEGER NOT NULL, ms INTEGER NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(media_id, run_id, target_key)
    );
    CREATE TABLE IF NOT EXISTS flow_scans (
      media_id TEXT NOT NULL, run_id TEXT NOT NULL, points_json TEXT NOT NULL, blind INTEGER NOT NULL,
      actor_id INTEGER NOT NULL, ms INTEGER NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(media_id, run_id)
    );
    """)


# ------------------------------------------------------------------ which videos

def flow_ids() -> set[str]:
    with closing(connect()) as c:
        return {row['media_id'] for row in c.execute('SELECT media_id FROM flow_media')}


def is_flow(media_id: str) -> bool:
    with closing(connect()) as c:
        return c.execute('SELECT 1 FROM flow_media WHERE media_id=?', (media_id,)).fetchone() is not None


def _has_wet_work(c, media_id: str, directory: Path) -> bool:
    from . import annotation_state
    if c.execute('SELECT 1 FROM annotation_reviews WHERE media_id=? LIMIT 1', (media_id,)).fetchone():
        return True
    return bool(annotation_state.read_state(directory).get('manualAnnotations'))


def add(media_ids: list[str], sample: str, actor: int) -> list[str]:
    """Judge these videos in microfluidic mode. Refused as a whole when one already has wet-mount work."""
    from .main import find_video, media_dir
    sample = sample.strip()
    if len(sample) > 100:
        raise ReviewError('INVALID_SAMPLE', '样本号最多 100 个字', 422)
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        for media_id in media_ids:
            directory = media_dir(media_id)
            if not find_video(directory):
                raise ReviewError('MEDIA_NOT_FOUND', f'素材不存在：{media_id}', 404)
            if c.execute('SELECT 1 FROM flow_media WHERE media_id=?', (media_id,)).fetchone():
                continue
            if _has_wet_work(c, media_id, directory):
                raise ReviewError('FLOW_HAS_WET_WORK', f'「{media_id}」已有人工标注或审查记录，不能改成微流控')
        for media_id in media_ids:
            c.execute('INSERT INTO flow_media(media_id,sample,added_by,added_at) VALUES (?,?,?,?) '
                      "ON CONFLICT(media_id) DO UPDATE SET sample=CASE WHEN excluded.sample<>'' THEN excluded.sample ELSE flow_media.sample END",
                      (media_id, sample, actor, now()))
    log.info('flow.added actor=%s count=%s sample=%s', actor, len(media_ids), bool(sample))
    return media_ids


def _answered(c, media_id: str, run_id: str | None = None) -> bool:
    where, args = ('media_id=?', (media_id,)) if run_id is None else ('media_id=? AND run_id=?', (media_id, run_id))
    return any(c.execute(f'SELECT 1 FROM {table} WHERE {where} LIMIT 1', args).fetchone() for table in ('flow_pages', 'flow_answers', 'flow_scans'))


def remove(media_ids: list[str], actor: int) -> list[str]:
    """Back to the wet-mount library; only while nobody has judged anything."""
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        for media_id in media_ids:
            if _answered(c, media_id):
                raise ReviewError('FLOW_HAS_ANSWERS', f'「{media_id}」已经有判定，不能改回湿片')
        for media_id in media_ids:
            c.execute('DELETE FROM flow_media WHERE media_id=?', (media_id,))
            c.execute('DELETE FROM flow_runs WHERE media_id=?', (media_id,))
    log.info('flow.removed actor=%s count=%s', actor, len(media_ids))
    return media_ids


# ------------------------------------------------------------------ runs

def _pool(c, media_id: str) -> list[tuple[str, str, str]]:
    """Sperm people confirmed in other videos: picked on the wall and confirmed on the card, not a fixed-up track."""
    rows = c.execute("""
      SELECT a.media_id,a.run_id,a.target_key,p.picked_json FROM flow_answers a
      JOIN flow_media m ON m.media_id=a.media_id AND m.run_id=a.run_id
      JOIN flow_pages p ON p.media_id=a.media_id AND p.run_id=a.run_id
      WHERE a.answer='yes' AND a.dropped_json='[]' AND a.media_id<>?""", (media_id,)).fetchall()
    return sorted({(r['media_id'], r['run_id'], r['target_key']) for r in rows if r['target_key'] in json.loads(r['picked_json'])})


def publish_run(media_id: str, record: dict) -> None:
    """Make the targets just found the current round of this video."""
    run_id, keys = record['runId'], [item['id'] for item in record['targets']]
    seed = int(hashlib.sha256((media_id + run_id).encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT 1 FROM flow_runs WHERE media_id=? AND run_id=?', (media_id, run_id)).fetchone()
        if not old:
            plants = {}
            pool = _pool(c, media_id) if keys else []
            if len(pool) >= PLANT_POOL:
                number = len(keys)
                for source in rng.sample(pool, min(PLANTS, max(1, len(keys) // PAGE))):
                    number += 1
                    plants[f'T{number:04d}'] = list(source)
            order = keys + list(plants)
            rng.shuffle(order)
            info = {name: record.get(name) for name in ('lostFrames', 'detector', 'unsteady', 'slow', 'scanFrame', 'frameCount', 'width', 'height')}
            c.execute('INSERT INTO flow_runs(media_id,run_id,target_count,order_json,plants_json,scan_needed,second_json,info_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                      (media_id, run_id, len(keys), packed(order), packed(plants), int(seed % SCAN_EVERY == 0 and record['frameCount'] >= 5),
                       None, packed(info), now()))
            if not order:
                c.execute('UPDATE flow_runs SET second_json=? WHERE media_id=? AND run_id=?', ('[]', media_id, run_id))
        c.execute('UPDATE flow_media SET run_id=? WHERE media_id=?', (run_id, media_id))


def restore_run(media_id: str, record: dict, judged: dict) -> None:
    """A finished round that came with a package: published and judged in one step, or not at all."""
    run_id, keys = record['runId'], [item['id'] for item in record['targets']]
    order = list(keys)
    random.Random(media_id + run_id).shuffle(order)
    picked, answers = set(judged['picked']), judged['answers']
    info = {name: record.get(name) for name in ('lostFrames', 'detector', 'unsteady', 'slow', 'scanFrame', 'frameCount', 'width', 'height')}
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM flow_runs WHERE media_id=? AND run_id=?', (media_id, run_id)).fetchone():
            c.execute('INSERT INTO flow_runs(media_id,run_id,target_count,order_json,plants_json,scan_needed,second_json,info_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                      (media_id, run_id, len(keys), packed(order), '{}', int(bool(judged['scanned'])), packed(second_list(record, picked, {})), packed(info), now()))
            # Actor 0: whoever judged it is not an account of this server.
            for page, tiles in enumerate(pages_of(order)):
                c.execute('INSERT INTO flow_pages VALUES (?,?,?,?,?,?,?,?)', (media_id, run_id, page, packed([k for k in tiles if k in picked]), 0, 0, 1, now()))
            for key, value in answers.items():
                c.execute('INSERT INTO flow_answers VALUES (?,?,?,?,?,?,?,?,?)', (media_id, run_id, key, value['answer'], packed(value['dropped']), 0, 0, 1, now()))
            if judged['scanned']:
                c.execute('INSERT INTO flow_scans VALUES (?,?,?,?,?,?,?)', (media_id, run_id, '[]', 0, 0, 0, now()))
        c.execute('UPDATE flow_media SET run_id=? WHERE media_id=?', (run_id, media_id))


_records: OrderedDict[tuple[str, str], dict] = OrderedDict()
_records_lock = Lock()


def record_of(media_id: str, run_id: str) -> dict:
    """The published targets of the current run; a missing or replaced folder is an error, never an empty video."""
    from .main import media_dir
    run_id = run_id.split('~')[0]  # A re-check looks at the same targets.
    with _records_lock:
        if (media_id, run_id) in _records:
            _records.move_to_end((media_id, run_id))
            return _records[media_id, run_id]
    record = flow_targets.read_record(media_dir(media_id))
    if not record or record['runId'] != run_id:
        raise ReviewError('FLOW_TARGETS_MISSING', '目标文件不存在或已变化，请重新找目标', 409)
    with _records_lock:
        _records[media_id, run_id] = record
        while len(_records) > 4:
            _records.popitem(last=False)
    return record


def _run(c, media_id: str, run_id: str | None = None):
    media = c.execute('SELECT * FROM flow_media WHERE media_id=?', (media_id,)).fetchone()
    if not media:
        raise ReviewError('FLOW_NOT_FOUND', '这个视频不是微流控模式', 404)
    if not media['run_id']:
        raise ReviewError('FLOW_NOT_READY', '还没有找目标', 409)
    if run_id is not None and '~' in run_id:
        from .flow_recheck import active
        row = active(c, media_id, media['run_id'])
        if not row or row['recheck_id'] != run_id:
            raise ReviewError('FLOW_RECHECK_GONE', '这次复查已经作废，请刷新页面')
        return media, c.execute('SELECT * FROM flow_runs WHERE media_id=? AND run_id=?', (media_id, run_id)).fetchone()
    if run_id is not None and run_id != media['run_id']:
        raise ReviewError('FLOW_RUN_CHANGED', '目标已重新找过，请刷新页面', 409)
    return media, c.execute('SELECT * FROM flow_runs WHERE media_id=? AND run_id=?', (media_id, media['run_id'])).fetchone()


def _writable(c, media_id: str, run) -> None:
    """Two rounds that can still change while they are compared would not be two rounds."""
    from .flow_recheck import active
    row = active(c, media_id, run['run_id'].split('~')[0])
    if '~' in run['run_id']:
        if row['state'] != 'open':
            raise ReviewError('FLOW_RECHECK_SHOWN', '已经看过两次的不一致，复查的答案不能再改')
    elif row:
        raise ReviewError('FLOW_RECHECKING', '这段有复查，原来的判定不能改；要改请先作废复查')


def pages_of(order: list[str]) -> list[list[str]]:
    return [order[i:i + PAGE] for i in range(0, len(order), PAGE)]


def _picked(c, media_id: str, run_id: str) -> set[str]:
    return {key for row in c.execute('SELECT picked_json FROM flow_pages WHERE media_id=? AND run_id=?', (media_id, run_id)) for key in json.loads(row['picked_json'])}


def second_list(record: dict, picked: set[str], plants: dict) -> list[str]:
    """Second look: what a person picked, what the detector rates, and every pale head. Planted ones have done their duty."""
    keys = [item['id'] for item in record['targets']
            if item['id'] in picked or item['score'] >= SECOND_SCORE or item['head'] == 'pale']
    keys = [key for key in keys if key not in plants]
    return sorted(keys, key=lambda key: hashlib.sha256((record['runId'] + key).encode()).hexdigest())


def _refresh_second(c, media_id: str, run, record: dict) -> None:
    order, plants = json.loads(run['order_json']), json.loads(run['plants_json'])
    done = c.execute('SELECT COUNT(*) FROM flow_pages WHERE media_id=? AND run_id=?', (media_id, run['run_id'])).fetchone()[0]
    value = packed(second_list(record, _picked(c, media_id, run['run_id']), plants)) if done >= len(pages_of(order)) else None
    c.execute('UPDATE flow_runs SET second_json=? WHERE media_id=? AND run_id=?', (value, media_id, run['run_id']))


def _state(pages: int, pages_done: int, second, answered: int, scan_needed: bool, scan_done: bool) -> str:
    if scan_needed and not scan_done and not pages_done:
        return 'scan'
    if pages_done < pages or second is None:
        return 'wall'
    return 'done' if answered >= len(second) else 'cards'


def _progress(c, media_id: str, run) -> dict:
    run_id, order = run['run_id'], json.loads(run['order_json'])
    second = json.loads(run['second_json']) if run['second_json'] is not None else None
    pages = len(pages_of(order))
    pages_done = c.execute('SELECT COUNT(*) FROM flow_pages WHERE media_id=? AND run_id=?', (media_id, run_id)).fetchone()[0]
    answers = {row['target_key']: row['answer'] for row in c.execute('SELECT target_key,answer FROM flow_answers WHERE media_id=? AND run_id=?', (media_id, run_id))}
    live = {key: answers[key] for key in (second or []) if key in answers}
    scan_done = c.execute('SELECT 1 FROM flow_scans WHERE media_id=? AND run_id=?', (media_id, run_id)).fetchone() is not None
    last = c.execute("""SELECT actor_id,updated_at FROM (
        SELECT actor_id,updated_at FROM flow_pages WHERE media_id=? AND run_id=? UNION ALL
        SELECT actor_id,updated_at FROM flow_answers WHERE media_id=? AND run_id=? UNION ALL
        SELECT actor_id,updated_at FROM flow_scans WHERE media_id=? AND run_id=?) ORDER BY updated_at DESC LIMIT 1""", (media_id, run_id) * 3).fetchone()
    return {
        'runId': run_id, 'targets': run['target_count'], 'pages': pages, 'pagesDone': pages_done,
        'second': len(second) if second is not None else None, 'secondDone': len(live),
        'yes': sum(v == 'yes' for v in live.values()), 'unsure': sum(v == 'unsure' for v in live.values()),
        'scanNeeded': bool(run['scan_needed']), 'scanDone': scan_done, **{k: v for k, v in json.loads(run['info_json']).items() if k in ('lostFrames', 'detector', 'unsteady', 'slow')},
        'lastActorId': last['actor_id'] if last else None, 'lastActivityAt': last['updated_at'] if last else None,
        'state': _state(pages, pages_done, second, len(live), bool(run['scan_needed']), scan_done),
    }


def overview() -> dict[str, dict]:
    """{mediaId: {sample, progress|None, recheck|None}} for the library; reads no target files."""
    from . import flow_recheck
    with closing(connect()) as c:
        out = {}
        for media in c.execute('SELECT * FROM flow_media').fetchall():
            run = c.execute('SELECT * FROM flow_runs WHERE media_id=? AND run_id=?', (media['media_id'], media['run_id'])).fetchone() if media['run_id'] else None
            progress = _progress(c, media['media_id'], run) if run else None
            recheck = flow_recheck.summary(c, media['media_id'], run['run_id']) if run else None
            if recheck and recheck['state'] == 'done':
                final = [a['answer'] for a in flow_recheck.final_answers(c, media['media_id'], run['run_id']).values()]
                progress.update(yes=final.count('yes'), unsure=final.count('unsure'))
            out[media['media_id']] = {'sample': media['sample'], 'addedAt': media['added_at'], 'imported': media['imported_json'] is not None,
                                      'progress': progress, 'recheck': recheck}
        return out


# ------------------------------------------------------------------ judging

def tile_of(item: dict) -> dict:
    return {'key': item['id'], 'n': len(item['shown']), 'best': item['best'], 'frames': item['shown']}


def _tile(record: dict, plants: dict, key: str) -> dict:
    """What the page may know: how many frames and which one to show first. No score, no origin."""
    if key in plants:
        media_id, run_id, target = plants[key]
        item = next((t for t in record_of(media_id, run_id)['targets'] if t['id'] == target), None)
        if item is None:
            raise ReviewError('FLOW_TARGETS_MISSING', '目标文件不存在或已变化，请重新找目标', 409)
    else:
        item = next(t for t in record['targets'] if t['id'] == key)
    return {'key': key, 'n': len(item['shown']), 'best': item['best'], 'frames': item['shown']}


def detail(media_id: str, run_id: str | None = None) -> dict:
    from . import flow_recheck
    with closing(connect()) as c:
        media, run = _run(c, media_id, run_id)
        record = record_of(media_id, run['run_id'])
        plants = json.loads(run['plants_json'])
        by_id = {item['id']: item for item in record['targets']}

        def tile(key):
            item = by_id.get(key)
            return {'key': key, 'n': len(item['shown']), 'best': item['best'], 'frames': item['shown']} if item and key not in plants else _tile(record, plants, key)
        try:
            pages = [[tile(key) for key in page] for page in pages_of(json.loads(run['order_json']))]
        except ReviewError:
            # A video that lent a known sperm was deleted or searched again: drop the planted ones, keep judging.
            pages = [[tile(key) for key in page if key not in plants] for page in pages_of(json.loads(run['order_json']))]
        picks = {str(row['page']): {'picked': json.loads(row['picked_json']), 'revision': row['revision']}
                 for row in c.execute('SELECT * FROM flow_pages WHERE media_id=? AND run_id=?', (media_id, run['run_id']))}
        second = json.loads(run['second_json']) if run['second_json'] is not None else None
        answers = {row['target_key']: {'answer': row['answer'], 'dropped': json.loads(row['dropped_json']), 'revision': row['revision']}
                   for row in c.execute('SELECT * FROM flow_answers WHERE media_id=? AND run_id=?', (media_id, run['run_id']))}
        scan = c.execute('SELECT * FROM flow_scans WHERE media_id=? AND run_id=?', (media_id, run['run_id'])).fetchone()
        return {
            'recheck': flow_recheck.summary(c, media_id, run['run_id']) if run_id is None else None, 'imported': media['imported_json'] is not None,
            'mediaId': media_id, 'sample': media['sample'], 'cell': record['cell'], 'progress': _progress(c, media_id, run),
            'pages': pages, 'picks': picks, 'second': [tile(key) for key in second] if second is not None else None,
            'answers': {key: value for key, value in answers.items() if second is not None and key in second},
            'scan': {'frame': record['scanFrame'], 'width': record['width'], 'height': record['height'],
                     'points': json.loads(scan['points_json']) if scan else [], 'done': scan is not None},
        }


def strip_path(media_id: str, key: str, view: str, run_id: str) -> Path:
    from .main import media_dir
    with closing(connect()) as c:
        _, run = _run(c, media_id, run_id)
    source, name = media_id, key
    plants = json.loads(run['plants_json'])
    if key in plants:
        source, _, name = plants[key]
    path = media_dir(source) / flow_targets.DIRECTORY / 'strips' / f'{name}_{view}.jpg'
    if not path.is_file():
        raise ReviewError('FLOW_STRIP_MISSING', '目标小图不存在，请重新找目标', 404)
    return path


def scan_path(media_id: str, view: str, run_id: str) -> Path:
    from .main import media_dir
    with closing(connect()) as c:
        _run(c, media_id, run_id)
    path = media_dir(media_id) / flow_targets.DIRECTORY / f'scan_{view}.jpg'
    if not path.is_file():
        raise ReviewError('FLOW_STRIP_MISSING', '整帧图片不存在，请重新找目标', 404)
    return path


def _ms(value: int) -> int:
    return max(0, min(int(value), 3_600_000))


def save_page(media_id: str, run_id: str, page: int, picked: list[str], ms: int, revision: int, actor: int) -> dict:
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        _, run = _run(c, media_id, run_id)
        pages = pages_of(json.loads(run['order_json']))
        if not 0 <= page < len(pages) or not set(picked) <= set(pages[page]) or len(picked) != len(set(picked)):
            raise ReviewError('INVALID_FLOW_PAGE', '这一页和服务器上的不一致，请刷新页面', 422)
        _writable(c, media_id, run)
        picked = [key for key in pages[page] if key in set(picked)]
        row = c.execute('SELECT * FROM flow_pages WHERE media_id=? AND run_id=? AND page=?', (media_id, run_id, page)).fetchone()
        if row and json.loads(row['picked_json']) == picked:
            pass  # The same answer again, for instance a retry after a lost reply.
        elif (row['revision'] if row else 0) != revision:
            raise ReviewError('FLOW_CONFLICT', '这一页刚被别人改过，已换成最新的，请再看一眼')
        else:
            c.execute('INSERT INTO flow_pages VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(media_id,run_id,page) DO UPDATE SET '
                      'picked_json=excluded.picked_json,actor_id=excluded.actor_id,ms=flow_pages.ms+excluded.ms,revision=excluded.revision,updated_at=excluded.updated_at',
                      (media_id, run_id, page, packed(picked), actor, _ms(ms), revision + 1, now()))
        _refresh_second(c, media_id, run, record_of(media_id, run_id))
        run = c.execute('SELECT * FROM flow_runs WHERE media_id=? AND run_id=?', (media_id, run_id)).fetchone()
        row = c.execute('SELECT * FROM flow_pages WHERE media_id=? AND run_id=? AND page=?', (media_id, run_id, page)).fetchone()
        result = {'page': page, 'picked': json.loads(row['picked_json']), 'revision': row['revision'], 'progress': _progress(c, media_id, run)}
    log.info('flow.page_saved media=%s run=%s page=%s picked=%s actor=%s', media_id, run_id, page, len(picked), actor)
    return result


def checked_dropped(item: dict, answer: str, dropped: list[int]) -> list[int]:
    dropped = sorted(set(dropped))
    if answer not in ANSWERS or not set(dropped) < set(item['shown']) and dropped:
        raise ReviewError('INVALID_FLOW_ANSWER', '答案无效：至少要留一帧', 422)
    return dropped


def save_answer(media_id: str, run_id: str, key: str, answer: str, dropped: list[int], ms: int, revision: int, actor: int) -> dict:
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        _, run = _run(c, media_id, run_id)
        if run['second_json'] is None or key not in json.loads(run['second_json']):
            raise ReviewError('FLOW_NOT_IN_SECOND', '这个目标不在第二遍里，请刷新页面', 409)
        item = next(t for t in record_of(media_id, run_id)['targets'] if t['id'] == key)
        dropped = checked_dropped(item, answer, dropped)
        _writable(c, media_id, run)
        row = c.execute('SELECT * FROM flow_answers WHERE media_id=? AND run_id=? AND target_key=?', (media_id, run_id, key)).fetchone()
        if row and row['answer'] == answer and json.loads(row['dropped_json']) == dropped:
            pass
        elif (row['revision'] if row else 0) != revision:
            raise ReviewError('FLOW_CONFLICT', '这个目标刚被别人判过，已换成最新的，请再看一眼')
        else:
            c.execute('INSERT INTO flow_answers VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id,run_id,target_key) DO UPDATE SET '
                      'answer=excluded.answer,dropped_json=excluded.dropped_json,actor_id=excluded.actor_id,ms=flow_answers.ms+excluded.ms,'
                      'revision=excluded.revision,updated_at=excluded.updated_at',
                      (media_id, run_id, key, answer, packed(dropped), actor, _ms(ms), revision + 1, now()))
        row = c.execute('SELECT * FROM flow_answers WHERE media_id=? AND run_id=? AND target_key=?', (media_id, run_id, key)).fetchone()
        result = {'key': key, 'answer': row['answer'], 'dropped': json.loads(row['dropped_json']), 'revision': row['revision'], 'progress': _progress(c, media_id, run)}
    log.info('flow.answer_saved media=%s run=%s target=%s answer=%s dropped=%s actor=%s', media_id, run_id, key, answer, len(dropped), actor)
    return result


def save_scan(media_id: str, run_id: str, points: list[list[float]], ms: int, actor: int) -> dict:
    """Heads a person saw on one whole frame. Counts as blind only while the wall of this run is untouched."""
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        _, run = _run(c, media_id, run_id)
        _writable(c, media_id, run)
        info = json.loads(run['info_json'])
        if '~' in run_id:
            raise ReviewError('INVALID_FLOW_SCAN', '复查不做整帧查漏', 422)
        if len(points) > MAX_POINTS or any(len(p) != 2 or not (math.isfinite(p[0]) and math.isfinite(p[1]) and 0 <= p[0] <= info['width'] and 0 <= p[1] <= info['height']) for p in points):
            raise ReviewError('INVALID_FLOW_SCAN', '点的位置无效', 422)
        started = c.execute('SELECT 1 FROM flow_pages WHERE media_id=? AND run_id=? LIMIT 1', (media_id, run_id)).fetchone()
        c.execute('INSERT INTO flow_scans VALUES (?,?,?,?,?,?,?) ON CONFLICT(media_id,run_id) DO UPDATE SET '
                  'points_json=excluded.points_json,blind=MIN(flow_scans.blind,excluded.blind),actor_id=excluded.actor_id,ms=flow_scans.ms+excluded.ms,updated_at=excluded.updated_at',
                  (media_id, run_id, packed([[round(float(x), 1), round(float(y), 1)] for x, y in points]), int(not started), actor, _ms(ms), now()))
        result = {'done': True, 'points': points, 'progress': _progress(c, media_id, run)}
    log.info('flow.scan_saved media=%s run=%s points=%s blind=%s actor=%s', media_id, run_id, len(points), not started, actor)
    return result


# ------------------------------------------------------------------ how much is missed

def position_at(item: dict, times: list[float], frame: int) -> tuple[float, float] | None:
    """Where a target is in this frame: found there, or between two frames where it was found."""
    rows = item['frames']
    if not rows[0][0] <= frame <= rows[-1][0]:
        return None
    for row in rows:
        if row[0] == frame:
            return row[1], row[2]
    before = max((row for row in rows if row[0] < frame), key=lambda row: row[0])
    after = min((row for row in rows if row[0] > frame), key=lambda row: row[0])
    w = (times[frame] - times[before[0]]) / (times[after[0]] - times[before[0]])
    return before[1] + w * (after[1] - before[1]), before[2] + w * (after[2] - before[2])


def times_of(record: dict) -> list[float]:
    out = [0.0]
    for length in record['intervals']:
        out.append(out[-1] + length)
    return out


def misses(media_ids: list[str] | None = None) -> dict:
    """Measured, not assumed: planted sperm people overlooked, and blind marks the program has no target for."""
    from . import flow_recheck
    from .main import find_video, media_dir
    planted = overlooked = marks = unmatched = rescued = yes = 0
    checked = {'sperm': 0, 'bothYes': 0, 'firstMissed': 0, 'secondMissed': 0, 'differences': 0, 'videos': 0}
    with closing(connect()) as c:
        rows = c.execute('SELECT r.*,m.imported_json FROM flow_runs r JOIN flow_media m ON m.media_id=r.media_id AND m.run_id=r.run_id').fetchall()
        for run in rows:
            media_id, run_id = run['media_id'], run['run_id']
            if (media_ids is not None and media_id not in media_ids) or not find_video(media_dir(media_id)):
                continue  # A deleted video is not part of any set any more.
            recheck = flow_recheck.active(c, media_id, run_id)
            if recheck and recheck['state'] == 'done':
                checked['videos'] += 1
                for name, value in flow_recheck.stats(c, recheck).items():
                    checked[name] += value
            if run['imported_json'] is not None:
                continue  # How the first round was picked is not known here in full.
            plants = json.loads(run['plants_json'])
            seen = c.execute('SELECT page,picked_json FROM flow_pages WHERE media_id=? AND run_id=?', (media_id, run_id)).fetchall()
            pages = pages_of(json.loads(run['order_json']))
            picked = {key for row in seen for key in json.loads(row['picked_json'])}
            shown = {key for row in seen if row['page'] < len(pages) for key in pages[row['page']]}
            planted += sum(key in shown for key in plants)
            overlooked += sum(key in shown and key not in picked for key in plants)
            second = set(json.loads(run['second_json']) if run['second_json'] is not None else [])
            for row in c.execute("SELECT target_key FROM flow_answers WHERE media_id=? AND run_id=? AND answer='yes'", (media_id, run_id)):
                if row['target_key'] in second:
                    yes += 1
                    rescued += row['target_key'] not in picked
            scan = c.execute('SELECT points_json FROM flow_scans WHERE media_id=? AND run_id=? AND blind=1', (media_id, run_id)).fetchone()
            points = json.loads(scan['points_json']) if scan else []
            if points:
                try:
                    record = record_of(media_id, run_id)
                except ReviewError:
                    continue
                times, reach = times_of(record), 80 * max(record['width'], record['height']) / 4096
                places = [p for p in (position_at(item, times, record['scanFrame']) for item in record['targets']) if p]
                marks += len(points)
                unmatched += sum(all(math.hypot(x - px, y - py) > reach for px, py in places) for x, y in points)
    return {'planted': planted, 'plantedMissed': overlooked, 'blindMarks': marks, 'blindUnmatched': unmatched, 'yes': yes, 'secondPassOnly': rescued, 'recheck': checked}
