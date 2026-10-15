"""The offline policy tool must preserve evidence and real failure protection."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts/replay_tracking_pause_cases.py"
POLICY = ROOT / "backend/app/services/anomaly_detector.py"
spec = importlib.util.spec_from_file_location("tracking_pause_replay_tool", TOOL)
replay = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(replay)


def manifest(episode):
    return {"format": "tracking-pause-replay-v1", "frameWidth": 640,
            "frameHeight": 480, "fps": 30, "episodes": [episode]}


def episode():
    return {"id": "stable-recorded-boxes", "objectIds": [1],
            "manualBaselines": {"1": {"bbox": [100, 100, 180, 140], "frameIndex": 0}},
            "seed": {"frameIndex": 0, "boxes": {"1": [100, 100, 180, 140]}},
            "observations": [{"frameIndex": frame,
                              "boxes": {"1": [100 + frame, 100, 180 + frame, 140]}}
                             for frame in range(1, 5)], "expectPause": False,
            "provenance": {"kind": "synthetic-behavior-test"}}


def test_offline_policy_stable_then_actual_missing_still_pauses():
    healthy = episode()
    missing = copy.deepcopy(healthy)
    missing["id"] = "missing-interior-object"
    missing["observations"][-1]["boxes"] = {}
    missing["expectPause"] = True
    data = manifest(healthy)
    data["episodes"].append(missing)
    result = replay.replay_manifest(data, replay.load_policy(POLICY))
    assert result["expectationsPassed"] is True
    assert result["episodes"][0]["pause"] is False
    failed = result["episodes"][1]
    assert failed["frames"][-1]["pauseFrameIndex"] == 4
    assert failed["frames"][-1]["objects"][0]["reasons"] == ["unexpected_disappearance"]
    assert result["modelInference"] is False
    assert result["rawMaskRecomputation"] is False


def test_cli_emits_hashes_and_fails_an_unmet_expectation(tmp_path):
    data = manifest(episode())
    data["episodes"][0]["expectPause"] = True
    source = tmp_path / "input.json"
    output = tmp_path / "result.json"
    source.write_text(json.dumps(data))
    completed = subprocess.run(
        [sys.executable, str(TOOL), str(source), "--output", str(output)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 1
    result = json.loads(output.read_text())
    assert result["expectationsPassed"] is False
    assert len(result["inputSHA256"]) == len(result["policySHA256"]) == 64
    assert result["businessWrites"] is False
    assert json.loads(source.read_text()) == data


def test_cli_refuses_to_overwrite_observations(tmp_path):
    source = tmp_path / "input.json"
    original = json.dumps(manifest(episode()))
    source.write_text(original)
    completed = subprocess.run(
        [sys.executable, str(TOOL), str(source), "--output", str(source)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 2
    assert source.read_text() == original


def test_replay_does_not_invent_native_verification_or_mutate_input():
    data = manifest(episode())
    for row in data["episodes"][0]["observations"]:
        row["identityEvidence"] = {"1": {"version": 2, "source": "native-frame-mask-v2",
                                          "objectId": 1, "frameIndex": row["frameIndex"],
                                          "status": "supported"}}
    original = copy.deepcopy(data)
    result = replay.replay_manifest(data, replay.load_policy(POLICY))
    assert data == original
    assert all("identity_evidence_error" in item["details"]
               for frame in result["episodes"][0]["frames"] for item in frame["objects"])
    assert all("identity_evidence" not in item["details"]
               for frame in result["episodes"][0]["frames"] for item in frame["objects"])


def _native_observation(oid, frame, box, baseline, *, reason="reference_missing", risk=False):
    """Explicit synthetic producer output; never used as field/model evidence."""
    point = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
    return {"version": 2, "source": "native-frame-mask-v2", "sourceSignature": ["fixture", 1, 2, 3],
            "observationVerified": True, "objectId": oid, "frameIndex": frame,
            "bbox": box, "maskArea": 100,
            "geometryReferenceFrameIndex": 0, "geometryReferenceBBox": baseline,
            "status": "risk" if risk else "unknown", "reasons": [reason],
            "head": {"status": "lost" if risk else "unknown", "point": point,
                     "referenceFrameIndex": 0, "referenceBBox": baseline,
                     "referencePoint": [(baseline[0] + baseline[2]) / 2,
                                        (baseline[1] + baseline[3]) / 2],
                     "associationStatus": "reliable", "confidence": 1, "radius": 4},
            "coverage": {"status": "unknown", "metrics": {}}}


@pytest.mark.parametrize("object_count", [30, 60])
def test_many_rotating_objects_continue_but_one_real_head_loss_still_stops(object_count):
    """More targets must not amplify ordinary axis-aligned shape changes."""
    baselines = {}
    for oid in range(1, object_count + 1):
        cx = 100 + ((oid - 1) % 10) * 120
        cy = 100 + ((oid - 1) // 10) * 120
        baselines[str(oid)] = {"bbox": [cx - 40, cy - 10, cx + 40, cy + 10], "frameIndex": 0}
    value = {"id": f"{object_count}-rotating-heads", "objectIds": list(range(1, object_count + 1)),
             "manualBaselines": baselines, "sourceSignature": ["fixture", 1, 2, 3],
             "seed": {"frameIndex": 0, "boxes": {k: v["bbox"] for k, v in baselines.items()}},
             "observations": [], "expectPause": False,
             "provenance": {"kind": "synthetic-policy-negative-and-positive-pair"}}
    for frame in range(1, 9):
        row = {"frameIndex": frame, "boxes": {}, "identityEvidence": {}, "maskAreas": {}}
        for key, baseline in baselines.items():
            old = baseline["bbox"]
            cx, cy = (old[0] + old[2]) / 2, (old[1] + old[3]) / 2
            box = [cx - 10, cy - 40, cx + 10, cy + 40] if frame % 2 else old
            row["boxes"][key] = box
            row["maskAreas"][key] = 100
            row["identityEvidence"][key] = _native_observation(int(key), frame, box, old)
        value["observations"].append(row)
    data = manifest(value)
    data.update(frameWidth=1400, frameHeight=900)
    healthy = replay.replay_manifest(data, replay.load_policy(POLICY))
    assert healthy["expectationsPassed"] is True
    broken = copy.deepcopy(data)
    broken["episodes"][0]["expectPause"] = True
    for row in broken["episodes"][0]["observations"][-3:]:
        row["identityEvidence"]["1"] = _native_observation(
            1, row["frameIndex"], row["boxes"]["1"], baselines["1"]["bbox"],
            reason="mask_head_missing", risk=True,
        )
    failed = replay.replay_manifest(broken, replay.load_policy(POLICY))
    assert failed["expectationsPassed"] is True
    pauses = [row for row in failed["episodes"][0]["frames"] if row["pause"]]
    assert len(pauses) == 1
    assert pauses[0]["pauseFrameIndex"] == 6
    assert [obj["objectId"] for obj in pauses[0]["objects"] if obj["level"] == "anomaly"] == [1]
