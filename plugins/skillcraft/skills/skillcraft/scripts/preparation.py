"""Explicit fixture setup on final execution roots, outside model observation."""
from pathlib import Path
import subprocess
import time

from scripts.utils import write_json


def prepare_fixture(directory, manifest, steps, output):
    directory = Path(directory).resolve()
    tools = {tool['id']: tool for tool in manifest['tools']}
    if not isinstance(steps, list):
        raise ValueError('preparation must be a list')
    commands = []
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'tool', 'args', 'cwd'} or step['tool'] not in tools:
            raise ValueError('Preparation requires a configured tool, args and cwd')
        args = step['args']
        if not isinstance(args, list) or any(not isinstance(x, str) or '\0' in x for x in args):
            raise ValueError('Preparation args must be strings')
        relative = Path(step['cwd'])
        cwd = directory / relative
        if relative.is_absolute() or '..' in relative.parts or not cwd.resolve().is_relative_to(directory / 'inputs') or not cwd.is_dir():
            raise ValueError('Preparation cwd must be an existing directory under inputs/')
        commands.append(([tools[step['tool']]['launcher'], *args], cwd))
    evidence = {'schema_version': 1, 'phase': 'preparation', 'status': 'completed', 'steps': [],
                'scope': 'configured CLI launchers, not arbitrary shell or an OS sandbox'}
    start = time.monotonic()
    for command, cwd in commands:
        try:
            result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=60)
            evidence['steps'].append({'command': command, 'cwd': str(cwd), 'exit_code': result.returncode,
                                      'stdout': result.stdout, 'stderr': result.stderr})
            if result.returncode:
                evidence['status'] = 'error'
                break
        except (OSError, subprocess.TimeoutExpired) as error:
            evidence['status'] = 'error'
            evidence['steps'].append({'command': command, 'cwd': str(cwd), 'error': str(error)})
            break
    evidence['duration_seconds'] = time.monotonic() - start
    for tool in manifest['tools']:
        path = Path(tool['invocations'])
        tool['preparation_calls'] = len(path.read_text().splitlines()) if path.exists() else 0
    write_json(Path(output) / 'preparation.json', evidence)
    if evidence['status'] != 'completed':
        raise ValueError('Fixture preparation failed; see preparation.json')
    return evidence
