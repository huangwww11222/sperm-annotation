"""Mandatory browser regression, with fresh isolated data and recorded coverage.

Runs all registered suites serially. Exits nonzero on a failed test or startup;
always stops only its own services. Supports an existing Python env or CI image.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
SUITES = [
    ('critical-workflows', []),
    ('workflow-transitions', ['confirmation_browser_fixture', 'workflow_transitions_browser_fixture']),
    ('annotation-ux', ['confirmation_browser_fixture', 'annotation_ux_fixture']),
    ('tracking-recovery', ['confirmation_browser_fixture', 'tracking_recovery_fixture']),
    ('review-failure', ['confirmation_browser_fixture', 'review_browser_fixture']),
    ('confirmation-failure', ['confirmation_browser_fixture']),
    ('confirmation', ['confirmation_browser_fixture']),
    ('training-export', ['confirmation_browser_fixture']),
    ('workbench-consistency', ['confirmation_browser_fixture', 'annotation_ux_fixture', 'review_browser_fixture']),
    ('independent-integrity', ['independent_browser_fixture']),
]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_http(url, process):
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError('Test server exited before readiness')
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError):
            pass
        time.sleep(.2)
    raise RuntimeError('Test server readiness timed out: '+url)


def preserve_review_logs(base):
    """Keep the backend's fault stacks in the existing CI log artifact scope."""
    for source in (base / 'data' / 'logs').glob('review.log*'):
        if not source.is_file() or source.is_symlink():
            continue
        suffix = source.name.removeprefix('review.log')
        target = base / ('review' + suffix + '.log')
        content = source.read_text(encoding='utf-8', errors='replace')
        content = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted-test-token]', content)
        target.write_text(content, encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--python', default=sys.executable)
    p.add_argument('--backend-image', help='Run backend and fixtures in this already-built image')
    p.add_argument('--channel', default='chrome')
    p.add_argument('--critical-only', action='store_true', help='Diagnostic subset; never reports full regression passed')
    args = p.parse_args()
    os.chdir(ROOT)
    (ROOT/'work').mkdir(exist_ok=True)
    (ROOT/'output/playwright').mkdir(parents=True, exist_ok=True)
    # Fixtures use fixed output filenames, so concurrent runners must not share them.
    lock = ROOT/'work/browser-regression.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise SystemExit('Another browser regression owns work/browser-regression.lock; check it before retrying')
    run_id = uuid.uuid4().hex[:10]
    base = ROOT/'work'/('e2e-confirm-ux-regression-'+run_id)
    base.mkdir()
    backend_port, frontend_port = free_port(), free_port()
    while frontend_port == backend_port:
        frontend_port = free_port()
    relative = base.relative_to(ROOT)
    container = 'annotation-browser-'+run_id
    runtime_root = Path('/workspace') if args.backend_image else ROOT
    runtime_base = runtime_root/relative
    env = dict(os.environ, APP_DATA_DIR=str(runtime_base/'data'), APP_DB_FILE=str(runtime_base/'data/app.db'),
               APP_STORAGE_DIR=str(runtime_base/'storage'), PYTHONPATH=str(runtime_root/'backend'),
               BROWSER_SIMULATED_MODEL='1', SAM3_ENABLED='true', SAM3_DEVICE='cpu',
               API_PROXY_TARGET=f'http://127.0.0.1:{backend_port}', CHROME_CHANNEL=args.channel,
               CRITICAL_AI_MODE='simulated', CRITICAL_VIDEO=str(base/'critical-input.avi'),
               CONFIRMATION_ORIGIN=f'http://127.0.0.1:{frontend_port}', REVIEW_ORIGIN=f'http://127.0.0.1:{frontend_port}',
               CRITICAL_ORIGIN=f'http://127.0.0.1:{frontend_port}')
    env.update(INDEPENDENT_AUDIT_ROOT=str(base), INDEPENDENT_AUDIT_ORIGIN=f'http://127.0.0.1:{frontend_port}')
    # Preserve virtualenv symlinks: resolving bin/python selects the base env.
    python = [os.path.abspath(args.python)]
    if args.backend_image:
        python = ['docker', 'exec', '-i', '-w', '/workspace', container, 'python']
    # Existing ZIP checks execute Python; keep them on the same dependency/runtime version.
    wrapper = base/'python-under-test'
    wrapper.write_text('#!/bin/sh\nexec '+shlex.join(python)+' "$@"\n')
    wrapper.chmod(0o700)
    env['TEST_PYTHON'] = str(wrapper)
    selected = SUITES[:1] if args.critical_only else SUITES
    report = {'scope':'critical-only' if args.critical_only else 'mandatory',
              'gitHead':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
              'workingTree':subprocess.check_output(['git','status','--short'],text=True).splitlines(),
              'backendMode':'container' if args.backend_image else 'local',
              'backendRuntime':args.backend_image or python[0], 'browserChannel':args.channel,
              'model':'simulated; no GPU validation', 'requiredSuites':[s for s,_ in selected],
              'suites':[], 'status':'running'}
    processes, handles = [], []

    def save_report():
        (base/'run.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def run(command, name, timeout=300):
        started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        output = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted-test-token]', result.stdout)
        (base/(name+'.log')).write_text(output)
        if result.returncode:
            print(output[-7000:], flush=True)
            raise RuntimeError(f'{name} failed ({result.returncode}); see {base/name}.log')
        return round(time.monotonic()-started, 2)

    def fixture(name):
        run(python+['backend/tests/'+name+'.py'], name)
        if args.backend_image:
            # Browser runs on host; translate only fixture filesystem paths.
            def translate(value):
                if isinstance(value, str) and value.startswith('/workspace/'):
                    return str(ROOT/value.removeprefix('/workspace/'))
                if isinstance(value, dict): return {k:translate(v) for k,v in value.items()}
                if isinstance(value, list): return [translate(v) for v in value]
                return value
            for file in (ROOT/'work').glob('*fixture.json'):
                file.write_text(json.dumps(translate(json.loads(file.read_text()))))

    try:
        save_report()
        backend_command = python+['backend/tests/browser_server.py','--port',str(backend_port)]
        if args.backend_image:
            docker_env = {k:env[k] for k in ('APP_DATA_DIR','APP_DB_FILE','APP_STORAGE_DIR','PYTHONPATH','BROWSER_SIMULATED_MODEL','SAM3_ENABLED','SAM3_DEVICE')}
            docker_env['CRITICAL_VIDEO'] = str(runtime_base/'critical-input.avi')
            docker_env['INDEPENDENT_AUDIT_ROOT'] = str(runtime_base)
            # Fixtures share the host checkout: root-owned files cannot be
            # translated or cleaned by the unprivileged Linux Actions runner.
            backend_command = ['docker','run','--rm','--name',container,
                               '--user',f'{os.getuid()}:{os.getgid()}',
                               '--entrypoint','python','-w','/workspace',
                               '-v',str(ROOT)+':/workspace','-p',f'127.0.0.1:{backend_port}:{backend_port}']
            for k,v in docker_env.items(): backend_command += ['-e', k+'='+v]
            backend_command += [args.backend_image,'backend/tests/browser_server.py','--host','0.0.0.0','--port',str(backend_port)]
        for name, command in [('backend',backend_command),('frontend',['npm','run','dev','--prefix','frontend','--','--host','127.0.0.1','--port',str(frontend_port),'--strictPort'])]:
            handle = (base/(name+'.log')).open('w'); handles.append(handle)
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True))
        wait_http(f'http://127.0.0.1:{backend_port}/api/health', processes[0])
        wait_http(f'http://127.0.0.1:{frontend_port}', processes[1])
        fixture('critical_browser_fixture')
        for name, fixtures in selected:
            for f in fixtures: fixture(f)
            print('RUN '+name, flush=True)
            item = {'name':name,'status':'running'}; report['suites'].append(item);save_report()
            try:
                item['seconds'] = run(['node','frontend/tests/'+name+'-browser.mjs'], name, timeout=360)
                item['status'] = 'passed'
            except Exception:
                item['status'] = 'failed';raise
            finally: save_report()
            print('PASS '+name, flush=True)
        report['status']='passed'
    except BaseException as error:
        report['status']='failed';report['error']=str(error);raise
    finally:
        save_report()
        if args.backend_image:
            subprocess.run(['docker','stop','--time','5',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        import signal
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL)
        for handle in handles: handle.close()
        try:
            preserve_review_logs(base)
        except OSError as error:
            report['diagnosticsError'] = str(error)
            save_report()
            print('Could not preserve backend review logs: '+str(error), file=sys.stderr)
        lock.rmdir()
        print('Regression evidence: '+str(base/'run.json'), flush=True)


if __name__ == '__main__':
    main()
