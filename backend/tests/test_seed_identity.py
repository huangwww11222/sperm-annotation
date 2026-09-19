from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.annotation_seed import JSONSeedAnnotations
from app.tracker import _load_seed


def _write_seed(path: Path, annotations: list[dict]) -> None:
    path.write_text(json.dumps({
        "media": {"id": "synthetic", "name": "synthetic.mp4", "type": "video", "width": 640, "height": 432},
        "frame": {"frameIndex": 17, "timestampMs": 1000},
        "coordinateSystem": {"bbox": "[x1, y1, x2, y2]"},
        "annotations": annotations,
    }), encoding="utf-8")


def test_seed_identity_is_preserved_independent_of_annotation_order(tmp_path: Path):
    seed = tmp_path / "seed.json"
    _write_seed(seed, [
        {"id": "a", "object_id": 15, "name": "rare sperm 15", "frameIndex": 17, "bbox": [220, 320, 258, 347]},
        {"id": "b", "object_id": 8, "name": "rare sperm 8", "frameIndex": 17, "bbox": [250, 280, 280, 300]},
    ])

    parsed = JSONSeedAnnotations(seed).get_boxes_at_frame(17)
    assert [b.track_id for b in parsed] == [15, 8]

    _, _, loaded = _load_seed(str(seed), 640, 432, 100)
    assert [o["object_id"] for o in loaded] == [15, 8]


def test_seed_missing_object_id_is_rejected(tmp_path: Path):
    seed = tmp_path / "seed_missing_id.json"
    _write_seed(seed, [
        {"id": "a", "name": "rare sperm", "frameIndex": 17, "bbox": [220, 320, 258, 347]},
    ])
    with pytest.raises(ValueError, match="object_id"):
        JSONSeedAnnotations(seed)
    with pytest.raises(ValueError, match="object_id"):
        _load_seed(str(seed), 640, 432, 100)


def test_seed_duplicate_object_id_is_rejected(tmp_path: Path):
    seed = tmp_path / "seed_duplicate_id.json"
    _write_seed(seed, [
        {"id": "a", "object_id": 15, "name": "rare sperm 15", "frameIndex": 17, "bbox": [220, 320, 258, 347]},
        {"id": "b", "object_id": 15, "name": "rare sperm 15 duplicate", "frameIndex": 17, "bbox": [300, 300, 330, 330]},
    ])
    with pytest.raises(ValueError, match="duplicate object_id=15"):
        JSONSeedAnnotations(seed)
    with pytest.raises(ValueError, match="duplicate object_id=15"):
        _load_seed(str(seed), 640, 432, 100)
