from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

import tests
from scripts import hermes_runner, hermes_worker
from scripts.harness import model_ref
from scripts.compliance import assess, runner_hash, skill_hash
from scripts.grade import grade_run
from scripts.grading_store import read_grading
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

    def check(self, before=None, after=None, *, block=None):
        return hermes_runner.check_requests([self.before if before is None else before,
                                             self.after if after is None else after],
                                            self.request, 'native-session', self.profile, block=block)

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
        self.assertEqual(self.check(before=before, block=hashed)['block_sha256'], hashed)
        for changed in (block.replace('Private tool instructions.', 'Modified.'),
                        block.replace(str(path.parent), '/another-skill')):
            with self.assertRaises(ValueError):
                hermes_worker.preload_hash(self.request, changed, [])
        with self.assertRaises(ValueError):
            hermes_worker.preload_hash(self.request, '', [{'role': 'user', 'content': block}])
        with self.assertRaises(ValueError):
            self.check(before={**before, 'skill_sha256': 'invalid'}, block=hashed)
        path.write_text('Modified.')
        with self.assertRaises(ValueError):
            hermes_worker.preload_hash(self.request, block, [])

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

    def save_run(self, path, proof, request, before, metadata=None):
        output = path / 'outputs'
        output.mkdir(parents=True)
        (artifact := output / 'actions.csv').write_text('fixture')
        (answer := path / 'response.txt').write_text('done')
        (output / '_response.txt').write_text(answer.read_text())
        (path / 'events.jsonl').write_text(
            '{"type":"system","subtype":"init"}\n{"type":"result","session_id":"native-session","exit_code":0}\n')
        documents = {'request.json': request, 'hermes-requests.json': [before, self.after],
                     'hermes-proof.json': {**proof, 'run_id': 'run', 'session_id': 'native-session',
                                           'profile': str(self.profile), 'outputs': {'actions.csv': digest(artifact)},
                                           'sandbox': {'read_input': 0, 'write_output': 0, 'deny_input_write': 1,
                                                       'deny_host_read': 1, 'deny_network': 1}},
                     **(metadata or {})}
        for name, value in documents.items():
            write_json(path / name, value)
        return {**request, 'session_id': 'native-session', 'turn_id': 'native-turn',
                'profile_home': str(self.profile), 'evidence_hashes': {
                    name: digest(path / name) for name in (*documents, 'events.jsonl', 'response.txt')}}

    def test_saved_run_binds_copied_artifact_and_profile(self):
        path = self.root / 'run'
        run = self.save_run(path, {**self.check(), 'schema_version': 1}, self.request, self.before)
        self.assertEqual(hermes_runner.verify_saved_run(path, run)['catalog'], [])
        with self.assertRaises(ValueError):
            hermes_runner.verify_saved_run(path, {**run, 'profile_home': '/other'})
        (path / 'outputs/actions.csv').write_text('modified')
        with self.assertRaises(ValueError):
            hermes_runner.verify_saved_run(path, run)

    def test_candidate_workspace_is_portable_across_proof_schemas(self):
        original = self.root / 'workspace'
        skill = original / 'snapshots/demo'
        skill.mkdir(parents=True)
        body = '---\nname: demo\ndescription: Test portable evidence.\n---\n\nRead and report.\n'
        (skill / 'SKILL.md').write_text(body)
        staged = self.root / 'scratch/demo'
        shutil.copytree(skill, staged)
        markdown = staged / 'SKILL.md'
        self.request.update(harness='hermes', mode='quality', attach_skill=True, skill_id='demo',
                            skill_path=str(markdown), skill_sha256=digest(markdown))
        block = hermes_worker.sha(hermes_worker.native_block(self.request).encode())
        before = {**self.before, 'catalog': ['demo'], 'block_sha256': block, 'skill_sha256': digest(markdown)}
        profile = {'controlled': True}
        environment = {'server_version': 'test', 'candidate': {'id': 'demo', 'path': str(markdown)},
                       'background': False, 'plugins': ['skillcraft_eval'], 'configuration_fingerprint': 'test',
                       'controlled': True}
        metadata = {'profile.json': profile, 'profile-source.json': None, 'environment.json': environment,
                    'messages.json': []}
        proof = self.check(before=before, block=block)
        rubric = {'prompt': 'Make a file.', 'expectations': ['Actions saved'],
                  'checks': [{'text': 'Actions saved', 'type': 'equals', 'file': 'actions.csv', 'value': 'fixture'}]}
        for schema in (1, 2):
            request = self.request if schema == 1 else {**self.request, 'preload_block_sha256': block}
            folder = original / f'run-{schema}'
            run = self.save_run(folder, {**proof, 'schema_version': schema}, request, before, metadata)
            run.update(profile=profile, environment=environment, runner_hash=runner_hash('hermes'),
                       status='completed', attached=True, installed_hash=skill_hash(skill))
            write_json(folder / 'run.json', run)
            self.assertEqual(grade_run(folder, rubric, harness='hermes')['status'], 'completed')
        shutil.rmtree(staged.parent)
        workspace = original.rename(self.root / 'moved')
        for schema in (1, 2):
            with self.subTest(schema=schema):
                folder = workspace / f'run-{schema}'
                run = read_json(folder / 'run.json')
                self.assertEqual(hermes_runner.verify_saved_run(folder, run)['block_sha256'], block)
                check = assess(workspace / 'snapshots/demo', folder, harness='hermes')
                self.assertEqual(check['hermes']['status'], 'PASS', check['hermes'])
                self.assertEqual(read_grading(folder)[0]['summary']['passed'], 1)

        folder = workspace / 'run-2'
        run = read_json(folder / 'run.json')
        write_json(folder / 'request.json', {**self.request, 'preload_block_sha256': '0' * 64})
        run['evidence_hashes']['request.json'] = digest(folder / 'request.json')
        with self.assertRaisesRegex(ValueError, 'Invalid native request receipt'):
            hermes_runner.verify_saved_run(folder, run)
        with self.assertRaisesRegex(ValueError, 'changed/different execution evidence'):
            read_grading(folder)

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
