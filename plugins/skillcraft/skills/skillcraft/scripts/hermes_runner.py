"""File-only quality evaluation through the native Hermes Chat CLI gateway."""
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

from scripts import hermes_worker as worker
from scripts.utils import digest, read_json, write_json

PLUGIN = 'skillcraft_eval'


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def secure_directory(path, *, create=False):
    """Validate each ancestor in order before entering or creating its child."""
    directory = Path(os.path.abspath(path))
    owners = {0, os.geteuid()}
    for current in (*reversed(directory.parents), directory):
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise
            current.mkdir(mode=0o700, exist_ok=True)
            info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise ValueError(f'Unsafe Hermes directory: {current}')
    return directory


def require_private(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f'Hermes requires an owner-only directory: {path}')


def create_run_directory(override):
    default = '/private/tmp' if sys.platform == 'darwin' else tempfile.gettempdir()
    parent = secure_directory(override if override is not None else default, create=override is not None)
    root = Path(tempfile.mkdtemp(prefix='skillcraft-hermes-', dir=parent))
    require_private(root)
    directory = Path(tempfile.mkdtemp(prefix='skill-eval-', dir=root))
    require_private(directory)
    return directory


def write_profile_auth(home, auth):
    home = secure_directory(home)
    require_private(home)
    # mkstemp uses exclusive creation, O_NOFOLLOW where supported, and mode 0600.
    fd, name = tempfile.mkstemp(prefix='.auth-', dir=home)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(auth, output)
        os.replace(name, home / 'auth.json')
    finally:
        Path(name).unlink(missing_ok=True)


