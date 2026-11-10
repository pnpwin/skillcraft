import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.historical_baseline import freeze, verify
from scripts.shrink_description import reformulate, preserved_content, semantic_review, verify_variant_runs, resource_metrics
from scripts.utils import copy_tree, set_description, write_json
from scripts.compliance import assess


class Reformulation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.skill = self.root / 'source/demo-skill'
        copy_tree(ROOT / 'tests/fixtures/demo-skill', self.skill, skill=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_1024_is_not_rewritten_and_1025_enters_process(self):
        set_description(self.skill, 'x' * 1024)
        a = SimpleNamespace(skill=self.skill, workspace=self.root / 'no-change')
        with patch('scripts.shrink_description.improve_description') as optimizer:
            self.assertEqual(reformulate(a)['status'], 'not_needed')
        optimizer.assert_not_called()
        set_description(self.skill, 'x' * 1025)
        with patch('scripts.shrink_description.freeze', side_effect=ValueError('entered process')) as freeze_mock:
            a.workspace = self.root / 'long';a.repeats=1;a.workers=1
            with self.assertRaisesRegex(ValueError, 'entered process'):reformulate(a)
        freeze_mock.assert_called_once()

    def test_baseline_is_only_oversized_and_never_eligible(self):
        set_description(self.skill, 'x' * 1025)
        frozen = self.root / 'baseline/demo-skill';manifest=freeze(self.skill, frozen)
        self.assertFalse(verify(frozen, manifest)['eligible_for_promotion'])
        self.assertEqual(assess(frozen)['agent_skills']['status'], 'FAIL')
        p=frozen/'SKILL.md';p.chmod(0o644);set_description(frozen, 'Other valid description')
        with self.assertRaises(ValueError):verify(frozen, manifest)
        (self.skill/'SKILL.md').write_text((self.skill/'SKILL.md').read_text().replace('name: demo-skill','name: Demo Skill'))
        with self.assertRaises(ValueError):freeze(self.skill,self.root/'bad/demo-skill')

    def test_body_optional_types_and_resources_preserved(self):
        with (self.skill/'SKILL.md').open('a') as f:f.write('\nFull instructions\n')
        (self.skill/'references').mkdir();(self.skill/'references/SKILL.md').write_text('Nested resource')
        target=self.root/'candidate/demo-skill';copy_tree(self.skill,target,skill=True)
        set_description(target,'Different description')
        preserved_content(self.skill,target)
        (target/'references/SKILL.md').write_text('Modified')
        with self.assertRaises(ValueError):preserved_content(self.skill,target)

    def test_blind_request_has_no_mapping_and_verdict_precedes_unblinding(self):
        other=self.root/'candidate/demo-skill';copy_tree(self.skill,other,skill=True)
        set_description(other,'Use whenever processing purchase receipts.')
        output=self.root/'blind'
        def model(prompt,target,*args,**kwargs):
            self.assertNotIn(str(self.skill),prompt);self.assertNotIn(str(other),prompt)
            self.assertNotIn('historical',prompt);self.assertNotIn('candidate',prompt)
            self.assertFalse((output/'unblinded.json').exists())
            target.mkdir();write_json(target/'response.txt',{'A':{'missing':[],'invented':[]},'B':{'missing':[],'invented':[]},'reasoning':'Scope preserved'})
            return {'status':'completed'}
        with patch('scripts.shrink_description.run_model',side_effect=model):
            result=semantic_review(self.skill,other,output,'test/model',seed=42,timeout=20,profile=None)
        self.assertTrue(result['preserved'])
        self.assertLessEqual((output/'verdict.json').stat().st_mtime_ns,(output/'unblinded.json').stat().st_mtime_ns)

    def test_cross_split_snapshot_drift_and_missing_usage_are_not_hidden(self):
        verify_variant_runs('fixed', {'results': [{'installed_hash': 'fixed'}]})
        for rows in [[], [{'installed_hash': 'other'}], [{}]]:
            with self.assertRaises(ValueError):verify_variant_runs('fixed', {'results': rows})
        target=self.root/'trigger/eval-one/natural/run-001';target.mkdir(parents=True)
        metrics=resource_metrics(self.root/'trigger')
        self.assertIsNone(metrics['executor_tokens']);self.assertEqual(metrics['unknown_token_runs'],1)


if __name__ == '__main__':
    unittest.main()
