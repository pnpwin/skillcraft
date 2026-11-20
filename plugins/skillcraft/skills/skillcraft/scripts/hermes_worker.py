"""Trusted code installed only in a disposable Hermes evaluation profile.

Hermes imports this file as a native plugin. Keep its dependencies within the
standard library and the Hermes runtime, separate from SkillCraft's Python.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile


BACKEND = 'skillcraft_seatbelt'
SANDBOX = '/usr/bin/sandbox-exec'
TOOLS = frozenset({'terminal', 'read_file', 'write_file', 'patch', 'search_files'})
# Hermes advertises process_manage with the native terminal toolset. The
# pre-tool hook denies it, together with terminal's background modes.
ADVERTISED_TOOLS = TOOLS | {'process_manage'}
PRELOAD = re.compile(r'^\[IMPORTANT: The user launched this CLI session with the "([^"\n]+)" skill preloaded\.', re.M)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def policy(root, skill_path=None):
    root = Path(root).resolve()
    readable = [root / name for name in ('inputs', 'outputs', '.tool-tmp')]
    if skill_path:
        readable.append(Path(skill_path).parent.resolve())
    system = ('/System/Library', '/System/Cryptexes/OS', '/usr/lib', '/usr/share', '/usr/bin', '/bin',
              '/private/var/db/dyld', '/Library/Apple/usr/lib')
    read = ' '.join(f'(subpath {json.dumps(str(p))})' for p in (*system, *readable))
    write = ' '.join(f'(subpath {json.dumps(str(root / name))})' for name in ('outputs', '.tool-tmp'))
    parents = ' '.join(f'(literal {json.dumps(str(path))})' for path in (root, *root.parents))
    devices = ' '.join(f'(literal "/dev/{name}")' for name in ('null', 'zero', 'random', 'urandom'))
    return ('(version 1)(deny default)(allow process-exec process-fork)(allow sysctl-read)'
            f'(allow file-read* {parents} {read} {devices})(allow file-write* {write} (literal "/dev/null"))')


def stop_process(proc):
    # A command may fork even if its immediate shell has already exited.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


class Environment:
    uses_host_filesystem = False

    def __init__(self, spec, timeout):
        self.cwd = spec['directory']
        self.timeout = timeout
        self.spec = spec

    def execute(self, command, timeout=None, cwd=None, stdin_data=None, **kwargs):
        root = Path(self.cwd)
        target = (root / (cwd or '.')).resolve()
        if not target.is_relative_to(root):
            return {'output': 'Working directory is outside the run', 'returncode': 126}
        tmp = str(root / '.tool-tmp')
        environment = {'PATH': '/usr/bin:/bin', 'HOME': tmp, 'TMPDIR': tmp,
                       'LANG': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(
                [SANDBOX, '-p', policy(self.cwd, self.spec.get('skill_path')),
                 '/bin/bash', '--noprofile', '--norc', '-c', 'ulimit -f 32768 || exit 125; ' + command],
                cwd=target, env=environment, stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                data = stdin_data.encode() if isinstance(stdin_data, str) else stdin_data
                proc.communicate(input=data, timeout=min(timeout or self.timeout, self.timeout))
                code = proc.returncode
            except subprocess.TimeoutExpired:
                return {'output': 'Sandboxed command timed out', 'returncode': 124}
            finally:
                stop_process(proc)
            output.seek(0)
            data = output.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            return {'output': 'Sandboxed command exceeded the output limit', 'returncode': 1}
        return {'output': data.decode('utf-8', errors='replace'), 'returncode': code}

    def cleanup(self):
        pass


def request_text(system, messages):
    if isinstance(system, str):
        yield system
    for message in messages or ():
        if not isinstance(message, dict) or message.get('role') not in {'system', 'developer'}:
            continue
        content = message.get('content')
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    yield part['text']


def native_block(spec):
    name = spec['skill_id']
    path = Path(spec['skill_path'])
    if sha(path.read_bytes()) != spec['skill_sha256']:
        raise ValueError('Skill snapshot changed')
    body = path.read_text(encoding='utf-8-sig').strip()
    activation = (f'[IMPORTANT: The user launched this CLI session with the "{name}" skill '
                  'preloaded. Treat its instructions as active guidance for the duration of this '
                  'session unless the user overrides them.]')
    return f'{activation}\n\n{body}\n\n[Skill directory: {path.parent}]'


def preload_hash(spec, system, messages):
    texts = list(request_text(system, messages))
    markers = {name for text in texts for name in PRELOAD.findall(text)}
    if not spec.get('skill_path'):
        if markers:
            raise ValueError('Unexpected native skill preload')
        return None
    block = native_block(spec)
    if markers != {spec['skill_id']} or not any(block in text for text in texts):
        raise ValueError('Full native skill block was not present in the provider request')
    return sha(block.encode())


def register(ctx):
    from agent.terminal_env_provider import TerminalEnvironmentProvider
    from hermes_constants import get_hermes_home
    from tools.skills_tool import _skill_catalog, skills_list

    home = get_hermes_home().resolve()
    spec = json.loads((home / 'skillcraft-eval.json').read_text())
    receipt = home / 'requests.jsonl'

    class Seatbelt(TerminalEnvironmentProvider):
        name = BACKEND
        is_container = False

        def is_available(self):
            return Path(SANDBOX).is_file()

        def create_environment(self, *, cwd, timeout, **kwargs):
            return Environment(spec, timeout)

    ctx.register_terminal_environment_provider(Seatbelt())

    def append(row):
        with receipt.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, separators=(',', ':')) + '\n')

    def identity(phase, event):
        return {'phase': phase, 'run_id': spec['run_id'], 'profile': str(home),
                **{name: event.get(name) for name in ('session_id', 'turn_id', 'api_request_id', 'model', 'provider')}}

    def pre_api_request(**event):
        row = identity('request', event)
        try:
            listing = json.loads(skills_list())
            if listing.get('success') is not True:
                raise ValueError('Native skill catalog unavailable')
            row['catalog'] = sorted(s['name'] for s in listing['skills'])
            expected = [spec['skill_id']] if spec.get('skill_path') else []
            if row['catalog'] != expected:
                raise ValueError('Unexpected native skill catalog')
            candidates = [(s['load_name'], str(Path(s['path']).resolve()))
                          for s in _skill_catalog() if s['load_name']]
            if candidates != ([(spec['skill_id'], spec['skill_path'])] if expected else []):
                raise ValueError('Native skill resolves to another path')
            row['block_sha256'] = preload_hash(spec, event.get('system_prompt'), event.get('request_messages'))
            row['skill_sha256'] = spec.get('skill_sha256')
            request = event.get('request') or {}
            definitions = request.get('body', {}).get('tools')
            if not isinstance(definitions, list):
                raise ValueError('Native tool definitions unavailable')
            tool_names = {t.get('function', t).get('name') for t in definitions}
            row['tools'] = sorted(name for name in tool_names if isinstance(name, str))
            if not {'read_file', 'write_file', 'terminal'} <= tool_names or tool_names - ADVERTISED_TOOLS:
                raise ValueError('Unexpected tools in provider request')
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            row['error'] = str(error)
        append(row)

    def post_api_request(**event):
        append({**identity('response', event), 'response_model': event.get('response_model')})

    def pre_tool_call(tool_name, args, **event):
        from tools.terminal_tool import _get_env_config
        if (tool_name not in TOOLS or _get_env_config().get('env_type') != BACKEND
                or tool_name == 'terminal' and (args.get('background') or args.get('watch_patterns')
                                                or args.get('notify_on_complete'))):
            return {'action': 'block', 'message': 'Tool unavailable in the SkillCraft evaluation profile'}

    ctx.register_hook('pre_api_request', pre_api_request)
    ctx.register_hook('post_api_request', post_api_request)
    ctx.register_hook('pre_tool_call', pre_tool_call)