def regular_artifacts(root):
    root = Path(root)
    entries = {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Missing or replaced output directory')
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Output contains an unsafe filesystem entry')
        entries[str(path.relative_to(root))] = digest(path)
    return entries


def check_requests(rows, request, session, home, *, block):
    requests, responses = {}, {}
    skill = [request['skill_id']] if request.get('skill_path') else []
    natural = request.get('mode') == 'trigger' and not request.get('attach_skill')
    if request.get('mode') == 'trigger' and not natural:
        raise ValueError('Hermes trigger evaluation must use natural skill selection')
    allowed_tools = (worker.ADVERTISED_TOOLS if natural else
                     worker.ADVERTISED_TOOLS - worker.NATURAL_SKILL_TOOLS - worker.BLOCKED_SKILL_TOOLS)
    if natural and not skill:
        raise ValueError('Natural Hermes selection requires a controlled candidate')
    if skill and not natural:
        if not isinstance(block, str) or not re.fullmatch(r'[a-f0-9]{64}', block):
            raise ValueError('Missing or invalid expected skill preload hash')
    elif block is not None:
        raise ValueError('A baseline must not have an expected skill preload hash')
    for row in rows:
        if row.get('error'):
            raise ValueError(f"Hermes provider request refused: {row['error']}")
        if (row.get('run_id') != request['run_id'] or row.get('profile') != str(home)
                or row.get('session_id') != session
                or row.get('provider') != request['model']['provider']
                or row.get('model') != request['model']['id']):
            raise ValueError('Hermes request identity, catalog or skill proof failed')
        turn, key = row.get('turn_id'), row.get('api_request_id')
        if not turn or not key:
            raise ValueError('Missing native turn or request ID')
        if row['phase'] == 'request':
            if (key in responses or (key in requests and requests[key] != row)
                    or row.get('catalog') != skill or 'block_sha256' not in row
                    or row['block_sha256'] != block or row.get('skill_sha256') != request.get('skill_sha256')
                    or not {'read_file', 'write_file', 'terminal'} <= set(row.get('tools', []))
                    or set(row['tools']) - allowed_tools):
                raise ValueError('Invalid native request receipt')
            # Hermes can repeat the receipt for an identical provider attempt.
            # Every retry must keep the original catalog and preload evidence.
            requests[key] = row
        elif row['phase'] == 'response':
            if key in responses or key not in requests or requests[key]['turn_id'] != turn:
                raise ValueError('Unmatched native provider response')
            responses[key] = row
        else:
            raise ValueError('Unknown Hermes receipt phase')
    if not responses or len({r['turn_id'] for r in requests.values()}) != 1:
        raise ValueError('No confirmed native response in the evaluated turn')
    return {'turn_id': next(iter(responses.values()))['turn_id'], 'catalog': skill,
            'skill_sha256': request.get('skill_sha256'), 'block_sha256': block,
            'request_ids': list(requests), 'response_ids': list(responses),
            'response_models': sorted({r['response_model'] for r in responses.values() if r.get('response_model')})}


def natural_skill_load(rows, request, receipts, *, session_id, turn_id, request_ids, profile):
    """Classify native Hermes skill_view evidence: exact load, no load, or unknown."""
    if not (request.get('mode') == 'trigger' and not request.get('attach_skill')
            and request.get('skill_path') and request.get('skill_id')):
        return {'status': 'unknown', 'skill_id': request.get('skill_id'), 'reason': 'not a natural-selection request'}
    calls = {}
    for row in rows:
        if row.get('type') not in {'tool_use', 'tool_result'}:
            continue
        key = row.get('tool_call_id') or row.get('name')
        if not key:
            return {'status': 'unknown', 'skill_id': request['skill_id'], 'reason': 'missing native tool call id'}
        if row['type'] == 'tool_use':
            if key in calls:
                return {'status': 'unknown', 'skill_id': request['skill_id'], 'reason': 'duplicate native tool call'}
            calls[key] = {'use': row, 'result': None}
        elif row.get('name') == 'skill_view' and key not in calls:
            return {'status': 'unknown', 'skill_id': request['skill_id'], 'reason': 'orphan native skill result'}
        elif key in calls:
            if calls[key]['result'] is not None:
                return {'status': 'unknown', 'skill_id': request['skill_id'], 'reason': 'duplicate native tool result'}
            calls[key]['result'] = row
    relevant = [entry for entry in calls.values()
                if entry['use'].get('name') == 'skill_view']
    if any(entry['use'].get('name') in worker.BLOCKED_SKILL_TOOLS for entry in calls.values()):
        return {'status': 'unknown', 'skill_id': request['skill_id'],
                'reason': 'a blocked skill-management tool was attempted'}
    if not relevant:
        if receipts:
            return {'status': 'unknown', 'skill_id': request['skill_id'],
                    'reason': 'private load receipt has no matching native tool event'}
        return {'status': 'not_loaded', 'skill_id': request['skill_id']}
    expected = Path(request['skill_path']).read_text(encoding='utf-8-sig', errors='replace')
    for entry in relevant:
        use, result = entry['use'], entry['result']
        args = use.get('input')
        call_id = use.get('tool_call_id')
        if (not isinstance(args, dict) or args.get('name') != request['skill_id'] or args.get('file_path') is not None
                or result is None or result.get('name') != 'skill_view' or result.get('is_error') or not call_id):
            continue
        matching = [row for row in receipts if row.get('schema_version') == 1
                    and row.get('run_id') == request['run_id'] and row.get('profile') == str(profile)
                    and row.get('tool_call_id') == call_id
                    and row.get('tool_name') == 'skill_view' and row.get('skill_id') == request['skill_id']
                    and row.get('returned_name') == request['skill_id']
                    and row.get('session_id') == session_id and row.get('turn_id') == turn_id
                    and row.get('api_request_id') in request_ids and row.get('skill_sha256') == request['skill_sha256']
                    and row.get('content_sha256') == worker.sha(expected.encode('utf-8'))
                    and row.get('status') == 'ok' and row.get('success') is True and row.get('exact_content') is True
                    and row.get('receipt_sha256') == worker.sha(json.dumps(
                        {key: value for key, value in row.items() if key != 'receipt_sha256'},
                        sort_keys=True, separators=(',', ':')).encode('utf-8'))]
        if len(matching) == 1:
            return {'status': 'loaded', 'skill_id': request['skill_id'],
                    'content_sha256': worker.sha(expected.encode('utf-8')),
                    'tool_call_id': call_id, 'receipt_sha256': matching[0]['receipt_sha256']}
    return {'status': 'unknown', 'skill_id': request['skill_id'],
            'reason': 'skill_view was attempted but exact successful content was not confirmed'}


def verify_saved_run(path, run):
    for name in ('request.json', 'events.jsonl', 'hermes-proof.json', 'hermes-requests.json', 'response.txt'):
        if digest(path / name) != run['evidence_hashes'].get(name):
            raise ValueError(f'Hermes evidence hash mismatch: {name}')
    request = read_json(path / 'request.json')
    proof = read_json(path / 'hermes-proof.json')
    schema = proof.get('schema_version')
    if schema == 1:
        # The original runner recorded its validated preload hash in the proof.
        block = proof['block_sha256']
    elif schema == 2:
        block = request['preload_block_sha256']
    else:
        raise ValueError('Unsupported Hermes proof schema')
    event_rows = read_lines(path / 'events.jsonl')
    native, _ = native_events(event_rows)
    observed = check_requests(read_json(path / 'hermes-requests.json'), request,
                              native['session_id'], run['profile_home'], block=block)
    selection = natural_skill_load(event_rows, request, read_json(path / 'natural-load-receipts.json'),
                                   session_id=native['session_id'], turn_id=observed['turn_id'],
                                   request_ids=observed['request_ids'], profile=run['profile_home']) if request.get('mode') == 'trigger' else None
    expected_trigger = (selection['status'] == 'loaded' if selection and selection['status'] != 'unknown' else None)
    output_files = regular_artifacts(path / 'outputs')
    if (output_files.pop('_response.txt', None) != digest(path / 'response.txt')
            or request['run_id'] != run['run_id'] or request['model'] != run['model']
            or proof['run_id'] != run['run_id'] or proof['session_id'] != run['session_id']
            or proof['profile'] != run['profile_home'] or native['session_id'] != run['session_id']
            or native.get('exit_code') != 0 or proof['turn_id'] != run['turn_id']
            or proof['outputs'] != output_files or any(proof.get(key) != value for key, value in observed.items())
            or selection is not None and proof.get('natural_load_receipts_sha256') != digest(path / 'natural-load-receipts.json')
            or selection is not None and proof.get('natural_selection') != selection
            or selection is not None and (run.get('skill_selection') != selection or run.get('triggered') != expected_trigger)
            or proof['sandbox'] != {'read_input': 0, 'write_output': 0, 'deny_input_write': 1,
                                   'deny_host_read': 1, 'deny_network': 1}):
        raise ValueError('Hermes proof or output artifacts do not match the run')
    return proof


def native_events(rows):
    initial = [row for row in rows if row.get('type') == 'system' and row.get('subtype') == 'init']
    final = [row for row in rows if row.get('type') == 'result']
    if len(initial) != 1 or len(final) != 1 or not final[0].get('session_id'):
        raise ValueError('Missing unique native Hermes start/result')
    parts = []
    tools = {}
    for row in rows:
        kind = row.get('type')
        if kind == 'tool_use':
            key = row.get('tool_call_id') or row['name']
            part = {'type': 'tool', 'name': row['name'], 'id': key,
                    'state': {'input': row.get('input', {}), 'status': 'running'}}
            tools[key] = part
            parts.append(part)
        if kind == 'tool_result' and (key := row.get('tool_call_id') or row['name']) in tools:
            tools[key]['state'].update(
                status='error' if row.get('is_error') else 'completed',
                output=row.get('output', row.get('content', '')))
    if final[0].get('text'):
        parts.append({'type': 'text', 'text': final[0]['text']})
    return final[0], parts


def validate_output_schema(value, schema, path='$'):
    """Validate the small JSON Schema subset used for trusted optimizer output."""
    if not isinstance(schema, dict) or not isinstance(schema.get('type'), str):
        raise ValueError(f'Invalid output schema at {path}')
    kind = schema['type']
    allowed = {
        'object': {'type', 'properties', 'required', 'additionalProperties'},
        'array': {'type', 'items', 'maxItems'},
        'string': {'type'}, 'integer': {'type'}, 'number': {'type'},
        'boolean': {'type'}, 'null': {'type'},
    }
    if kind not in allowed or set(schema) - allowed[kind]:
        raise ValueError(f'Unsupported output schema at {path}')
    if kind == 'object':
        properties = schema.get('properties', {})
        required = schema.get('required', [])
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or len(required) != len(set(required))
                or set(required) - set(properties)
                or schema.get('additionalProperties', False) is not False):
            raise ValueError(f'Invalid object schema at {path}')
        if not isinstance(value, dict) or set(required) - set(value) or set(value) - set(properties):
            raise ValueError(f'Output does not match object schema at {path}')
        for key, item in value.items():
            validate_output_schema(item, properties[key], f'{path}.{key}')
        return
    if kind == 'array':
        maximum = schema.get('maxItems')
        if (not isinstance(value, list) or not isinstance(schema.get('items'), dict)
                or maximum is not None and (type(maximum) is not int or maximum < 0)
                or maximum is not None and len(value) > maximum):
            raise ValueError(f'Output does not match array schema at {path}')
        for index, item in enumerate(value):
            validate_output_schema(item, schema['items'], f'{path}[{index}]')
        return
    valid = {
        'string': lambda item: isinstance(item, str),
        'integer': lambda item: type(item) is int,
        'number': lambda item: type(item) in {int, float},
        'boolean': lambda item: type(item) is bool,
        'null': lambda item: item is None,
    }[kind](value)
    if not valid:
        raise ValueError(f'Output does not match {kind} schema at {path}')


