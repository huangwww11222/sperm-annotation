"""Merge downloaded YOLO training packages into one dataset split by whole video.

Each input is an exported ZIP or its extracted directory. Every source video goes
to exactly one of train / val / test, so neighbouring frames never appear on both
sides. Automatic assignment uses the same stable content hash as the server
(training_export.video_bucket), so a video keeps its split across merges and
re-exports. Older frame-order packages are re-split the same way.

Example:
  python3 scripts/merge_training_exports.py 训练数据集-第一批 训练数据集-第二批 \
      --output work/merged-dataset --train 0.7 --test 0.15
"""

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

SPLITS = ("train", "val", "test")


def video_bucket(sha256):
    # Must stay identical to backend/app/training_export.py:video_bucket.
    return int(sha256[:12], 16) / float(16**12)


def find_packages(paths, scratch):
    """Yield (extracted package root, package file name) pairs."""
    for raw in paths:
        path = Path(raw)
        if path.is_file() and path.suffix == ".zip":
            target = Path(tempfile.mkdtemp(dir=scratch))
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    # Exported archives only hold relative paths; refuse anything else.
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise SystemExit(f"不安全的压缩包路径：{path} -> {name}")
                z.extractall(target)
            yield target, path.stem
        elif path.is_dir():
            roots = [path] if (path / "manifest.json").is_file() else sorted(
                p.parent for p in path.glob("*/manifest.json")
            )
            zips = [] if roots else sorted(path.glob("*.zip"))
            if not roots and not zips:
                raise SystemExit(f"{path} 中没有找到导出包")
            yield from ((root, root.name) for root in roots)
            yield from find_packages(zips, scratch)
        else:
            raise SystemExit(f"无法读取：{path}")


