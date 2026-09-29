"""Offline updater failure/rollback contract. Fake Docker never touches a daemon."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def updater(tmp_path):
    deploy = tmp_path / 'deployment with spaces'
    deploy.mkdir()
    (deploy / '.env').write_text('MOCK=true\n')
    (deploy / 'compose.yaml').write_text('services: {}\n')
    (deploy / 'compose.gpu.yaml').write_text('services: {}\n')
    main = tmp_path / 'main.py'
    main.write_text('# isolated installed source\n')
    digest = hashlib.sha256(main.read_bytes()).hexdigest()
    files = {'manifest.json': json.dumps({'acceptedBackendHashes': [digest]}).encode(), 'backend/main.py': main.read_bytes(), 'frontend/dist/index.html': b'<html>fixture</html>'}
    files['SHA256SUMS'] = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in files.items()).encode()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w:gz') as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    installer = tmp_path / 'update.sh'
    installer.write_bytes((ROOT/'scripts/install_compat_update.sh').read_bytes() + b'\n__COMPAT_PAYLOAD_BELOW__\n' + base64.encodebytes(payload.getvalue()))
    fake = tmp_path / 'bin'
    fake.mkdir()
    docker = fake / 'docker'
    docker.write_text(f'#!{sys.executable}\n' + '''
import json,os,pathlib,shutil,sys
a=sys.argv[1:]
with open(os.environ['MOCK_CALLS'],'a') as f: f.write(json.dumps(a)+'\\n')
failure=os.environ.get('MOCK_FAIL','')
if a[0]=='compose' and 'ps' in a: print(a[-1])
elif a[0]=='compose' and 'config' in a:
 print(json.dumps({'services':{'backend':{'volumes':[{'type':'bind','source':'/different' if failure=='config' else '/test/db','target':'/data/database'}]},'frontend':{}}}))
elif a[:2]==['inspect','--format']:
 if a[2]=='{{json .Config.Env}}': print(json.dumps(['SAM3_ENABLED='+os.environ.get('MOCK_GPU','true')]))
 elif a[2]=='{{.Config.Image}}': print('test-'+a[-1]+':original')
 elif a[2]=='{{.Image}}': print('sha256:old-'+a[-1])
elif a[0]=='inspect':
 print(json.dumps([{'Config':{'Env':[]},'Mounts':[{'Source':'/test/db','Destination':'/data/database'},{'Source':'/test/storage','Destination':'/data/storage'}]},{'Config':{'Env':[]},'Mounts':[]}]))
elif a[0]=='cp': shutil.copyfile(os.environ['MOCK_MAIN'],a[-1])
elif a[0]=='exec' and failure=='busy': sys.exit(21)
elif a[0]=='build' and failure=='build': sys.exit(22)
elif a[0]=='compose' and 'up' in a and failure=='up':
 marker=pathlib.Path(os.environ['MOCK_CALLS']+'.failed')
 if not marker.exists(): marker.touch(); sys.exit(23)
''')
    docker.chmod(0o755)
    calls = tmp_path / 'calls.jsonl'
    env = {**os.environ, 'PATH': str(fake)+os.pathsep+os.environ['PATH'], 'MOCK_CALLS': str(calls), 'MOCK_MAIN': str(main)}

    def run(failure='', gpu=True):
        result = subprocess.run(['bash', str(installer), str(deploy)], env={**env, 'MOCK_FAIL': failure, 'MOCK_GPU': str(gpu).lower()}, capture_output=True, text=True, timeout=30)
        return result, [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return run, deploy, main


@pytest.mark.parametrize('gpu', [False, True])
def test_update_stays_offline_and_preserves_config(updater, gpu):
    run, deploy, _ = updater
    original = (deploy/'.env').read_bytes()
    result, calls = run(gpu=gpu)
    assert result.returncode == 0, result.stdout + result.stderr
    builds = [c for c in calls if c[0]=='build']
    assert len(builds) == 2 and all('--pull=false' in c and '--network=none' in c for c in builds)
    up = next(c for c in calls if c[0]=='compose' and 'up' in c)
    assert ('compose.gpu.yaml' in up) == gpu
    assert '--no-build' in up and up[up.index('--pull')+1]=='never' and '--no-deps' in up
    assert (deploy/'.env').read_bytes() == original
    assert len(list((deploy/'update-backups').glob('*/rollback.sh'))) == 1


def test_unknown_backend_version_stops_before_build_or_tag(updater):
    run, _, main = updater
    main.write_text('# unrelated server customization\n')
    result, calls = run()
    assert result.returncode != 0 and '版本与本补丁不匹配' in result.stdout
    assert not any(c[0] in {'build','image'} for c in calls)


@pytest.mark.parametrize('failure', ['busy', 'build', 'config'])
def test_prepublication_failure_never_recreates_services(updater, failure):
    run, _, _ = updater
    result, calls = run(failure)
    assert result.returncode != 0
    assert not any(c[0]=='compose' and 'up' in c for c in calls)
    assert not any(c[:2]==['image','tag'] and c[-1].endswith(':original') for c in calls)


def test_failed_restart_restores_original_images_and_recreates(updater):
    run, _, _ = updater
    result, calls = run('up')
    assert result.returncode == 23 and '正在恢复原镜像' in result.stdout
    updates = [c for c in calls if c[0]=='compose' and 'up' in c]
    assert len(updates) == 2
    tags = [c for c in calls if c[:2]==['image','tag'] and c[-1].endswith(':original')]
    assert len(tags) == 4 and all(c[2].startswith('annotation-update-backup:') for c in tags[-2:])
