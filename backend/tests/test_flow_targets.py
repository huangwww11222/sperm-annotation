"""Finding and linking targets in a flow: fast, frame loss, pale heads, look-alike debris."""
import json
import math

import cv2
import numpy as np
import pytest

from app import flow_targets as ft

W, H = 2048, 1500  # Working copy 1024x750; one working pixel = 4 "4K pixels" = 2 source pixels.
rng = np.random.default_rng(7)


@pytest.fixture(autouse=True)
def same_scene_every_time():
    global rng
    rng = np.random.default_rng(7)


def head(image, x, y, kind='dark', size=7, tail=False):
    """A sperm-like thing on the working copy. Pale: half dark, half bright, as a head out of focus."""
    sign = {'dark': -1, 'bright': 1}.get(kind)
    yy, xx = np.mgrid[0:image.shape[0], 0:image.shape[1]]
    blob = np.exp(-(((xx - x) / (size * .55)) ** 2 + ((yy - y) / (size * .42)) ** 2))
    if sign is None:
        image += 60 * np.exp(-(((xx - x) / 3.4) ** 2 + ((yy - y - 2.6) / 2.4) ** 2)) - 60 * np.exp(-(((xx - x) / 3.4) ** 2 + ((yy - y + 2.6) / 2.4) ** 2))
    else:
        image += sign * 80 * blob
    if tail:
        for k in range(8, 40):
            image[int(y + 3 * math.sin(k / 8)), int(min(image.shape[1] - 1, max(0, x - k)))] -= 14


def picture(things, n, lost=(), noise=.6):
    """things: (x0, y, speed per normal frame, kind, size). lost: intervals f->f+1 with one frame missing."""
    base = np.full((750, 1024), 190, np.float32)
    frames, t = [], 0.0
    for f in range(n):
        image = base + rng.normal(0, noise, base.shape).astype(np.float32)
        for x0, y, speed, kind, size in things:
            x = x0 + speed * t
            if -30 < x < 1054:
                head(image, x, y, kind, size)
        frames.append(np.clip(image, 0, 255).astype(np.uint8))
        t += 2 if f in lost else 1
    return np.stack(frames)


def analyse(frames):
    still, noise = ft.background(frames)
    return [ft.candidates(*ft.energy(frame, still, noise)) for frame in frames]


def crowd(count, n, speed=(70, 110), clear=None):
    """Enough moving debris to measure the flow, entering all the time. clear: a row (y) kept free of it."""
    out = []
    for i in range(count):
        v = float(rng.uniform(*speed))
        out.append((float(rng.uniform(-v * n, 1000)), float(rng.uniform(40, 710)), v, 'dark', float(rng.uniform(5, 8))))
    return [thing for thing in out if clear is None or abs(thing[1] - clear) > 30]


def test_fast_targets_are_followed_across_lost_frames():
    lost = (4, 9, 13, 17, 22)
    frames = analyse(picture(crowd(110, 26), 26, lost))
    angle, slow = ft.flow_angle(frames)
    assert abs(angle) < 2 and not slow
    result = ft.targets_of(frames)
    assert [f for f, k in enumerate(result['intervals']) if k == 2] == list(lost)
    assert result['share'] > .9
    # Without knowing the lost frames the same targets fall to pieces.
    naive = ft.link(frames, np.ones(25, int), angle)
    whole = np.median([len(t) for t in result['tracks']])
    assert whole >= 8 and np.median([len(t) for t in naive.tracks]) < whole


def test_a_wrong_interval_is_found_and_corrected_by_the_targets_themselves():
    frames = analyse(picture(crowd(110, 26), 26, (8, 15)))
    angle, _ = ft.flow_angle(frames)
    k = np.ones(25, int)
    k[15] = 2  # Interval 8 is also double, but was missed.
    linked = ft.link(frames, k, angle)
    fixed, changed = ft.recheck(linked, k)
    assert (8, 1, 2) in changed and fixed[8] == 2 and fixed[15] == 2


def test_most_intervals_losing_a_frame_still_gives_the_right_unit():
    lost = tuple(f for f in range(25) if f % 3 != 1)
    result = ft.targets_of(analyse(picture(crowd(150, 44, (45, 75)), 26, lost)))
    assert sum(int(k) - 1 for k in result['intervals']) == len(lost)


@pytest.mark.parametrize('kind,expected', [('dark', 'dark'), ('bright', 'bright'), ('pale', 'pale')])
def test_heads_are_found_whatever_their_brightness(kind, expected):
    things = crowd(90, 20, clear=375) + [(-40.0, 375.0, 90.0, kind, 7)]
    frames = analyse(picture(things, 14))
    result = ft.targets_of(frames)
    near = [t for t in result['tracks'] if all(abs(frames[f][i]['y'] / 4 - 375) < 8 for f, i in t.items())
            and all(abs(frames[f][i]['x'] / 4 - (-40 + 90 * f)) < 14 for f, i in t.items())]
    assert len(near) == 1 and len(near[0]) >= 9
    track = near[0]
    level = float(np.median([frames[f][i]['e'] for f, i in track.items()]))
    assert ft.head_kind([frames[f][i]['head'] for f, i in track.items()], level) == expected


def test_a_head_that_turns_black_and_white_is_pale():
    assert ft.head_kind([-9, 8, -7, 9, 8], 12) == 'pale'
    assert ft.head_kind([-9, -8, -7], 12) == 'dark' and ft.head_kind([9, 8, 7], 12) == 'bright'
    assert ft.head_kind([1, -1, 2], 5) == 'clear'  # Too faint to be worth a second look.


