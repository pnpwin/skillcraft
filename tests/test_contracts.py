import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.utils import frontmatter, copy_tree, load_evals, snapshot_fixtures, write_json
from scripts.run_eval import summarize
from scripts.run_loop import split_evals, selection_score
from scripts.grade import grade_run, validate_grading, check_one
from scripts.aggregate_benchmark import stats, aggregate
from scripts.package_skill import should_exclude

spec = importlib.util.spec_from_file_location('viewer', ROOT / 'eval-viewer' / 'generate_review.py')
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_yaml_block_scalars_and_alias_loader(self):
        skill = self.root / 'SKILL.md'
        skill.write_text('---\nname: "A: B"\ndescription: >-\n  first line\n  second line\n---\nBody\n')
        self.assertEqual(frontmatter(skill)[0]['description'], 'first line second line')
        evals = self.root / 'evals.json'
        write_json(evals, {'evals': [{'id': 'one', 'prompt': 'p', 'assertions': ['a']}, {'id': 'two', 'prompt': 'p'}]})
        self.assertEqual(len(load_evals(evals)), 2)
        self.assertEqual(load_evals(evals)[0]['expectations'], ['a'])

    def test_symlink_and_recursive_snapshot_rejected(self):
        src = self.root / 'source'
        src.mkdir()
        (src / 'escape').symlink_to('/etc/hosts')
        with self.assertRaises(ValueError):
            copy_tree(src, self.root / 'copy')
        with self.assertRaises(ValueError):
            copy_tree(src, src / 'copy')

    def test_error_is_not_false_negative(self):
        report = summarize([
            {'status': 'timeout', 'triggered': None, 'should_trigger': True},
            {'status': 'completed', 'triggered': True, 'should_trigger': True},
            {'status': 'completed', 'triggered': False, 'should_trigger': False},
        ])
        self.assertEqual(report['fn'], 0)
        self.assertEqual(report['errors'], 1)
        self.assertIsNone(selection_score({'summary': report}))

    def test_grouped_split_prevents_duplicate_leakage(self):
        rows = load_evals(ROOT / 'tests' / 'fixtures' / 'trigger.json', trigger=True)
        rows += [{**rows[0], 'id': 'duplicate'}]
        splits = split_evals(rows)
        where = {r['id']: k for k, values in splits.items() for r in values}
        self.assertEqual(where['receipt-a'], where['duplicate'])
        self.assertEqual(set(splits), {'train', 'validation', 'test'})
        self.assertTrue(splits['test'])

    def test_invalid_judge_and_missing_judge_are_errors(self):
        with self.assertRaises(ValueError):
            validate_grading({'expectations': [{'text': 'x', 'passed': 'true', 'evidence': ''}]}, ['x'])
        write_json(self.root / 'run.json', {'status': 'completed'})
        (self.root / 'outputs').mkdir()
        result = grade_run(self.root, {'prompt': 'p', 'expectations': ['x']})
        self.assertEqual(result['status'], 'error')
        self.assertIsNone(result['summary'])

    def test_deterministic_check_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            check_one(self.root, {'type': 'exists', 'file': '../secret', 'text': 'x'})

    def test_missing_metrics_remain_unknown(self):
        self.assertIsNone(stats([None])['mean'])
        self.assertIsNone(stats([1])['stddev'])
        write_json(self.root / 'manifest.json', {'configurations': {'without_skill': {}, 'with_skill': {}}})
        r = self.root / 'eval-1' / 'with_skill' / 'run-001'
        write_json(r / 'run.json', {'status': 'timeout'})
        (r / 'outputs').mkdir()
        (r / 'outputs' / 'out.txt').write_text('x' * 200)
        result = aggregate(self.root)
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['runs'][0]['output_chars'], 200)
        self.assertIsNone(result['runs'][0]['tokens'])
        self.assertEqual(result['run_summary']['without_skill']['runs'], 0)

    def test_viewer_nested_layout_and_script_injection(self):
        run = self.root / 'eval-1' / 'with_skill' / 'run-001'
        (run / 'outputs').mkdir(parents=True)
        (run / 'outputs' / 'x.txt').write_text('</script><script>alert(1)</script>')
        write_json(run / 'run.json', {'run_id': 'unique', 'status': 'completed'})
        write_json(run.parent.parent / 'eval_metadata.json', {'eval_id': 'one', 'prompt': 'Prompt'})
        runs = viewer.find_runs(self.root)
        self.assertEqual(runs[0]['prompt'], 'Prompt')
        self.assertNotIn('</script><script>alert(1)', viewer.generate_html(runs, 'demo'))

    def test_feedback_rejects_stale_snapshot_and_unreviewed_complete(self):
        runs = [{'id': 'one', 'snapshot_hash': 'abc'}]
        review = {'run_id': 'one', 'snapshot_hash': 'other', 'reviewed': True, 'feedback': 'test'}
        data = {'schema_version': 1, 'reviews': [review], 'status': 'complete'}
        with self.assertRaises(ValueError):
            viewer.validate_feedback(data, runs)
        review.update(snapshot_hash='abc', reviewed=False)
        with self.assertRaises(ValueError):
            viewer.validate_feedback(data, runs)
        review['reviewed'] = True
        self.assertEqual(viewer.validate_feedback(data, runs), data)

    def test_distribution_excludes_runtime_and_eval_data(self):
        for path in ['skill/evals/test.json', 'skill/.venv/bin/python', 'skill/runner/node_modules/pkg/a.js']:
            self.assertTrue(should_exclude(Path(path)))
        self.assertFalse(should_exclude(Path('skill/runner/package-lock.json')))

    def test_fixtures_are_frozen_before_workers(self):
        original = self.root / 'input.txt'
        original.write_text('version one')
        rows = [{'id': 'one', 'files': ['input.txt']}]
        files, hashes = snapshot_fixtures(rows, self.root, self.root / 'experiment')
        original.write_text('version two')
        self.assertEqual(files['one'][0].read_text(), 'version one')
        self.assertEqual(len(hashes['one']['input.txt']), 64)

    def test_incomplete_experiment_is_not_silently_successful(self):
        write_json(self.root / 'manifest.json', {'configurations': {'without_skill': {}, 'with_skill': {}},
                   'evals': [{'id': 'one'}], 'repeats': 2})
        report = aggregate(self.root)
        self.assertEqual(len(report['missing_runs']), 4)
        self.assertEqual(report['errors'], 4)


if __name__ == '__main__':
    unittest.main()
