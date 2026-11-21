#!/usr/bin/env python3
"""Conservatively observe Codex hook messages for opt-in SkillCraft learning."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

SUBPROCESS_TIMEOUT_SECONDS = 4


def read_state(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.last-assistant-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def python_executable(plugin_root):
    configured = os.environ.get('SKILLCRAFT_VENV')
    if configured:
        candidate = Path(configured) / 'bin/python'
        return str(candidate) if candidate.is_file() else None
    xdg_data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    candidates = [
        plugin_root / 'skills/skillcraft/.venv/bin/python',
        xdg_data / 'skillcraft/venv/bin/python',
        Path.home() / '.local/share/skillcraft/venv/bin/python',
    ]
    return str(next((candidate for candidate in candidates if candidate.is_file()), Path(sys.executable)))


def inventory(python, script, project):
    try:
        result = subprocess.run(
            [python, '-B', str(script), 'inventory', project],
            cwd=project, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=SUBPROCESS_TIMEOUT_SECONDS, check=True, text=True,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def observe(python, script, payload):
    try:
        result = subprocess.run(
            [python, '-B', str(script), 'observe'],
            input=json.dumps(payload), text=True, cwd=payload['project'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            env=os.environ.copy(), check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def enabled(project):
    directory = Path(project).resolve()
    for root in (directory, *directory.parents):
        path = root / '.skillcraft/learning.json'
        if not path.exists():
            continue
        if path.is_symlink() or path.parent.is_symlink():
            return False
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        sources = config.get('sources', ['sessions', 'evals']) if isinstance(config, dict) else []
        return isinstance(config, dict) and config.get('schema_version') == 1 and config.get('enabled') is True and isinstance(sources, list) and 'sessions' in sources
    return False


def command_skill_path(item, cwd, candidates):
    parts = item.get('parsed_cmd') or []
    if not isinstance(parts, list):
        return None
    if len(parts) == 1 and parts[0]['type'] == 'read':
        return (cwd / parts[0]['path']).resolve()

    # A wrapper may hide the read from the command parser. The actual
    # arguments and the parser's source text can still name the file.
    command = item.get('command')
    arguments = set(command[1:]) if isinstance(command, list) else set()
    for part in parts:
        source = part.get('cmd')
        if not isinstance(source, str):
            continue
        try:
            arguments.update(shlex.split(source, comments=True))
        except ValueError:
            continue
    paths = set()
    for argument in arguments:
        path = Path(argument)
        if path.name == 'SKILL.md':
            resolved = (cwd / path).resolve()
            if resolved in candidates:
                paths.add(resolved)
    return next(iter(paths)) if len(paths) == 1 else None


def confirmed_reads(path, session, turn, versions):
    if not isinstance(path, str) or not versions:
        return []
    matches = {}
    expected = {}
    for version in versions:
        expected.setdefault(version['hash'], {})[Path(version['path'])] = version
    # Native completion records include their own thread and turn identifiers.
    try:
        transcript = Path(path)
        if not transcript.is_file():
            return []
        with transcript.open('rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            if size > 8 * 1024 * 1024:
                stream.seek(-8 * 1024 * 1024, os.SEEK_END)
                stream.readline()
            else:
                stream.seek(0)
            lines = stream.read(8 * 1024 * 1024).splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
            payload = row['payload']
            if (row['type'] != 'event_msg' or payload['type'] != 'item_completed'
                    or payload['thread_id'] != session or payload['turn_id'] != turn):
                continue
            item = payload['item']
            if item['type'] != 'CommandExecution' or item['status'] != 'completed' or item['exit_code'] != 0:
                continue
            cwd = urlsplit(item['cwd'])
            if cwd.scheme not in {'', 'file'} or cwd.netloc not in {'', 'localhost'} or not cwd.path.startswith('/'):
                continue
            output = item['aggregated_output']
            if not isinstance(output, str):
                continue
            candidates = expected.get(hashlib.sha256(output.encode()).hexdigest())
            if candidates:
                source = command_skill_path(item, Path(unquote(cwd.path)), candidates)
                if source in candidates:
                    matches[source] = candidates[source]
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            continue
    return list(matches.values())


def completed_model_steps(path, session, turn):
    """Infer a conservative lower bound from older Codex rollout records.

    Rollout files are a private, versioned format. Fail closed when the file
    is unavailable or the turn has an explicit abort marker.
    """
    if not isinstance(path, str):
        return None
    try:
        transcript = Path(path)
        if not transcript.is_file():
            return None
        with transcript.open('rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            if size > 8 * 1024 * 1024:
                stream.seek(-8 * 1024 * 1024, os.SEEK_END)
                stream.readline()
            else:
                stream.seek(0)
            lines = stream.read(8 * 1024 * 1024).splitlines()
    except OSError:
        return None

    saw_completed_message = False
    for line in lines:
        try:
            row = json.loads(line)
            payload = row.get('payload', {})
            if (row.get('type') != 'event_msg' or payload.get('thread_id') != session
                    or payload.get('turn_id') != turn):
                continue
            if payload.get('type') in {'turn_aborted', 'turn_failed', 'turn_interrupted'}:
                return None
            if payload.get('type') != 'item_completed':
                continue
            item = payload.get('item')
            if not isinstance(item, dict) or item.get('type') != 'AgentMessage':
                continue
            saw_completed_message = True
            # An AgentMessage does not prove a one-to-one model-call boundary.
            # Older runtimes lack PostModelRequest IDs, so use one lower-bound
            # step instead of counting message parts.
        except (AttributeError, TypeError, ValueError):
            continue
    return 1 if saw_completed_message else None


def aborted_turn(path, session, turn):
    if not isinstance(path, str):
        return False
    try:
        transcript = Path(path)
        if not transcript.is_file():
            return False
        with transcript.open('rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            if size > 8 * 1024 * 1024:
                stream.seek(-8 * 1024 * 1024, os.SEEK_END)
                stream.readline()
            else:
                stream.seek(0)
            lines = stream.read(8 * 1024 * 1024).splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            row = json.loads(line)
            payload = row.get('payload', {})
            if (row.get('type') == 'event_msg' and payload.get('thread_id') == session
                    and payload.get('turn_id') == turn and payload.get('type') == 'turn_aborted'):
                return True
        except (AttributeError, TypeError, ValueError):
            continue
    return False


def completed_model_request(event, project, session, turn, state_path, state):
    """Persist successful main-thread model request IDs, without response text."""
    if (event.get('hook_event_name') != 'PostModelRequest'
            or event.get('status') != 'completed'
            or event.get('kind') != 'sampling'
            or event.get('agent_id') or event.get('agent_type')
            or state.get('pending_turn') != turn):
        return
    request_id = event.get('request_id')
    if not isinstance(request_id, str) or not request_id:
        return
    requests = state.get('completed_model_requests', {})
    requests = requests if isinstance(requests, dict) else {}
    turn_requests = requests.get(turn, [])
    turn_requests = [item for item in turn_requests if isinstance(item, str)] if isinstance(turn_requests, list) else []
    if request_id in turn_requests:
        return
    requests = {turn: [*turn_requests, request_id]}
    write_state(state_path, {**state, 'completed_model_requests': requests})


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0

    if not isinstance(event, dict) or os.environ.get('SKILLCRAFT_LEARNING_INTERNAL') == '1':
        return 0
    plugin_root = Path(os.environ.get('PLUGIN_ROOT') or Path(__file__).resolve().parents[2]).resolve()
    script = plugin_root / 'skills/skillcraft/scripts/learning.py'
    project = event.get('cwd')
    session = event.get('session_id')
    turn = event.get('turn_id')
    event_name = event.get('hook_event_name')
    if not script.is_file() or not isinstance(project, str) or not isinstance(session, str) or not isinstance(turn, str):
        return 0
    if not enabled(project):
        return 0
    data_root = os.environ.get('PLUGIN_DATA')
    data_dir = Path(data_root).resolve() if data_root else None
    session_key = hashlib.sha256(session.encode()).hexdigest()
    state_path = data_dir / 'learning-capture' / f'{session_key}.json' if data_dir else None
    state = read_state(state_path) if state_path else {}
    if event_name == 'PostModelRequest':
        if state_path:
            completed_model_request(event, project, session, turn, state_path, state)
        return 0

    python = python_executable(plugin_root)
    versions = inventory(python, script, project) if python else None
    if versions is None:
        return 0
    loaded = []
    diagnostic = 'No complete skill read could be verified in this Codex turn.'

    if event_name == 'UserPromptSubmit':
        prompt_turns = state.get('prompt_turns', [])
        if not isinstance(prompt_turns, list):
            prompt_turns = []
        if turn in prompt_turns or state.get('pending_turn') == turn or state.get('last_assistant_turn') == turn:
            return 0
        # A newer prompt supersedes any unfinished turn; confirmation cannot
        # be attributed to an older completed answer across that gap.
        target_turn = None if state.get('pending_turn') else state.get('last_assistant_turn')
        accepted = observe(python, script, {
            'project': project,
            'harness': 'codex',
            'session': session,
            'turn': turn,
            'role': 'user',
            'text': event.get('prompt', '') if isinstance(event.get('prompt'), str) else '',
            'loaded': loaded,
            'target_turn': target_turn if isinstance(target_turn, str) else None,
            'diagnostic': diagnostic if not target_turn else None,
            'defer_feedback': True,
        })
        if accepted and state_path:
            recent_turns = [item for item in prompt_turns if isinstance(item, str)] + [turn]
            write_state(state_path, {
                **state,
                'pending_turn': turn,
                'completed_model_requests': {},
                'versions': versions,
                'prompt_turns': recent_turns,
            })
    elif event_name == 'Stop':
        text = event.get('last_assistant_message')
        # Only accept a Stop for the prompt currently pending. Delayed or
        # duplicate Stops must not replace a newer pending turn or its target.
        if not isinstance(text, str) or not text or state.get('pending_turn') != turn:
            return 0
        if aborted_turn(event.get('transcript_path'), session, turn):
            return 0
        unchanged = [version for version in versions if version in state.get('versions', [])]
        loaded = confirmed_reads(event.get('transcript_path'), session, turn, unchanged)
        model_requests = state.get('completed_model_requests', {})
        model_requests = model_requests.get(turn, []) if isinstance(model_requests, dict) else []
        model_steps = len({item for item in model_requests if isinstance(item, str)}) if isinstance(model_requests, list) else 0
        if model_steps <= 0:
            model_steps = completed_model_steps(event.get('transcript_path'), session, turn)
        accepted = observe(python, script, {
            'project': project,
            'harness': 'codex',
            'session': session,
            'turn': turn,
            'role': 'assistant',
            'text': text,
            'loaded': loaded,
            'diagnostic': diagnostic if not loaded else None,
            **({'model_steps': model_steps} if model_steps is not None else {}),
        })
        if accepted and state_path:
            prompt_turns = state.get('prompt_turns', [])
            if not isinstance(prompt_turns, list):
                prompt_turns = []
            write_state(state_path, {
                'last_assistant_turn': turn,
                'prompt_turns': prompt_turns,
            })
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
