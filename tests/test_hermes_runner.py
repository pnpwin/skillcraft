from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import tests
from scripts import hermes_runner, hermes_worker
from scripts.harness import model_ref
from scripts.utils import digest, read_json, write_json


class HermesRunnerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = self.root / 'profile'
        self.request = {'run_id': 'run', 'model': model_ref('openai-codex:example', 'hermes')}
        self.base = {'run_id': 'run', 'session_id': 'native-session', 'turn_id': 'native-turn',
                     'profile': str(self.profile), 'api_request_id': 'request',
                     'provider': 'openai-codex', 'model': 'example'}
        self.before = {**self.base, 'phase': 'request', 'catalog': [], 'block_sha256': None,
                       'skill_sha256': None, 'tools': ['read_file', 'write_file', 'terminal']}
        self.after = {**self.base, 'phase': 'response', 'response_model': 'example-served'}

    def check(self, before=None, after=None):
        return hermes_runner.check_requests([self.before if before is None else before,
                                             self.after if after is None else after],
                                            self.request, 'native-session', self.profile)

    def test_baseline_and_rejected_receipts(self):
        self.assertEqual(self.check()['response_models'], ['example-served'])
        for changes in ({'catalog': ['other-skill']}, {'profile': '/another-profile'},
                        {'block_sha256': 'forged'}, {'turn_id': ''}, {'provider': 'other'},
                        {'tools': self.before['tools'] + ['delegate_task']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.check(before={**self.before, **changes})
        without_block = self.before.copy()
        without_block.pop('block_sha256')
        with self.assertRaises(ValueError):
            self.check(before=without_block)
        with self.assertRaises(ValueError):
            self.check(after={**self.after, 'api_request_id': 'unrelated'})

    def test_complete_native_preload(self):
        path = self.root / 'SKILL.md'
        path.write_text('---\nname: demo\n---\n\nPrivate tool instructions.\n')
        self.request.update(skill_id='demo', skill_path=str(path), skill_sha256=digest(path))
        block = ('[IMPORTANT: The user launched this CLI session with the "demo" skill preloaded. '
                 'Treat its instructions as active guidance for the duration of this session unless the user overrides them.]'
                 f'\n\n{path.read_text().strip()}\n\n[Skill directory: {path.parent}]')
        hashed = hermes_worker.preload_hash(self.request, '', [{'role': 'system', 'content': block}])
        before = {**self.before, 'catalog': ['demo'], 'block_sha256': hashed, 'skill_sha256': digest(path)}
        self.assertEqual(self.check(before=before)['block_sha256'], hashed)
        for changed in (block.replace('Private tool instructions.', 'Modified.'),
                        block.replace(str(path.parent), '/another-skill')):
            with self.assertRaises(ValueError):
                hermes_worker.preload_hash(self.request, changed, [])
        with self.assertRaises(ValueError):
            hermes_worker.preload_hash(self.request, '', [{'role': 'user', 'content': block}])
        with self.assertRaises(ValueError):
            self.check(before={**before, 'skill_sha256': 'invalid'})
        path.write_text('Modified.')
        with self.assertRaises(ValueError):
            self.check(before=before)

    def test_native_events_project_tool_and_result(self):
        native, parts = hermes_runner.native_events([
            {'type': 'system', 'subtype': 'init'},
            {'type': 'tool_use', 'name': 'write_file', 'input': {'path': 'outputs/a'}},
            {'type': 'tool_result', 'name': 'write_file', 'output': 'done'},
            {'type': 'result', 'session_id': 'native-session', 'text': 'Finished', 'exit_code': 0},
        ])
        self.assertEqual(native['session_id'], 'native-session')
        self.assertEqual(parts[0]['state']['status'], 'completed')
        self.assertEqual(parts[1]['text'], 'Finished')
        with self.assertRaises(ValueError):
            hermes_runner.native_events([{'type': 'system', 'subtype': 'init'}])

    def test_corrupted_and_nonregular_artifacts(self):
        (path := self.root / 'actions.csv').write_text('before')
        first = hermes_runner.regular_artifacts(self.root)
        path.write_text('after')
        self.assertNotEqual(first, hermes_runner.regular_artifacts(self.root))
        (self.root / 'escape').symlink_to(path)
        with self.assertRaises(ValueError):
            hermes_runner.regular_artifacts(self.root)

    def test_saved_run_binds_copied_artifact_and_profile(self):
        path = self.root / 'run'
        output = path / 'outputs'
        output.mkdir(parents=True)
        (artifact := output / 'actions.csv').write_text('fixture')
        (answer := path / 'response.txt').write_text('done')
        (output / '_response.txt').write_text(answer.read_text())
        (path / 'events.jsonl').write_text(
            '{"type":"system","subtype":"init"}\n{"type":"result","session_id":"native-session","exit_code":0}\n')
        documents = {'request.json': self.request, 'hermes-requests.json': [self.before, self.after],
                     'hermes-proof.json': {**self.check(), 'run_id': 'run', 'session_id': 'native-session',
                                           'profile': str(self.profile), 'outputs': {'actions.csv': digest(artifact)},
                                           'sandbox': {'read_input': 0, 'write_output': 0, 'deny_input_write': 1,
                                                       'deny_host_read': 1, 'deny_network': 1}}}
        for name, value in documents.items():
            write_json(path / name, value)
        run = {**self.request, 'session_id': 'native-session', 'turn_id': 'native-turn',
               'profile_home': str(self.profile), 'evidence_hashes': {
                   name: digest(path / name) for name in (*documents, 'events.jsonl', 'response.txt')}}
        self.assertEqual(hermes_runner.verify_saved_run(path, run)['catalog'], [])
        with self.assertRaises(ValueError):
            hermes_runner.verify_saved_run(path, {**run, 'profile_home': '/other'})
        artifact.write_text('modified')
        with self.assertRaises(ValueError):
            hermes_runner.verify_saved_run(path, run)

    def test_profile_copies_only_selected_model_settings_and_auth(self):
        source = self.root / 'source'
        source.mkdir()
        self.profile.mkdir()
        write_json(source / 'config.yaml', {'agent': {'reasoning_effort': 'medium', 'max_turns': 100},
                                            'plugins': {'enabled': ['other']}})
        write_json(source / 'auth.json', {'version': 1, 'providers': {
            'openai-codex': {'token': 'test-codex'}, 'other': {'token': 'test-other'}}})
        args, model = hermes_runner.provider_config(source, self.profile, 'openai-codex')
        self.assertEqual(args, [])
        self.assertEqual(model, {'reasoning_effort': 'medium'})
        isolated = read_json(self.profile / 'auth.json')
        self.assertEqual(isolated, {'version': 1, 'active_provider': 'openai-codex',
                                    'providers': {'openai-codex': {'token': 'test-codex'}}})

    def test_child_timeout_and_cancel(self):
        from unittest.mock import patch
        launcher = [sys.executable, '-c', 'import time; time.sleep(60)']
        with self.assertRaises(subprocess.TimeoutExpired):
            hermes_runner.invoke(launcher, self.root, {}, 0.05)
        with patch.object(subprocess.Popen, 'communicate',
                          side_effect=[KeyboardInterrupt, ('', '')]):
            with self.assertRaises(KeyboardInterrupt):
                hermes_runner.invoke(launcher, self.root, {}, 10)

if __name__ == '__main__':
    unittest.main()
