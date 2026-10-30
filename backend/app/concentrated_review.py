"""Immutable review evidence and explicit review-as-final-acceptance adapter.

Legacy C storage is reused for F provenance, not a second human decision.
Every generated decision is labelled review-acceptance, never a fabricated C click.
"""
import json
import logging
from . import review_workflow as w

log = logging.getLogger('review.concentrated')


def migrate(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS concentrated_reviews (
      session_id TEXT PRIMARY KEY REFERENCES review_sessions(id),
      source_hash TEXT NOT NULL, evidence_json TEXT NOT NULL,
      final_version_id TEXT REFERENCES final_versions(id)
    );
    CREATE TABLE IF NOT EXISTS concentrated_reopens (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES review_sessions(id),
      frame_index INTEGER NOT NULL, actor_id INTEGER NOT NULL, created_at TEXT NOT NULL,
      previous_final_id TEXT NOT NULL REFERENCES final_versions(id),
      invalidated_submission_id TEXT
    );
    ''')
    if 'invalidated_submission_id' not in {r[1] for r in c.execute('PRAGMA table_info(concentrated_reopens)')}:
        c.execute('ALTER TABLE concentrated_reopens ADD COLUMN invalidated_submission_id TEXT')


def group_findings(rows):
    groups, active = [], {}
    for row in sorted(rows, key=lambda r: r['source_frame_index']):
        frame = row['source_frame_index']
        evidence = row.get('reviewEvidence') or {}
        for finding in evidence.get('findings', []):
            key = (finding.get('objectId'), finding['kind'])
            if key[0] is None and finding.get('bbox'):
                b=finding['bbox'];key=(*key,round((b[0]+b[2])/40),round((b[1]+b[3])/40))
            group = active.get(key)
            # Group contiguous risks or adjacent detector samples. Samples retain
            # exact checked frames; the interval still needs whole-frame review.
            gap = 5 if group and frame%5==0 and group['endFrame']%5==0 and evidence.get('state')=='checked' else 1
            if group is None or frame > group['endFrame'] + gap:
                group = dict(id=f'doubt-{len(groups)+1}', objectId=key[0], kind=key[1],
                             message=finding['message'], startFrame=frame, endFrame=frame,
                             samples=[])
                groups.append(group); active[key] = group
            group['endFrame'] = frame
            group['samples'].append(dict(frameIndex=frame, **finding))
    return groups


def register(c, sid, rows, source_hash):
    payload = dict(version=1, groups=group_findings(rows),
                   checkedFrames=[r['source_frame_index'] for r in rows if (r.get('reviewEvidence') or {}).get('state') == 'checked'],
                   failedFrames=[r['source_frame_index'] for r in rows if (r.get('reviewEvidence') or {}).get('state') == 'failed'],
                   policy='head-review-v1', backwardCheck='bounded',
                   backwardChecked=[r['source_frame_index'] for r in rows if (r.get('reviewEvidence') or {}).get('backwardCheck')=='checked'],
                   backwardFailed=[r['source_frame_index'] for r in rows if (r.get('reviewEvidence') or {}).get('backwardCheck')=='failed'])
    endpoints = [(r.get('reviewEvidence') or {}).get('endpointCheck') for r in rows]
    endpoint = next((e for e in endpoints if e and 'terminalAnnotations' in e), None) or next((e for e in endpoints if e), None)
    if endpoint:
        payload['endpointVerification'] = endpoint
        payload['backwardCheck'] = 'human-endpoints'
    joint = [(r.get('reviewEvidence') or {}).get('jointCheck') for r in rows]
    if any(joint):
        payload['jointTracking'] = dict(state='checked' if all(e and e.get('state')=='checked' for e in joint) else 'failed',
                                       checkedFrames=sum(bool(e and e.get('state')=='checked') for e in joint))
    c.execute('INSERT INTO concentrated_reviews(session_id,source_hash,evidence_json) VALUES (?,?,?)',
              (sid, source_hash, w.packed(payload)))
    log.info('review.evidence_frozen session=%s source=%s groups=%s checked=%s failed=%s', sid, source_hash, len(payload['groups']), len(payload['checkedFrames']), len(payload['failedFrames']))


def info(c, sid):
    r = c.execute('SELECT * FROM concentrated_reviews WHERE session_id=?', (sid,)).fetchone()
    return dict(r) if r else None


def evidence(c, s):
    r = info(c, s['id'])
    if not r:
        return dict(groups=[], checkedFrames=[], failedFrames=[], policy='legacy', backwardCheck='not_run')
    payload = json.loads(r['evidence_json'])
    submitted = {x[0] for x in c.execute("SELECT frame_index FROM review_frames WHERE session_id=? AND state='submitted'", (s['id'],))}
    for g in payload['groups']:
        g['reviewed'] = all(i in submitted for i in range(g['startFrame'], g['endFrame']+1))
    return dict(payload, sourceHash=r['source_hash'])


def finalize(c, s, cid, uid):
    from . import confirmation_workflow as cf
    c.execute('UPDATE confirmation_sessions SET confirmer_id=? WHERE id=?', (uid, cid))
    cs = cf.session_row(c, cid)
    for row in c.execute('SELECT id FROM review_changes WHERE review_version_id=?', (cs['review_version_id'],)).fetchall():
        cf.append_decision(c, cs, row['id'], 'B', uid, 'review-acceptance:explicit-video-completion')
    cf.finalize(c, cf.session_row(c, cid), uid)
    fv = c.execute('SELECT id FROM final_versions WHERE confirmation_id=? ORDER BY rowid DESC LIMIT 1', (cid,)).fetchone()[0]
    c.execute('UPDATE concentrated_reviews SET final_version_id=? WHERE session_id=?', (fv, s['id']))
    log.info('review.final_accepted session=%s final=%s actor=%s', s['id'], fv, uid)
    return fv


def reopen(c, s, uid, body):
    import uuid
    r = info(c, s['id'])
    if not r or not r['final_version_id'] or s['state'] != 'reviewed':
        raise w.ReviewError('NOT_FINAL_REVIEW', '当前任务不是已完成的集中审查任务')
    if s['reviewer_id'] != uid:
        raise w.ReviewError('NOT_REVIEWER', '只有当前审查员可以重新审查', 403)
    if body['expectedSessionRevision'] != s['revision']:
        raise w.ReviewError('SESSION_REVISION_CONFLICT', '任务已更新，请重新读取')
    fi = body['frameIndex']
    if type(fi) is not int or not 0 <= fi < s['frame_count']:
        raise w.ReviewError('INVALID_FRAME_INDEX', '帧号无效', 422)
    submission=c.execute('SELECT submission_id FROM review_frames WHERE session_id=? AND frame_index=?',(s['id'],fi)).fetchone()[0]
    c.execute('INSERT INTO concentrated_reopens(id,session_id,frame_index,actor_id,created_at,previous_final_id,invalidated_submission_id) VALUES (?,?,?,?,?,?,?)',
              ('rr_'+uuid.uuid4().hex, s['id'], fi, uid, w.now(), r['final_version_id'],submission))
    c.execute("UPDATE review_sessions SET state='in_progress',revision=revision+1 WHERE id=?", (s['id'],))
    c.execute("UPDATE review_frames SET state='unreviewed',frame_revision=frame_revision+1 WHERE session_id=? AND frame_index=?", (s['id'],fi))
    c.execute('UPDATE concentrated_reviews SET final_version_id=NULL WHERE session_id=?', (s['id'],))
    log.info('review.reopened session=%s frame=%s actor=%s previous_final=%s', s['id'],fi,uid,r['final_version_id'])
    return dict(session=w.session_data(c,w.session_row(c,s['id']),uid), frame=w.frame_data(c,w.session_row(c,s['id']),fi,uid))


def read_rows(path):
    if not path.is_file(): return []
    raw = path.read_text(encoding='utf-8')
    if not raw.strip(): return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if isinstance(value, dict): value = value.get('frames', value.get('results', [value]))
    from .annotation_state import normalize_tracking_row
    return [normalize_tracking_row(row) for row in value]