def points(*rows):
    """Hand-made candidates: (x, y, energy) per frame in 4K pixels."""
    return [[{'x': float(x), 'y': float(y), 'w': 40., 'h': 32., 'e': float(e), 'dark': 1., 'head': -e, 'size': 80, 'score': 0.} for x, y, e in row] for row in rows]


def test_a_sperm_does_not_jump_to_the_small_debris_it_passes():
    frames = points(*[[(100 + 300 * f, 500, 30), (700 + 100 * f, 506, 8)] for f in range(8)])
    linked = ft.link(frames, np.ones(7, int), 0.)
    by_energy = sorted((sorted({frames[f][i]['e'] for f, i in t.items()}), len(t)) for t in linked.tracks)
    assert by_energy == [([8.0], 8), ([30.0], 8)]


def test_pieces_of_one_target_are_joined_but_a_different_one_is_not():
    rows = [[(100 + 300 * f, 500, 30)] if f not in (4, 5, 6, 7) else [] for f in range(12)]
    assert [len(t) for t in ft.link(points(*rows), np.ones(11, int), 0.).tracks] == [8]
    other = [[(100 + 300 * f, 500, 30 if f < 4 else 6)] if f not in (4, 5, 6, 7) else [] for f in range(12)]
    assert sorted(len(t) for t in ft.link(points(*other), np.ones(11, int), 0.).tracks) == [4, 4]


def test_slow_flow_is_matched_by_place():
    things = [(float(x), float(y), 1.0, 'dark', 6) for x, y in rng.uniform((60, 60), (960, 690), (60, 2))]
    frames = analyse(np.concatenate([picture([], 30), picture(things, 12)]))[30:]
    angle, slow = ft.flow_angle(frames)
    assert slow
    result = ft.targets_of(frames)
    assert len(result['tracks']) >= 50 and set(result['intervals']) == {1}


def test_detector_boxes_over_empty_background_are_not_targets():
    found = points([(500, 500, 20)])[0]
    ft.attach_scores(found, [[2 * 40, 2 * 40, 2 * 60, 2 * 60, .9]], .5)
    assert found[0]['score'] == 0
    ft.attach_scores(found, [[240, 242, 262, 258, .9]], .5)  # Source pixels of a 2K video: centre (251, 250) = 4K (502, 500).
    assert found[0]['score'] == .9 and found[0]['w'] == pytest.approx(44)


def write_video(path, frames):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 20, (W, H))
    for frame in frames:
        writer.write(cv2.cvtColor(cv2.resize(frame, (W, H), interpolation=cv2.INTER_CUBIC), cv2.COLOR_GRAY2BGR))
    writer.release()


@pytest.fixture(scope='module')
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp('flow') / 'clip.avi'
    write_video(path, picture(crowd(70, 18) + [(-30.0, 300.0, 85.0, 'pale', 7)], 12))
    return path


def test_find_publishes_source_pixels_strips_and_a_stable_run(clip, tmp_path):
    info = {'frameCount': 12, 'width': W, 'height': H}
    seen = []

    def detect(batch):
        seen.append(len(batch))
        return [[[400., 400., 440., 430., .9]] for _ in batch]
    record = ft.find(tmp_path, clip, info, 'a' * 64, detect)
    assert seen == [4, 4, 4] and record['detector'] == 'ok'
    assert ft.read_record(tmp_path) == record and len(record['targets']) > 20
    for item in record['targets']:
        assert all(-40 <= row[1] <= W + 40 and -40 <= row[2] <= H + 40 for row in item['frames'])
        assert set(item['shown']) <= {row[0] for row in item['frames']}
        strip = cv2.imread(str(tmp_path / 'flow' / 'strips' / f"{item['id']}_e.jpg"), 0)
        assert strip.shape == (ft.STRIP_CELL, ft.STRIP_CELL * len(item['shown']))
    assert (tmp_path / 'flow' / 'scan_e.jpg').is_file() and 0 < record['scanFrame'] < 12
    # The same video gives the same targets and the same run, so answers are not lost by finding again.
    again = ft.find(tmp_path, clip, info, 'a' * 64, detect)
    assert again['runId'] == record['runId']
    assert not [p for p in tmp_path.iterdir() if p.name.startswith('.flow')]


def test_find_works_without_the_detector_and_says_so(clip, tmp_path):
    def broken(batch):
        raise RuntimeError('权重缺失')
    record = ft.find(tmp_path, clip, {'frameCount': 12, 'width': W, 'height': H}, 'a' * 64, broken)
    assert record['detector'] == 'failed' and '权重缺失' in record['detectorError'] and record['targets']


def test_a_failed_run_leaves_the_previous_targets_alone(clip, tmp_path, monkeypatch):
    info = {'frameCount': 12, 'width': W, 'height': H}
    first = ft.find(tmp_path, clip, info, 'a' * 64)
    monkeypatch.setattr(ft, '_jpeg', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        ft.find(tmp_path, clip, info, 'b' * 64)
    assert ft.read_record(tmp_path) == first
    with pytest.raises(RuntimeError):
        ft.find(tmp_path, clip, {**info, 'frameCount': 99}, 'a' * 64)  # Fewer frames than promised is an error, not an empty tail.
    assert ft.read_record(tmp_path) == first


def test_leftovers_of_a_killed_run_are_cleaned_and_an_interrupted_swap_is_restored(tmp_path):
    (tmp_path / '.flow-abc' / 'strips').mkdir(parents=True)
    (tmp_path / '.flow-old-1').mkdir()
    (tmp_path / '.flow-old-1' / 'targets.json').write_text(json.dumps({'policy': ft.POLICY, 'runId': 'r'}))
    ft.clean_leftovers(tmp_path)
    assert ft.read_record(tmp_path)['runId'] == 'r'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['flow']
