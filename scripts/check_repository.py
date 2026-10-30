"""Fast release guard. Run from a Git checkout; never reads runtime file contents."""
from pathlib import Path
import hashlib
import json
import ast
import collections
import re
import subprocess

root = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
errors = []
for name in filter(None, paths):
    path = Path(name)
    if name.startswith(('backend/storage/', 'backend/track_data/', 'backend/track_modul/', 'runtime/', 'models/', 'work/', 'output/', 'update-backups/', '.offline/', '.offline-partial-', '测试精子视频/')) or path.name == '.DS_Store' or (path.name.startswith('.env') and not path.name.endswith('.example')) or re.search(r'\.db(?:-wal|-shm)?$', name):
        errors.append(f'运行数据/本机配置不应跟踪：{name}')
        continue
    full = root / path
    if path.suffix in {'.pt', '.pth', '.safetensors'}:
        if name != 'backend/app/model_assets/head-detector.pt':
            errors.append(f'未批准的模型权重不应跟踪：{name}')
        else:
            record = json.loads(full.with_suffix('.json').read_text())
            if not full.is_file() or full.stat().st_size != record['size'] or hashlib.sha256(full.read_bytes()).hexdigest() != record['sha256']:
                errors.append(f'内置检测权重大小或 SHA256 校验失败：{name}')
        continue
    if not full.exists() or full.suffix not in {'.py', '.ts', '.vue', '.yaml', '.yml', '.json', '.sh', '.ps1'}:
        continue
    text = full.read_text(encoding='utf-8-sig')
    if re.search(r'^(<<<<<<< |=======$|>>>>>>> )', text, flags=re.M):
        errors.append(f'未解决的合并标记：{name}')
    if full.suffix == '.py':
        try:
            tree = ast.parse(text, filename=name)
            counts = collections.Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            errors.extend(f'重复顶层定义：{name}:{key}' for key, count in counts.items() if count > 1)
        except SyntaxError as error:
            errors.append(f'语法错误：{error}')
if errors:
    raise SystemExit('\n'.join(errors))
print('Repository guard passed: no runtime files, conflict markers or duplicate Python definitions.')
