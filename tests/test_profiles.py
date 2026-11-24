import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from scripts import runtime
from scripts.learning_worker import REPLACEMENTS
from scripts.runtime import profile_settings
from scripts.utils import read_json, write_json


class Profiles(unittest.TestCase):
    def test_default_and_opt_in_worker(self):
        default = profile_settings(None, 'candidate')
        self.assertTrue(default['controlled'])
        self.assertFalse(default['allow_delegation'])
        custom = profile_settings(ROOT / 'tests/fixtures/delegation-profile.json', 'candidate')
        self.assertFalse(custom['controlled'])
        self.assertEqual(custom['permissions'][-1], {'action': 'subagent', 'resource': 'skillcraft-worker', 'effect': 'allow'})
        self.assertTrue(any(p['action'] == 'skill' and p['resource'] == 'candidate' for p in custom['permissions']))
        base = profile_settings(ROOT / 'tests/fixtures/shell-profile.json')
        self.assertFalse(any(p['action'] == 'skill' and p['effect'] == 'allow' for p in base['permissions']))
        self.assertEqual(base['steps'], 48)

    def test_bad_config_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.json'
            for bad in [{'allow_delegation': 'true'}, {'steps': 0}, {'image_generation': True}, {'permissions': [{'action': 'shell'}]}]:
                write_json(path, bad)
                with self.assertRaises(ValueError):
                    profile_settings(path)

    def test_opencode_optimizer_schema_reaches_the_agent_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runner = root / 'scripts/opencode-runner/node_modules/.bin/tsx'
            runner.parent.mkdir(parents=True)
            runner.touch()
            output = root / 'result'

            def completed(timeout):
                write_json(output / 'run.json', {'status': 'completed'})
                return '', ''

            with (patch.object(runtime, 'ROOT', root),
                  patch.dict(runtime.os.environ, {'SKILLCRAFT_TEMP': str(root / 'runs')}),
                  patch.object(runtime.subprocess, 'Popen') as process):
                process.return_value.communicate.side_effect = completed
                result = runtime.run_model('Revise the skill.', output, 'openai/test',
                                           mode='optimizer', harness='opencode', output_schema=REPLACEMENTS)
            self.assertEqual(result['status'], 'completed')
            request = read_json(output / 'request.json')
            self.assertEqual(request['output_schema'], REPLACEMENTS)
            configuration = read_json(Path(request['directory']) / '.opencode/opencode.json')
            system = configuration['agents']['skillcraft-eval']['system']
            instruction, _, schema = system.rpartition('\n')
            self.assertIn('as the final response', instruction)
            self.assertEqual(json.loads(schema), REPLACEMENTS)

    def test_unsupported_opencode_operation_fails_before_creating_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result'
            with self.assertRaisesRegex(ValueError, 'OpenCode operation must be run, probe or catalog_probe'):
                runtime.run_model('', output, 'openai/test', operation='verify_environment')
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
