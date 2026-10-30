"""Conservative edge-entry proposals, not a sperm identity classifier.

Only a detector-supported track starting at the boundary can be admitted.
Motion is measured relative to camera translation; ambiguous associations fail
closed. All boxes remain AI annotations, never human-confirmed references.
"""
import copy
import logging
import math
from statistics import median

import cv2
import numpy as np

log = logging.getLogger('review.tracking')


def center(box):
    return ((box[0]+box[2])/2, (box[1]+box[3])/2)


def near(a, b):
    ax, ay = center(a)
    bx, by = center(b)
    return (b[0]-3 <= ax <= b[2]+3 and b[1]-3 <= ay <= b[3]+3) or math.hypot(ax-bx, ay-by) < max(6, min(a[2]-a[0], a[3]-a[1]))


class EntryGate:
    def __init__(self, width, height, fps, state=None):
        self.width, self.height, self.fps = width, height, fps
        self.tracks = copy.deepcopy((state or {}).get('tracks', []))
        self.initialized = bool(state)
        self.band = max(3., min(width, height)*.01)
        self.counts = {'accepted':0, 'initial_or_interior':0, 'association_rejected':0, 'motion_or_existing_rejected':0}

    def snapshot(self):
        return {'version': 1, 'tracks': copy.deepcopy(self.tracks)}

    def observe(self, frame, rows, occupied, shift=(0., 0.), motion_valid=True):
        rows = [list(map(float, r)) for r in rows if len(r) == 5 and all(math.isfinite(float(v)) for v in r)
                and .5 <= float(r[4]) <= 1 and 0 <= r[0] < r[2] <= self.width and 0 <= r[1] < r[3] <= self.height]
        # Suppress tile duplicates without merging separate neighbouring heads.
        clean = []
        for row in sorted(rows, key=lambda r: -r[4]):
            if not any(near(row, old) for old in clean):
                clean.append(row)
        rows = clean
        accepted, updated = [], []
        used = set()
        for track in self.tracks:
            last = track['samples'][-1]
            x, y = center(last['box'])
            radius = max(8., min(last['box'][2]-last['box'][0], last['box'][3]-last['box'][1])*2)
            candidates = [i for i, r in enumerate(rows) if math.hypot(center(r)[0]-x-shift[0], center(r)[1]-y-shift[1]) <= radius]
            if len(candidates) != 1 or candidates[0] in used:
                # No nearest-neighbour guess across a collision or missed frame.
                self.counts['association_rejected'] += 1
                used.update(candidates)
                track['blocked'] = True
                track['samples'] = [last]
                if frame-last['frame'] <= max(1, round(self.fps)):
                    updated.append(track)
                continue
            i = candidates[0]
            competing = sum(math.dist(center(t['samples'][-1]['box']), center(rows[i])) <= radius for t in self.tracks)
            used.add(i)
            row = rows[i]
            rejected = competing > 1 or not motion_valid or last['frame'] != frame-1 or any(near(row, b) for b in occupied)
            if rejected: self.counts['motion_or_existing_rejected'] += 1
            track['blocked'] |= rejected
            track['shift'][0] += shift[0]; track['shift'][1] += shift[1]
            track['samples'].append({'frame': frame, 'box': row[:4], 'score': row[4]})
            samples = track['samples']
            first = samples[0]
            if not track['blocked'] and len(samples) >= 4 and (frame-first['frame'])/self.fps >= .15:
                a, b = center(first['box']), center(row)
                dx, dy = b[0]-a[0]-track['shift'][0], b[1]-a[1]-track['shift'][1]
                inward = (dx, -dx, dy, -dy)[track['side']]
                size = min(first['box'][2]-first['box'][0], first['box'][3]-first['box'][1])
                if inward >= max(3., size*.5) and median(s['score'] for s in samples) >= .7:
                    accepted.append(copy.deepcopy(track))
                    self.counts['accepted'] += 1
                    log.info('tracking.edge_entry_accepted frame=%s first=%s samples=%s inward=%.2f score=%.3f', frame, first['frame'], len(samples), inward, median(s['score'] for s in samples))
                    track['blocked'] = True
            # Keep blocked tracks to avoid rediscovering stationary/old objects.
            if track['blocked'] or len(samples) >= 128 or (frame-first['frame'])/self.fps > 1.5:
                track['blocked'] = True
                track['samples'] = samples[-1:]
            updated.append(track)
        for i, row in enumerate(rows):
            if i in used:
                continue
            distances = [row[0], self.width-row[2], row[1], self.height-row[3]]
            side = min(range(4), key=lambda j: distances[j])
            if not self.initialized or distances[side] > self.band: self.counts['initial_or_interior'] += 1
            # Track interior detections only as exclusions, never entry births.
            updated.append({'samples': [{'frame': frame, 'box': row[:4], 'score': row[4]}], 'side': side,
                            'shift': [0., 0.], 'blocked': not self.initialized or distances[side] > self.band or not motion_valid or any(near(row, b) for b in occupied)})
        self.initialized = True
        # Overload must not silently turn discarded tracks into new candidates.
        if len(updated) > 256:
            raise RuntimeError('边缘候选过多，已停止自动新增，请关闭边缘识别后重试')
        self.tracks = updated
        return accepted


