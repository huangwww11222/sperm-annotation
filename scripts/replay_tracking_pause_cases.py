#!/usr/bin/env python3
"""Replay frozen tracking observations without loading SAM3 or editing media.

The input is a private JSON manifest, not an annotation upload. Each episode
contains ``manualBaselines``, optional ``geometryReferences`` / ``seed``, and
``observations`` accepted by AnomalyDetector.push. Evidence is passed through
unchanged: this tool never invents masks or upgrades an old image summary.

Use separate manifests for actual recorded observations and controlled policy
experiments. Mark derived/repeated observations explicitly in ``provenance``.
The output records input and policy hashes so before/after use identical data.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from typing import Any


def load_policy(path: Path):
    name = "tracking_pause_replay_policy_" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load policy source")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _int_keys(value: dict[str, Any] | None) -> dict[int, Any]:
    return {int(key): copy.deepcopy(item) for key, item in (value or {}).items()}


def replay_episode(policy, episode: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Run real detector state transitions; optional expectations do not alter it."""
    manual = {
        int(key): policy.ManualBaseline(
            bbox=value["bbox"], frame_index=value["frameIndex"],
            source=value.get("source", "manual"), name=value.get("name"),
            mask_area=value.get("maskArea"),
        ) for key, value in episode.get("manualBaselines", {}).items()
    }
    geometry = {
        int(key): [policy.ConfirmedGeometryReference(
            bbox=value["bbox"], frame_index=value["frameIndex"],
            mask_area=value.get("maskArea"), event_id=value.get("eventId"),
        ) for value in values]
        for key, values in episode.get("geometryReferences", {}).items()
    }
    overlaps = {}
    for value in episode.get("overlapReferences", []):
        pair = tuple(sorted(int(oid) for oid in value["pair"]))
        overlaps[pair] = policy.OverlapReference(
            pair=pair, boxes=_int_keys(value["boxes"]), frame_index=value["frameIndex"],
            event_id=value.get("eventId"), source=value.get("source", "confirmed-normal"),
        )
    config = policy.AnomalyConfig(**episode.get("config", {}))
    detector = policy.AnomalyDetector(
        config=config, frame_width=defaults["frameWidth"], frame_height=defaults["frameHeight"],
        fps=defaults["fps"], all_object_ids=episode["objectIds"], manual_baselines=manual,
        geometry_references=geometry, overlap_references=overlaps,
    )
    if episode.get("sourceSignature") is not None:
        detector.image_source_signature = copy.deepcopy(episode["sourceSignature"])
    seed = episode.get("seed")
    if seed is not None:
        detector.initialize_seed(seed["frameIndex"], _int_keys(seed["boxes"]))
    frames = []
    for row in episode["observations"]:
        report = detector.push(
            row["frameIndex"], _int_keys(row["boxes"]),
            mask_areas=_int_keys(row.get("maskAreas")),
            identity_evidence=_int_keys(row.get("identityEvidence")),
        )
        frames.append({
            "frameIndex": report.frame_index, "pause": report.should_pause,
            "pauseFrameIndex": getattr(report, "pause_frame_index", None),
            "objects": [{"objectId": item.object_id, "level": item.level.value,
                         "reasons": item.reasons, "details": item.details}
                        for item in report.frames],
        })
    paused = any(frame["pause"] for frame in frames)
    expected = episode.get("expectPause")
    return {"id": episode["id"], "provenance": episode.get("provenance", {}),
            "pause": paused, "expectPause": expected,
            "expectationMet": None if expected is None else paused is expected,
            "frames": frames}


def replay_manifest(manifest: dict[str, Any], policy) -> dict[str, Any]:
    if manifest.get("format") != "tracking-pause-replay-v1":
        raise ValueError("Unsupported replay manifest format")
    started = time.perf_counter()
    episodes = [replay_episode(policy, episode, manifest) for episode in manifest["episodes"]]
    checked = [episode for episode in episodes if episode["expectationMet"] is not None]
    return {"format": "tracking-pause-replay-result-v1", "modelInference": False,
            "rawMaskRecomputation": False, "businessWrites": False,
            "provenance": manifest.get("provenance", {}), "episodes": episodes,
            "elapsedSeconds": time.perf_counter() - started,
            "expectationsPassed": all(episode["expectationMet"] for episode in checked),
            "checkedEpisodes": len(checked)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--policy-source", type=Path,
                        default=Path(__file__).resolve().parents[1] / "backend/app/services/anomaly_detector.py")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Resolving paths also prevents accidentally overwriting the input/policy.
    inputs = {args.manifest.resolve(), args.policy_source.resolve()}
    if args.output.resolve() in inputs:
        parser.error("Output must differ from the input and policy source")
    raw = args.manifest.read_bytes()
    result = replay_manifest(json.loads(raw), load_policy(args.policy_source))
    result.update({"inputSHA256": hashlib.sha256(raw).hexdigest(),
                   "policySHA256": hashlib.sha256(args.policy_source.read_bytes()).hexdigest()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"episodes": len(result["episodes"]), "checkedEpisodes": result["checkedEpisodes"],
                      "expectationsPassed": result["expectationsPassed"],
                      "elapsedSeconds": result["elapsedSeconds"]}))
    return 0 if result["expectationsPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
