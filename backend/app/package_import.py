"""Import a training-dataset ZIP exported with its original videos back into the workbench.

The package's provenance/<finalVersionId>.json holds every frame's final boxes (pixel
xyxy, stable objectId, class). Each video becomes a new medium whose boxes are manual
workspace annotations, so it can be checked and corrected like any other video. The
sender's review pass is never imported: the video starts unreviewed and must pass review
here before it can be exported again. Frames the package declared empty stay unknown
until a reviewer confirms them, because "unknown" must never silently become "empty".
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import annotation_state
from .config import MAX_VIDEO_BYTES
from .review_source_lock import source_write

log = logging.getLogger('review.media')
MANIFEST_LIMIT = 256 * 1024 * 1024
PROVENANCE_LIMIT = 512 * 1024 * 1024
SAFE_ID = re.compile(r'^[A-Za-z0-9_.-]{1,128}$')
VIDEO_PATH = re.compile(r'^videos/v\d{3}__([^/\\]+)$')


class Skip(Exception):
    """This source cannot be imported; others in the package still can."""


def _read_json(archive: zipfile.ZipFile, name: str, limit: int) -> Any:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise Skip(f'包内缺少 {name}') from exc
    if info.file_size > limit:
        raise Skip(f'{name} 过大')
    try:
        return json.loads(archive.read(info).decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Skip(f'{name} 不是有效 JSON') from exc


def _extract_video(archive: zipfile.ZipFile, name: str, target: Path) -> str:
    info = archive.getinfo(name)
    if info.file_size <= 0 or info.file_size > MAX_VIDEO_BYTES:
        raise Skip('原视频为空或超过大小限制')
    digest, total = hashlib.sha256(), 0
    with archive.open(info) as source, target.open('wb') as out:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            total += len(chunk)
            if total > MAX_VIDEO_BYTES:  # Never trust the declared size alone.
                raise Skip('原视频超过大小限制')
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def manual_objects(frames: Any, info: dict[str, Any], class_name: str | None = None) -> tuple[list[dict], list[dict], int]:
    """Provenance frames -> workspace manualAnnotations/manualBaselines (0-100 percent)."""
    width, height, count = info['width'], info['height'], info['frameCount']
    if not isinstance(frames, list):
        raise Skip('标注帧格式无效')
    annotations, latest, seen_frames = [], {}, set()
    for frame in frames:
        fi = frame.get('frameIndex') if isinstance(frame, dict) else None
        if type(fi) is not int or not 0 <= fi < count or fi in seen_frames:
            raise Skip('标注帧号无效、重复或超过视频长度')
        seen_frames.add(fi)
        objects = frame.get('objects')
        if not isinstance(objects, list):
            raise Skip(f'第 {fi + 1} 帧对象列表无效')
        ids = set()
        for obj in objects:
            oid = obj.get('objectId') if isinstance(obj, dict) else None
            box = obj.get('bbox') if isinstance(obj, dict) else None
            if type(oid) is not int or oid <= 0 or oid in ids:
                raise Skip(f'第 {fi + 1} 帧对象编号无效或重复')
            ids.add(oid)
            if (not isinstance(box, list) or len(box) != 4
                    or not all(type(v) in (int, float) and math.isfinite(v) for v in box)
                    or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                raise Skip(f'第 {fi + 1} 帧对象 {oid} 的框无效或越界')
            name = str(obj.get('classKey') or class_name or 'rare sperm').strip() or 'rare sperm'
            x1, y1, x2, y2 = (float(v) for v in box)
            annotations.append({
                'id': f'manual-import-{fi}-{oid}', 'objectId': oid, 'name': f'{name} {oid}',
                'source': 'manual', 'frameIndex': fi,
                'bbox': {'x': x1 / width * 100, 'y': y1 / height * 100,
                         'width': (x2 - x1) / width * 100, 'height': (y2 - y1) / height * 100},
            })
            if oid not in latest or fi >= latest[oid]['frameIndex']:
                latest[oid] = {'objectId': oid, 'name': f'{name} {oid}', 'source': 'manual',
                               'frameIndex': fi, 'bbox': [x1, y1, x2, y2]}
    if len(seen_frames) != count:
        raise Skip('标注帧数与视频帧数不一致')
    return annotations, list(latest.values()), sum(1 for f in frames if not f['objects'])


@source_write
def _write_imported_workspace(media_id: str, directory: Path, uid: int, annotations: list, baselines: list,
                              provenance: dict[str, Any]) -> None:
    meta_path = directory / 'media.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    meta['importedFrom'] = provenance
    temporary = meta_path.with_name(f'.media.json.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(meta_path)
    body = {'expectedRevision': 0, 'manualAnnotations': annotations, 'manualBaselines': baselines,
            'deletedTrackingIds': [], 'deletedObjectIds': [], 'deletedFrameObjects': []}

    def build(previous: dict[str, Any]) -> dict[str, Any]:
        clean = {**previous, **{k: v for k, v in body.items() if k != 'expectedRevision'}}
        annotation_state.validate_workspace_geometry(clean, meta)
        return clean
    annotation_state.write_state(directory, media_id, uid, 'package-import-' + uuid.uuid4().hex, body, 'workspace', build)


@source_write
def _discard(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def import_package(package: Path, uid: int, work: Path) -> dict[str, Any]:
    """Import every source that carries its original video. Returns imported and skipped lists."""
    from . import main
    try:
        archive = zipfile.ZipFile(package)
    except (zipfile.BadZipFile, OSError) as exc:
        raise HTTPException(422, '不是有效的 ZIP 文件') from exc
    with archive:
        try:
            manifest = _read_json(archive, 'manifest.json', MANIFEST_LIMIT)
        except Skip as exc:
            raise HTTPException(422, f'这不是本工具导出的训练数据集：{exc}') from exc
        sources = manifest.get('sources') if isinstance(manifest, dict) else None
        if manifest.get('schemaVersion') != 2 or not isinstance(sources, list) or not sources:
            raise HTTPException(422, '数据集版本不受支持，请用新版工具重新导出')
        class_names = manifest.get('classNames') or []
        imported, skipped = [], []
        for index, source in enumerate(sources):
            label = f'第 {index + 1} 个视频'
            try:
                if not isinstance(source, dict):
                    raise Skip('来源记录无效')
                video_file, final_id = source.get('videoFile'), source.get('finalVersionId')
                match = VIDEO_PATH.match(video_file) if isinstance(video_file, str) else None
                if video_file is None:
                    raise Skip('导出时没有勾选“包含原视频”，无法导入')
                if not match or video_file not in archive.namelist():
                    raise Skip('原视频路径无效或缺失')
                filename = label = match.group(1)
                if not isinstance(final_id, str) or not SAFE_ID.match(final_id):
                    raise Skip('最终版本编号无效')
                frames = _read_json(archive, f'provenance/{final_id}.json', PROVENANCE_LIMIT)
                frames = frames.get('frames') if isinstance(frames, dict) else None
                target = work / f'{index:03d}' / filename
                target.parent.mkdir(parents=True)
                if _extract_video(archive, video_file, target) != source.get('sourceSha256'):
                    raise Skip('原视频校验失败，文件可能已损坏')
                try:
                    info = main._probe_video(target)
                except Exception as exc:
                    raise Skip(f'无法解码原视频：{exc}') from exc
                if (info.get('width'), info.get('height'), info.get('frameCount')) != (source.get('width'), source.get('height'), source.get('frameCount')):
                    raise Skip('原视频尺寸或帧数与标注记录不一致')
                annotations, baselines, empty = manual_objects(frames, info, class_names[0] if len(class_names) == 1 else None)
                if any(float(info.get(k) or 0) <= 0 for k in ('frameCount', 'width', 'height', 'fps')):
                    raise Skip('原视频无法获得有效的帧数、尺寸或 FPS')
                try:
                    result = main.publish_upload(target, filename, info, uid)
                except HTTPException as exc:
                    raise Skip(str(exc.detail)) from exc
                if not isinstance(result, dict):  # Duplicate video: never overwrite existing work.
                    existing = json.loads(bytes(result.body))['duplicate']
                    skipped.append({'videoName': filename, 'mediaId': existing['mediaId'],
                                    'reason': f'服务器已有同一视频「{existing["videoName"]}」，未覆盖。如需改用导入的标注，请先删除该素材再导入'})
                    continue
                media_id = result['mediaId']
                try:
                    _write_imported_workspace(media_id, main.media_dir(media_id), uid, annotations, baselines, {
                        'exportId': manifest.get('exportId'), 'sourceSystemId': manifest.get('sourceSystemId'),
                        'finalVersionId': final_id, 'snapshotHash': source.get('snapshotHash'),
                        'importedBy': uid, 'importedAt': datetime.now(timezone.utc).isoformat()})
                except Exception as exc:
                    # A video without its imported boxes would look like an unannotated
                    # upload; remove the medium this call just created.
                    log.exception('media.package_import_failed media=%s actor=%s', media_id, uid)
                    _discard(main.media_dir(media_id))
                    raise Skip(str(getattr(exc, 'detail', '')) or '写入标注失败，已撤销该视频的导入') from exc
                imported.append({**result, 'objects': len(annotations), 'emptyFrames': empty})
                log.info('media.package_imported media=%s actor=%s export=%s final=%s objects=%s',
                         media_id, uid, manifest.get('exportId'), final_id, len(annotations))
            except Skip as exc:
                skipped.append({'videoName': label, 'reason': str(exc)})
                log.warning('media.package_skipped actor=%s source=%s reason=%s', uid, label, exc)
    return {'imported': imported, 'skipped': skipped}