def load(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    config = yaml.safe_load((root / "data.yaml").read_text(encoding="utf-8")) if (root / "data.yaml").is_file() else None
    if config is None or any(s.get("label") is None for s in manifest["samples"]):
        raise SystemExit(f"{root} 不是 YOLO 导出包（缺少 data.yaml 或标签），请导出 YOLO 或 YOLO + COCO 格式")
    names = config["names"]
    names = names if isinstance(names, dict) else dict(enumerate(names))
    return manifest, {int(k): v for k, v in names.items()}


def collect(packages):
    """One entry per source video; a video exported several times keeps its newest package."""
    videos = {}
    for root, package in packages:
        manifest, names = load(root)
        review = manifest.get("reviewDecisions") or {}
        decisions_by_video = {}
        if review.get("file"):
            path = root / review["file"]
            if review.get("count", 0) and not path.is_file():
                raise SystemExit(f"{root} 的审查判断文件缺失，无法完整合并")
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []
            if "count" in review and review["count"] != len(lines):
                raise SystemExit(f"{root} 的审查判断行数与清单不一致，无法完整合并")
            known = {source["sourceSha256"] for source in manifest["sources"]}
            for line in lines:
                try:
                    sha = json.loads(line)["sourceSha256"]
                    if sha not in known:
                        raise ValueError("判断不属于包内视频")
                except (ValueError, KeyError, TypeError) as exc:
                    raise SystemExit(f"{root} 的审查判断格式或视频来源无效：{exc}") from exc
                # Keep the original JSONL bytes/fields; attach each judgment to
                # its own video so deduplication also chooses its right review.
                decisions_by_video.setdefault(sha, []).append(line)
        for source in manifest["sources"]:
            sha = source["sourceSha256"]
            entry = {
                "sha": sha,
                "root": root,
                "names": names,
                "createdAt": manifest["createdAt"],
                "exportId": manifest["exportId"],
                "decisions": decisions_by_video.get(sha, []),
                # Downloads are named "<video>-…训练数据集-<exportId>"; single-video packages show that name.
                "package": package.split("-已确认训练数据集")[0].split("-训练数据集")[0] if len(manifest["sources"]) == 1 else None,
                "source": source,
                "samples": [s for s in manifest["samples"] if s["finalVersionId"] == source["finalVersionId"]],
            }
            old = videos.get(sha)
            if old and old["createdAt"] >= entry["createdAt"]:
                print(f"跳过重复视频（保留较新的导出 {old['exportId']}）：{entry['exportId']}", file=sys.stderr)
                continue
            if old:
                print(f"跳过重复视频（保留较新的导出 {entry['exportId']}）：{old['exportId']}", file=sys.stderr)
            videos[sha] = entry
    return sorted(videos.values(), key=lambda v: video_bucket(v["sha"]))


def video_label(v):
    return v["source"].get("videoFile") or v["package"] or v["source"]["mediaId"]


def assign(videos, train, test, overrides):
    def manual(v):
        for key in (v["sha"], v["source"]["mediaId"], Path(video_label(v)).name, v["package"]):
            if key in overrides:
                return overrides[key]
        return None

    positive = [any(s["objectCount"] for s in v["samples"]) for v in videos]
    chosen, movable = [], []
    for i, v in enumerate(videos):
        fixed = manual(v)
        u = video_bucket(v["sha"])
        chosen.append(fixed or ("train" if u < train else "test" if u >= 1 - test else "val"))
        if not fixed:
            movable.append(i)
    if len(videos) > 1:
        if not any(p and s == "train" for p, s in zip(positive, chosen)):
            i = next((i for i in movable if positive[i]), None)
            if i is None:
                raise SystemExit("训练集没有含目标的视频，请调整 --assign")
            chosen[i] = "train"
        if sum(p and s == "train" for p, s in zip(positive, chosen)) > 1 and not any(
            p and s == "val" for p, s in zip(positive, chosen)
        ):
            i = next((i for i in reversed(movable) if positive[i] and chosen[i] == "train"), None)
            if i is not None:
                chosen[i] = "val"
    return chosen


def merge(videos, chosen, output, with_videos):
    classes = sorted({name for v in videos for name in v["names"].values()})
    class_ids = {name: i for i, name in enumerate(classes)}
    counts = {s: {"videos": 0, "frames": 0, "boxes": 0} for s in SPLITS}
    sources, review_lines = [], []
    for v, split in zip(videos, chosen):
        remap = {old: class_ids[name] for old, name in v["names"].items()}
        counts[split]["videos"] += 1
        for sample in v["samples"]:
            stem = Path(sample["image"]).stem
            image = output / "images" / split / Path(sample["image"]).name
            label = output / "labels" / split / (stem + ".txt")
            image.parent.mkdir(parents=True, exist_ok=True)
            label.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(v["root"] / sample["image"], image)
            lines = []
            for line in (v["root"] / sample["label"]).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    cid, *box = line.split()
                    lines.append(" ".join([str(remap[int(cid)]), *box]))
            label.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            counts[split]["frames"] += 1
            counts[split]["boxes"] += len(lines)
        video_file = None
        original = v["source"].get("videoFile")
        if with_videos and original and (v["root"] / original).is_file():
            video_file = f"videos/{split}/{Path(original).name}"
            (output / "videos" / split).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(v["root"] / original, output / video_file)
        sources.append({**v["source"], "split": split, "exportId": v["exportId"], "videoFile": video_file})
        review_lines.extend(v.get("decisions") or [])
    config = {"train": "images/train", "val": "images/val"}
    if counts["test"]["frames"]:
        config["test"] = "images/test"
    config.update(nc=len(classes), names=dict(enumerate(classes)))
    (output / "data.yaml").write_text(
        "# 由 merge_training_exports.py 生成；按整段视频划分。\n"
        + yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    review_meta = {"file": "review/decisions.jsonl", "count": len(review_lines)}
    if review_lines:
        (output / review_meta["file"]).parent.mkdir(parents=True, exist_ok=True)
        (output / review_meta["file"]).write_text("\n".join(review_lines) + "\n", encoding="utf-8")
    (output / "manifest.json").write_text(
        json.dumps(
            {"splitStrategy": "video-level (merged)", "classNames": classes, "counts": counts, "sources": sources,
             "reviewDecisions": review_meta},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", help="导出的 ZIP、解压目录，或包含它们的文件夹")
    parser.add_argument("--output", required=True, help="合并后的数据集目录（必须不存在或为空）")
    parser.add_argument("--train", type=float, default=0.7, help="训练集比例，默认 0.7")
    parser.add_argument("--test", type=float, default=0.15, help="测试集比例，默认 0.15；其余为验证集")
    parser.add_argument(
        "--assign", action="append", default=[], metavar="视频=train|val|test",
        help="手动指定某个视频的去向；视频可写文件名、mediaId 或 SHA-256，可重复",
    )
    parser.add_argument("--no-videos", action="store_true", help="不复制包内的原视频")
    args = parser.parse_args(argv)
    if not (0 < args.train < 1 and 0 <= args.test < 1 and args.train + args.test < 1):
        parser.error("需要 0 < train < 1、0 <= test < 1，且 train + test < 1")
    overrides = {}
    for item in args.assign:
        key, _, split = item.rpartition("=")
        if not key or split not in SPLITS:
            parser.error(f"--assign 格式应为 视频=train|val|test：{item}")
        overrides[key] = split
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        parser.error(f"输出目录不为空：{output}")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        videos = collect(list(find_packages(args.inputs, scratch)))
        if not videos:
            raise SystemExit("没有可合并的视频")
        chosen = assign(videos, args.train, args.test, overrides)
        counts = merge(videos, chosen, output, not args.no_videos)
    for split in SPLITS:
        c = counts[split]
        print(f"{split}: {c['videos']} 个视频，{c['frames']} 帧，{c['boxes']} 个框")
    for v, split in zip(videos, chosen):
        print(f"  {split:5s} {video_label(v)}")
    print(f"已写入 {output / 'data.yaml'}")
    return counts


if __name__ == "__main__":
    main()
