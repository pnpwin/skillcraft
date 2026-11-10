"""Hash-bound Agent Skills and OpenCode checks, separate from task grading."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.quick_validate import REVISION, EXIT_CODES, validate_standard
from scripts.utils import ROOT, EXCLUDED, digest, read_json, write_json

STATUSES = {'PASS', 'FAIL', 'NOT_RUN', 'ERROR'}


def skill_hash(skill):
    """Identity of the execution projection, identical to copy_tree(skill=True).

    Root tests/evals are evaluation inputs, never executable skill resources.
    Runtime dependencies/caches are installed separately and not skill artifacts.
    """
    root = Path(skill)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Skill must be a directory, not a symlink')
    files = []
    for directory, dirs, names in os.walk(root):
        rel = Path(directory).relative_to(root)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (rel == Path('.') and d in {'evals', 'tests'}))
        for name in dirs + names:
            p = Path(directory) / name
            if p.is_symlink():
                raise ValueError('Symlinks are not permitted in skill artifacts')
        files.extend(Path(directory) / n for n in names if n not in EXCLUDED and not n.endswith('.pyc'))
    h = hashlib.sha256()
    for p in sorted(files):
        h.update(p.relative_to(root).as_posix().encode() + b'\0' + p.read_bytes() + b'\0')
    return h.hexdigest()


def runner_hash():
    h = hashlib.sha256()
    for name in ['scripts/runtime.py', 'scripts/utils.py', 'scripts/compliance.py',
                 'runner/src/main.ts', 'runner/src/events.ts', 'runner/package-lock.json']:
        h.update(name.encode() + b'\0' + (ROOT / name).read_bytes() + b'\0')
    return h.hexdigest()


def result(status, code=None, message=None, **extra):
    return {'status': status, 'issues': [{'code': code, 'message': message}] if code else [], 'evidence': [], **extra}


def require_standard(skill):
    check = validate_standard(skill)
    if check['status'] != 'PASS':
        raise ValueError('Agent Skills: ' + check['status'] + '; ' + '; '.join(i['field'] + ': ' + i['message'] for i in check['issues']))
    return check


def environment_identity(run):
    e = run['environment']
    return {k: e[k] for k in ['server_version', 'background', 'plugins', 'configuration_fingerprint', 'controlled']} | {
        'profile': run['profile'], 'model': run['model'], 'runner_hash': run['runner_hash']}


def checked_run(path, skill, *, probe=False):
    """Recompute evidence hashes; an old or partially written record is not PASS."""
    path = Path(path)
    run = read_json(path / 'run.json')
    if 'runner_hash' not in run or 'evidence_hashes' not in run:
        return result('NOT_RUN', 'legacy_evidence', 'Run predates hash-bound compatibility evidence'), run
    if run['runner_hash'] != runner_hash():
        return result('FAIL', 'runner_changed', 'Runner contract changed; rerun compatibility'), run
    required = {'request.json', 'profile.json', 'profile-source.json', 'messages.json', 'response.txt', 'environment.json'}
    if not required.issubset(run['evidence_hashes']):
        return result('ERROR', 'incomplete_evidence', 'Required execution evidence is missing'), run
    for name in required:
        if digest(path / name) != run['evidence_hashes'][name]:
            return result('ERROR', 'corrupt_evidence', 'Execution evidence hash mismatch'), run
    request = read_json(path / 'request.json')
    profile = read_json(path / 'profile.json')
    environment = read_json(path / 'environment.json')
    environment.pop('version', None)
    if environment != run['environment']:
        return result('ERROR', 'environment_evidence_mismatch', 'Stored environment and run disagree'), run
    if profile != run['profile'] or request['model'] != run['model'] or request['run_id'] != run['run_id']:
        return result('ERROR', 'provenance_mismatch', 'Request, profile and run identities disagree'), run
    if request.get('operation', 'run') != ('probe' if probe else 'run'):
        return result('NOT_RUN', 'wrong_operation', 'This evidence is not the required execution/probe'), run
    if run['status'] != 'completed':
        return result('ERROR', 'execution_error', 'Runtime did not complete', execution_status=run['status']), run
    if run['installed_hash'] != skill_hash(skill):
        return result('FAIL', 'artifact_changed', 'Evidence belongs to a different skill snapshot'), run
    installed = Path(request['skill_path']).parent
    candidate = run['environment']['candidate']
    if skill_hash(installed) != run['installed_hash'] or digest(installed / 'SKILL.md') != request['skill_sha256']:
        return result('FAIL', 'installed_changed', 'Installed snapshot no longer matches the evidence'), run
    if (candidate['id'] != Path(skill).resolve().name or candidate['id'] != request['skill_id']
            or Path(candidate['path']) != installed / 'SKILL.md'):
        return result('FAIL', 'discovery_mismatch', 'Registry identity/path does not match the candidate'), run
    if run['environment']['server_version'] != '2.2.6':
        return result('ERROR', 'runtime_version', 'Unverified OpenCode version'), run
    if not probe:
        if request['mode'] != 'quality' or not request.get('attach_skill') or not run.get('attached'):
            return result('NOT_RUN', 'execution_required', 'Natural-trigger/preflight evidence alone does not prove representative execution'), run
        messages = read_json(path / 'messages.json')
        if not any(m.get('type') == 'user' and m.get('id') == run.get('prompt_id')
                   and any(s.get('id') == request['skill_id'] for s in m.get('skills', [])) for m in messages):
            return result('ERROR', 'attachment_missing', 'Native attachment is absent from durable messages'), run
    identity = environment_identity(run)
    return result('PASS', evidence=[str(path / 'run.json')], environment=identity,
                  scope='Discovery, native attachment and completed representative execution; quality is separate'), run


def assess(skill, run_dir=None, *, probe_dir=None, require_fresh=False):
    standard = validate_standard(skill)
    runtime = result('NOT_RUN', 'no_execution', 'No compatibility execution supplied')
    artifact_hash = None
    try:
        artifact_hash = skill_hash(skill)
        if run_dir:
            runtime, run = checked_run(run_dir, skill)
            if runtime['status'] == 'PASS' and require_fresh:
                if not probe_dir:
                    runtime = result('NOT_RUN', 'no_current_environment', 'Current environment has not been checked')
                else:
                    fresh, probe = checked_run(probe_dir, skill, probe=True)
                    if fresh['status'] != 'PASS':
                        runtime = fresh
                    elif environment_identity(run) != environment_identity(probe):
                        runtime = result('FAIL', 'environment_changed', 'Runtime/profile/model environment differs from execution evidence')
                    else:
                        runtime['evidence'] += fresh['evidence']
                        runtime['current_environment_verified'] = True
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        runtime = result('ERROR', 'evidence_error', 'Cannot verify artifact/evidence', error_type=type(error).__name__)
    return {'schema_version': 1, 'skill_hash': artifact_hash,
            'contract': {'agent_skills_revision': REVISION, 'opencode_version': '2.2.6'},
            'agent_skills': standard, 'opencode': runtime,
            'accepted': standard['status'] == runtime['status'] == 'PASS',
            'recommendations': standard.get('recommendations', [])}


def acceptance_exit(check):
    statuses = [check[k]['status'] for k in ['agent_skills', 'opencode']]
    if check.get('accepted') and statuses == ['PASS', 'PASS']:
        return 0
    for status in ['ERROR', 'FAIL', 'NOT_RUN']:
        if status in statuses:
            return EXIT_CODES[status]
    return 3


def accept_current(skill, run_dir, workspace):
    """Refresh only preflight/environment; representative execution is retained."""
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    check = assess(skill, run_dir)
    if check['accepted']:
        from scripts.runtime import run_model
        try:
            run = read_json(Path(run_dir) / 'run.json')
            custom = read_json(Path(run_dir) / 'profile-source.json')
            profile = None
            if custom is not None:
                profile = workspace / 'profile.json'
                write_json(profile, custom)
            model = run['model']; model_id = model['providerID'] + '/' + model['id'] + ('#' + model['variant'] if model.get('variant') else '')
            run_model('', workspace / 'probe', model_id, skill=skill, attach=False,
                      timeout=60, profile=profile, operation='probe')
            check = assess(skill, run_dir, probe_dir=workspace / 'probe', require_fresh=True)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            check['opencode'] = result('ERROR', 'refresh_error', 'Environment refresh could not complete', error_type=type(error).__name__)
            check['accepted'] = False
    write_json(workspace / 'compliance.json', check)
    return check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('skill', type=Path)
    parser.add_argument('--run', type=Path, help='Completed representative native-attachment execution directory')
    parser.add_argument('--workspace', type=Path, required=True, help='New directory for fresh environment probe and acceptance evidence')
    args = parser.parse_args()
    check = accept_current(args.skill, args.run, args.workspace)
    print(json.dumps(check, ensure_ascii=False, indent=2))
    return acceptance_exit(check)


if __name__ == '__main__':
    sys.exit(main())
