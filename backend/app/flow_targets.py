"""Targets in a microfluidic video: every moving thing, linked across its frames, once.

The wet-mount prelabel (detector_tracks.py) expects a dark head that moves at most 100 px per
frame. In a chip a head moves about 350 px per frame, the recorder loses frames, heads tumble,
leave focus and turn white or half transparent. So here

* candidates are places where the picture differs from the static background, dark or bright
  alike (local energy of the difference), not dark spots;
* the number of frames lost in each interval is estimated from how far everything moved and
  checked again with the linked targets: one wrong interval would cut every target there;
* linking uses constant velocity in real time plus appearance, so a head does not jump to the
  debris it passes.

Nothing here decides what is a sperm. People do, on one film strip per target. Geometry inside
this module is in "4K pixels" (long side 4096); results are written in source pixels.
Only NumPy and OpenCV are used. See docs/decisions/0008.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

POLICY = 'flow-targets-v1'
DIRECTORY = 'flow'
TARGETS_FILE = 'targets.json'
SMALL = 1024                 # Long side of the working copy; 4 "4K pixels" per working pixel.
UNIT = 4096 / SMALL
ENERGY_MIN = 5.0             # Candidate threshold, in units of background noise.
SIZE_MIN = 40                # Working pixels that differ clearly; all blind-marked sperm were >= 42.
SIZE_FAINT = 30              # Between these two a target is not shown and becomes "ignore" on export.
SHORT_ENERGY = 8.0           # A one- or two-frame piece must stand out this much to be shown.
PALE_ENERGY = 8.0
MIN_LENGTH = 3
MAX_LOST = 4                 # Largest interval considered, in normal frame times.
LOOKALIKE = math.log(2.6)    # Largest brightness change between frames of one target.
STRIP_FRAMES, STRIP_CELL, STRIP_HALF = 9, 220, 150
BACKGROUND_SAMPLES, FULL_SAMPLES = 90, 24
UNSTEADY = .85               # Share of clear candidates that must end up on a linked target.
SPARSE_PEAKS = 12            # Below this many strong points per frame an interval cannot be measured.
log = logging.getLogger('review.flow')

Progress = Callable[..., None] | None


# ------------------------------------------------------------------ candidates

def small_gray(image: np.ndarray) -> np.ndarray:
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    scale = SMALL / max(h, w)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)


def background(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Static picture and its noise. Channel walls shake: the larger noise stays only on them."""
    sample = frames if len(frames) <= BACKGROUND_SAMPLES else frames[::max(1, len(frames) // BACKGROUND_SAMPLES)]
    still = np.median(sample, 0).astype(np.float32)
    noise = np.median(np.abs(sample - still), 0).astype(np.float32) * 1.4826 + 1.0
    smooth = cv2.GaussianBlur(still, (0, 0), 1.5)
    edge = np.abs(cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)) + np.abs(cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3))
    wall = cv2.dilate((edge > 25).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    noise = np.where(wall, np.maximum(noise, 2.5), np.minimum(noise, np.median(noise) * 1.5)).astype(np.float32)
    return still, cv2.GaussianBlur(noise, (0, 0), 1.0)


def energy(frame: np.ndarray, still: np.ndarray, noise: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    difference = (frame.astype(np.float32) - still) / noise
    difference = cv2.GaussianBlur(difference, (0, 0), .8) - cv2.GaussianBlur(difference, (0, 0), 7.0)
    return difference, np.sqrt(cv2.GaussianBlur(difference * difference, (0, 0), 2.4))


_DISC = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))


