import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.grade import grade_run, validate_feedback
from scripts.utils import write_json


class Critique(unittest.TestCase):
    def test_validation(self):
        issue = {'category': 'weak_check', 'expectation': 'exists', 'explanation': 'Existence does not prove correctness',
                 'suggestion': 'Check actual amounts', 'evidence': None}
        self.assertEqual(validate_feedback({'issues': [issue]}, ['exists'])['status'], 'completed')
        for bad in [dict(issue, expectation='invented'), dict(issue, category='other'), dict(issue, suggestion=''), dict(issue, evidence=1)]:
            with self.assertRaises(ValueError):
                validate_feedback({'issues': [bad]}, ['exists'])

    def run_case(self, response, model='test/model', profile=None):
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp)
            write_json(r / 'run.json', {'status': 'completed'})
            (r / 'outputs').mkdir()
            (r / 'outputs/result.txt').write_text('result')
            (r / 'transcript.md').write_text('transcript')
            row = {'prompt': 'write a useful report', 'expectations': ['exists'],
                   'checks': [{'text': 'exists', 'type': 'exists', 'file': 'result.txt'}]}
            def judge(prompt, target, *args, **kw):
                self.assertEqual(kw.get('profile'), profile)
                self.assertIn('all_expectations', prompt)
                self.assertIn('deterministic_results', prompt)
                self.assertIn('"grade_expectations": []', prompt)
                target.mkdir()
                (target / 'response.txt').write_text(json.dumps(response))
                return {'status': 'completed'}
            with patch('scripts.grade.run_model', side_effect=judge) as call:
                result = grade_run(r, row, judge_model=model, judge_profile=profile)
                self.assertEqual(call.call_count, 1 if model else 0)
            return result

    def test_deterministic_critique_only(self):
        r = self.run_case({'expectations': [], 'eval_feedback': {'issues': []}})
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(r['eval_feedback']['status'], 'completed')

    def test_explicit_judge_profile_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / 'judge-profile.json'
            write_json(profile, {'inherit_bounded_files': True})
            r = self.run_case({'expectations': [], 'eval_feedback': {'issues': []}}, profile=profile)
            self.assertEqual(r['eval_feedback']['status'], 'completed')

    def test_bad_critique_preserves_verdict(self):
        r = self.run_case({'expectations': [], 'eval_feedback': {'issues': 'invalid'}})
        self.assertEqual(r['summary']['pass_rate'], 1)
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(r['eval_feedback']['status'], 'error')
        self.assertFalse(r['evaluation_complete'])
        self.assertEqual(r['pending_expectations'], [])

    def test_no_judge_does_not_claim_review(self):
        r = self.run_case({}, model=None)
        self.assertEqual(r['eval_feedback']['status'], 'not_run')
        self.assertFalse(r['eval_feedback']['required'])


if __name__ == '__main__':
    unittest.main()
