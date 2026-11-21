from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import learning, learning_worker
from scripts.utils import tree_hash


class DynamicLearningWorkerTest(unittest.TestCase):
    def test_ineligible_discovered_skill_does_not_block_eligible_peer(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = root / 'state'
            skills = {}
            for name in ('stale', 'ready'):
                source = root / '.agents' / 'skills' / name
                source.mkdir(parents=True)
                (source / 'SKILL.md').write_text(
                    f'---\nname: {name}\ndescription: Demo.\n---\n\nBehavior.\n', encoding='utf-8')
                skills[name] = {'source': source.relative_to(root).as_posix()}
            config = {'root': root, 'state': state, 'selection': 'all', 'skills': skills}

            with learning.connection(config) as db:
                learning.queue(db, 'checkpoint')
                for index in range(3):
                    learning.record(db, f'evidence-{index}', 'ready',
                                    tree_hash(root / skills['ready']['source']), 'feedback',
                                    'codex', f's{index}', f't{index}', f'case-{index}', f'case-{index}',
                                    'correction', f'Keep behavior {index}.', True)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 3)

            reports = []

            def revise(_config, name, _settings, _source, _version, _rows, _attempt, _db, **_kwargs):
                reports.append(name)
                return {'skill': name, 'status': 'no_change'}

            with (patch.object(learning_worker, 'model_eligible', side_effect=lambda _c, name, _s: name == 'ready'),
                  patch.object(learning_worker, 'revise', side_effect=revise)):
                with learning.connection(config) as db:
                    learning_worker.drain(config, db)
                    revisions = [row['skill'] for row in db.execute('SELECT skill FROM revisions')]
                    status = db.execute('SELECT status FROM checkpoints').fetchone()['status']

            self.assertEqual(reports, ['ready'])
            self.assertEqual(revisions, ['ready'])
            self.assertEqual(status, 'done')

    def test_source_binding_detects_discovered_id_moving_to_another_path(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            captured = root / 'old-skill'
            current = root / 'new-skill'
            captured.mkdir()
            current.mkdir()
            self.assertFalse(learning_worker.source_matches_report(current, {'source': str(captured)}))


if __name__ == '__main__':
    unittest.main()
