import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.harness import harness_metadata, model_ref, resolve_harness


class HarnessTests(unittest.TestCase):
    def test_detects_session_signals_independently_of_path(self):
        self.assertEqual(resolve_harness(environ={'CODEX_THREAD_ID': 'thread', 'PATH': '/both/bin'}), 'codex')
        self.assertEqual(resolve_harness(environ={'OPENCODE': '1', 'OPENCODE_SESSION_ID': 'session',
                                                  'PATH': '/both/bin'}), 'opencode')

    def test_ambiguous_or_missing_signals_require_an_override(self):
        both = {'CODEX_THREAD_ID': 'thread', 'OPENCODE': '1', 'OPENCODE_SESSION_ID': 'session'}
        for env in [both, {}, {'OPENCODE': '1'}, {'AI_AGENT': 'opencode'}]:
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, '--harness'):
                resolve_harness(environ=env)
        self.assertEqual(resolve_harness('opencode', environ=both), 'opencode')
        self.assertEqual(resolve_harness('codex', environ={}), 'codex')
        self.assertEqual(resolve_harness('chatgpt', environ=both), 'chatgpt')

    def test_cli_stops_before_creating_workspace(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'eval'
            env = {key: value for key, value in os.environ.items()
                   if key not in {'CODEX_THREAD_ID', 'OPENCODE', 'OPENCODE_SESSION_ID'}}
            result = subprocess.run([sys.executable, '-B', 'scripts/run_quality_eval.py', '--model', 'example',
                                     '--workspace', str(workspace), '--skill', 'unused', '--evals', 'unused'],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn('--harness', result.stderr)
            self.assertFalse(workspace.exists())

    def test_model_refs_are_harness_specific(self):
        self.assertEqual(model_ref('model#high', 'codex'), {'id': 'model', 'effort': 'high'})
        self.assertEqual(model_ref('model#high', 'chatgpt'), {'id': 'model', 'effort': 'high'})
        self.assertEqual(harness_metadata('chatgpt'), {'harness': 'codex', 'requested_harness': 'chatgpt'})
        self.assertEqual(model_ref('provider/model#high', 'opencode'),
                         {'providerID': 'provider', 'id': 'model', 'variant': 'high'})
        for value in ['provider/model', 'model#', '#high']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                model_ref(value, 'codex')


if __name__ == '__main__':
    unittest.main()
