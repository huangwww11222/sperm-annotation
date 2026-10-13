"""Select the first risky source frame after a short observation window.

Only scalar geometry and JSONL evidence are buffered. Observed future rows
are not published as normal annotations when persistent risk is confirmed.
"""
from copy import deepcopy
import math
from typing import Any

from .services.anomaly_detector import AnomalyFrame, AnomalyLevel, AnomalyReport


def require_confirmed_geometry(workspace: dict[str, Any], seed_frame: int, active_ids: set[int],
                               manual_baselines: dict[int, Any], references: dict[int, Any]) -> None:
    """Never silently continue without the newest applicable human approval.

    Reset/correction and newer real manual work supersede approvals. Legacy
    motion-only events and future confirmations are not geometry references.
    """
    from .annotation_state import is_deleted
    expected: dict[int, list[dict[str, Any]]] = {}
    for event in workspace.get('trackingFeedbackEvents', []):
        if not isinstance(event, dict):
            continue
        oid, frame, decision = event.get('objectId'), event.get('frameIndex'), event.get('decision')
        if type(oid) is not int or oid not in active_ids:
            continue
        if decision == 'reset':
            expected.pop(oid, None)
            continue
        if type(frame) is not int or not 0 <= frame <= seed_frame:
            continue
        if decision == 'corrected':
            expected.pop(oid, None)
            continue
        if is_deleted(workspace, frame, oid) or is_deleted(workspace, seed_frame, oid):
            continue
        baseline = manual_baselines.get(oid)
        if baseline is not None and baseline.frame_index >= frame:
            continue
        if decision != 'normal' or 'geometryReference' not in event:
            continue
        expected.setdefault(oid, []).append(event)
    for oid, history in expected.items():
        # Resolve reset/correction and the bounded history before validation.
        # An obsolete damaged audit entry must not defeat a successful reset.
        candidates = sorted(history, key=lambda event: event['frameIndex'])[-3:]
        values = references.get(oid, [])
        if not isinstance(values, list):
            values = [values]
        for event in candidates:
            reference, frame = event['geometryReference'], event['frameIndex']
            if (not isinstance(reference, dict) or reference.get('source') != 'confirmed-normal'
                    or reference.get('objectId') != oid or reference.get('frameIndex') != frame):
                raise ValueError('无异常确认的尺寸参考损坏，未继续追踪；请重新读取工作区并查看日志')
            box = reference.get('bbox')
            if (not isinstance(box, list) or len(box) != 4
                    or not all(type(value) in (int, float) and math.isfinite(value) for value in box)
                    or box[2] <= box[0] or box[3] <= box[1]):
                raise ValueError('无异常确认的尺寸参考损坏，未继续追踪；请重新读取工作区并查看日志')
            if not any(loaded.frame_index == frame and loaded.bbox == box for loaded in values):
                raise ValueError(f'对象 #{oid} 的无异常确认参考未正确载入，未继续追踪；请重新读取工作区并查看日志')


