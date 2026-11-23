import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


CAPTURE = Path(__file__).parents[1] / 'plugins/skillcraft/hooks/learning_capture.py'
IMPLEMENTATION = CAPTURE.parents[1] / 'src/codex/learning_capture.py'
SPEC = importlib.util.spec_from_file_location('skillcraft_learning_capture', IMPLEMENTATION)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class LearningCaptureTests(unittest.TestCase):
    def test_hook_command_loads_relocated_implementation(self):
        hooks = json.loads((CAPTURE.parent / 'hooks.json').read_text())['hooks']
        command = hooks['UserPromptSubmit'][0]['hooks'][0]['command']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'plugin with spaces'
            project = Path(directory) / 'project'
            project.mkdir()
            (project / '.skillcraft').mkdir()
            (project / '.skillcraft/learning.json').write_text(json.dumps({
                'schema_version': 1, 'enabled': True, 'sources': ['sessions'],
            }))
            for source in (CAPTURE, IMPLEMENTATION):
                target = root / source.relative_to(CAPTURE.parents[1])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            script = root / 'skills/skillcraft/scripts/learning.py'
            script.parent.mkdir(parents=True)
            script.write_text("import json, sys\n"
                              "if sys.argv[1] == 'inventory': print('[]')\n"
                              "else: json.load(sys.stdin)\n")
            event = {'cwd': str(project), 'session_id': 's', 'turn_id': 't',
                     'hook_event_name': 'UserPromptSubmit', 'prompt': 'hello'}
            for configured_root in (False, True):
                data = Path(directory) / f'data-{configured_root}'
                env = os.environ.copy()
                env.pop('SKILLCRAFT_VENV', None)
                env.pop('SKILLCRAFT_LEARNING_INTERNAL', None)
                env['PLUGIN_DATA'] = str(data)
                # The loader expands PLUGIN_ROOT in the command. To exercise
                # fallback root discovery, invoke the same entrypoint directly.
                if configured_root:
                    env['PLUGIN_ROOT'] = str(root)
                    args = command
                else:
                    env.pop('PLUGIN_ROOT', None)
                    args = ['python3', str(root / 'hooks/learning_capture.py')]
                result = subprocess.run(args, shell=configured_root, cwd=project, env=env,
                                        input=json.dumps(event), text=True, capture_output=True,
                                        timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                state_files = list((data / 'learning-capture').glob('*.json'))
                self.assertEqual(len(state_files), 1, result.stderr)
                self.assertEqual(json.loads(state_files[0].read_text())['pending_turn'], 't')

    def invoke(self, event):
        with mock.patch.object(capture.sys, 'stdin', io.StringIO(json.dumps(event))):
            return capture.main()

    def test_pending_turn_suppresses_old_confirmation_target_and_late_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'project'
            project.mkdir()
            data = Path(directory) / 'data'
            env = {
                'PLUGIN_ROOT': str(CAPTURE.parents[1]),
                'PLUGIN_DATA': str(data),
            }
            base = {'cwd': str(project), 'session_id': 'session', 'prompt': 'hello'}
            rollout = project / 'rollout.jsonl'
            rollout.write_text('')
            with mock.patch.dict(capture.os.environ, env, clear=False), \
                    mock.patch.object(capture, 'enabled', return_value=True), \
                    mock.patch.object(capture, 'inventory', return_value=[]), \
                    mock.patch.object(capture, 'observe', return_value=True) as observe, \
                    mock.patch.object(capture, 'confirmed_reads', return_value=[]):
                self.invoke({**base, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'A'})
                self.invoke({**base, 'hook_event_name': 'PostModelRequest', 'turn_id': 'A',
                             'kind': 'sampling', 'request_id': 'req-1', 'attempt_number': 1,
                             'status': 'failed'})
                self.invoke({**base, 'hook_event_name': 'PostModelRequest', 'turn_id': 'A',
                             'kind': 'sampling', 'request_id': 'req-1', 'attempt_number': 2,
                             'status': 'completed'})
                self.invoke({**base, 'hook_event_name': 'PostModelRequest', 'turn_id': 'A',
                             'kind': 'sampling', 'request_id': 'req-2', 'attempt_number': 1,
                             'status': 'completed'})
                self.invoke({**base, 'hook_event_name': 'PostModelRequest', 'turn_id': 'A',
                             'kind': 'sampling', 'request_id': 'subagent', 'status': 'completed',
                             'agent_id': 'child', 'agent_type': 'worker'})
                self.invoke({**base, 'hook_event_name': 'Stop', 'turn_id': 'A', 'transcript_path': str(rollout), 'last_assistant_message': 'answer A'})
                assistant_a = next(call.args[2] for call in observe.call_args_list if call.args[2]['role'] == 'assistant')
                self.assertEqual(assistant_a['model_steps'], 2)
                user_a = next(call.args[2] for call in observe.call_args_list if call.args[2]['role'] == 'user')
                self.assertTrue(user_a['defer_feedback'])
                self.invoke({**base, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'B'})
                state_path = next((data / 'learning-capture').glob('*.json'))
                self.invoke({**base, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'C'})
                prompt_c = next(call.args[2] for call in observe.call_args_list if call.args[2]['turn'] == 'C')
                self.assertIsNone(prompt_c['target_turn'])
                # A duplicated delayed prompt must not replace B as pending.
                self.invoke({**base, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'A'})
                self.invoke({**base, 'hook_event_name': 'Stop', 'turn_id': 'A', 'last_assistant_message': 'late A'})
                self.invoke({**base, 'hook_event_name': 'Stop', 'turn_id': 'B', 'last_assistant_message': 'late B'})
                state = json.loads(state_path.read_text())
                self.assertEqual(state['pending_turn'], 'C')
                self.assertEqual(state['versions'], [])

                self.invoke({**base, 'hook_event_name': 'Stop', 'turn_id': 'C', 'last_assistant_message': 'answer C'})
                self.assertEqual(json.loads(state_path.read_text()), {
                    'last_assistant_turn': 'C', 'prompt_turns': ['A', 'B', 'C'],
                })
                self.invoke({**base, 'hook_event_name': 'Stop', 'turn_id': 'A', 'last_assistant_message': 'duplicate A'})
                self.assertEqual(json.loads(state_path.read_text()), {
                    'last_assistant_turn': 'C', 'prompt_turns': ['A', 'B', 'C'],
                })

    def test_prompt_deduplication_does_not_expire_after_one_hundred_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'project'
            project.mkdir()
            data = Path(directory) / 'data'
            env = {'PLUGIN_ROOT': str(CAPTURE.parents[1]), 'PLUGIN_DATA': str(data)}
            base = {'cwd': str(project), 'session_id': 'session', 'prompt': 'hello'}
            with mock.patch.dict(capture.os.environ, env, clear=False), \
                    mock.patch.object(capture, 'enabled', return_value=True), \
                    mock.patch.object(capture, 'inventory', return_value=[]), \
                    mock.patch.object(capture, 'observe', return_value=True) as observe:
                for index in range(101):
                    self.invoke({**base, 'hook_event_name': 'UserPromptSubmit', 'turn_id': f't{index}'})
                calls_before_duplicate = observe.call_count
                self.invoke({**base, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 't0'})
                state_path = next((data / 'learning-capture').glob('*.json'))
                state = json.loads(state_path.read_text())
                self.assertEqual(observe.call_count, calls_before_duplicate)
                self.assertEqual(state['pending_turn'], 't100')

    def test_inventory_and_observe_use_a_bounded_subprocess_timeout(self):
        with mock.patch.object(capture.subprocess, 'run', side_effect=subprocess.TimeoutExpired('python', 4)) as run:
            self.assertIsNone(capture.inventory('project', 'learning.py', 'project'))
            self.assertFalse(capture.observe('python', 'learning.py', {'project': 'project'}))
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.kwargs['timeout'] == capture.SUBPROCESS_TIMEOUT_SECONDS for call in run.call_args_list))

    def test_counts_completed_codex_model_items_only_for_the_requested_non_aborted_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / 'rollout.jsonl'
            def row(payload):
                return {'type': 'event_msg', 'payload': payload}
            rows = [
                row({'type': 'item_completed', 'thread_id': 's', 'turn_id': 't',
                     'item': {'type': 'AgentMessage', 'id': 'a1', 'content': 'not retained'}}),
                row({'type': 'item_completed', 'thread_id': 's', 'turn_id': 't',
                     'item': {'type': 'CommandExecution', 'id': 'cmd'}}),
                row({'type': 'item_completed', 'thread_id': 'other', 'turn_id': 't',
                     'item': {'type': 'AgentMessage', 'id': 'other'}}),
                row({'type': 'item_completed', 'thread_id': 's', 'turn_id': 'other',
                     'item': {'type': 'AgentMessage', 'id': 'other-turn'}}),
                row({'type': 'item_completed', 'thread_id': 's', 'turn_id': 't',
                     'item': {'type': 'AgentMessage', 'id': 'a1', 'content': 'duplicate'}}),
                row({'type': 'item_completed', 'thread_id': 's', 'turn_id': 't',
                     'item': {'type': 'AgentMessage', 'id': 'a2'}}),
            ]
            transcript.write_text(''.join(json.dumps(item) + '\n' for item in rows))
            self.assertEqual(capture.completed_model_steps(str(transcript), 's', 't'), 1)
            rows.append(row({'type': 'turn_aborted', 'thread_id': 's', 'turn_id': 't'}))
            transcript.write_text(''.join(json.dumps(item) + '\n' for item in rows))
            self.assertIsNone(capture.completed_model_steps(str(transcript), 's', 't'))
            self.assertTrue(capture.aborted_turn(str(transcript), 's', 't'))

    def test_codex_tool_events_are_turn_scoped_minimal_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / 'rollout.jsonl'
            rows = [
                {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 's', 'turn_id': 't',
                 'item': {'type': 'CommandExecution', 'id': 'cmd-1', 'status': 'completed', 'exit_code': 0,
                          'command': ['secret command']}}},
                {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 's', 'turn_id': 't',
                 'item': {'type': 'CommandExecution', 'id': 'cmd-1', 'status': 'completed', 'exit_code': 0}}},
                {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 's', 'turn_id': 't',
                 'item': {'type': 'McpToolCall', 'id': 'mcp-1', 'status': 'failed', 'name': 'private/tool'}}},
                {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 's', 'turn_id': 'other',
                 'item': {'type': 'CommandExecution', 'id': 'other', 'status': 'failed'}}},
            ]
            transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            self.assertEqual(capture.completed_tool_events(str(transcript), 's', 't'), [
                {'id': 'cmd-1', 'tool': 'command', 'status': 'unknown'},
                {'id': 'mcp-1', 'tool': 'mcp', 'status': 'error'},
            ])

    def test_codex_stop_observation_includes_tool_events(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'project'
            project.mkdir()
            rollout = project / 'rollout.jsonl'
            rollout.write_text(json.dumps({'type': 'event_msg', 'payload': {
                'type': 'item_completed', 'thread_id': 'session', 'turn_id': 'turn',
                'item': {'type': 'CommandExecution', 'id': 'command-1', 'status': 'completed', 'exit_code': 0},
            }}) + '\n')
            env = {'PLUGIN_ROOT': str(CAPTURE.parents[1]), 'PLUGIN_DATA': str(Path(directory) / 'data')}
            with mock.patch.dict(capture.os.environ, env, clear=False), \
                    mock.patch.object(capture, 'enabled', return_value=True), \
                    mock.patch.object(capture, 'inventory', return_value=[]), \
                    mock.patch.object(capture, 'observe', return_value=True) as observe:
                base = {'cwd': str(project), 'session_id': 'session'}
                self.invoke({**base, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'turn', 'prompt': 'prompt'})
                self.invoke({**base, 'hook_event_name': 'Stop', 'turn_id': 'turn',
                             'transcript_path': str(rollout), 'last_assistant_message': 'answer'})
            assistant = next(call.args[2] for call in observe.call_args_list if call.args[2]['role'] == 'assistant')
            self.assertEqual(assistant['tool_events'], [{'id': 'command-1', 'tool': 'command', 'status': 'unknown'}])

    def test_aborted_codex_stop_does_not_send_an_assistant_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'project'
            project.mkdir()
            rollout = project / 'rollout.jsonl'
            rollout.write_text(''.join(json.dumps({'type': 'event_msg', 'payload': payload}) + '\n' for payload in (
                {'type': 'item_completed', 'thread_id': 'session', 'turn_id': 'turn',
                 'item': {'type': 'AgentMessage', 'id': 'partial'}},
                {'type': 'turn_aborted', 'thread_id': 'session', 'turn_id': 'turn', 'reason': 'interrupted'},
            )))
            env = {'PLUGIN_ROOT': str(CAPTURE.parents[1]), 'PLUGIN_DATA': str(Path(directory) / 'data')}
            base = {'cwd': str(project), 'session_id': 'session'}
            with mock.patch.dict(capture.os.environ, env, clear=False), \
                    mock.patch.object(capture, 'enabled', return_value=True), \
                    mock.patch.object(capture, 'inventory', return_value=[]), \
                    mock.patch.object(capture, 'observe', return_value=True) as observe:
                self.invoke({**base, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'turn', 'prompt': 'prompt'})
                self.invoke({**base, 'hook_event_name': 'Stop', 'turn_id': 'turn',
                             'transcript_path': str(rollout), 'last_assistant_message': 'partial'})
            self.assertEqual([call.args[2]['role'] for call in observe.call_args_list], ['user'])


if __name__ == '__main__':
    unittest.main()
