"""Capture verified skill reads from local Hermes turns."""

from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from threading import Lock

import yaml

SCRIPT = Path(__file__).resolve().parent / 'skills/skillcraft/scripts/learning.py'
LOCAL_SURFACES = {'cli', 'tui', 'desktop'}
MAX_SESSIONS = 128
LOG = logging.getLogger(__name__)


def run_learning(home, verb, *args, payload=None):
    # The CLI owns its imports and SQLite state outside the gateway process.
    # Hermes may add the managed dependency environment to sys.path only.
    env = {**os.environ, 'PYTHONPATH': str(Path(yaml.__file__).resolve().parents[1])}
    try:
        result = subprocess.run(
            [sys.executable, '-B', str(SCRIPT), '--hermes-home', str(home.resolve()), verb, *args],
            input=json.dumps(payload) if payload is not None else '',
            capture_output=True, text=True, timeout=4, env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('SkillCraft journal command timed out') from error
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'SkillCraft journal command failed')
    return json.loads(result.stdout)


@dataclass
class Turn:
    id: str
    project: Path
    inventory: dict
    loaded: dict = field(default_factory=dict)
    finished: bool = False


class Learning:
    def __init__(self, context, get_home):
        self.context = context
        self.get_home = get_home
        self.sessions = OrderedDict()
        self.lock = Lock()

    def key(self, session_id, turn_id):
        if (self.context.session_surface() not in LOCAL_SURFACES
                or os.environ.get('SKILLCRAFT_LEARNING_INTERNAL') == '1'):
            return None
        if not all(isinstance(value, str) and value for value in (session_id, turn_id)):
            return None
        return (self.get_home().resolve(), session_id)

    def record(self, key, turn_id, project, role, text, *, loaded=(), target=None):
        return run_learning(key[0], 'observe', payload={
            'harness': 'hermes', 'session': key[1], 'turn': turn_id,
            'project': str(project), 'role': role, 'text': text,
            'target_turn': target, 'loaded': list(loaded),
        })['inserted']

    def start(self, *, session_id=None, turn_id=None, user_message=None,
              parent_session_id=None, **kwargs):
        key = self.key(session_id, turn_id)
        if not key or parent_session_id:
            return
        cwd = self.context.session_cwd()
        with self.lock:
            prior = self.sessions.get(key)
            if prior and prior.id == turn_id:
                return
            if not cwd or not isinstance(user_message, str) or not user_message.strip():
                self.sessions.pop(key, None)
                return
            project = Path(cwd).resolve()
            if not any((root / '.skillcraft/learning.json').is_file()
                       for root in (project, *project.parents)):
                self.sessions.pop(key, None)
                return
            try:
                versions = run_learning(key[0], 'inventory', str(project))
                target = prior.id if prior and prior.finished and prior.project == project else None
                if not self.record(key, turn_id, project, 'user', user_message, target=target):
                    return
            except (OSError, RuntimeError, ValueError, KeyError):
                self.sessions.pop(key, None)
                LOG.debug('Could not record the Hermes prompt in SkillCraft', exc_info=True)
                return
            self.sessions[key] = Turn(turn_id, project, {v['path']: v for v in versions})
            self.sessions.move_to_end(key)
            if len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)

    def tool(self, *, session_id=None, turn_id=None, tool_name=None, args=None,
             result=None, status=None, **kwargs):
        if tool_name != 'skill_view' or status != 'ok' or not isinstance(args, dict) or args.get('file_path'):
            return
        key = self.key(session_id, turn_id)
        if not key:
            return
        try:
            view = json.loads(result) if isinstance(result, str) else result
            if not isinstance(view, dict) or view.get('success') is not True:
                return
            directory = Path(view.get('skill_dir') or '.')
            content = view.get('content')
            if not directory.is_absolute() or not isinstance(content, str):
                return
            path = str((directory / 'SKILL.md').resolve())
            digest = sha256(content.encode()).hexdigest()
        except (OSError, ValueError, TypeError, RuntimeError):
            return
        with self.lock:
            turn = self.sessions.get(key)
            if turn and turn.id == turn_id and not turn.finished:
                entry = turn.inventory.get(path)
                if entry and digest == entry['hash']:
                    turn.loaded[path] = entry

    def finish(self, *, session_id=None, turn_id=None, assistant_response=None,
               conversation_history=None, **kwargs):
        key = self.key(session_id, turn_id)
        if not key or not isinstance(assistant_response, str) or not assistant_response.strip():
            return
        if not conversation_history:
            return
        last = conversation_history[-1]
        if (not isinstance(last, dict) or last.get('role') != 'assistant' or last.get('tool_calls')
                or last.get('phase') == 'commentary' or last.get('content') != assistant_response):
            return
        with self.lock:
            turn = self.sessions.get(key)
            if not turn or turn.id != turn_id or turn.finished:
                return
            try:
                turn.finished = self.record(key, turn.id, turn.project, 'assistant', assistant_response,
                                            loaded=turn.loaded.values())
            except (OSError, RuntimeError, ValueError, KeyError):
                LOG.debug('Could not record the Hermes reply in SkillCraft', exc_info=True)
