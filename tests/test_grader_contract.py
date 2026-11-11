import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.grade import grade_run, validate_grading
from scripts.utils import write_json


class GraderContract(unittest.TestCase):
    def test_mixed_payload_and_permission_status_cannot_be_masked_by_valid_json(self):
        for status in ['completed', 'permission_blocked']:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                write_json(root/'run.json', {'status': 'completed'})
                write_json(root/'outputs/root/package.json', {'name':'root'})
                write_json(root/'outputs/root/web/package.json', {'name':'web'})
                (root/'transcript.md').write_text('genuine evidence')
                row = {'prompt':'inspect files', 'expectations':['root', 'web'],
                       'checks':[{'text':'root','type':'json_equals','file':'root/package.json','path':['name'],'value':'root'}]}
                def judge(prompt, target, *args, **kwargs):
                    self.assertNotIn('outputs_dir', prompt)
                    self.assertIn('inputs/artifacts/root/web/package.json', prompt)
                    self.assertIn('inputs/evidence/transcript.md', prompt)
                    self.assertIn('"grade_expectations": ["web"]', prompt)
                    target.mkdir()
                    write_json(target/'response.txt', {'expectations':[{'text':'web','passed':True,'evidence':'nested file name is web'}],
                                                      'eval_feedback':{'issues':[]}})
                    return {'status':status}
                with patch('scripts.grade.run_model', side_effect=judge):
                    grade = grade_run(root, row, judge_model='test/model')
                self.assertEqual(grade['status'], 'completed' if status == 'completed' else 'error')
                self.assertEqual(grade['grader_contract']['version'], 2)

    def test_missing_extra_and_nonboolean_verdicts_rejected(self):
        for rows in [[], [{'text':'extra','passed':True,'evidence':'x'}], [{'text':'a','passed':'true','evidence':'x'}]]:
            with self.assertRaises(ValueError):
                validate_grading({'expectations':rows}, ['a'])