class EdgeReader:
    def __init__(self, video, width, height, fps, state=None):
        from .video_frames import open_capture_at
        self.video = video
        self.cap = open_capture_at(video, 0)
        self.gate = EntryGate(width, height, fps, state)
        self.previous = None
        self.last = None

    def observe(self, frame, occupied, allow_new=True):
        from .head_click import detect_edges
        if self.last != frame-1:
            from .video_frames import open_capture_at
            self.cap.release()
            self.cap = open_capture_at(self.video, frame)
        ok, image = self.cap.read()
        if not ok:
            raise RuntimeError(f'边缘检测读取第 {frame+1} 帧失败')
        gray = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (256, 192)).astype(np.float32)
        shift, valid = (0., 0.), True
        if self.previous is not None:
            delta, response = cv2.phaseCorrelate(self.previous, gray)
            shift = (delta[0]*image.shape[1]/256, delta[1]*image.shape[0]/192)
            valid = math.isfinite(response) and response >= .15 and all(math.isfinite(v) for v in shift)
        self.previous, self.last = gray, frame
        return self.gate.observe(frame, detect_edges(image), occupied, shift, valid and allow_new)

    def prime_frame(self, frame):
        from .video_frames import open_capture_at
        self.cap.release()
        self.cap = open_capture_at(self.video, frame)
        ok, image = self.cap.read()
        if not ok: raise RuntimeError('边缘检测恢复帧读取失败')
        self.previous = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (256,192)).astype(np.float32)
        self.last = frame

    def close(self):
        log.info('tracking.edge_entry_summary frame=%s counts=%s', self.last, self.gate.counts)
        self.cap.release()


class EntryPropagation:
    """Restart the iterator at injection frame to encode new SAM3 prompts.

    Existing session identities/memories remain; duplicate frame is consumed,
    never published a second time. Closing on failure releases the generator.
    """
    def __init__(self, engine, session, count):
        self.engine, self.session, self.count = engine, session, count
        self.stream = iter(engine.propagate_manual(session, max_frames=count, start_frame_idx=0))
        self.pending = None
        self.last = -1

    def add(self, frame, objects):
        self.pending = (frame, objects)

    def __iter__(self): return self

    def __next__(self):
        if self.pending:
            frame, objects = self.pending
            self.close()
            self.engine.add_manual_boxes(self.session, frame, objects)
            self.stream = iter(self.engine.propagate_manual(self.session, max_frames=self.count-frame, start_frame_idx=frame))
            self.pending = None
        while True:
            output = next(self.stream)
            if int(output.frame_idx) > self.last:
                self.last = int(output.frame_idx)
                return output

    def close(self):
        close = getattr(self.stream, 'close', None)
        if close: close()