def candidates(difference: np.ndarray, power: np.ndarray, threshold: float = ENERGY_MIN) -> list[dict[str, float]]:
    """One entry per thing in this frame: x, y, w, h in 4K pixels, energy, dark share, head brightness, size."""
    highest = cv2.dilate(power, _DISC)
    rows, columns = np.nonzero((power >= highest - 1e-6) & (power > threshold))
    height, width = power.shape
    smooth = cv2.GaussianBlur(difference, (0, 0), 1.3)
    core = np.abs(smooth)
    head = cv2.GaussianBlur(difference, (0, 0), 1.2)
    found = []
    for y, x in zip(rows, columns):
        # Lock on the strongest core (the head), not on head plus tail.
        y0, y1, x0, x1 = max(0, y - 7), min(height, y + 8), max(0, x - 7), min(width, x + 8)
        window = core[y0:y1, x0:x1]
        weight = window ** 4
        gy, gx = np.mgrid[y0:y1, x0:x1]
        total = float(weight.sum()) or 1.0
        cy, cx = float((weight * gy).sum() / total), float((weight * gx).sum() / total)
        near = difference[max(0, y - 5):y + 6, max(0, x - 5):x + 6]
        dark, bright = float(np.clip(-near, 0, None).sum()), float(np.clip(near, 0, None).sum())
        ix, iy = min(max(int(round(cx)), 0), width - 1), min(max(int(round(cy)), 0), height - 1)
        around = difference[max(0, iy - 9):iy + 10, max(0, ix - 9):ix + 10]
        mask = (window > .4 * float(window.max())).astype(np.uint8)
        _, labels = cv2.connectedComponents(mask, connectivity=8)
        part = labels == labels[min(max(iy - y0, 0), mask.shape[0] - 1), min(max(ix - x0, 0), mask.shape[1] - 1)]
        ys, xs = np.nonzero(part & (mask > 0))
        w = float(xs.max() - xs.min() + 1) if len(xs) else 8.0
        h = float(ys.max() - ys.min() + 1) if len(ys) else 8.0
        found.append({
            'x': cx * UNIT, 'y': cy * UNIT, 'w': min(64.0, max(24.0, (w + 2) * UNIT)), 'h': min(64.0, max(24.0, (h + 2) * UNIT)),
            'e': float(power[y, x]), 'dark': dark / (dark + bright + 1e-6), 'head': float(head[iy, ix]),
            'size': int((np.abs(around) > 4).sum()), 'score': 0.0,
        })
    return found


def attach_scores(found: list[dict[str, float]], boxes: list[list[float]], unit: float) -> None:
    """Give a candidate the detector score and box that sits on it (boxes in source pixels)."""
    for x1, y1, x2, y2, score in boxes:
        cx, cy = (x1 + x2) / 2 / unit, (y1 + y2) / 2 / unit
        near = [c for c in found if abs(c['x'] - cx) < 60 and abs(c['y'] - cy) < 60]
        if not near:
            continue  # A box over empty background is not a target.
        best = min(near, key=lambda c: abs(c['x'] - cx) + abs(c['y'] - cy))
        if score > best['score']:
            best.update(score=float(score), w=(x2 - x1) / unit, h=(y2 - y1) / unit)


# ------------------------------------------------------------------ flow

def _blur1(values: np.ndarray, sigma: float) -> np.ndarray:
    return cv2.GaussianBlur(values.reshape(1, -1).astype(np.float32), (0, 0), sigma).reshape(-1).astype(np.float64)


def rotate(points: np.ndarray, angle: float) -> np.ndarray:
    """Coordinates along and across the flow."""
    a = math.radians(angle)
    along, across = np.array([math.cos(a), math.sin(a)]), np.array([-math.sin(a), math.cos(a)])
    return np.c_[points @ along, points @ across]


