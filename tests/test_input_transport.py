import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.inputs import copy_inputs, input_specs, serial_inputs
from scripts.utils import snapshot_fixtures


class InputTransport(unittest.TestCase):
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
