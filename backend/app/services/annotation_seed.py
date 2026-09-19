from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Box:
    track_id: int
    frame: int
    xtl: float
    ytl: float
    xbr: float
    ybr: float
    outside: bool = False
    occluded: bool = False
    label: str = ""
    annotation_id: str = ""
    source: str = ""

    @property
    def width(self) -> float:
        return max(0.0, self.xbr - self.xtl)

    @property
    def height(self) -> float:
        return max(0.0, self.ybr - self.ytl)


class JSONSeedAnnotations:
    """Parse the frontend-exported single-frame seed annotation JSON."""

    def __init__(self, json_path: str | Path):
        self.json_path = Path(json_path)
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(
                f"JSON annotation file not found: {self.json_path}"
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON annotation file {self.json_path}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ValueError("Annotation JSON root must be an object")

        coord = data.get("coordinateSystem") or {}
        if coord.get("bbox", "[x1, y1, x2, y2]") != "[x1, y1, x2, y2]":
            raise ValueError("Only pixel bbox format [x1, y1, x2, y2] is supported")

        frame = data.get("frame") or {}
        self.seed_frame = int(frame.get("frameIndex", 0))
        annotations = data.get("annotations")
        if not isinstance(annotations, list):
            raise ValueError("Annotation JSON must contain an 'annotations' array")

        self._boxes_by_frame: dict[int, list[Box]] = {}

        seen_ids_by_frame: dict[int, set[int]] = {}
        for idx, item in enumerate(annotations):
            if not isinstance(item, dict):
                raise ValueError(f"annotations[{idx}] must be an object")
            frame_index = int(item.get("frameIndex", self.seed_frame))
            bbox = item.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            try:
                object_id_raw = item.get("object_id", item.get("objectId"))
                if object_id_raw is None:
                    raise ValueError("missing object_id")
                object_id = int(object_id_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"annotations[{idx}] must contain a valid positive object_id;"
                    f" never derive identity from annotation array order"
                ) from exc
            if object_id <= 0:
                raise ValueError(f"annotations[{idx}].object_id must be > 0")
            frame_ids = seen_ids_by_frame.setdefault(frame_index, set())
            if object_id in frame_ids:
                raise ValueError(
                    f"duplicate object_id={object_id} on frame {frame_index};"
                    f" seed identities must be unique"
                )
            frame_ids.add(object_id)

            try:
                x1, y1, x2, y2 = [float(v) for v in bbox]
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"annotations[{idx}].bbox contains non-numeric values"
                ) from exc
            if x2 <= x1 or y2 <= y1:
                continue

            self._boxes_by_frame.setdefault(frame_index, []).append(
                Box(
                    track_id=object_id,
                    frame=frame_index,
                    xtl=x1,
                    ytl=y1,
                    xbr=x2,
                    ybr=y2,
                    label=str(item.get("name", "")),
                    annotation_id=str(item.get("id", f"annotation-{object_id}")),
                    source=str(item.get("source", "")),
                )
            )

    def get_boxes_at_frame(self, frame: int) -> list[Box]:
        return list(self._boxes_by_frame.get(int(frame), []))
