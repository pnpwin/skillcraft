import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.grade import grade_run
from scripts.grading_store import read_grading
from scripts.grading_store import reserve_attempt
from scripts.grading_store import grading_signature
from scripts.aggregate_benchmark import aggregate
from scripts.utils import write_json,tree_hash


class Regrading(unittest.TestCase):
    def test_judge_environment_drift_excludes_pair_but_temp_paths_do_not(self):
        base={'rubric_hash':'r','judge_model':'model','judge_profile_hash':'p','grader_contract':{'version':2},
              'judge_execution':{'runner_hash':'runner','environment':{'server_version':'2.2.7',
                'configuration_fingerprint':'config','background':[],'plugins':[],'controlled':False,
                'configuration_sources':['/temp/first/config.json']}}}
        for field,value in [('server_version','2.2.8'),('configuration_fingerprint','different'),
                            ('background',[{'id':'other','hash':'changed'}]),('plugins',['new-plugin']),('controlled',True)]:
            changed={**base,'judge_execution':{**base['judge_execution'],'environment':{**base['judge_execution']['environment'],field:value}}}
            self.assertNotEqual(grading_signature(base),grading_signature(changed),field)
        moved={**base,'judge_execution':{**base['judge_execution'],'environment':{**base['judge_execution']['environment'],
               'configuration_sources':['/temp/second/config.json'],'concerns':['Location-specific diagnostic']}}}
        self.assertEqual(grading_signature(base),grading_signature(moved))

    def prepare(self, workspace):
        row={'id':'one','prompt':'produce result','expectations':['correct'],
             'checks':[{'text':'correct','type':'equals','file':'result.txt','value':'yes'}]}
        write_json(workspace/'manifest.json',{'configurations':{'without_skill':{},'with_skill':{}},'evals':[row],'repeats':1})
        for config in ['without_skill','with_skill']:
            run=workspace/'eval-one'/config/'run-001'
            write_json(run/'run.json',{'status':'completed','run_id':config,'model':'m','agent':'a','fixtures':{}})
            write_json(run/'messages.json',[])
            write_json(run/'eval_metadata.json',row)
            write_json(run/'request.json',{'prompt':row['prompt']})
            (run/'outputs').mkdir();(run/'outputs/result.txt').write_text('yes')
            (run/'transcript.md').write_text('synthetic unit-test execution fixture')
            grade_run(run,row)
        return row

    def test_two_attempts_readers_hashes_and_explicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'source';row=self.prepare(source)
            aggregate(source);before=tree_hash(source)
            selection={'schema_version':1,'source_workspace':str(source),'rule':'all runs, same rubric','runs':{}}
            with patch('scripts.grade.run_model') as model:
                for config in ['without_skill','with_skill']:
                    key=f'eval-one/{config}/run-001';run=source/key
                    attempts=[]
                    for n in [1,2]:
                        attempt=root/f'{config}-attempt-{n}'
                        result=grade_run(run,row,grading_dir=attempt)
                        self.assertEqual(result['status'],'completed');attempts.append(str(attempt))
                    selection['runs'][key]={'selected':attempts[1],'history':attempts}
                    with self.assertRaises(FileExistsError):grade_run(run,row,grading_dir=attempt)
                    with self.assertRaises(ValueError):grade_run(run,row)
                model.assert_not_called()
            report=aggregate(source,selection=selection,output=root/'report')
            self.assertEqual(report['deltas']['without_skill']['paired_pass_rate']['n'],1)
            self.assertTrue(all(r['judge_attempts']==3 for r in report['runs']))
            self.assertEqual(tree_hash(source),before)
            spec=importlib.util.spec_from_file_location('attempt_viewer',ROOT/'eval-viewer/generate_review.py')
            viewer=importlib.util.module_from_spec(spec);spec.loader.exec_module(viewer)
            runs=viewer.find_runs(source,selection)
            self.assertTrue(all('@' in r['id'] and len(r['grading_history'])==3 for r in runs))
            bad={'schema_version':1,'reviews':[{'run_id':'with_skill','snapshot_hash':None,'feedback':'x','reviewed':True}]}
            with self.assertRaises(ValueError):viewer.validate_feedback(bad,runs)
            self.assertEqual(tree_hash(source),before)

    def test_changed_task_missing_evidence_corruption_and_mixed_rubric(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'source';row=self.prepare(source)
            run=source/'eval-one/with_skill/run-001'
            for changed in [{**row,'prompt':'different task'},{**row,'files':['new.py']},
                            {**row,'preparation':[{'tool':'index','args':['rebuild'],'cwd':'inputs'}]},
                            {**row,'required_evidence':['outputs/missing.txt']}]:
                with self.assertRaises(ValueError):grade_run(run,changed,grading_dir=root/'bad')
                self.assertFalse((root/'bad').exists())
            revised={**row,'expectations':['new criterion'],'checks':[{'text':'new criterion','type':'equals','file':'result.txt','value':'yes'}]}
            grade_run(run,revised,grading_dir=root/'new')
            selection={'schema_version':1,'source_workspace':str(source),'rule':'intentional mismatch test','runs':{
                'eval-one/with_skill/run-001':{'selected':str(root/'new')},
                'eval-one/without_skill/run-001':{'selected':'legacy'}}}
            report=aggregate(source,selection=selection,output=root/'report')
            self.assertEqual(report['deltas']['without_skill']['paired_pass_rate']['n'],0)
            self.assertTrue(report['validation_errors'])
            (run/'outputs/result.txt').write_text('changed')
            with self.assertRaises(ValueError):read_grading(run,root/'new')
            with self.assertRaises(ValueError):grade_run(run,row,grading_dir=root/'regrade-mutated')

    def test_concurrent_attempt_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'source';row=self.prepare(source)
            def work(_):
                try:return grade_run(source/'eval-one/with_skill/run-001',row,grading_dir=root/'attempt')['status']
                except FileExistsError:return 'already_exists'
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(work,range(2)))
            self.assertCountEqual(results,['completed','already_exists'])

    def test_unsealed_attempt_visible_only_as_history_and_costs_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'source';row=self.prepare(source)
            run=source/'eval-one/with_skill/run-001'
            destination,_=reserve_attempt(run,row,root/'unsealed','test/model',None)
            with self.assertRaises(ValueError):read_grading(run,destination)
            self.assertEqual(read_grading(run,destination,allow_incomplete=True)[0]['status'],'unsealed')
            for config in ['without_skill','with_skill']:
                p=source/'eval-one'/config/'run-001'
                write_json(p/'judge/usage.json',{'executor':{'total_tokens':10}})
            selection={'schema_version':1,'source_workspace':str(source),'rule':'alias deduplication test','runs':{
                f'eval-one/{c}/run-001':{'selected':'legacy','history':[str(source/'eval-one'/c/'run-001')]}
                for c in ['without_skill','with_skill']}}
            report=aggregate(source,selection=selection,output=root/'report')
            self.assertTrue(all(r['all_judge_tokens']==10 and r['judge_attempts']==1 for r in report['runs']))
