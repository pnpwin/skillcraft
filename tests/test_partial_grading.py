import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.grade import grade_run
from scripts.aggregate_benchmark import aggregate
from scripts.utils import write_json


class PartialGrading(unittest.TestCase):
    def setup_case(self, root):
        write_json(root/'run.json', {'status':'completed'})
        (root/'outputs').mkdir()
        (root/'outputs/result.txt').write_text('correct')
        (root/'transcript.md').write_text('evidence')
        return {'prompt':'do work', 'expectations':['a','b','c'],
                'checks':[{'text':s,'type':'equals','file':'result.txt','value':'correct'} for s in ['a','b']]}

    def test_timeout_preserves_two_verdicts_and_pending_without_full_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            row = self.setup_case(root)
            with patch('scripts.grade.run_model', return_value={'status':'timeout'}):
                result = grade_run(root, row, judge_model='test/model')
            self.assertEqual(result['status'], 'error')
            self.assertIsNone(result['summary'])
            self.assertEqual(result['pending_expectations'], ['c'])
            self.assertEqual(result['coverage'], {'graded':2,'total':3,'pending':1})
            self.assertEqual(len(result['expectations']),2)
            self.assertFalse(result['evaluation_complete'])
            self.assertEqual(json.loads((root/'grading.json').read_text()),result)

    def test_invalid_second_check_keeps_first_and_empty_rubric_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            row = self.setup_case(root)
            row['checks'][1]['type']='invalid'
            result=grade_run(root,row)
            self.assertEqual(result['failure_stage'],'checks')
            self.assertEqual([g['text'] for g in result['expectations']],['a'])
            self.assertEqual(result['pending_expectations'],['b','c'])
            empty_root=root/'new-execution'
            write_json(empty_root/'run.json', {'status':'completed'})
            empty=grade_run(empty_root,{'prompt':'work','expectations':[]})
            self.assertIsNone(empty['summary']['pass_rate'])

    def test_partial_visible_to_viewer_but_excluded_from_quality_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            write_json(root/'manifest.json',{'configurations':{'with_skill':{},'without_skill':{}}})
            for config in ['with_skill','without_skill']:
                r=root/'eval-x'/config/'run-001';r.mkdir(parents=True)
                row=self.setup_case(r)
                with patch('scripts.grade.run_model',return_value={'status':'timeout'}):
                    grade_run(r,row,judge_model='test/model')
            report=aggregate(root)
            self.assertEqual(report['deltas']['without_skill']['paired_pass_rate']['n'],0)
            self.assertEqual(report['run_summary']['with_skill']['evaluation_completion_rate'],0)
            spec=importlib.util.spec_from_file_location('partial_viewer',ROOT/'eval-viewer/generate_review.py')
            viewer=importlib.util.module_from_spec(spec);spec.loader.exec_module(viewer)
            data=viewer.build_run(root,root/'eval-x/with_skill/run-001')
            self.assertEqual(data['grading']['coverage']['graded'],2)
            self.assertEqual(data['grading']['pending_expectations'],['c'])
