import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.aggregate_benchmark import aggregate
from scripts.grade import grade_run
from scripts.run_quality_eval import run_quality
from scripts.utils import digest, read_json, write_json


class PromptAB(unittest.TestCase):
    def setup_inputs(self, root):
        a, b = root / 'a.md', root / 'b.md'
        a.write_text('Instruction set A: inspect all records.\n', encoding='utf-8')
        b.write_text('Instruction set B: inspect all records carefully.\n', encoding='utf-8')
        (root / 'support.txt').write_text('A synthetic fixture.\n', encoding='utf-8')
        evals = root / 'evals.json'
        write_json(evals, [{'id': 'one', 'prompt': 'return yes', 'expectations': ['correct'],
                            'files': ['support.txt'],
                            'checks': [{'text': 'correct', 'type': 'equals', 'file': 'result.txt', 'value': 'yes'}]}])
        return a, b, evals

    def executor(self, calls, fail_main_b=False):
        def run(prompt, target, model, **kwargs):
            instructions = kwargs.get('additional_instructions') or ''
            calls.append((target, kwargs.get('additional_instructions'), kwargs.get('skill'), kwargs.get('attach'), kwargs.get('skill_id')))
            failed = fail_main_b and 'pilot' not in target.parts and instructions.startswith('Instruction set B')
            status = 'timeout' if failed else 'completed'
            result = {'status': status, 'error': 'timeout' if failed else None,
                      'environment': {'server_version': '2.3', 'background': [], 'plugins': [],
                                      'configuration_fingerprint': 'same', 'controlled': True}}
            write_json(target / 'run.json', result)
            write_json(target / 'messages.json', [])
            (target / 'transcript.md').write_text('Fixture transcript')
            (target / 'outputs').mkdir()
            (target / 'outputs/result.txt').write_text('yes')
            write_json(target / 'timing.json', {'total_duration_seconds': 4 if instructions.startswith('Instruction set B') else 2})
            write_json(target / 'usage.json', {'executor': {'total_tokens': 20 if instructions.startswith('Instruction set B') else 10}})
            return result
        return run

    def test_two_frozen_arms_share_inputs_and_report_paired_quality_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            a, b, evals = self.setup_inputs(root)
            calls = []
            with patch('scripts.run_quality_eval.run_model', side_effect=self.executor(calls)):
                report = run_quality(None, evals, root / 'workspace', 'test/model', repeats=1,
                                     instruction_a=a, instruction_b=b)
            self.assertEqual(len(calls), 4)  # Two-arm pilot and two-arm main run.
            self.assertEqual({call[1] for call in calls}, {a.read_text(), b.read_text()})
            self.assertTrue(all(call[2:] == (None, False, None) for call in calls))
            self.assertEqual(set(report['metadata']['configurations']), {'prompt_a', 'prompt_b'})
            self.assertEqual(report['metadata']['instruction_files']['prompt_a']['sha256'], digest(a))
            self.assertEqual(report['metadata']['instruction_files']['prompt_b']['sha256'], digest(b))
            self.assertEqual(report['metadata']['instruction_files']['prompt_a']['placement'],
                             report['metadata']['instruction_files']['prompt_b']['placement'])
            delta = report['deltas']['prompt_b_minus_a']
            self.assertEqual(delta['paired_pass_rate']['mean'], 0)
            self.assertEqual(delta['paired_tokens']['mean'], 10)
            self.assertEqual(delta['paired_time_seconds']['mean'], 2)
            self.assertEqual(delta['incomplete_pairs'], [])
            static_review = root / 'workspace' / 'review.html'
            generator = Path(__file__).parents[1] / 'plugins/skillcraft/skills/skillcraft/scripts/generate_review.py'
            subprocess.run([sys.executable, str(generator), str(root / 'workspace'), '--static', str(static_review)],
                           check=True, capture_output=True, text=True)
            html = static_review.read_text(encoding='utf-8')
            self.assertIn('"configuration": "prompt_a"', html)
            self.assertIn('"configuration": "prompt_b"', html)
            self.assertIn('"prompt_b_minus_a"', html)
            self.assertIn('prompt_b − prompt_a', html)

    def test_incomplete_arm_is_counted_and_excluded_from_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            a, b, evals = self.setup_inputs(root)
            calls = []
            with patch('scripts.run_quality_eval.run_model', side_effect=self.executor(calls, fail_main_b=True)):
                report = run_quality(None, evals, root / 'workspace', 'test/model', repeats=1,
                                     instruction_a=a, instruction_b=b)
            delta = report['deltas']['prompt_b_minus_a']
            self.assertEqual(delta['paired_pass_rate']['n'], 0)
            self.assertEqual(delta['paired_tokens']['n'], 0)
            self.assertEqual(len(delta['incomplete_pairs']), 1)
            self.assertIn('execution status', ' '.join(delta['incomplete_pairs'][0]['reasons']))

    def test_prompt_pair_requires_two_distinct_nonempty_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            a, _, evals = self.setup_inputs(root)
            with self.assertRaisesRegex(ValueError, 'different bytes'):
                run_quality(None, evals, root / 'workspace', 'test/model', instruction_a=a, instruction_b=a)

    def test_prompt_pair_requires_known_matching_grading_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            a, b, evals = self.setup_inputs(root)
            workspace = root / 'workspace'
            with patch('scripts.run_quality_eval.run_model', side_effect=self.executor([])):
                run_quality(None, evals, workspace, 'test/model', repeats=1, instruction_a=a, instruction_b=b)
            run_a = 'eval-one/prompt_a/run-001'
            run_b = 'eval-one/prompt_b/run-001'
            alternate_grade = root / 'alternate'
            rubric = read_json(evals)[0]
            rubric['checks'][0]['value'] = 'no'
            grade_run(workspace / run_b, rubric, grading_dir=alternate_grade, harness='opencode')
            for index, grade in enumerate((str(alternate_grade), 'legacy')):
                with self.subTest(grade=grade):
                    if grade == 'legacy':
                        for run in (run_a, run_b):
                            (workspace / run / 'grading-manifest.json').unlink()
                    report = aggregate(workspace, selection={
                        'schema_version': 1, 'source_workspace': str(workspace), 'rule': 'Test grading provenance',
                        'runs': {run_a: {'selected': 'legacy'}, run_b: {'selected': grade}},
                    }, output=root / f'report-{index}')
                    delta = report['deltas']['prompt_b_minus_a']
                    self.assertEqual(delta['paired_pass_rate']['n'], 0)
                    self.assertEqual(delta['incomplete_pairs'][0]['reasons'], ['incompatible or unknown grading contract'])
                    self.assertIn('incompatible or unknown grading contracts', report['validation_errors'][0])

    def test_native_skill_baseline_keeps_skill_id_for_runtime_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            a, b, evals = self.setup_inputs(root)
            skill = root / 'example'
            skill.mkdir()
            (skill / 'SKILL.md').write_text('---\nname: example\ndescription: Example skill.\n---\nUse the task instructions.\n')
            (skill / 'support.txt').write_text('A synthetic fixture.\n', encoding='utf-8')
            calls = []
            with patch('scripts.run_quality_eval.run_model', side_effect=self.executor(calls)):
                run_quality(skill, evals, root / 'workspace', 'test/model', repeats=1)
            self.assertEqual({(call[3], call[4]) for call in calls}, {(False, 'example'), (True, 'example')}, calls)


if __name__ == '__main__':
    unittest.main()