def require_structured_output(text, schema):
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError('Hermes response is not a single JSON value') from error
    validate_output_schema(value, schema)
    return value


def binary():
    configured = os.environ.get('SKILLCRAFT_HERMES_BIN', 'hermes')
    value = shutil.which(configured)
    if not value:
        raise ValueError('Hermes CLI is unavailable. Set SKILLCRAFT_HERMES_BIN to the local Hermes executable.')
    return str(Path(value).resolve())


def provider_config(source, home, provider):
    source_config = source / 'config.yaml'
    import yaml
    selected = (yaml.safe_load(source_config.read_text()) or {}) if source_config.is_file() else {}
    agent = selected.get('agent') or {}
    settings = {key: agent[key] for key in ('reasoning_effort', 'service_tier')
                if isinstance(agent.get(key), str) and agent[key]}
    if provider == 'openai-codex':
        auth = read_json(source / 'auth.json')
        data = {key: {provider: value[provider]} for key in ('providers', 'credential_pool')
                if isinstance((value := auth.get(key)), dict) and provider in value}
        if not data:
            raise ValueError('The selected Hermes profile has no ChatGPT sign-in')
        write_profile_auth(home, {'version': auth.get('version', 1), **data, 'active_provider': provider})
        # Hermes refreshes rotating OAuth credentials. Each run reads from the source
        # profile and keeps any updates in its disposable copy.
        return [], settings
    if provider == 'custom':
        model = selected.get('model')
        base = model.get('base_url') if isinstance(model, dict) else None
        if isinstance(base, str) and re.fullmatch(r'http://127\.0\.0\.1:[0-9]+/v1/?', base):
            return ['--base-url', base, '--api-key', 'skillcraft-loopback'], settings
    raise ValueError('Hermes evaluation supports openai-codex and an explicit loopback custom model')


