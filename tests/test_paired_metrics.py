from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.aggregate_benchmark import aggregate
from scripts.utils import write_json


class PairedMetrics(unittest.TestCase):
    def test_independent_sample_counts_zero_missing_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configs = ['without_skill', 'with_skill', 'previous_skill']
            write_json(root / 'manifest.json', {'configurations': dict.fromkeys(configs, {}),
                       'evals': [{'id': 'one'}], 'repeats': 4, 'environment_consistent': True})
            for config in configs:
                for repeat in range(1, 4):
                    run = root / f'eval-one/{config}/run-{repeat:03d}'
                    write_json(run / 'run.json', {'status': 'timeout' if repeat == 3 else 'completed'})
                    write_json(run / 'grading.json', {'status': 'completed', 'summary': {'pass_rate': 1}})
                    write_json(run / 'timing.json', {'total_duration_seconds': 2 if config == 'with_skill' else 0})
                    write_json(run / 'usage.json', {'executor': {'total_tokens': (10 if config == 'with_skill' else 0) if repeat == 1 else None}})
            result = aggregate(root)
            for baseline in ['without_skill', 'previous_skill']:
                d = result['deltas'][baseline]
                self.assertEqual(d['paired_time_seconds']['mean'], 2)
                self.assertEqual(d['paired_time_seconds']['n'], 2)
                self.assertEqual(d['paired_tokens']['mean'], 10)
                self.assertEqual(d['paired_tokens']['n'], 1)
                self.assertIsNone(d['paired_tokens']['stddev'])
                self.assertEqual(len(d['excluded_pairs']), 1)
            s = result['run_summary']['with_skill']
            self.assertEqual(s['completion_rate'], 2 / 3)
            self.assertEqual(s['planned_completion_rate'], .5)
            self.assertEqual(s['missing_runs'], 1)

    def test_drift_and_unmatched_pairs_do_not_produce_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / 'manifest.json', {'configurations': {'with_skill': {}, 'without_skill': {}}, 'environment_consistent': False})
            for config in ['with_skill', 'without_skill']:
                run = root / f'eval-one/{config}/run-001'
                write_json(run / 'run.json', {'status': 'completed'})
                write_json(run / 'timing.json', {'total_duration_seconds': 10})
            self.assertEqual(aggregate(root)['deltas']['without_skill']['paired_time_seconds']['n'], 0)

    def test_model_mismatch_and_unmatched_repeat_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / 'manifest.json', {'configurations': {'with_skill': {}, 'without_skill': {}}, 'environment_consistent': True})
            for config, model in [('with_skill', 'a'), ('without_skill', 'b')]:
                run = root / f'eval-one/{config}/run-001'
                write_json(run / 'run.json', {'status': 'completed', 'model': model})
                write_json(run / 'timing.json', {'total_duration_seconds': 10})
            run = root / 'eval-one/with_skill/run-002'
            write_json(run / 'run.json', {'status': 'completed', 'model': 'a'})
            write_json(run / 'timing.json', {'total_duration_seconds': 1})
            d = aggregate(root)['deltas']['without_skill']
            self.assertEqual(d['paired_time_seconds']['n'], 0)
            self.assertEqual(len(d['excluded_pairs']), 2)
            self.assertTrue(any('baseline missing or excluded' in reason for reason in d['excluded_pairs']))


if __name__ == '__main__':
    unittest.main()
