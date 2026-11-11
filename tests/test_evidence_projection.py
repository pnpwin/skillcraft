import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evidence import project_evidence, verify_evidence, judge_evidence
from scripts.inputs import copy_inputs
from scripts.utils import write_json, digest


class EvidenceProjection(unittest.TestCase):
    def test_lossless_unicode_tail_errors_and_determinism(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            data=[{'id':'user1','type':'user','text':'Task'},
                  {'id':'assistant1','type':'assistant','content':[{'id':'call1','type':'tool','name':'shell',
                   'state':{'status':'error','input':{'command':'check'},'content':[{'type':'text','text':'Ж'*20000+'CRITICAL_TAIL_72'}],
                            'error':'permission denied'}}]},
                  {'id':'assistant2','type':'assistant','content':[{'type':'text','text':'Final answer'}]}]
            manifests=[]
            for name in ['a','b']:
                run=root/name
                write_json(run/'messages.json',data)
                (run/'transcript.md').write_text('legacy retained')
                raw=digest(run/'messages.json')
                manifests.append(project_evidence(run))
                self.assertEqual(digest(run/'messages.json'),raw)
                self.assertEqual((run/'transcript.md').read_text(),'legacy retained')
                self.assertTrue(all((run/'evidence'/p).stat().st_size<16384 for p in manifests[-1]['files']))
                files,_=judge_evidence(run)
                copy_inputs(files,root/f'judge-{name}/inputs')
                tail=manifests[-1]['messages'][1]['parts'][-1]['path']
                self.assertIn('CRITICAL_TAIL_72',(root/f'judge-{name}/inputs/evidence'/tail).read_text())
            self.assertEqual(manifests[0],manifests[1])
            part=root/'a/evidence'/manifests[0]['messages'][1]['parts'][-1]['path']
            part.write_text('tampered')
            with self.assertRaises(ValueError):verify_evidence(root/'a')

    def test_missing_parts_and_source_mismatch_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            write_json(root/'messages.json',[{'type':'user','text':'proof'}])
            m=project_evidence(root)
            (root/'evidence'/m['messages'][0]['parts'][0]['path']).unlink()
            with self.assertRaises(OSError):judge_evidence(root)
            write_json(root/'messages.json',[])
            with self.assertRaises(ValueError):judge_evidence(root)