def seed_dependencies(source, home):
    # Hermes accepts dependency facts only inside the selected data root.
    # APFS clones its existing generation without sharing writable profile state.
    roots = list(dict.fromkeys([source, source.parent.parent] if source.parent.name == 'profiles' else [source]))
    for root in roots:
        for path in (root / 'installs').glob('*/facts.json'):
            facts = read_json(path)
            dependency = facts.get('packages', {}).get('venv')
            if not isinstance(dependency, dict):
                continue
            environment = Path(dependency.get('environment', ''))
            lock = Path(dependency.get('resolved_lock', ''))
            if (environment / 'bin/python').is_file() and lock.is_file():
                generation = environment.parent
                if not lock.is_relative_to(generation):
                    continue
                install = home / 'installs' / path.parent.name
                target = install / 'environments' / generation.name
                target.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(['/bin/cp', '-cR', str(generation), str(target)], check=True,
                               capture_output=True, timeout=45)
                copied = {**dependency, 'environment': str(target / environment.relative_to(generation)),
                          'resolved_lock': str(target / lock.relative_to(generation))}
                write_json(install / 'facts.json', {'schema': facts['schema'], 'packages': {'venv': copied}})


def sandbox_test(request, outside):
    directory = Path(request['directory'])
    cover = directory / 'inputs' / '.seatbelt-test'
    cover.write_text('skillcraft')
    try:
        env = worker.Environment(request, 10)
        commands = {
            'read_input': 'cat inputs/.seatbelt-test',
            'write_output': 'echo skillcraft > outputs/.seatbelt-test',
            'deny_input_write': 'echo unexpected > inputs/.seatbelt-test',
            'deny_host_read': f'cat {shlex.quote(str(outside))}',
        }
        evidence = {name: env.execute(command) for name, command in commands.items()}
        with socket.socket() as check:
            check.bind(('127.0.0.1', 0))
            check.listen()
            port = check.getsockname()[1]
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                pass
            evidence['deny_network'] = env.execute(f'/usr/bin/nc -z -G 1 127.0.0.1 {port}')
        if (evidence['read_input'] != {'output': 'skillcraft', 'returncode': 0}
                or evidence['write_output']['returncode'] != 0
                or evidence['deny_network']['returncode'] != 1
                or not all(evidence[k]['returncode'] != 0 and 'Operation not permitted' in evidence[k]['output']
                           for k in ('deny_input_write', 'deny_host_read'))):
            raise ValueError(f'macOS Seatbelt capability checks failed: {evidence}')
        return {name: value['returncode'] for name, value in evidence.items()}
    finally:
        cover.unlink(missing_ok=True)
        (directory / 'outputs/.seatbelt-test').unlink(missing_ok=True)