def _strong(frames: list[list[dict[str, float]]]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    points, power = [], []
    for found in frames:
        keep = [c for c in found if c['e'] > 10 and c['dark'] > .5]
        points.append(np.array([[c['x'], c['y']] for c in keep], float).reshape(-1, 2))
        power.append(np.log(np.array([c['e'] for c in keep], float)) if keep else np.zeros(0))
    return points, power


def flow_angle(frames: list[list[dict[str, float]]]) -> tuple[float, bool]:
    """(direction of travel in degrees, whether things move slowly enough to be matched by place)."""
    points, power = _strong(frames)
    near = total = 0
    steps = []
    for f in range(len(points) - 1):
        a, b = points[f], points[f + 1]
        if not len(a) or not len(b):
            continue
        shift = b[None] - a[:, None]
        alike = np.abs(power[f + 1][None] - power[f][:, None]) < .45
        distance = np.hypot(shift[..., 0], shift[..., 1])
        near += int(((distance < 25) & alike).any(1).sum())
        total += len(a)
        steps.append(shift[alike & (distance > 60) & (distance < 900)])
    if total and near / total > .6:
        return 0.0, True
    moved = np.concatenate(steps) if steps else np.zeros((0, 2))
    if len(moved) < 40:
        return 0.0, False
    angles = np.degrees(np.arctan2(moved[:, 1], moved[:, 0]))
    histogram, edges = np.histogram(angles, bins=np.arange(-180, 180.5, .5))
    ring = np.r_[histogram[-8:], histogram, histogram[:8]].astype(float)
    smooth = _blur1(ring, 2.0)[8:-8]
    # True movement shows one way only; chance pairs are as common the other way round.
    excess = smooth - np.roll(smooth, len(smooth) // 2)
    best = int(np.argmax(excess))
    return float((edges[best] + edges[best + 1]) / 2), False


def interval_lengths(frames: list[list[dict[str, float]]], angle: float) -> np.ndarray:
    """Normal frame times between neighbouring frames: 1, or 2 and more where the recorder lost frames."""
    points, power = _strong(frames)
    n = len(points)
    ones = np.ones(max(0, n - 1), int)
    if n < 3 or sum(len(p) for p in points) / n < SPARSE_PEAKS:
        return ones
    along = [rotate(p, angle) if len(p) else p for p in points]
    edges = np.arange(0, 1610, 10)
    middle = (edges[:-1] + edges[1:]) / 2
    excess = []
    for f in range(n - 1):
        a, b = along[f], along[f + 1]
        if not len(a) or not len(b):
            excess.append(np.zeros(len(middle)))
            continue
        shift = b[None] - a[:, None]
        ok = (np.abs(shift[..., 1]) <= 20) & (np.abs(power[f + 1][None] - power[f][:, None]) < .45)
        moved = shift[..., 0][ok]
        forward = np.histogram(moved[moved > 30], edges)[0]
        backward = np.histogram(-moved[moved < -30], edges)[0]
        excess.append(_blur1((forward - backward).astype(float), 1.5))
    excess = np.array(excess)
    modes = np.array([middle[int(np.argmax(row))] for row in excess])
    usual = float(np.median(modes))
    best: tuple[float, np.ndarray] | None = None
    # The usual step may itself span a lost frame (two intervals in three do in the densest recording),
    # so the normal step is tried as the usual one, half and a third of it.
    for shortest in (float(np.median(modes[modes <= 1.35 * usual])), usual / 2, usual / 3):
        if shortest < 40:
            continue
        k = np.clip(np.round(modes / shortest), 1, MAX_LOST).astype(int)
        fit = 0.0
        for _ in range(12):
            reference = excess[k == 1]
            if len(reference) < max(3, .08 * n):
                fit = -1.0
                break
            shape = _blur1(np.clip(reference.mean(0), 0, None), 1.0)
            shape = shape / (shape.sum() + 1e-9)
            templates = np.array([np.interp(middle / q, middle, shape, left=0, right=0) / q for q in range(1, MAX_LOST + 1)])
            templates = templates / np.sqrt((templates ** 2).sum(1, keepdims=True) + 1e-12)
            match = excess @ templates.T
            updated = match.argmax(1) + 1
            fit = float(match.max(1).sum())
            if (updated == k).all():
                break
            k = updated
        if best is None or fit > best[0]:
            best = (fit, k)
    return best[1] if best else ones


# ------------------------------------------------------------------ linking

class Linked:
    def __init__(self, tracks, positions, power, times):
        self.tracks, self.positions, self.power, self.times = tracks, positions, power, times


def link(frames: list[list[dict[str, float]]], k: np.ndarray, angle: float, slow: bool = False) -> Linked:
    """Tracks as {frame: candidate index}. Constant velocity in real time, similar brightness throughout."""
    lowest, highest, cross = (-60.0, 160.0, 60.0) if slow else (10.0, 1000.0, 46.0)
    x = [rotate(np.array([[c['x'], c['y']] for c in found], float).reshape(-1, 2), angle) if found else np.zeros((0, 2)) for found in frames]
    level = [np.log(np.array([c['e'] for c in found], float)) if found else np.zeros(0) for found in frames]
    n = len(x)
    t = np.r_[0, np.cumsum(k)].astype(float)
    owner = [-np.ones(len(p), int) for p in x]
    tracks: list[dict[int, int]] = []

    def brightness(track):
        return float(np.median([level[f][i] for f, i in track.items()]))

    def extend(track, direction, number):
        while True:
            keys = sorted(track)
            last, before = (keys[-1], keys[-min(4, len(keys))]) if direction > 0 else (keys[0], keys[min(3, len(keys) - 1)])
            v = (x[last][track[last]] - x[before][track[before]]) / (t[last] - t[before])
            own = brightness(track)
            found = False
            for gap in range(1, 4):
                f = last + direction * gap
                if f < 0 or f >= n:
                    break
                if not len(x[f]):
                    continue
                dt = t[f] - t[last]
                loose = 1 + .35 * (abs(dt) - 1)
                reach, side = max(40.0, .18 * abs(v[0])) * loose, cross * loose
                off = x[f] - (x[last][track[last]] + v * dt)
                change = np.abs(level[f] - own)
                free = owner[f] < 0
                fits = np.nonzero((np.abs(off[:, 0]) <= reach) & (np.abs(off[:, 1]) <= side) & free & (change <= LOOKALIKE))[0]
                if len(fits):
                    cost = np.hypot(off[fits, 0] / reach, off[fits, 1] / side) + 1.1 * change[fits] / LOOKALIKE
                    pick = int(fits[np.argmin(cost)])
                elif own > math.log(10.) and len(track) >= 3:
                    # Nothing where expected: a clear target may have sped up; take it only if it is the one look-alike.
                    far = np.nonzero((np.abs(off[:, 0]) <= 2.2 * reach) & (np.abs(off[:, 1]) <= 1.8 * side) & free & (change <= math.log(1.9)))[0]
                    if len(far) != 1:
                        continue
                    pick = int(far[0])
                else:
                    continue
                track[f] = pick
                owner[f][pick] = number
                found = True
                break
            if not found:
                return

    seeds = []
    for f in range(n - 2):
        if not len(x[f]) or not len(x[f + 1]) or not len(x[f + 2]):
            continue
        first = (x[f + 1][None] - x[f][:, None]) / k[f]
        change = np.abs(level[f + 1][None] - level[f][:, None])
        ok = (first[..., 0] >= lowest) & (first[..., 0] <= highest) & (np.abs(first[..., 1]) <= cross + .06 * np.abs(first[..., 0])) & (change <= LOOKALIKE)
        for a, b in zip(*np.nonzero(ok)):
            d1 = first[a, b]
            second = (x[f + 2] - x[f + 1][b]) / k[f + 1]
            off = second - d1
            limit = max(28.0, .13 * abs(d1[0]))
            again = np.abs(level[f + 2] - level[f + 1][b])
            for c in np.nonzero((np.abs(off[:, 0]) <= limit) & (np.abs(off[:, 1]) <= 28) & (again <= LOOKALIKE))[0]:
                cost = abs(off[c, 0]) / limit + abs(off[c, 1]) / 28 + .8 * (change[a, b] + again[c]) / LOOKALIKE + .15 * abs(d1[1]) / cross
                seeds.append((float(cost), f, int(a), int(b), int(c)))
    seeds.sort()
    for _, f, a, b, c in seeds:
        if owner[f][a] >= 0 or owner[f + 1][b] >= 0 or owner[f + 2][c] >= 0:
            continue
        number = len(tracks)
        track = {f: a, f + 1: b, f + 2: c}
        owner[f][a] = owner[f + 1][b] = owner[f + 2][c] = number
        extend(track, 1, number)
        extend(track, -1, number)
        tracks.append(track)
    return Linked(_join(tracks, x, level, t, brightness), x, level, t)


def _join(tracks, x, level, t, brightness):
    """Put pieces of one target back together when the second starts where the first was heading."""
    def ends(track):
        keys = sorted(track)
        v = (x[keys[-1]][track[keys[-1]]] - x[keys[0]][track[keys[0]]]) / (t[keys[-1]] - t[keys[0]])
        return keys[0], keys[-1], v
    order = sorted(range(len(tracks)), key=lambda i: min(tracks[i]))
    used: set[int] = set()
    joined = []
    for i in order:
        if i in used:
            continue
        track = dict(tracks[i])
        used.add(i)
        while True:
            _, last, v = ends(track)
            own = brightness(track)
            best = None
            for j in order:
                if j in used:
                    continue
                start, _, other = ends(tracks[j])
                if not 1 <= start - last <= 5:
                    continue
                dt = t[start] - t[last]
                off = x[start][tracks[j][start]] - (x[last][track[last]] + v * dt)
                if (abs(off[0]) <= max(60.0, .2 * abs(v[0])) * (1 + .4 * (dt - 1)) and abs(off[1]) <= 50 * (1 + .3 * (dt - 1))
                        and abs(other[0] - v[0]) <= max(40.0, .25 * abs(v[0])) and abs(brightness(tracks[j]) - own) <= LOOKALIKE):
                    distance = float(np.hypot(*off))
                    if best is None or distance < best[0]:
                        best = (distance, j)
            if best is None:
                break
            track.update(tracks[best[1]])
            used.add(best[1])
        joined.append(track)
    return joined


def recheck(linked: Linked, k: np.ndarray) -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """For every interval, which length lets the most targets continue? One wrong value cuts them all."""
    x, level, t = linked.positions, linked.power, linked.times
    tracks = [(track, sorted(track)) for track in linked.tracks if len(track) >= 3]
    k = k.copy()
    changed = []
    for f in range(len(x) - 1):
        votes = np.zeros(MAX_LOST + 1)
        voters = 0
        for track, keys in tracks:
            for here, there, sign in ((f, f + 1, 1), (f + 1, f, -1)):
                if here not in track or not len(x[there]):
                    continue
                side = [g for g in keys if (g <= f if sign > 0 else g >= f + 1)]
                side = side[-4:] if sign > 0 else side[:4]
                if len(side) < 2:
                    continue
                v = (x[side[-1]][track[side[-1]]] - x[side[0]][track[side[0]]]) / (t[side[-1]] - t[side[0]])
                if abs(v[0]) < 120:
                    continue
                own = float(np.median([level[g][track[g]] for g in side]))
                voters += 1
                for length in range(1, MAX_LOST + 1):
                    off = x[there] - (x[here][track[here]] + sign * v * length)
                    votes[length] += bool(((np.abs(off[:, 0]) <= max(30.0, .10 * abs(v[0]))) & (np.abs(off[:, 1]) <= 30) & (np.abs(level[there] - own) <= LOOKALIKE)).any())
        if voters < 6:
            continue
        best = int(np.argmax(votes[1:])) + 1
        if best != k[f] and votes[best] >= 1.4 * votes[k[f]] and votes[best] >= 5:
            changed.append((f, int(k[f]), best))
            k[f] = best
    return k, changed


def targets_of(frames: list[list[dict[str, float]]]) -> dict[str, Any]:
    """Link everything; returns tracks to show, short pieces to show, faint ones that are only masked on export."""
    angle, slow = flow_angle(frames)
    k = np.ones(max(0, len(frames) - 1), int) if slow else interval_lengths(frames, angle)
    linked = link(frames, k, angle, slow)
    fixes: list[tuple[int, int, int]] = []
    for _ in range(0 if slow else 3):
        k2, changed = recheck(linked, k)
        if not changed:
            break
        fixes += changed
        k = k2
        linked = link(frames, k, angle, slow)
    x, level, t = linked.positions, linked.power, linked.times
    size = lambda track: max(frames[f][i]['size'] for f, i in track.items())
    score = lambda track: max(frames[f][i]['score'] for f, i in track.items())
    shown = [track for track in linked.tracks if len(track) >= MIN_LENGTH and (size(track) >= SIZE_MIN or score(track) >= .1)]
    faint = [track for track in linked.tracks if len(track) >= MIN_LENGTH and SIZE_FAINT <= size(track) < SIZE_MIN and score(track) < .1]
    used = {(f, i) for track in linked.tracks for f, i in track.items()}
    paths = []
    for track in linked.tracks:
        keys = sorted(track)
        paths.append((keys[0], keys[-1], [t[f] for f in keys], [x[f][track[f]][0] for f in keys], [x[f][track[f]][1] for f in keys]))

    def on_track(f, i):
        px, py = x[f][i]
        for first, last, times, xs, ys in paths:
            if first - 2 <= f <= last + 2:
                span = times[-1] - times[0]
                vx, vy = (xs[-1] - xs[0]) / span, (ys[-1] - ys[0]) / span
                if f < first:
                    ex, ey = xs[0] + vx * (t[f] - times[0]), ys[0] + vy * (t[f] - times[0])
                elif f > last:
                    ex, ey = xs[-1] + vx * (t[f] - times[-1]), ys[-1] + vy * (t[f] - times[-1])
                else:
                    ex, ey = np.interp(t[f], times, xs), np.interp(t[f], times, ys)
                if abs(px - ex) < 90 and abs(py - ey) < 60:
                    return True
        return False

    short: list[dict[int, int]] = []
    for f in range(len(frames)):
        for i, c in enumerate(frames[f]):
            if (f, i) in used or not ((c['e'] >= SHORT_ENERGY and c['size'] >= SIZE_MIN) or c['score'] >= .3) or on_track(f, i):
                continue
            best = None
            for piece in short:
                last = max(piece)
                if not 0 < f - last <= 2:
                    continue
                j = piece[last]
                vx = (x[f][i][0] - x[last][j][0]) / (t[f] - t[last])
                side, change = abs(x[f][i][1] - x[last][j][1]), abs(level[f][i] - level[last][j])
                if 10 < vx < 900 and side < 60 and change < math.log(1.7):
                    cost = change / math.log(1.7) + side / 60
                    if best is None or cost < best[0]:
                        best = (cost, piece)
            if best:
                best[1][f] = i
            else:
                short.append({f: i})
    clear = [(f, i) for f, found in enumerate(frames) for i, c in enumerate(found) if c['size'] >= SIZE_MIN]
    share = sum(place in used for place in clear) / len(clear) if clear else 1.0
    return {'angle': angle, 'slow': slow, 'intervals': k, 'fixes': fixes, 'times': t, 'tracks': shown, 'short': short, 'faint': faint, 'share': share}


def head_kind(values: list[float], level: float) -> str:
    """dark / bright / clear, or pale: half transparent or changing between black and white while tumbling.
    The detector hardly knows pale heads, so a person always gets a second look at them."""
    middle = float(np.median(values))
    if level >= PALE_ENERGY and (abs(middle) <= 3 or (len(values) >= 3 and min(values) < -4 and max(values) > 4)):
        return 'pale'
    return 'bright' if middle > 3 else 'clear' if abs(middle) <= 3 else 'dark'


# ------------------------------------------------------------------ strips and publishing

def _crop(image: np.ndarray, x: float, y: float, half: int, fill: float) -> np.ndarray:
    x, y = int(round(x)), int(round(y))
    h, w = image.shape[:2]
    out = np.full((2 * half, 2 * half), fill, np.float32)
    x0, y0 = x - half, y - half
    xs, ys, xe, ye = max(0, x0), max(0, y0), min(w, x + half), min(h, y + half)
    if xe > xs and ye > ys:
        out[ys - y0:ye - y0, xs - x0:xe - x0] = image[ys:ye, xs:xe]
    return out


def enhanced(crop: np.ndarray, still: np.ndarray) -> np.ndarray:
    return np.clip(128 + 5 * (cv2.GaussianBlur(crop, (0, 0), .8) - still), 0, 255).astype(np.uint8)


def _jpeg(path: Path, image: np.ndarray, quality: int) -> None:
    ok, data = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError('目标小图写入失败')
    path.write_bytes(data.tobytes())


def _median_rows(stack: list[np.ndarray]) -> np.ndarray:
    out = np.empty(stack[0].shape, np.float32)
    for y in range(0, out.shape[0], 200):
        out[y:y + 200] = np.median(np.stack([frame[y:y + 200] for frame in stack]), 0)
    return out


def run_id_of(sha256: str, frame_count: int, items: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps([POLICY, sha256, frame_count, [[item['id'], item['frames']] for item in items]], separators=(',', ':')).encode()).hexdigest()[:16]


def redraw(directory: Path, video: Path, record: dict[str, Any], progress: Progress = None) -> None:
    """Publish targets that are already known, for instance from a package: only the pictures are made anew."""
    from .detector_tracks import read_frames
    n = record['frameCount']
    report = progress or (lambda **_: None)
    sample_at = {round(i * (n - 1) / max(1, min(FULL_SAMPLES, n) - 1)) for i in range(min(FULL_SAMPLES, n))}
    full = [cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame for index, frame in enumerate(read_frames(video, n)) if index in sample_at]
    _publish(directory, video, record, n, max(record['width'], record['height']) / 4096.0, _median_rows(full), report)


def find(directory: Path, video: Path, info: dict[str, Any], sha256: str,
         detect: Callable[[list[np.ndarray]], list[list[list[float]]]] | None = None, progress: Progress = None) -> dict[str, Any]:
    """Find, link and publish the targets of one video. Returns the published record."""
    from .detector_tracks import read_frames
    n, width, height = int(info['frameCount']), int(info['width']), int(info['height'])
    unit = max(width, height) / 4096.0  # Source pixels per 4K pixel.
    report = progress or (lambda **_: None)
    sample_at = {round(i * (n - 1) / max(1, min(FULL_SAMPLES, n) - 1)) for i in range(min(FULL_SAMPLES, n))}
    small: np.ndarray | None = None
    full: list[np.ndarray] = []
    boxes: list[list[list[float]]] = []
    batch: list[np.ndarray] = []
    detector_error = None
    for index, frame in enumerate(read_frames(video, n)):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        tiny = small_gray(gray)
        if small is None:
            small = np.empty((n, *tiny.shape), np.uint8)
        small[index] = tiny
        if index in sample_at:
            full.append(gray)
        if detect is not None and detector_error is None:
            batch.append(frame)
            if len(batch) == 4 or index == n - 1:
                try:
                    boxes += detect(batch)
                except Exception as exc:  # Works without the detector, only with fewer hints; the record says so.
                    detector_error = str(exc)
                    log.exception('flow.detector_unavailable media=%s', directory.name)
                batch = []
        if index % 8 == 7 or index == n - 1:
            report(stage='detecting', message='正在找画面里的目标', processedFrames=index + 1, lastProcessedFrame=index, totalFrames=n)
    if small is None:
        raise RuntimeError('原视频没有可读取的画面')
    if detect is None or detector_error is not None:
        boxes = [[] for _ in range(n)]
    report(stage='linking', message='正在连成目标', processedFrames=n, lastProcessedFrame=n - 1, totalFrames=n)
    still, noise = background(small)
    frames: list[list[dict[str, float]]] = []
    for index in range(n):
        found = candidates(*energy(small[index], still, noise))
        attach_scores(found, boxes[index], unit)
        frames.append(found)
    linked = targets_of(frames)

    items = []
    for number, track in enumerate(linked['tracks'] + linked['short']):
        keys = sorted(track)
        shown = keys if len(keys) <= STRIP_FRAMES else sorted({keys[round(i * (len(keys) - 1) / (STRIP_FRAMES - 1))] for i in range(STRIP_FRAMES)})
        rank = lambda f: (frames[f][track[f]]['score'] if frames[f][track[f]]['score'] >= .1 else 0, frames[f][track[f]]['e'])
        items.append({
            'id': f'T{number + 1:04d}', 'first': keys[0], 'last': keys[-1],
            'frames': [[f, *(round(frames[f][track[f]][key] * unit, 2) for key in ('x', 'y', 'w', 'h'))] for f in keys],
            'shown': shown, 'best': max(range(len(shown)), key=lambda i: rank(shown[i])),
            'score': round(max(frames[f][track[f]]['score'] for f in keys), 4),
            'energy': round(float(np.median([frames[f][track[f]]['e'] for f in keys])), 2),
            'head': head_kind([frames[f][track[f]]['head'] for f in keys], float(np.median([frames[f][track[f]]['e'] for f in keys]))),
        })
    faint = [[[f, *(round(frames[f][i][key] * unit, 2) for key in ('x', 'y', 'w', 'h'))] for f, i in sorted(track.items())] for track in linked['faint']]
    run_id = run_id_of(sha256, n, items)
    pick = int(hashlib.sha256((run_id + 'scan').encode()).hexdigest()[:8], 16)
    scan_frame = int(n * .2) + pick % max(1, int(n * .6)) if n >= 5 else 0
    record = {
        'policy': POLICY, 'runId': run_id, 'sha256': sha256, 'width': width, 'height': height, 'frameCount': n,
        'angle': round(linked['angle'], 2), 'slow': linked['slow'], 'intervals': [int(v) for v in linked['intervals']],
        'lostFrames': int(sum(int(v) - 1 for v in linked['intervals'])), 'intervalFixes': len(linked['fixes']),
        'linkedShare': round(linked['share'], 3), 'unsteady': bool(linked['share'] < UNSTEADY),
        'detector': 'none' if detect is None else 'failed' if detector_error is not None else 'ok', 'detectorError': detector_error,
        'scanFrame': scan_frame, 'cell': STRIP_CELL, 'targets': items, 'faint': faint,
    }
    _publish(directory, video, record, n, unit, _median_rows(full), report)
    log.info('flow.found media=%s run=%s frames=%s targets=%s lost=%s fixes=%s detector=%s', directory.name, run_id, n, len(items),
             record['lostFrames'], record['intervalFixes'], record['detector'])
    return record


def _publish(directory: Path, video: Path, record: dict[str, Any], n: int, unit: float, still: np.ndarray, report) -> None:
    """Second sequential read: cut the film strips, then swap the finished folder in at once."""
    from .detector_tracks import read_frames
    work = directory / f'.{DIRECTORY}-{uuid.uuid4().hex}'
    strips = work / 'strips'
    strips.mkdir(parents=True)
    half = max(8, int(round(STRIP_HALF * unit)))
    base = float(np.median(still[::16, ::16]))
    by_frame: dict[int, list[tuple[int, int, float, float]]] = {}
    for number, item in enumerate(record['targets']):
        place = {row[0]: row for row in item['frames']}
        for slot, f in enumerate(item['shown']):
            by_frame.setdefault(f, []).append((number, slot, place[f][1], place[f][2]))
    cells: dict[int, dict[int, tuple[np.ndarray, np.ndarray]]] = {}
    area = cv2.INTER_AREA if 2 * half >= STRIP_CELL else cv2.INTER_CUBIC
    try:
        for index, frame in enumerate(read_frames(video, n)):
            gray = (cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame).astype(np.float32)
            if index == record['scanFrame']:
                _jpeg(work / 'scan_e.jpg', np.clip(128 + 5 * (cv2.GaussianBlur(gray, (0, 0), .8) - still), 0, 255).astype(np.uint8), 85)
                _jpeg(work / 'scan_r.jpg', gray.astype(np.uint8), 85)
            for number, slot, x, y in by_frame.get(index, ()):
                crop, quiet = _crop(gray, x, y, half, base), _crop(still, x, y, half, base)
                pair = (enhanced(crop, quiet), np.clip(150 + 2.2 * (crop - base), 0, 255).astype(np.uint8))
                cells.setdefault(number, {})[slot] = tuple(cv2.resize(c, (STRIP_CELL, STRIP_CELL), interpolation=area) for c in pair)
                item = record['targets'][number]
                if len(cells[number]) == len(item['shown']):
                    done = cells.pop(number)
                    for view, suffix in ((0, 'e'), (1, 'r')):
                        _jpeg(strips / f"{item['id']}_{suffix}.jpg", np.hstack([done[s][view] for s in range(len(item['shown']))]), 82)
            if index % 8 == 7 or index == n - 1:
                report(stage='drawing', message='正在准备小图', processedFrames=index + 1, lastProcessedFrame=index, totalFrames=n)
        (work / TARGETS_FILE).write_text(json.dumps(record, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        target = directory / DIRECTORY
        old = directory / f'.{DIRECTORY}-old-{uuid.uuid4().hex}'
        if target.exists():
            os.replace(target, old)
        try:
            os.replace(work, target)
        except OSError:
            if old.exists():
                os.replace(old, target)
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def read_record(directory: Path) -> dict[str, Any] | None:
    try:
        record = json.loads((directory / DIRECTORY / TARGETS_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get('policy') == POLICY else None


def clean_leftovers(directory: Path) -> None:
    """A run killed half way leaves only a hidden work folder; an interrupted swap may leave the old one."""
    target = directory / DIRECTORY
    for path in directory.glob(f'.{DIRECTORY}-old-*'):
        if target.exists():
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.replace(path, target)
    for path in directory.glob(f'.{DIRECTORY}-*'):
        shutil.rmtree(path, ignore_errors=True)
