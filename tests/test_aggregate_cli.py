"""Exercise the executable contract, including valid negative quality verdicts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.utils import write_json


class AggregateCLI(unittest.TestCase):
    def test_exit_codes_and_persisted_reports(self):
        for scenario in ['success', 'quality_fail', 'missing', 'execution_error', 'grading_error', 'critique_error', 'drift']:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_json(root / 'manifest.json', {
                    'configurations': {'with_skill': {}}, 'evals': [{'id': 'one'}],
                    'repeats': 1, 'environment_consistent': scenario != 'drift'})
                if scenario != 'missing':
                    run = root / 'eval-one/with_skill/run-001'
                    write_json(run / 'run.json', {'status': 'execution_error' if scenario == 'execution_error' else 'completed'})
                    write_json(run / 'grading.json', {
                        'status': 'error' if scenario == 'grading_error' else 'completed',
                        'eval_feedback': {'required': scenario == 'critique_error', 'status': 'error' if scenario == 'critique_error' else 'not_run'},
                        'summary': {'pass_rate': 0 if scenario == 'quality_fail' else 1}})
                proc = subprocess.run([sys.executable, str(ROOT / 'scripts/aggregate_benchmark.py'), str(root)],
                                      cwd=tmp, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0 if scenario in {'success', 'quality_fail'} else 1, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), json.loads((root / 'benchmark.json').read_text()))
                if scenario == 'quality_fail':
                    self.assertEqual(json.loads(proc.stdout)['errors'], 0)

    def test_invalid_input_is_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(ROOT / 'scripts/aggregate_benchmark.py'), tmp], capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('Aggregation failed:', proc.stderr)


if __name__ == '__main__':
    unittest.main()