def select_pause_evidence(report: AnomalyReport, rows: list[dict[str, Any]],
                          decision_frame: int) -> tuple[int, list[AnomalyFrame], list[dict[str, Any]]]:
    pause_frame = getattr(report, 'pause_frame_index', None)
    pause_frame = decision_frame if pause_frame is None else int(pause_frame)
    if pause_frame > decision_frame or pause_frame < 0:
        raise ValueError('Invalid first risk frame; tracking results were not published')
    current_risks = [frame for frame in report.frames
                     if frame.level in (AnomalyLevel.ANOMALY, AnomalyLevel.DISAPPEARED)
                     and int(frame.details.get('pause_started_frame', decision_frame)) == pause_frame]
    if not current_risks:
        raise ValueError('Pause decision has no matching first-frame evidence')
    row = next((row for row in rows if int(row.get('source_frame_index', row.get('frame_index', -1))) == pause_frame), None)
    if row is None:
        raise ValueError('First risk frame is missing; tracking results were not published')
    selected = []
    objects = {int(obj['object_id']): obj for obj in row.get('objects', [])}
    for current in current_risks:
        original = objects.get(current.object_id)
        if pause_frame == decision_frame:
            details, reasons = deepcopy(current.details), list(current.reasons)
        elif original is not None:
            details = deepcopy(original.get('anomaly_details', {}))
            reasons = list(original.get('anomaly_reasons', []))
            if not reasons:
                raise ValueError('First risk frame has no diagnostic evidence')
            details['decision_bbox'] = deepcopy(current.details.get('current_bbox'))
            details['decision_reasons'] = list(current.reasons)
            for key in ('geometry_observation_count', 'geometry_observation_required', 'overlap_streak', 'overlap_persistence_frames'):
                if key in current.details:
                    details[key] = current.details[key]
            hard_geometry = set(details.get('geometry_risk_reasons', []))
            if current.details.get('risk_type') == 'geometry' or hard_geometry:
                reasons = [reason.replace(' SOFT', ' HARD') if reason.replace(' SOFT', ' HARD') in hard_geometry else reason
                           for reason in reasons]
                details['geometry_pending'] = False
            current_pairs = {tuple(pair.get('object_ids', [])): pair for pair in current.details.get('overlap_pairs', [])}
            for pair in details.get('overlap_pairs', []):
                decision = current_pairs.get(tuple(pair.get('object_ids', [])), {})
                if decision.get('level') == 'anomaly' and decision.get('pause_started_frame') == pause_frame:
                    pair.update(level='anomaly', risk_streak=decision.get('risk_streak'),
                                pause_started_frame=pause_frame, decision_frame_index=decision_frame)
                    other = next((oid for oid in pair.get('object_ids', []) if oid != current.object_id), None)
                    reasons = [reason.replace(' SOFT', ' HARD') if reason.startswith(f'bbox_overlap_with={other} ') else reason
                               for reason in reasons]
        else:
            raise ValueError('First risk object is missing; tracking results were not published')
        details.update(decision_frame_index=decision_frame, pause_started_frame=pause_frame)
        frame = AnomalyFrame(pause_frame, current.object_id, current.level, reasons, details)
        selected.append(frame)
        if original is not None:
            original.update(anomaly_level=frame.level.value, anomaly_reasons=frame.reasons, anomaly_details=frame.details)
    row['anomalies'] = [{'object_id': frame.object_id, 'level': frame.level.value,
                         'reasons': frame.reasons, 'details': frame.details} for frame in selected]
    # This also truncates a first risk that began in the previous tracking
    # window. Human annotations and source/seed files are never altered here.
    retained_by_frame = {int(row.get('source_frame_index', row.get('frame_index', -1))): row for row in rows
                         if int(row.get('source_frame_index', row.get('frame_index', -1))) <= pause_frame}
    # A fresh SAM3 seed row has no diagnostic. At a window boundary the same
    # frame can already be the first pending-risk prediction; keep its real
    # evidence, not the replacement seed's empty annotation.
    retained_by_frame[pause_frame] = row
    retained = [retained_by_frame[index] for index in sorted(retained_by_frame)]
    return pause_frame, selected, retained


def summarize_warnings(rows: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
    summary = {}
    for row in rows:
        frame_index = int(row['source_frame_index'])
        for obj in row.get('objects', []):
            if obj.get('anomaly_level') != AnomalyLevel.WARNING.value:
                continue
            oid = int(obj['object_id'])
            groups = {('motion' if reason.startswith('adjacent_center_shift=') else
                       'overlap' if reason.startswith('bbox_overlap_with=') else 'size')
                      for reason in obj.get('anomaly_reasons', [])}
            for reason in groups:
                item = summary.setdefault((oid, reason), {'objectId': oid, 'name': obj.get('name'), 'reason': reason,
                                                         'count': 0, 'firstFrame': frame_index, 'lastFrame': frame_index,
                                                         'calibrated': False})
                item['count'] += 1
                item['lastFrame'] = frame_index
                item['calibrated'] |= reason == 'motion' and bool(obj.get('anomaly_details', {}).get('normal_sample_matched'))
    return summary
