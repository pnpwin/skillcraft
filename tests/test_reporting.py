import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import SKILL
from scripts.reporting import concise,resource_totals
from scripts.aggregate_benchmark import aggregate
from scripts.utils import write_json


class Reporting(unittest.TestCase):
    def test_bounded_summary_for_large_payload_and_unknowns(self):
        report={'metadata':{'evals':[{'id':str(i)} for i in range(100)],'repeats':3,'huge':'x'*1000000},
                'runs':[{'run':str(i),'status':'timeout','grading_status':'missing','error':'x'*10000} for i in range(1000)],
                'errors':1000,'validation_errors':['failure']*100,'run_summary':{}}
        self.assertLessEqual(len(concise(report,'report/benchmark.json').splitlines()),40)
        self.assertIsNone(resource_totals([None,10])['total'])
        self.assertEqual(resource_totals([None,10])['known_sum'],10)

    def test_cli_default_json_and_subset_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'source'
            write_json(source/'manifest.json',{'configurations':{'with_skill':{}},'evals':[{'id':'one'}],'repeats':2,'large_metadata':'x'*100000})
            specs={}
            for n in [1,2]:
                key=f'eval-one/with_skill/run-{n:03d}';run=source/key
                write_json(run/'run.json',{'status':'completed','run_id':str(n)})
                write_json(run/'grading.json',{'status':'completed','summary':{'pass_rate':float(n==1)}})
                write_json(run/'usage.json',{'executor':{'total_tokens':n*10,'cost':0.5 if n==1 else None},'maintenance':None})
                specs[key]={'selected':'legacy','included':n==1,'reason':'fixed first-repeat interface sample, not performance selection'}
            proc=subprocess.run([sys.executable,str(SKILL / 'scripts/aggregate_benchmark.py'),str(source)],capture_output=True,text=True)
            self.assertEqual(proc.returncode,0,proc.stderr)
            self.assertIn('COMPLETE',proc.stdout);self.assertLessEqual(len(proc.stdout.splitlines()),40)
            self.assertLess(len(proc.stdout),8000)
            self.assertIn(str(source/'benchmark.json'),proc.stdout)
            proc=subprocess.run([sys.executable,str(SKILL / 'scripts/aggregate_benchmark.py'),str(source),'--json'],capture_output=True,text=True)
            self.assertEqual(json.loads(proc.stdout),json.loads((source/'benchmark.json').read_text()))
            selection={'schema_version':1,'source_workspace':str(source),'rule':'first repeat fixed by test design','runs':specs}
            report=aggregate(source,selection=selection,output=root/'subset')
            self.assertEqual(report['run_summary']['with_skill']['pass_rate']['mean'],1)
            self.assertEqual(report['run_summary']['with_skill']['excluded_runs'],1)
            self.assertEqual(len(report['runs']),2)
            self.assertEqual(report['resources']['source_executions']['tokens']['total'],30)
            self.assertEqual(report['resources']['selected_executions']['tokens']['total'],10)
            self.assertIsNone(report['resources']['source_executions']['cost']['total'])
            self.assertEqual(report['resources']['selected_executions']['cost']['total'],0.5)
