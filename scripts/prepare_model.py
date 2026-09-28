"""Install this repository's pinned SAM3 Release assets without Python on the host."""
from pathlib import Path
import argparse
import fcntl
import hashlib
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import time
import urllib.error
import urllib.request

log = logging.getLogger('model.setup')
BLOCK = 8 * 1024 * 1024


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        checksum = hashlib.sha256()
        while block := stream.read(BLOCK):
            checksum.update(block)
        return checksum.hexdigest()


def matches(path: Path, record: dict) -> bool:
    return path.is_file() and path.stat().st_size == record['size'] and digest(path) == record['sha256']


def download(url: str, destination: Path, record: dict) -> None:
    if matches(destination, record):
        return
    partial = destination.with_name(destination.name + '.partial')
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= record['size']:
            if matches(partial, record):
                partial.replace(destination)
                return
            partial.unlink()
            offset = 0
        try:
            request = urllib.request.Request(url, headers={'Range': f'bytes={offset}-'} if offset else {})
            with urllib.request.urlopen(request, timeout=60) as response:
                if offset and response.status == 206:
                    if not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                        raise ValueError('Invalid resume response')
                    mode = 'ab'
                else:
                    mode = 'wb'
                    offset = 0
                with partial.open(mode) as stream:
                    while block := response.read(BLOCK):
                        offset += len(block)
                        if offset > record['size']:
                            raise ValueError('Download exceeds manifest size')
                        stream.write(block)
            if not matches(partial, record):
                # A complete but corrupt file must be fetched again, not resumed.
                if partial.stat().st_size == record['size']:
                    partial.unlink()
                raise ValueError('Downloaded size/SHA256 does not match manifest')
            partial.replace(destination)
            log.info('model.asset_verified asset=%s bytes=%s', destination.name, record['size'])
            return
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
            log.warning('model.download_retry asset=%s attempt=%s', destination.name, attempt + 1)
            if attempt == 2:
                raise RuntimeError(f'模型下载或校验失败：{destination.name}；缓存保留，修复网络后重试。') from None
            time.sleep(1 + attempt)


def complete_custom_model(target: Path) -> bool:
    if not (target / 'config.json').is_file():
        return False
    if not any((target / n).is_file() for n in ['processor_config.json', 'preprocessor_config.json']):
        return False
    indexes = list(target.glob('*.index.json'))
    weights = list(target.glob('*.safetensors')) + list(target.glob('pytorch_model*.bin'))
    for index in indexes:
        weights += [target / name for name in set(json.loads(index.read_text()).get('weight_map', {}).values())]
    return bool(weights) and all(p.is_file() and p.stat().st_size > 0 for p in weights)


def prepare(target: Path, manifest_path: Path, auto_download: bool = True) -> None:
    manifest = json.loads(manifest_path.read_text())
    if Path(manifest['release']).name != manifest['release'] or manifest['release'] in {'.', '..'}:
        raise ValueError('Unsafe model manifest release')
    for record in manifest['files']:
        for name in [record['name']] + [p['asset'] for p in record['parts']]:
            if not name or Path(name).name != name or name in {'.', '..'}:
                raise ValueError('Unsafe model manifest filename')
    marker = target / '.sam3-release.json'
    if marker.exists():
        for record in manifest['files']:
            if not matches(target / record['name'], record):
                raise ValueError(f'已安装模型校验失败：{record["name"]}；不会自动覆盖，请检查文件。')
        log.info('model.ready release=%s cached=true', manifest['release'])
        return
    if complete_custom_model(target):
        log.info('model.custom_ready 使用已有模型；未覆盖，启动前检查将进一步验证。')
        return
    if not auto_download:
        raise ValueError('自动下载已关闭且模型不完整；请准备模型或设置 SAM3_AUTO_DOWNLOAD=true。')
    target.mkdir(parents=True, exist_ok=True)
    cache = target / '.sam3-download' / manifest['release']
    cache.mkdir(parents=True, exist_ok=True)
    with (target / '.sam3-download' / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('另一个部署正在准备模型，请等待完成后重试。') from None
        staging = cache / 'files'
        staging.mkdir(exist_ok=True)
        for record in manifest['files']:
            existing = target / record['name']
            if existing.exists() and not matches(existing, record):
                raise ValueError(f'已有不完整或不同版本模型文件：{record["name"]}；请指定新的 SAM3_MODEL_HOST_PATH，不覆盖原文件。')
        required = sum(x['size'] for x in manifest['files']) * 2
        if shutil.disk_usage(target).free < required:
            raise ValueError(f'模型下载/组装需要约 {required / 1024**3:.1f} GiB 可用空间。')
        log.info('model.download_started release=%s bytes=%s; 模型遵循随包 SAM LICENSE', manifest['release'], required // 2)
        for record in manifest['files']:
            assembled = staging / record['name']
            if matches(assembled, record):
                continue
            for part in record['parts']:
                download(manifest['baseUrl'] + part['asset'], cache / part['asset'], part)
            temporary = assembled.with_name(assembled.name + '.assembling')
            with temporary.open('wb') as stream:
                for part in record['parts']:
                    with (cache / part['asset']).open('rb') as source:
                        shutil.copyfileobj(source, stream, BLOCK)
            if not matches(temporary, record):
                raise ValueError(f'模型组装校验失败：{record["name"]}')
            temporary.replace(assembled)
        # Publish only after every file is complete; retry can recognize matching files.
        for record in manifest['files']:
            (staging / record['name']).replace(target / record['name'])
        marker_tmp = marker.with_suffix('.tmp')
        marker_tmp.write_text(json.dumps({'release': manifest['release'], 'upstreamRevision': manifest['upstreamRevision']}))
        marker_tmp.replace(marker)
        shutil.rmtree(cache)
        log.info('model.ready release=%s cached=false', manifest['release'])


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path('/models/sam3'))
    parser.add_argument('--manifest', type=Path, default=Path('/setup/model-distribution/manifest.json'))
    args = parser.parse_args()
    try:
        args.target.mkdir(parents=True, exist_ok=True)
        file_log = RotatingFileHandler(args.target / '.sam3-setup.log', maxBytes=2 * 1024 * 1024, backupCount=2)
        file_log.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logging.getLogger().addHandler(file_log)
        prepare(args.target, args.manifest, os.getenv('SAM3_AUTO_DOWNLOAD', 'true').lower() == 'true')
    except Exception:
        log.exception('model.prepare_failed；不启动半套 AI 服务。下载缓存和已有模型保留。')
        raise SystemExit(1)