def invoke(command, cwd, env, timeout):
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except BaseException:
        worker.stop_process(proc)
        proc.communicate()
        raise
    finally:
        proc.stdout.close()
        proc.stderr.close()


@contextmanager
def private_profile(parent):
    directory = Path(tempfile.mkdtemp(prefix='hermes-', dir=parent)).resolve()
    failure = None
    try:
        yield directory
    except BaseException as error:
        failure = error
        raise
    finally:
        home = directory / 'profile'
        (home / 'auth.json').unlink(missing_ok=True)
        if gateway_lock_active(home):
            detail = f'Private Hermes gateway is still active. Preserved its runtime directory: {directory}'
            if failure is None:
                raise ValueError(detail)
            failure.add_note(detail)
        else:
            for attempt in range(10):
                try:
                    shutil.rmtree(directory)
                    break
                except OSError as error:
                    if error.errno != errno.ENOTEMPTY or attempt == 9:
                        raise
                    time.sleep(0.1)


def gateway_lock_active(home):
    try:
        fd = os.open(home / 'gateway.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('Unsafe private Hermes gateway lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def gateway_record_pid(path, home):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('Unsafe private Hermes gateway identity record')
        try:
            record = json.load(stream)
        except json.JSONDecodeError:
            return None  # The gateway may still be publishing its record.
    if not isinstance(record, dict) or record.get('hermes_home') != str(home):
        raise ValueError('Private Hermes gateway identity does not match evaluation profile')
    pid = record.get('pid')
    return pid if type(pid) is int and pid > 1 else None


def stop_private_gateway(launcher, directory, env, home):
    deadline = time.monotonic() + 26  # Keep cleanup inside the runner's cancellation grace period.
    outcome = 'no owned gateway identity'
    requested = False
    while gateway_lock_active(home):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f'Private Hermes gateway did not release its runtime lock ({outcome})')
        # Hermes can remove the PID file before releasing the runtime lock. Its
        # native stop command verifies the live process identity from either file.
        if not requested:
            pid = gateway_record_pid(home / 'gateway.pid', home)
            owner = gateway_record_pid(home / 'gateway.lock', home)
            if (pid or owner) and (not pid or not owner or pid == owner):
                requested = True
                try:
                    code, _, _ = invoke([launcher, 'gateway', 'stop'], directory, env, min(20, remaining))
                except subprocess.TimeoutExpired:
                    outcome = 'native stop timed out'
                else:
                    outcome = f'native stop exited with status {code}'
        time.sleep(min(0.15, remaining))


