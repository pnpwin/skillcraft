"""Immutable grading attempts and explicit, hash-bound report selections."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from scripts.utils import digest, read_json, write_json


def value_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def rubric_hash(row):
    return value_hash({k: row.get(k) for k in ['expectations', 'assertions', 'checks', 'expected_output', 'required_evidence']})


def execution_identity(run_dir):
    root = Path(run_dir).resolve()
    names = ['run.json', 'request.json', 'eval_metadata.json', 'profile.json', 'profile-source.json',
             'environment.json', 'messages.json', 'response.txt', 'transcript.md', 'usage.json', 'timing.json',
             'input-manifest.json', 'cli-state.json', 'preparation.json']
    paths = [root / name for name in names if (root / name).exists()]
    for name in ['outputs', 'evidence']:
        if (root / name).exists():
            paths += sorted((root / name).rglob('*'))
    hashes = {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Execution evidence contains a symlink/escape')
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = digest(path)
    return {'source_root': str(root), 'run_id': read_json(root / 'run.json').get('run_id'),
            'files': hashes, 'sha256': value_hash(hashes)}


def matches_execution(recorded, current, *, portable=False):
    if portable and isinstance(recorded, dict):
        # An inline Hermes grading travels with the evidence it graded.
        current = {**current, 'source_root': recorded.get('source_root')}
    return recorded == current


def task_fields(row):
    return {'prompt': row.get('prompt'), 'files': row.get('files', []), 'expected_output': row.get('expected_output'),
            'preparation': row.get('preparation', [])}


def reserve_attempt(run_dir, row, output, judge_model, judge_profile):
    root = Path(run_dir).resolve()
    destination = Path(output).resolve() if output else root
    if output:
        if destination.is_relative_to(root) or root.is_relative_to(destination):
            raise ValueError('Regrading output must be outside the immutable execution directory')
        if not (root / 'messages.json').is_file():
            raise ValueError('Regrading requires recorded messages; run a new execution')
        if (root / 'eval_metadata.json').is_file():
            original = task_fields(read_json(root / 'eval_metadata.json'))
        elif (root / 'grading-manifest.json').is_file():
            original = task_fields(read_json(root / 'grading-manifest.json')['task'])
        elif (root / 'request.json').is_file():
            original = task_fields({'prompt': read_json(root / 'request.json')['prompt']})
        else:
            raise ValueError('Original task unavailable; cannot verify safe regrading')
        if task_fields(row) != original:
            raise ValueError('Task, fixtures or output contract changed; run a new execution')
        for relative in row.get('required_evidence', []):
            path = root / relative
            if not path.resolve().is_relative_to(root) or not path.is_file() or path.is_symlink():
                raise ValueError('Required evidence unavailable; run a new execution')
    profile_hash = digest(judge_profile) if judge_profile else None
    identity = execution_identity(root)
    if output:
        execution = read_json(root / 'run.json')
        if execution.get('status') != 'completed':
            raise ValueError('Executor did not complete; run a new execution')
        for name, expected in execution.get('evidence_hashes', {}).items():
            path = root / name
            if not path.resolve().is_relative_to(root) or digest(path) != expected:
                raise ValueError('Recorded execution evidence changed; cannot regrade')
        original_manifest = root / 'grading-manifest.json'
        if original_manifest.exists() and not matches_execution(read_json(original_manifest)['execution'], identity,
                                                                portable=execution.get('harness') == 'hermes'):
            raise ValueError('Execution changed since its original grading; cannot regrade')
    if output:
        destination.mkdir(parents=True, exist_ok=False)
    else:
        if (destination / 'grading.json').exists():
            raise ValueError('Grading exists; use --output for a new immutable attempt')
        # Exclusive reservation also prevents concurrent first graders.
        with (destination / '.grading-reservation').open('x') as lock:
            lock.write('reserved\n')
    manifest = {'schema_version': 1, 'attempt_id': uuid.uuid4().hex, 'status': 'in_progress',
                'created': datetime.now(timezone.utc).isoformat(), 'execution': identity,
                'task': task_fields(row), 'rubric_hash': rubric_hash(row), 'judge_model': judge_model,
                'judge_profile_hash': profile_hash}
    write_json(destination / 'rubric.json', row)
    write_json(destination / 'grading-manifest.json', manifest)
    return destination, manifest


def seal_attempt(destination, manifest, result):
    manifest = {**manifest, 'status': result['status'], 'evaluation_complete': result['evaluation_complete'],
                'finished': datetime.now(timezone.utc).isoformat(), 'result_sha256': digest(destination / 'grading.json'),
                'grader_contract': result.get('grader_contract'), 'evidence_projection': result.get('evidence_projection')}
    if (destination / 'judge/run.json').exists():
        judge = read_json(destination / 'judge/run.json')
        manifest['judge_execution'] = {k: judge.get(k) for k in ['run_id', 'model', 'profile', 'runner_hash', 'environment', 'status']}
    write_json(destination / 'grading-manifest.json', manifest)


def read_grading(run_dir, attempt=None, *, allow_incomplete=False):
    root = Path(run_dir).resolve()
    destination = root if attempt in (None, 'legacy') else Path(attempt).resolve()
    manifest_path = destination / 'grading-manifest.json'
    manifest = read_json(manifest_path) if manifest_path.is_file() else None
    if destination != root and manifest is None:
        raise ValueError('Selected grading attempt has no provenance manifest')
    if manifest:
        portable = destination == root and read_json(root / 'run.json').get('harness') == 'hermes'
        if not matches_execution(manifest['execution'], execution_identity(root), portable=portable):
            raise ValueError('Selected grading belongs to changed/different execution evidence')
        if manifest.get('status') == 'in_progress':
            if allow_incomplete:
                partial = read_json(destination / 'grading.json') if (destination / 'grading.json').exists() else {}
                return {**partial, 'status': 'unsealed', 'evaluation_complete': False}, destination, manifest
            raise ValueError('Selected grading attempt is not sealed')
        if digest(destination / 'grading.json') != manifest.get('result_sha256'):
            raise ValueError('Selected grading result was modified')
        if rubric_hash(read_json(destination / 'rubric.json')) != manifest['rubric_hash']:
            raise ValueError('Selected grading rubric was modified')
    grading = read_json(destination / 'grading.json') if (destination / 'grading.json').exists() else {}
    return grading, destination, manifest


def load_selection(workspace, path):
    if path is None:
        return None
    selection = read_json(path) if not isinstance(path, dict) else path
    root = Path(workspace).resolve()
    if (selection.get('schema_version') != 1 or Path(selection.get('source_workspace', '')).resolve() != root
            or not isinstance(selection.get('rule'), str) or not selection['rule'].strip()
            or not isinstance(selection.get('runs'), dict)):
        raise ValueError('Selection needs version, exact source_workspace, rule and runs mapping')
    actual = {p.parent.relative_to(root).as_posix() for p in root.glob('eval-*/*/run-*/run.json')}
    if set(selection['runs']) != actual:
        raise ValueError('Selection must explicitly cover every source run')
    for key, spec in selection['runs'].items():
        if not isinstance(spec, dict) or not isinstance(spec.get('selected'), str) or not isinstance(spec.get('history', []), list):
            raise ValueError('Each selection requires selected and optional history list')
        if spec['selected'] != 'legacy' and not Path(spec['selected']).is_absolute():
            raise ValueError('Selected attempt paths must be absolute')
        if type(spec.get('included', True)) is not bool or (spec.get('included') is False and
                (not isinstance(spec.get('reason'), str) or not spec['reason'].strip())):
            raise ValueError('Excluded runs need included:false and a nonempty reason')
        for item in spec.get('history', []):
            if not isinstance(item, str) or (item != 'legacy' and not Path(item).is_absolute()):
                raise ValueError('History attempt paths must be absolute or legacy')
    return selection


def grading_signature(manifest):
    if not manifest:
        return None
    environment = (manifest.get('judge_execution') or {}).get('environment')
    environment_identity = ({key: environment.get(key) for key in
                             ['server_version', 'controlled', 'background', 'plugins', 'configuration_fingerprint']}
                            if environment is not None else None)
    return value_hash({'rubric': manifest['rubric_hash'], 'judge_model': manifest['judge_model'],
                       'profile': manifest['judge_profile_hash'], 'prompt': manifest.get('grader_contract'),
                       'projection_version': (manifest.get('evidence_projection') or {}).get('version'),
                       'runner': (manifest.get('judge_execution') or {}).get('runner_hash'),
                       'environment': environment_identity})
