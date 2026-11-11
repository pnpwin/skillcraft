import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_quality_eval import run_quality
from scripts.scheduling import bounded_dispatch
from scripts.utils import write_json, read_json
from scripts.aggregate_benchmark import aggregate


class PilotGate(unittest.TestCase):
    def run_case(self, root, fault=None, *, previous=False, repeats=2):
        skill = root / 'example'
        skill.mkdir()
        (skill / 'SKILL.md').write_text('---\nname: example\ndescription: Example for pilot tests.\n---\nDo the task.\n')
        evals = root / 'evals.json'
        row = {'id': 'one', 'prompt': 'produce result', 'expectations': ['correct'],
               'checks': [{'text': 'correct', 'type': 'equals', 'file': 'result.txt', 'value': 'yes'}]}
        write_json(evals, [row])
        calls = []

        def executor(prompt, target, model, **kwargs):
            calls.append(target)
            if fault == 'transport':
                raise ValueError('Colliding input destination')
            status = 'permission_blocked' if fault == 'permission' else 'completed'
            if fault == 'main_timeout' and 'pilot' not in target.parts:
                status = 'timeout'
            result = {'status': status, 'error': None if status == 'completed' else status, 'environment': None}
            write_json(target / 'run.json', result)
            write_json(target / 'messages.json', [])
            (target / 'transcript.md').write_text('Unit fixture, not real execution')
            (target / 'outputs').mkdir()
            (target / 'outputs/result.txt').write_text('no')  # Valid quality FAIL must not block.
            return result

        def judge(prompt, target, model, **kwargs):
            target.mkdir()
            value = {'expectations': [], 'eval_feedback': {'issues': []}}
            if fault == 'critique':
                value['eval_feedback'] = {'issues': 'invalid'}
            (target / 'response.txt').write_text('invalid JSON' if fault == 'json' else json.dumps(value))
            return {'status': 'completed'}

        with patch('scripts.run_quality_eval.run_model', side_effect=executor), patch('scripts.grade.run_model', side_effect=judge):
            result = run_quality(skill, evals, root / 'workspace', 'test/model', repeats=repeats, judge_model='test/judge',
                                 previous=skill if previous else None)
        return result, calls

    def test_quality_fail_all_configurations_continue_and_pilot_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            report, calls = self.run_case(root, previous=True)
            self.assertEqual(report['pilot']['status'], 'completed')
            self.assertEqual(len(calls), 3 + 6)
            self.assertEqual(len(report['runs']), 6)
            self.assertEqual(report['run_summary']['with_skill']['pass_rate']['mean'], 0)
            self.assertFalse(report['errors'] or report['validation_errors'])
            pilot = read_json(root / 'workspace/pilot/benchmark.json')
            self.assertEqual(len(pilot['runs']), 3)
            self.assertEqual(pilot['missing_runs'], [])
            self.assertTrue((root / 'workspace/pilot/review.html').exists())

    def test_pilot_failure_stages_do_not_dispatch_main(self):
        for fault, stage in [('permission', 'executor'), ('transport', 'executor'), ('json', 'judge'), ('critique', 'critique')]:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                report, calls = self.run_case(Path(tmp).resolve(), fault)
                self.assertEqual(report['pilot']['status'], 'error')
                self.assertTrue(all('pilot' in path.parts for path in calls))
                self.assertEqual(report['pilot']['queue']['stop']['stage'], stage)
                self.assertEqual(len(report['scheduling']['not_started_after_stop']), 4)
                self.assertEqual(report['scheduling']['missing_unexpected'], [])
                self.assertTrue(report['errors'])
                if fault == 'critique':
                    grade = read_json(calls[0] / 'grading.json')
                    self.assertEqual(len(grade['expectations']), 1)
                    self.assertFalse(grade['evaluation_complete'])

    def test_main_repeated_error_stops_and_preserves_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, calls = self.run_case(Path(tmp).resolve(), 'main_timeout', repeats=5)
            self.assertEqual(report['pilot']['status'], 'completed')
            self.assertEqual(len(calls), 4)  # two pilots + two same failures
            self.assertEqual(report['scheduling']['planned'], 10)
            self.assertEqual(len(report['scheduling']['not_started_after_stop']), 8)
            self.assertEqual(report['run_summary']['with_skill']['planned_runs'], 5)

    def test_parallel_stop_drains_active_jobs_without_refill(self):
        barrier = threading.Barrier(2)
        started, snapshots = [], []
        def execute(job):
            started.append(job)
            barrier.wait(timeout=5)
            return {'complete': False, 'stage': 'judge', 'status': 'error', 'error': 'Invalid JSON'}
        outcomes, queue = bounded_dispatch([(str(i), i) for i in range(8)], execute, workers=2, threshold=1,
                                           persist=lambda state: snapshots.append(json.loads(json.dumps(state))))
        self.assertEqual(len(started), 2)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(s['status'] == 'not_started_after_stop' for s in queue['jobs'].values()), 6)

    def test_review_failure_blocks_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('scripts.run_quality_eval.subprocess.run') as review:
                review.return_value.returncode = 1
                review.return_value.stdout = ''
                review.return_value.stderr = 'viewer failed'
                report, calls = self.run_case(Path(tmp).resolve())
            self.assertEqual(len(calls), 2)
            self.assertEqual(report['pilot']['status'], 'error')
            self.assertIn('review generation failed', report['pilot']['error'])

    def test_report_failure_and_interrupt_are_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            def report(root, **kwargs):
                if Path(root).name == 'pilot':
                    raise ValueError('Injected report failure')
                return aggregate(root, **kwargs)
            with patch('scripts.aggregate_benchmark.aggregate', side_effect=report):
                result, calls = self.run_case(Path(tmp).resolve())
            self.assertEqual(len(calls), 2)
            self.assertEqual(result['pilot']['error']['stage'], 'report')
            self.assertTrue(result['errors'])
        from concurrent.futures import wait as real_wait
        first = True
        def interrupt_once(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt()
            return real_wait(*args, **kwargs)
        with patch('scripts.scheduling.wait', side_effect=interrupt_once):
            _, state = bounded_dispatch([(str(i), i) for i in range(4)], lambda job: {'complete': True},
                                        workers=1, threshold=2, persist=lambda state: None)
        self.assertEqual(state['stop']['stage'], 'dispatch')
        self.assertEqual(sum(s['status']=='not_started_after_stop' for s in state['jobs'].values()), 3)