def run(request):
    output, directory = Path(request['output']), Path(request['directory'])
    response = {'schema_version': 1, 'harness': 'hermes', 'status': 'runner_error',
                'run_id': request['run_id'], 'model': request['model'], 'triggered': None}
    started = time.monotonic()
    if platform.system() != 'Darwin' or not Path(worker.SANDBOX).is_file():
        raise ValueError('Hermes bounded-file evaluation needs macOS /usr/bin/sandbox-exec')
    secure_directory(directory)
    require_private(directory.parent)
    require_private(directory)
    launcher = binary()
    natural = request.get('mode') == 'trigger' and not request.get('attach_skill')
    if request.get('mode') == 'trigger' and not natural:
        raise ValueError('Hermes trigger evaluation must use natural skill selection')
    block = worker.sha(worker.native_block(request).encode()) if request.get('skill_path') and not natural else None
    write_json(output / 'request.json', {**request, 'preload_block_sha256': block})
    source = Path(os.environ.get('SKILLCRAFT_HERMES_HOME', os.environ.get('HERMES_HOME', '~/.hermes'))).expanduser().resolve()
    provider, model = request['model']['provider'], request['model']['id']
    (directory / '.tool-tmp').mkdir(mode=0o700)
    with private_profile(directory.parent) as private:
        home = private / 'profile'
        home.mkdir(mode=0o700)
        (home / '.no-bundled-skills').touch()
        plugin = home / 'plugins' / PLUGIN
        plugin.mkdir(parents=True)
        shutil.copyfile(worker.__file__, plugin / '__init__.py')
        (plugin / 'plugin.yaml').write_text('name: skillcraft_eval\nversion: 0.1.0\ndescription: SkillCraft evaluation\n')
        write_json(home / 'skillcraft-eval.json', {key: request[key] for key in
                   ('directory', 'run_id', 'mode', 'attach_skill', 'skill_id', 'skill_path', 'skill_sha256') if key in request})
        outside = private / 'outside'
        outside.write_text('sandbox canary')
        sandbox = sandbox_test(request, outside)
        provider_args, model_settings = provider_config(source, home, provider)
        seed_dependencies(source, home)
        config = {
            'model': {'provider': provider, 'default': model},
            'plugins': {'enabled': [PLUGIN]},
            'tools': {'tool_search': {'enabled': 'off'}},
            'terminal': {'backend': worker.BACKEND, 'cwd': str(directory), 'timeout': min(60, request['timeout_seconds'])},
            'agent': {'max_turns': 16, 'run_budget_seconds': request['timeout_seconds'], **model_settings},
            'memory': {'memory_enabled': False, 'user_profile_enabled': False},
            'security': {'allow_lazy_installs': False},
            'skills': {'template_vars': False, 'inline_shell': False, 'auto_load': [],
                       'external_dirs': [str(Path(request['skill_path']).parent.parent)] if request.get('skill_path') else []},
        }
        if provider_args:
            config['model']['base_url'] = provider_args[1]
        versioned = home / 'config.yaml'
        write_json(versioned, config)  # YAML accepts JSON
        (home / 'SOUL.md').write_text(request['instructions'] + '\n')
        prompt = private / 'query.txt'
        prompt.write_text(request['prompt'], encoding='utf-8')
        bundled = private / 'bundled-skills'
        bundled.mkdir()
        env = {'HERMES_BUNDLED_SKILLS': str(bundled), 'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', 'HOME': str(private),
               'HERMES_HOME': str(home), 'HERMES_DISABLE_LAZY_INSTALLS': '1', 'PYTHONDONTWRITEBYTECODE': '1',
               'HERMES_GATEWAY_LOCK_DIR': str(private / 'gateway-locks'),
               'SKILLCRAFT_LEARNING_INTERNAL': '1', 'LANG': 'en_US.UTF-8'}
        failure = None
        try:
            version_code, version, _ = invoke([launcher, '--version'], directory, env, min(30, request['timeout_seconds']))
            if version_code != 0 or not (match := re.search(r'Hermes Agent\s+([^\s]+)', version, re.I)):
                raise ValueError('The isolated Hermes CLI did not report an installed version')
            command = [launcher, 'chat', '--query-file', str(prompt), '--format', 'stream-json',
                       '--in', str(directory), '--ignore-rules', '--provider', provider, '--model', model,
                       '--toolsets', 'file,terminal,skills' if request.get('mode') == 'trigger' else 'file,terminal', *provider_args]
            if request.get('skill_path') and request.get('mode') != 'trigger' and request.get('attach_skill', True):
                command += ['--skills', request['skill_id']]
            code, stdout, stderr = invoke(command, directory, env, request['timeout_seconds'])
            events = output / 'events.jsonl'
            events.write_text(stdout, encoding='utf-8')
            if code != 0:
                # CLI failures can carry credential/provider details in stderr.
                raise ValueError(f'Native Hermes CLI exited with status {code}; inspect stream events')
            final, parts = native_events(read_lines(events))
            usage = final.get('tokens') or {}
            executor = {'agent_role': 'executor', 'total_tokens': usage.get('total'), 'cost': None,
                        'tokens': {'input': usage.get('input'), 'output': usage.get('output'),
                                   'cache': {'read': usage.get('cache_read'), 'write': usage.get('cache_write')}}}
            write_json(output / 'usage.json', {'schema_version': 1, 'executor': executor, 'root_executor': executor})
            write_json(output / 'timing.json', {'total_duration_seconds': time.monotonic() - started})
            if final.get('exit_code') != 0:
                raise ValueError('Hermes did not complete the native turn')
            if request.get('output_schema') is not None:
                require_structured_output(final.get('text'), request['output_schema'])
            session = final['session_id']
            receipts = read_lines(home / 'requests.jsonl')
            write_json(output / 'hermes-requests.json', receipts)
            proof = check_requests(receipts, request, session, home, block=block)
            natural_receipts = read_lines(home / 'natural-load-receipts.jsonl') if (home / 'natural-load-receipts.jsonl').exists() else []
            write_json(output / 'natural-load-receipts.json', natural_receipts)
            selection = natural_skill_load(read_lines(events), request, natural_receipts,
                                           session_id=session, turn_id=proof['turn_id'],
                                           request_ids=proof['request_ids'], profile=home) if request.get('mode') == 'trigger' else None
            if selection is not None:
                proof['natural_selection'] = selection
                proof['natural_load_receipts_sha256'] = digest(output / 'natural-load-receipts.json')
            proof.update(schema_version=2, run_id=request['run_id'], session_id=session, profile=str(home),
                         sandbox=sandbox, outputs=regular_artifacts(directory / 'outputs'))
            write_json(output / 'hermes-proof.json', proof)
            environment = {'server_version': match[1], 'candidate': {'id': request['skill_id'], 'path': request['skill_path']}
                           if request.get('skill_path') else None, 'background': False, 'plugins': [PLUGIN],
                           'controlled': True, 'model_settings': model_settings, 'configuration_fingerprint': worker.sha(json.dumps({
                               'terminal': worker.BACKEND, 'provider': provider, 'model': model,
                               'base_url': config['model'].get('base_url'), 'model_settings': model_settings,
                               'toolsets': ['file', 'terminal', 'skills'] if request.get('mode') == 'trigger' else ['file', 'terminal'],
                               'timeout': request['timeout_seconds']}, sort_keys=True).encode())}
            write_json(output / 'environment.json', environment)
            write_json(output / 'messages.json', [
                {'id': proof['turn_id'], 'type': 'user', 'content': [{'type': 'text', 'text': request['prompt']}]},
                {'id': session, 'type': 'assistant', 'content': parts}])
            (output / 'response.txt').write_text(final.get('text') or '', encoding='utf-8')
            response.update(status='completed', environment=environment,
                            attached=bool(request.get('skill_path') and request.get('attach_skill', True)),
                            session_id=session, turn_id=proof['turn_id'], profile_home=str(home),
                            triggered=(selection['status'] == 'loaded' if selection and selection['status'] != 'unknown' else None)
                                      if selection is not None else None,
                            skill_selection=selection,
                            response_models=proof['response_models'],
                            duration_ms=final.get('duration_ms'), usage={
                                key: usage[key] for key in ('input', 'output', 'total', 'cache_read', 'cache_write') if key in usage})
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                stop_private_gateway(launcher, directory, env, home)
            except (OSError, ValueError, subprocess.SubprocessError) as cleanup:
                if failure is None:
                    raise
                failure.add_note(str(cleanup))
    return {**response, 'duration_ms': response.get('duration_ms') or round((time.monotonic() - started) * 1000)}


def _terminate(signum, frame):
    raise KeyboardInterrupt


def main():
    request = read_json(Path(sys.argv[1]))
    output = Path(request['output'])
    signal.signal(signal.SIGTERM, _terminate)
    try:
        result = run(request)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        status = ('timeout' if isinstance(error, subprocess.TimeoutExpired) else
                  'cancelled' if isinstance(error, KeyboardInterrupt) else 'runner_error')
        result = {'schema_version': 1, 'status': status,
                  'error': '\n'.join((str(error) or status, *getattr(error, '__notes__', ()))),
                  'run_id': request['run_id'], 'harness': 'hermes', 'triggered': None}
    write_json(output / 'run.json', result)
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
