import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.compare import compare
from scripts.inputs import copy_inputs, input_specs, serial_inputs
from scripts.utils import snapshot_fixtures, write_json


class InputTransport(unittest.TestCase):
    def test_blind_comparison_copies_inputs_under_canonical_temp_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            actual = root / 'actual-temp'
            actual.mkdir()
            alias = root / 'temp-alias'
            alias.symlink_to(actual, target_is_directory=True)
            left, right = root / 'left', root / 'right'
            for run, answer in ((left, 'OLD'), (right, 'NEW')):
                write_json(run / 'run.json', {'status': 'completed'})
                write_json(run / 'eval_metadata.json', {'prompt': 'Synthetic receipt', 'expectations': ['NEW']})
                (run / 'outputs').mkdir()
                (run / 'outputs' / 'answer.txt').write_text(answer)

            def judge(prompt, output, model, *, files, **kwargs):
                input_specs(files)
                self.assertEqual({file.read_text() for file in files}, {'OLD', 'NEW'})
                self.assertTrue(all(actual in file.parents for file in files))
                write_json(output / 'response.txt', {'winner': 'A', 'reasoning': 'Synthetic judgment'})
                return {'status': 'completed'}

            args = SimpleNamespace(left=left, right=right, output=root / 'comparison',
                                   model='test', seed=42, timeout=1, profile=None,
                                   analyzer_model=None, harness='codex')
            with patch('tempfile.tempdir', str(alias)), patch('scripts.compare.run_model', judge):
                self.assertEqual(compare(args)['winner'], 'A')

    def test_nested_roundtrip_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            sources = root / 'sources'
            entries = []
            for name, text in [('root/package.json', 'root'), ('root/packages/веб app/package.json', 'web'), ('transcript.md', 'user')]:
                p = sources / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
                entries.append({'source': name, 'destination': name})
            (sources / 'legacy.txt').write_text('legacy')
            rows = [{'id': 'case', 'files': entries + ['legacy.txt']}]
            frozen, hashes = snapshot_fixtures(rows, sources, root / 'experiment')
            encoded = json.loads(json.dumps(serial_inputs(frozen['case'])))
            copied = copy_inputs(encoded, root / 'executor/inputs')
            for e in copied:
                self.assertEqual(e['sha256'], hashes['case'][e['destination']])
            self.assertEqual((root / 'executor/inputs/root/packages/веб app/package.json').read_text(), 'web')
            judge = [{'source': e['copied'], 'destination': 'artifacts/' + e['destination']} for e in copied]
            copy_inputs(judge, root / 'judge/inputs')
            self.assertEqual((root / 'judge/inputs/artifacts/transcript.md').read_text(), 'user')

    def test_escape_collisions_and_symlink_parents_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'source').write_text('test')
            def spec(dest):
                return {'source': str(root / 'source'), 'destination': dest}
            for entries in [[spec('../outside')], [spec('/absolute')], [spec('a'), spec('a')],
                            [spec('A'), spec('a')], [spec('a'), spec('a/file')], [spec('a/../b')]]:
                with self.assertRaises(ValueError):
                    copy_inputs(entries, root / 'target')
                self.assertFalse((root / 'target').exists())
            (root / 'link').symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                input_specs([{'source': 'link/source', 'destination': 'safe'}], base=root)
            (root / 'target').mkdir()
            (root / 'target/escape').symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                copy_inputs([spec('escape/stolen')], root / 'target')
