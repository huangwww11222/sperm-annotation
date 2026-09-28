"""Release installer uses isolated files and a local HTTP server, never real weights."""
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('prepare_model', ROOT/'scripts/prepare_model.py')
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


@pytest.fixture
def bundle(tmp_path):
    data = {'config.json': b'{}', 'processor_config.json': b'{}', 'LICENSE': b'SAM fixture license',
            'model.safetensors': b'fixture-weight-' * 19}
    assets = {}
    records = []
    for name, body in data.items():
        pieces = [body] if name != 'model.safetensors' else [body[:101], body[101:]]
        parts = []
        for index, content in enumerate(pieces):
            asset = name + f'.part{index}'
            assets[asset] = content
            parts.append({'asset': asset, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        records.append({'name': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'parts': parts})
    state = {'ranges': [], 'corrupt': False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = assets[self.path.lstrip('/')]
            offset = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
            state['ranges'].append(offset)
            self.send_response(206 if offset else 200)
            if offset:
                self.send_header('Content-Range', f'bytes {offset}-{len(body)-1}/{len(body)}')
            payload = body[offset:]
            if state['corrupt']:
                payload = b'x' * len(payload)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = {'schemaVersion': 1, 'release': 'fixture', 'upstreamRevision': 'fixture',
                'baseUrl': f'http://127.0.0.1:{server.server_port}/', 'files': records}
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps(manifest))
    try:
        yield data, assets, state, manifest, path
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_first_install_resumes_checksums_assembles_and_reuses_without_network(tmp_path, bundle, monkeypatch):
    data, assets, state, manifest, path = bundle
    target = tmp_path/'models'
    cache = target/'.sam3-download/fixture'
    cache.mkdir(parents=True)
    (cache/'model.safetensors.part0.partial').write_bytes(assets['model.safetensors.part0'][:17])
    model.prepare(target, path)
    assert 17 in state['ranges']
    assert all((target/name).read_bytes() == content for name, content in data.items())
    assert json.loads((target/'.sam3-release.json').read_text())['release'] == 'fixture'
    assert not cache.exists()
    monkeypatch.setattr(model, 'download', lambda *args: pytest.fail('cached model must not use network'))
    model.prepare(target, path)
    (target/'model.safetensors').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='校验失败'):
        model.prepare(target, path)


def test_corrupt_download_never_publishes_ready_or_model(tmp_path, bundle, monkeypatch):
    _, _, state, _, path = bundle
    state['corrupt'] = True
    monkeypatch.setattr(model.time, 'sleep', lambda _: None)
    target = tmp_path/'models'
    with pytest.raises(RuntimeError, match='校验失败'):
        model.prepare(target, path)
    assert not (target/'.sam3-release.json').exists()
    assert not (target/'model.safetensors').exists()


def test_auto_download_disabled_preserves_existing_custom_model(tmp_path, bundle, monkeypatch):
    _, _, _, _, path = bundle
    target = tmp_path/'models'
    with pytest.raises(ValueError, match='自动下载已关闭'):
        model.prepare(target, path, False)
    target.mkdir()
    for name, content in {'config.json': b'{}', 'processor_config.json': b'{}', 'model.safetensors': b'custom'}.items():
        (target/name).write_bytes(content)
    monkeypatch.setattr(model, 'download', lambda *args: pytest.fail('must preserve custom model'))
    model.prepare(target, path, False)
    assert (target/'model.safetensors').read_bytes() == b'custom'
    assert not (target/'.sam3-release.json').exists()


def test_partial_custom_model_not_overwritten_and_unsafe_paths_rejected(tmp_path, bundle):
    _, _, _, manifest, path = bundle
    target = tmp_path/'models'
    target.mkdir()
    (target/'config.json').write_text('custom-incomplete')
    with pytest.raises(ValueError, match='不完整或不同版本'):
        model.prepare(target, path)
    assert (target/'config.json').read_text() == 'custom-incomplete'
    manifest['files'][0]['name'] = '../outside'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Unsafe'):
        model.prepare(target, path)
