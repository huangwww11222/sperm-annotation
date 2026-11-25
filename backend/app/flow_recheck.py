"""Optional blind re-check of a finished microfluidic video.

A re-check is a second run over the same targets: own order, own answers, nothing of the first
round shown. Both rounds are kept. Only after the second is complete are the differences shown
and settled; the settled answers then replace the first round's in exports. Never required, and
discarding one leaves the first round exactly as it was.
"""
from __future__ import annotations

import json
import logging
import random
from contextlib import closing

from . import flow_store as store
from .db import connect
from .review_workflow import ReviewError, now, packed

log = logging.getLogger('review.flow')


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS flow_rechecks (
      media_id TEXT NOT NULL, recheck_id TEXT NOT NULL, run_id TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('open','settling','done','cancelled')),
      started_by INTEGER NOT NULL, started_at TEXT NOT NULL, closed_by INTEGER, closed_at TEXT,
      PRIMARY KEY(media_id, recheck_id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS flow_recheck_active ON flow_rechecks(media_id, run_id) WHERE state<>'cancelled';
    CREATE TABLE IF NOT EXISTS flow_settles (
      media_id TEXT NOT NULL, recheck_id TEXT NOT NULL, target_key TEXT NOT NULL,
      answer TEXT NOT NULL CHECK(answer IN ('yes','no','unsure')), dropped_json TEXT NOT NULL,
      actor_id INTEGER NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(media_id, recheck_id, target_key)
    );
    """)


def active(c, media_id: str, run_id: str):
    """The re-check of this run that has not been discarded, if any."""
    return c.execute("SELECT * FROM flow_rechecks WHERE media_id=? AND run_id=? AND state<>'cancelled'", (media_id, run_id)).fetchone()


def _need(c, media_id: str, recheck_id: str):
    media, run = store._run(c, media_id)
    row = active(c, media_id, run['run_id'])
    if not row or row['recheck_id'] != recheck_id:
        raise ReviewError('FLOW_RECHECK_GONE', '这次复查已经作废，请刷新页面')
    return media, run, row


def _answers(c, media_id: str, run_id: str) -> dict[str, dict]:
    run = c.execute('SELECT second_json FROM flow_runs WHERE media_id=? AND run_id=?', (media_id, run_id)).fetchone()
    second = set(json.loads(run['second_json']) if run and run['second_json'] is not None else [])
    return {row['target_key']: {'answer': row['answer'], 'dropped': json.loads(row['dropped_json']), 'actor': row['actor_id'], 'ms': row['ms']}
            for row in c.execute('SELECT * FROM flow_answers WHERE media_id=? AND run_id=?', (media_id, run_id)) if row['target_key'] in second}


NO = {'answer': 'no', 'dropped': []}  # Not picked and not asked about counts as "not a sperm".


def _differences(c, row) -> dict[str, tuple[dict, dict]]:
    first, second = _answers(c, row['media_id'], row['run_id']), _answers(c, row['media_id'], row['recheck_id'])
    out = {}
    for key in sorted(first.keys() | second.keys()):
        a, b = first.get(key, NO), second.get(key, NO)
        if a['answer'] != b['answer'] or (a['answer'] == 'yes' and a['dropped'] != b['dropped']):
            out[key] = (a, b)
    return out


def _settled(c, row) -> dict[str, dict]:
    return {s['target_key']: {'answer': s['answer'], 'dropped': json.loads(s['dropped_json']), 'actor': s['actor_id'], 'ms': 0, 'revision': s['revision']}
            for s in c.execute('SELECT * FROM flow_settles WHERE media_id=? AND recheck_id=?', (row['media_id'], row['recheck_id']))}


def final_answers(c, media_id: str, run_id: str) -> dict[str, dict]:
    """What counts: the first round, with the settled differences on top once a re-check is finished."""
    out = _answers(c, media_id, run_id)
    row = active(c, media_id, run_id)
    if row and row['state'] == 'done':
        settled = _settled(c, row)
        for key in _differences(c, row):
            out[key] = {k: settled[key][k] for k in ('answer', 'dropped', 'actor', 'ms')}
    return out


def stats(c, row) -> dict:
    first, second, final = _answers(c, row['media_id'], row['run_id']), _answers(c, row['media_id'], row['recheck_id']), final_answers(c, row['media_id'], row['run_id'])
    yes = lambda answers, key: answers.get(key, NO)['answer'] == 'yes'
    sperm = [key for key in final if yes(final, key)]
    return {'sperm': len(sperm), 'bothYes': sum(yes(first, k) and yes(second, k) for k in sperm),
            'firstMissed': sum(not yes(first, k) for k in sperm), 'secondMissed': sum(not yes(second, k) for k in sperm),
            'differences': len(_differences(c, row))}


def _actors(c, media_id: str, run_id: str) -> set[int]:
    return {r['actor_id'] for table in ('flow_pages', 'flow_answers') for r in c.execute(f'SELECT DISTINCT actor_id FROM {table} WHERE media_id=? AND run_id=?', (media_id, run_id))}


def summary(c, media_id: str, run_id: str) -> dict | None:
    row = active(c, media_id, run_id)
    if not row:
        return None
    run = c.execute('SELECT * FROM flow_runs WHERE media_id=? AND run_id=?', (media_id, row['recheck_id'])).fetchone()
    out = {'id': row['recheck_id'], 'state': row['state'], 'startedBy': row['started_by'], 'progress': store._progress(c, media_id, run),
           'samePerson': bool(_actors(c, media_id, run_id) & _actors(c, media_id, row['recheck_id']))}
    if row['state'] == 'done':
        out['stats'] = stats(c, row)
    return out


def start(media_id: str, actor: int) -> dict:
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        _, run = store._run(c, media_id)
        if store._progress(c, media_id, run)['state'] != 'done':
            raise ReviewError('FLOW_NOT_DONE', '还没判完，不能复查')
        row = active(c, media_id, run['run_id'])
        if not row:  # Already started, for instance by a retry or a teammate: the same one.
            number = c.execute('SELECT COUNT(*) FROM flow_rechecks WHERE media_id=? AND run_id=?', (media_id, run['run_id'])).fetchone()[0] + 1
            recheck_id = f"{run['run_id']}~{number}"
            plants = json.loads(run['plants_json'])
            order = [key for key in json.loads(run['order_json']) if key not in plants]
            random.Random(media_id + recheck_id).shuffle(order)
            c.execute('INSERT INTO flow_runs(media_id,run_id,target_count,order_json,plants_json,scan_needed,second_json,info_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                      (media_id, recheck_id, run['target_count'], packed(order), '{}', 0, None if order else '[]', run['info_json'], now()))
            c.execute('INSERT INTO flow_rechecks(media_id,recheck_id,run_id,state,started_by,started_at) VALUES (?,?,?,?,?,?)',
                      (media_id, recheck_id, run['run_id'], 'open', actor, now()))
            log.info('flow.recheck_started media=%s recheck=%s actor=%s', media_id, recheck_id, actor)
        return summary(c, media_id, run['run_id'])


def cancel(media_id: str, recheck_id: str, actor: int) -> dict:
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        _need(c, media_id, recheck_id)
        c.execute("UPDATE flow_rechecks SET state='cancelled',closed_by=?,closed_at=? WHERE media_id=? AND recheck_id=?", (actor, now(), media_id, recheck_id))
    log.info('flow.recheck_cancelled media=%s recheck=%s actor=%s', media_id, recheck_id, actor)
    return {'cancelled': True}


def _finish(c, row, actor: int) -> str:
    state = 'done' if not set(_differences(c, row)) - set(_settled(c, row)) else 'settling'
    c.execute('UPDATE flow_rechecks SET state=?,closed_by=?,closed_at=? WHERE media_id=? AND recheck_id=?',
              (state, actor if state == 'done' else None, now() if state == 'done' else None, row['media_id'], row['recheck_id']))
    return state


def compare(media_id: str, recheck_id: str, actor: int) -> dict:
    """Show the differences. From here on neither round can change: it would no longer be blind."""
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        _, _, row = _need(c, media_id, recheck_id)
        if row['state'] == 'open':
            run = c.execute('SELECT * FROM flow_runs WHERE media_id=? AND run_id=?', (media_id, recheck_id)).fetchone()
            if store._progress(c, media_id, run)['state'] != 'done':
                raise ReviewError('FLOW_NOT_DONE', '复查还没判完')
            _finish(c, row, actor)
            log.info('flow.recheck_compared media=%s recheck=%s actor=%s', media_id, recheck_id, actor)
    return detail(media_id)


def settle(media_id: str, recheck_id: str, key: str, answer: str, dropped: list[int], revision: int, actor: int) -> dict:
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        _, run, row = _need(c, media_id, recheck_id)
        if row['state'] == 'open' or key not in _differences(c, row):
            raise ReviewError('FLOW_NOT_IN_SECOND', '这个目标不在不一致的里面，请刷新页面')
        item = next(t for t in store.record_of(media_id, recheck_id)['targets'] if t['id'] == key)
        dropped = store.checked_dropped(item, answer, dropped)
        old = _settled(c, row).get(key)
        if old and old['answer'] == answer and old['dropped'] == dropped:
            pass
        elif (old['revision'] if old else 0) != revision:
            raise ReviewError('FLOW_CONFLICT', '这个目标刚被别人定过，已换成最新的，请再看一眼')
        else:
            c.execute('INSERT INTO flow_settles VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(media_id,recheck_id,target_key) DO UPDATE SET '
                      'answer=excluded.answer,dropped_json=excluded.dropped_json,actor_id=excluded.actor_id,revision=excluded.revision,updated_at=excluded.updated_at',
                      (media_id, recheck_id, key, answer, packed(dropped), actor, revision + 1, now()))
        _finish(c, row, actor)
        saved = _settled(c, row)[key]
        result = {'key': key, 'answer': saved['answer'], 'dropped': saved['dropped'], 'revision': saved['revision'], 'recheck': summary(c, media_id, run['run_id'])}
    log.info('flow.settled media=%s recheck=%s target=%s answer=%s actor=%s', media_id, recheck_id, key, answer, actor)
    return result


def detail(media_id: str) -> dict:
    """The re-check round alone; the first round's answers appear only among the differences, after comparing."""
    with closing(connect()) as c:
        _, run = store._run(c, media_id)
        row = active(c, media_id, run['run_id'])
        if not row:
            raise ReviewError('FLOW_RECHECK_GONE', '这次复查已经作废，请刷新页面')
        out = store.detail(media_id, row['recheck_id'])
        out['recheck'] = summary(c, media_id, run['run_id'])
        out['differences'] = None
        if row['state'] != 'open':
            settled, by_id = _settled(c, row), {t['id']: t for t in store.record_of(media_id, run['run_id'])['targets']}
            out['differences'] = [{**store.tile_of(by_id[key]), 'first': {k: a[k] for k in ('answer', 'dropped')}, 'second': {k: b[k] for k in ('answer', 'dropped')},
                                   'settled': {k: settled[key][k] for k in ('answer', 'dropped', 'revision')} if key in settled else None}
                                  for key, (a, b) in _differences(c, row).items()]
        return out
