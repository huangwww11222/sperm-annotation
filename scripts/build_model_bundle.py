"""Package the pinned SAM3 Transformers snapshot for this repository's Release.

Weights are Release assets, never Git objects. Invoke with an authorized snapshot
and an ignored work/ output directory. No credentials are read by this script.
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = '6d06f0a5f84e435071fe6603e61d0b4cc7b40e0d39d487cfd4d67d8cc11cc14a'
TAG = 'sam3-model-6d06f0a5'
FILES = ['config.json', 'processor_config.json', 'tokenizer.json', 'tokenizer_config.json',
         'special_tokens_map.json', 'vocab.json', 'merges.txt', 'LICENSE', 'README.md']


def build(source: Path, output: Path) -> dict:
    output.resolve().relative_to(ROOT / 'work')
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for name in FILES + ['model.safetensors']:
        full_hash = hashlib.sha256()
        parts = []
        with (source / name).open('rb') as stream:
            number = 0
            while True:
                block = stream.read(8 * 1024 * 1024)
                if not block:
                    break
                number += 1
                asset = name if name != 'model.safetensors' else f'{name}.part{number:02d}'
                part_hash = hashlib.sha256()
                size = 0
                with (output / asset).open('wb') as destination:
                    while block and size < 1024 ** 3:
                        destination.write(block)
                        full_hash.update(block)
                        part_hash.update(block)
                        size += len(block)
                        if size == 1024 ** 3:
                            break
                        block = stream.read(min(8 * 1024 * 1024, 1024 ** 3 - size))
                parts.append({'asset': asset, 'size': size, 'sha256': part_hash.hexdigest()})
        digest = full_hash.hexdigest()
        if name == 'model.safetensors' and digest != EXPECTED:
            raise ValueError('Model checksum differs from pinned official facebook/sam3 snapshot')
        records.append({'name': name, 'size': sum(p['size'] for p in parts), 'sha256': digest, 'parts': parts})
    manifest = {'schemaVersion': 1, 'model': 'facebook/sam3', 'upstreamRevision': '3c879f39826c281e95690f02c7821c4de09afae7',
                'release': TAG, 'baseUrl': f'https://github.com/huangwww11222/sperm-annotation/releases/download/{TAG}/',
                'files': records}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    shutil.copyfile(source / 'LICENSE', output / 'LICENSE-SAM.txt')
    print(f'Model bundle ready: {len(records)} files, {sum(x["size"] for x in records)} bytes, SHA256 verified')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build(args.source, args.output)
