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

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/skillcraft/scripts/learning.py'
LOCAL_SURFACES = {'cli', 'tui', 'desktop'}
MAX_SESSIONS = 128
LOG = logging.getLogger(__name__)


def run_learning(home, verb, *args, payload=None, interactive=False):
    # Hermes can load its managed PyYAML outside the interpreter's normal site
    # directories. Pass only trusted package roots without inherited Python paths.
    # Descendant workers use `python -m scripts.hermes_runner`; safe-path mode
    # deliberately excludes their working directory from module discovery.
    env = {key: value for key, value in os.environ.items() if not key.startswith('PYTHON')}
    env.update(PYTHONPATH=os.pathsep.join((str(SCRIPT.parent.parent),
                                         str(Path(yaml.__file__).resolve().parents[1]))),
               PYTHONNOUSERSITE='1', PYTHONSAFEPATH='1')
    env.setdefault('HERMES_HOME', str(home.resolve()))
    env.setdefault('SKILLCRAFT_HERMES_PYTHON', sys.executable)
    launcher = Path(sys.executable).with_name('hermes')
    if launcher.is_file():
        env.setdefault('SKILLCRAFT_HERMES_BIN', str(launcher))
    command = [sys.executable, '-B', str(SCRIPT), '--hermes-home', str(home.resolve()), verb, *args]
    try:
        if interactive:
            # Let an interactive enable prompt use the caller's terminal. Keep
            # stdout captured because the CLI result remains a JSON document.
            result = subprocess.run(command, stdout=subprocess.PIPE, text=True, env=env)
        else:
            result = subprocess.run(
                command, input=json.dumps(payload) if payload is not None else '',
                capture_output=True, text=True, timeout=15, env=env,
            )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('SkillCraft journal command timed out') from error
    if result.returncode:
        stderr = getattr(result, 'stderr', None)
        raise RuntimeError((stderr.strip() if stderr else None) or 'SkillCraft journal command failed')
    return json.loads(result.stdout)


def final_reply(message):
    return (isinstance(message, dict) and message.get('role') == 'assistant'
            and not message.get('tool_calls') and message.get('phase') != 'commentary')


def prior_reply_uid(history, prompt):
    if not history or not isinstance(history[-1], dict):
        return None
    last = history[-1]
    if last.get('role') == 'user':
        if last.get('content') != prompt or len(history) < 2:
            return None
        last = history[-2]
    if final_reply(last):
        uid = last.get('message_uid')
        return uid if isinstance(uid, str) and uid else None
    return None


@dataclass
class Turn:
    id: str
    project: Path
    inventory: dict
    auto_import: bool
    loaded: dict = field(default_factory=dict)
    tool_events: dict = field(default_factory=dict)
    model_steps: set = field(default_factory=set)
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

    def record(self, key, turn_id, project, role, text, *, loaded=(), target=None, uid=None,
               model_steps=0, tool_events=()):
        payload = {
            'harness': 'hermes', 'session': key[1], 'turn': turn_id,
            'project': str(project), 'role': role, 'text': text,
            'target_turn': target, 'loaded': list(loaded), 'hermes_uid': uid,
            'defer_feedback': role == 'user', 'model_steps': model_steps,
        }
        if role == 'assistant':
            payload['tool_events'] = list(tool_events)
        return run_learning(key[0], 'observe', payload=payload)['inserted']

    def start(self, *, session_id=None, turn_id=None, user_message=None,
              parent_session_id=None, conversation_history=None, **kwargs):
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
                enrollment = run_learning(key[0], 'inventory', str(project))
                project = Path(enrollment['root'])
                uid = prior_reply_uid(conversation_history, user_message)
                target = (prior.id if conversation_history is None and prior and prior.finished
                          and prior.project == project else None)
                if not self.record(key, turn_id, project, 'user', user_message, target=target, uid=uid):
                    return
            except (OSError, RuntimeError, ValueError, KeyError):
                self.sessions.pop(key, None)
                LOG.debug('Could not record the Hermes prompt in SkillCraft', exc_info=True)
                return
            self.sessions[key] = Turn(turn_id, project, {v['path']: v for v in enrollment['versions']},
                                      enrollment.get('auto_import', False))
            self.sessions.move_to_end(key)
            if len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)

    def tool(self, *, session_id=None, turn_id=None, tool_name=None, args=None,
             result=None, status=None, **kwargs):
        key = self.key(session_id, turn_id)
        if not key:
            return
        tool = tool_name[:128] if isinstance(tool_name, str) and tool_name else 'tool'
        native_id = kwargs.get('tool_call_id', kwargs.get('call_id'))
        event_id = native_id if isinstance(native_id, str) and native_id else f'unknown:{tool}'
        event_id = event_id[:256]
        allowed_statuses = {'ok', 'error', 'unknown', 'timeout', 'denied', 'unavailable'}
        outcome = status if isinstance(status, str) and status in allowed_statuses else 'unknown'
        if not isinstance(native_id, str) or not native_id:
            outcome = 'unknown'
        with self.lock:
            turn = self.sessions.get(key)
            if turn and turn.id == turn_id and not turn.finished:
                event = {'id': event_id, 'tool': tool, 'status': outcome}
                previous = turn.tool_events.get(event_id)
                if previous and previous != event:
                    turn.tool_events[event_id] = {'id': event_id, 'tool': 'tool', 'status': 'unknown'}
                else:
                    turn.tool_events[event_id] = event
        if tool_name != 'skill_view' or status != 'ok' or not isinstance(args, dict) or args.get('file_path'):
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
                if entry and digest == entry['content_hash']:
                    turn.loaded[path] = entry

    def model_finished(self, *, session_id=None, turn_id=None, api_request_id=None,
                       api_call_count=None, finish_reason=None, **kwargs):
        key = self.key(session_id, turn_id)
        if not key or finish_reason in {'error', 'interrupted', 'cancelled', 'content_filter'}:
            return
        step = api_call_count if type(api_call_count) is int and api_call_count > 0 else api_request_id
        if not isinstance(step, (int, str)) or not step:
            return
        with self.lock:
            turn = self.sessions.get(key)
            if turn and turn.id == turn_id and not turn.finished:
                turn.model_steps.add(step)

    def finish(self, *, session_id=None, turn_id=None, assistant_response=None,
               conversation_history=None, **kwargs):
        key = self.key(session_id, turn_id)
        if not key or not isinstance(assistant_response, str) or not assistant_response.strip():
            return
        if not conversation_history:
            return
        last = conversation_history[-1]
        if not final_reply(last) or last.get('content') != assistant_response:
            return
        with self.lock:
            turn = self.sessions.get(key)
            if not turn or turn.id != turn_id or turn.finished:
                return
            try:
                uid = last.get('message_uid')
                turn.finished = self.record(key, turn.id, turn.project, 'assistant', assistant_response,
                                            loaded=turn.loaded.values(),
                                            uid=uid if isinstance(uid, str) and uid else None,
                                            model_steps=len(turn.model_steps),
                                            tool_events=turn.tool_events.values())
            except (OSError, RuntimeError, ValueError, KeyError):
                LOG.debug('Could not record the Hermes reply in SkillCraft', exc_info=True)
                return
        if turn.auto_import:
            try:
                run_learning(key[0], 'sync-hermes', str(turn.project), key[1])
            except (OSError, RuntimeError, ValueError, KeyError):
                LOG.warning('Could not synchronize completed Hermes experience in SkillCraft', exc_info=True)
