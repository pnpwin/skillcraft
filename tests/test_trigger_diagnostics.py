from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.trigger_diagnostics import diagnose
from scripts.run_eval import summarize
from scripts.utils import write_json
from scripts.cli_state import prepare_tools, collect_state
from scripts.preparation import prepare_fixture
from scripts.runtime import run_model


class TriggerDiagnostics(unittest.TestCase):
    def test_shell_calls_counts_completed_commands_in_both_harnesses(self):
        for harness, tool_name in [('opencode', 'shell'), ('codex', 'commandExecution')]:
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tools = [
                    {'type': 'tool', 'name': name, 'state': {'status': status}}
                    for name, status in [(tool_name, 'completed'), (tool_name, 'failed'),
                                         (tool_name, 'inProgress'), ('read', 'completed')]
                ]
                write_json(root / 'messages.json', [{'type': 'assistant', 'content': tools}])
                result = {'harness': harness, 'status': 'completed', 'triggered': False}
                self.assertEqual(diagnose(root, result, 'candidate')['shell_calls'], 1)

    def test_load_survives_error_and_preparation_is_not_model_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root/'messages.json', [{'type':'assistant','time':{'completed':20},'content':[
                {'type':'tool','name':'skill','state':{'status':'completed','input':{'id':'candidate'}}}]}])
            result = {'status':'timeout','triggered':True,'cli_state':{'observations':[{'calls':[
                {'phase':'preparation','started_ms':10}]}]}}
            d = diagnose(root,result,'candidate')
            self.assertTrue(d['skill_loaded']);self.assertFalse(d['selection_valid'])
            self.assertEqual(d['scoped_cli_calls'],0)
            result['cli_state']['observations'][0]['calls'].append({'phase':'model','started_ms':30})
            self.assertTrue(diagnose(root,result,'candidate')['scoped_cli_after_load'])
            summary = summarize([{**result,'should_trigger':True}, {'status':'completed','triggered':False,'should_trigger':False},
                                 {'status':'preparation_error','triggered':None,'should_trigger':True}])
            self.assertEqual((summary['valid'],summary['tn'],summary['fn'],summary['errors']),(1,1,0,2))

    def test_final_root_preparation_and_failure_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'inputs/project').mkdir(parents=True)
            spec=prepare_tools(root,[{'id':'python','executable':sys.executable,'environment':{'CACHE':'{state}/cache'}}])
            code="import os,pathlib; p=pathlib.Path(os.environ['CACHE']); p.mkdir(); (p/'root').write_text(os.getcwd()); print(os.getcwd())"
            evidence=prepare_fixture(root,spec,[{'tool':'python','args':['-c',code],'cwd':'inputs/project'}],root/'report')
            self.assertIn(str(root/'inputs/project'),evidence['steps'][0]['stdout'])
            self.assertEqual(collect_state(spec,root/'report')['observations'][0]['calls'][0]['phase'],'preparation')
            with self.assertRaises(ValueError):
                prepare_fixture(root,spec,[{'tool':'python','args':[],'cwd':'../outside'}],root/'bad')
            with self.assertRaises(ValueError):
                prepare_fixture(root,spec,[{'tool':'python','args':['-c','raise SystemExit(3)'],'cwd':'inputs/project'}],root/'failed')
            self.assertTrue((root/'failed/preparation.json').exists())

    def test_preparation_error_prevents_any_model_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            profile=root/'profile.json'
            write_json(profile,{'inherit_bounded_files':True})
            with patch('scripts.runtime.subprocess.Popen') as process:
                result=run_model('task',root/'run','test/model',profile=profile,
                                 preparation=[{'tool':'unconfigured','args':[],'cwd':'inputs'}])
            process.assert_not_called()
            self.assertEqual(result['status'],'preparation_error')
            self.assertIsNone(result['triggered'])
