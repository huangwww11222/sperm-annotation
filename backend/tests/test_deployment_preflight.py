import json
from pathlib import Path
import shutil
import subprocess
import os

import pytest
from app import deployment_check as d


@pytest.mark.parametrize('secret', ['', 'short', 'dev-secret-change-me', 'replace-with-at-least-32-random-bytes'])
def test_preflight_rejects_missing_or_example_secrets(secret):
    with pytest.raises(ValueError, match='JWT_SECRET'):
        d.check_secret(secret)


def test_cpu_preflight_creates_persistent_locations_without_model(monkeypatch, tmp_path):
    for key, path in {'DATA_DIR': tmp_path/'database', 'DB_FILE':tmp_path/'database/app.db', 'MEDIA_STORAGE_DIR':tmp_path/'storage/media', 'DATASET_EXPORT_DIR':tmp_path/'storage/datasets'}.items():
        monkeypatch.setattr(d.config, key, path)
    monkeypatch.setattr(d.config, 'APP_ENV', 'production')
    monkeypatch.setattr(d.config, 'JWT_SECRET', 'a'*64)
    monkeypatch.setattr(d.config, 'SAM3_ENABLED', False)
    d.check()
    assert (tmp_path/'database').is_dir()
    assert (tmp_path/'storage/media').is_dir()
    assert not list(tmp_path.rglob('*deployment*'))


def test_model_preflight_rejects_incomplete_shards(tmp_path):
    (tmp_path/'config.json').write_text('{}')
    (tmp_path/'preprocessor_config.json').write_text('{}')
    (tmp_path/'model.safetensors.index.json').write_text(json.dumps({'weight_map':{'a':'part1.safetensors','b':'part2.safetensors'}}))
    (tmp_path/'part1.safetensors').write_bytes(b'fixture')
    with pytest.raises(ValueError, match='分片'):
        d.check_model(tmp_path)
    (tmp_path/'part2.safetensors').write_bytes(b'fixture')
    d.check_model(tmp_path)


def test_disabled_tracking_rejects_before_job_or_media_mutation(monkeypatch):
    from app import main
    from fastapi import HTTPException
    from app.schemas import TrackRequest
    monkeypatch.setattr(main, 'SAM3_ENABLED', False)
    monkeypatch.setattr(main, 'get_tracker_engine', lambda: pytest.fail('disabled mode must not initialize the model'))
    assert main.health()['sam3']['enabled'] is False
    with pytest.raises(HTTPException) as error:
        main.start_tracking(TrackRequest(mediaId='no-such-media', startFrame=0, annotations=[]), {'uid':1})
    assert error.value.status_code == 503
    monkeypatch.setattr(main, 'media_dir', lambda _: pytest.fail('disabled AI must not rewind prior results'))
    with pytest.raises(HTTPException) as rewind_error:
        main.rewind_tracking({'mediaId':'existing-video','startFrame':0}, {'uid':1})
    assert rewind_error.value.status_code == 503


def test_compose_modes_preserve_volumes_logs_and_isolate_gpu(tmp_path):
    if not shutil.which('docker'):
        pytest.skip('Docker CLI needed for real Compose interpolation')
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'JWT_SECRET':'a'*64, 'APP_DATA_ROOT':str(tmp_path/'data'), 'SAM3_MODEL_HOST_PATH':str(tmp_path/'model')}
    # Do not inherit a developer's Compose selection or production .env.
    env.pop('COMPOSE_FILE', None)
    empty_env = tmp_path / 'compose.env'
    empty_env.write_text('')
    for mode in ('cpu', 'gpu'):
        args = ['docker','compose','--env-file',str(empty_env),'-f',str(root/'compose.yaml')]
        if mode == 'gpu': args += ['-f',str(root/'compose.gpu.yaml')]
        result = subprocess.run(args+['config','--format','json'],env=env,capture_output=True,text=True,check=True)
        config = json.loads(result.stdout)
        b = config['services']['backend']
        assert 'ports' not in b
        targets = {v['target'] for v in b['volumes']}
        assert {'/data/database','/data/storage'} <= targets
        assert b['environment']['APP_DATA_DIR'] == '/data/database'
        assert b['environment']['SAM3_ENABLED'] == ('true' if mode == 'gpu' else 'false')
        assert ('/models/sam3' in targets) == (mode == 'gpu')
        assert bool(b.get('deploy',{}).get('resources',{}).get('reservations',{}).get('devices')) == (mode == 'gpu')


@pytest.mark.parametrize('mode', ['cpu', 'gpu'])
def test_deploy_script_initializes_once_and_preserves_existing_secret(tmp_path, mode):
    if not shutil.which('bash'):
        pytest.skip('Bash entry point')
    root = Path(__file__).resolve().parents[2]
    for name in ('deploy.sh','.env.docker.example'):
        shutil.copy(root/name, tmp_path/name)
    binary = tmp_path/'bin'; binary.mkdir()
    docker = binary/'docker'
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALL_LOG"\n')
    docker.chmod(0o755)
    env = {**os.environ, 'PATH':str(binary)+os.pathsep+os.environ['PATH'], 'DOCKER_CALL_LOG':str(tmp_path/'calls')}
    first = subprocess.run(['bash',str(tmp_path/'deploy.sh'),mode],env=env,capture_output=True,text=True,check=True)
    settings = (tmp_path/'.env').read_text()
    key = next(line.split('=',1)[1] for line in settings.splitlines() if line.startswith('JWT_SECRET='))
    assert len(key) == 64 and key not in first.stdout and key not in first.stderr
    assert ('COMPOSE_FILE=compose.yaml,compose.gpu.yaml' in settings) == (mode == 'gpu')
    subprocess.run(['bash',str(tmp_path/'deploy.sh'),mode],env=env,capture_output=True,text=True,check=True)
    assert (tmp_path/'.env').read_text() == settings
    other = 'gpu' if mode == 'cpu' else 'cpu'
    mismatch = subprocess.run(['bash',str(tmp_path/'deploy.sh'),other],env=env,capture_output=True,text=True)
    assert mismatch.returncode == 2
    assert (tmp_path/'.env').read_text() == settings
    assert '--no-build --wait' in (tmp_path/'calls').read_text()
