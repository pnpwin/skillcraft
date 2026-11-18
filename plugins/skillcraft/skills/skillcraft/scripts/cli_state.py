"""Opt-in, per-execution CLI launchers. These are scoped environments, not sandboxes."""
import json
import os
from pathlib import Path
import re
import sys

from scripts.utils import write_json


def validate_tools(tools):
    if not isinstance(tools, list):
        raise ValueError('cli_tools must be a list')
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {'id', 'executable', 'environment'}:
            raise ValueError('Each cli_tools entry requires id, executable and environment')
        name = tool['id']
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) or name in names:
            raise ValueError('CLI tool IDs must be unique safe identifiers')
        names.add(name)
        executable = tool['executable']
        if not isinstance(executable, str) or not Path(executable).is_absolute() or not os.access(executable, os.X_OK) or not Path(executable).is_file():
            raise ValueError(f'CLI executable must be an existing executable absolute path: {name}')
        env = tool['environment']
        if not isinstance(env, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', k)
                                          or not isinstance(v, str) or '\0' in v for k, v in env.items()):
            raise ValueError('CLI environment must be a string mapping of non-secret settings')
        if any(k in {'HOME', 'PATH'} or re.search(r'TOKEN|PASSWORD|SECRET|API_KEY|AUTH', k) for k in env):
            raise ValueError('CLI state profiles must not override HOME/PATH or contain credentials')
    return tools


def prepare_tools(directory, tools):
    directory = Path(directory).resolve()
    manifest = {'schema_version': 1, 'guarantee': 'scoped-launchers-only', 'tools': []}
    for tool in validate_tools(tools):
        root = directory / '.skillcraft-state' / tool['id']
        root.mkdir(parents=True, exist_ok=False)
        env = {k: v.replace('{state}', str(root)).replace('{project}', str(directory))
               for k, v in tool['environment'].items()}
        launcher = root / 'run'
        spec = {**tool, 'environment': env, 'state_root': str(root),
                'launcher': str(launcher), 'invocations': str(root / 'invocations.jsonl')}
        # The environment is applied here, inside the server's tool process, not
        # to the Python/TypeScript client that happens to request that process.
        source = f'''#!{sys.executable}
import json, os, sys, time
spec = json.loads({json.dumps(spec)!r})
env = dict(os.environ)
env.update(spec['environment'])
record = {{'cwd': os.getcwd(), 'executable': spec['executable'], 'environment': spec['environment'], 'arguments': sys.argv[1:], 'started_ms': time.time_ns() // 1000000}}
with open(spec['invocations'], 'a', encoding='utf-8') as f:
    f.write(json.dumps(record) + '\\n')
if sys.argv[1:] == ['--skillcraft-state']:
    print(json.dumps(record))
else:
    os.execve(spec['executable'], [spec['executable'], *sys.argv[1:]], env)
'''
        launcher.write_text(source, encoding='utf-8')
        launcher.chmod(0o555)
        manifest['tools'].append(spec)
    return manifest


def tool_instructions(manifest):
    if not manifest['tools']:
        return ''
    rows = [f"{t['id']}: {t['launcher']}" for t in manifest['tools']]
    return ('\nCLI state adapters for this execution (use these absolute launchers instead of bare commands):\n'
            + '\n'.join(rows) + '\nArguments are passed unchanged. --skillcraft-state reports the scoped environment. '
            'These settings apply only through the launcher; other shell commands are not sandboxed.\n')


def collect_state(manifest, output):
    observed = []
    for tool in manifest['tools']:
        p = Path(tool['invocations'])
        records = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
        records = [{**record, 'phase': 'preparation' if index < tool.get('preparation_calls', 0) else 'model'}
                   for index, record in enumerate(records)]
        observed.append({'id': tool['id'], 'status': 'observed' if records else 'unverified', 'calls': records})
    result = {**manifest, 'observations': observed}
    write_json(Path(output) / 'cli-state.json', result)
    return result
