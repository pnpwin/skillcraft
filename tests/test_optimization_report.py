import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.generate_report import generate_html, load_report
from scripts.run_loop import optimize


class Report(unittest.TestCase):
    def test_loop_preserves_error_details_in_report_history(self):
        root = Path(__file__).resolve().parents[1]
        def evaluate(skill, rows, target, *args, **kwargs):
            failed = target.name == 'final-test'
            return {'summary': {'errors': int(failed), 'valid': 2, 'tp': 1, 'tn': 1, 'fp': 0, 'fn': 0},
                    'results': [{'eval_id': 'receipt', 'repeat': 1, 'prompt': 'A receipt',
                                 'should_trigger': True, 'triggered': None if failed else True,
                                 'status': 'timeout' if failed else 'completed',
                                 'error': 'Provider <timeout>' if failed else None}]}
        with tempfile.TemporaryDirectory() as tmp:
            options = SimpleNamespace(workspace=Path(tmp) / 'loop', skill=root / 'tests/fixtures/demo-skill',
                evals=root / 'tests/fixtures/trigger.json', iterations=1, seed=42, profile=None,
                model='test/model', optimizer_model='test/model', repeats=1, workers=1, timeout=10, quality_evals=None)
            with patch('scripts.run_loop.run_eval', side_effect=evaluate):
                result = optimize(options)
            self.assertEqual(result['final_test_results'][0]['error'], 'Provider <timeout>')
            self.assertIn('Provider &lt;timeout&gt;', (options.workspace / 'report.html').read_text())
            training = json.loads((options.workspace / 'training-history.json').read_text())
            self.assertNotIn('error', training[0]['training_results'][0])

    def test_matrix_states_and_escaping(self):
        row = {'eval_id': 'one', 'prompt': '</script><img src=x onerror=alert(1)>', 'repeat': 1,
               'should_trigger': True, 'triggered': True, 'status': 'completed'}
        data = {'repeats': 2, 'best': 'v0', 'iterations': [
            {'candidate': 'v0', 'description': '<script>bad()</script>', 'train_results': [row, dict(row, eval_id='same-text-other-id', triggered=False)]},
            {'candidate': 'v1', 'train_results': [dict(row, status='timeout', triggered=None, error='Transport <failure>')]}],
            'final_test_results': [row], 'final_test': {'precision': 1}}
        text = generate_html(data)
        self.assertIn('same-text-other-id', text)
        self.assertIn('1 missing', text)
        self.assertIn('ERROR / unknown observation', text)
        self.assertIn('Transport &lt;failure&gt;', text)
        self.assertIn('FN;', text)
        self.assertNotIn('<script>bad()', text)
        self.assertNotIn('<img src=x', text)
        final = text.split('<section data-split="final_test">')[1]
        self.assertIn('Not run / no records', final)
        self.assertIn('Selected on validation', final)

    def test_legacy_hydration_and_missing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'history.json').write_text(json.dumps({'iterations': [{'iteration': 0, 'candidate': 'old'}]}))
            p = root / 'iteration-000/train/trigger-results.json'
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'results': [{'eval_id': 'a', 'status': 'completed', 'prompt': 'p'}]}))
            data = load_report(root / 'history.json')
            self.assertEqual(len(data['iterations'][0]['train_results']), 1)
            self.assertIn('Description not recorded', generate_html(data))
            self.assertIn('No per-query records', generate_html({'iterations': []}))


if __name__ == '__main__':
    unittest.main()
