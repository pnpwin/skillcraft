#!/usr/bin/env python3
"""Conservatively observe Codex hook messages for opt-in SkillCraft learning."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit


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
            timeout=4, check=True, text=True,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def observe(python, script, payload):
    try:
        result = subprocess.run(
            [python, '-B', str(script), 'observe'],
            input=json.dumps(payload), text=True, cwd=payload['project'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4,
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


def confirmed_reads(path, session, turn, versions):
    if not isinstance(path, str) or not versions:
        return []
    matches = {}
    expected = {Path(item['path']): item for item in versions}
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
            parts = item['parsed_cmd']
            if not isinstance(parts, list) or len(parts) != 1 or parts[0]['type'] != 'read':
                continue
            cwd = urlsplit(item['cwd'])
            if cwd.scheme not in {'', 'file'} or cwd.netloc not in {'', 'localhost'} or not cwd.path.startswith('/'):
                continue
            source = (Path(unquote(cwd.path)) / parts[0]['path']).resolve()
            version = expected.get(source)
            output = item['aggregated_output']
            if version and isinstance(output, str) and hashlib.sha256(output.encode()).hexdigest() == version['hash']:
                matches[source] = version
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            continue
    return list(matches.values())


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0

    if not isinstance(event, dict) or os.environ.get('SKILLCRAFT_LEARNING_INTERNAL') == '1':
        return 0
    plugin_root = Path(os.environ.get('PLUGIN_ROOT') or Path(__file__).resolve().parents[1]).resolve()
    script = plugin_root / 'skills/skillcraft/scripts/learning.py'
    project = event.get('cwd')
    session = event.get('session_id')
    turn = event.get('turn_id')
    event_name = event.get('hook_event_name')
    if not script.is_file() or not isinstance(project, str) or not isinstance(session, str) or not isinstance(turn, str):
        return 0
    if not enabled(project):
        return 0
    python = python_executable(plugin_root)
    versions = inventory(python, script, project) if python else None
    if versions is None:
        return 0

    data_root = os.environ.get('PLUGIN_DATA')
    data_dir = Path(data_root).resolve() if data_root else None
    session_key = hashlib.sha256(session.encode()).hexdigest()
    state_path = data_dir / 'learning-capture' / f'{session_key}.json' if data_dir else None
    state = read_state(state_path) if state_path else {}
    loaded = []
    diagnostic = 'No complete direct skill read could be verified in this Codex turn.'

    if event_name == 'UserPromptSubmit':
        target_turn = state.get('last_assistant_turn')
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
        })
        if accepted and state_path and state.get('pending_turn') != turn:
            write_state(state_path, {**state, 'pending_turn': turn, 'versions': versions})
    elif event_name == 'Stop':
        text = event.get('last_assistant_message')
        if not isinstance(text, str) or not text:
            return 0
        if state.get('pending_turn') == turn:
            unchanged = [version for version in versions if version in state.get('versions', [])]
            loaded = confirmed_reads(event.get('transcript_path'), session, turn, unchanged)
        accepted = observe(python, script, {
            'project': project,
            'harness': 'codex',
            'session': session,
            'turn': turn,
            'role': 'assistant',
            'text': text,
            'loaded': loaded,
            'diagnostic': diagnostic if not loaded else None,
        })
        if accepted and state_path:
            write_state(state_path, {'last_assistant_turn': turn})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
