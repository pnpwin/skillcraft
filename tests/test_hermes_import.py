from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import learning, learning_worker
from scripts.utils import tree_hash, write_json


class HermesImportTest(unittest.TestCase):
    def setUp(self):
        workspace = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.profile = workspace / 'Hermes with spaces'
        self.project = workspace / 'project'
        self.owner = workspace / 'owner'
        self.skill = self.project / '.agents/skills/demo'
        self.skill.mkdir(parents=True)
        self.markdown = self.skill / 'SKILL.md'
        self.markdown.write_text('---\nname: demo\ndescription: A demo skill.\n---\n\nRead the notes.\n')
        write_json(self.project / learning.CONFIG, {
            'schema_version': 1, 'enabled': True, 'sources': ['sessions'],
            'skills': {'demo': {'source': '.agents/skills/demo'}},
        })
        self.enterContext(patch.object(Path, 'home', return_value=self.owner))
        self.enterContext(patch.dict(os.environ, {learning.INTERNAL: '0'}))
        self.wake = self.enterContext(patch.object(learning, 'wake'))
        self.run_cli('enable', str(self.project))
        self.run_cli('--hermes-home', str(self.profile), 'enable', str(self.project))
        self.target = learning.config_for(self.project)
        self.source = learning.config_for(self.project, hermes_home=self.profile)

    def run_cli(self, *args):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(learning.main(list(args)), 0)
        return json.loads(output.getvalue())

    def experience(self, session, verdict='I verified it works.'):
        def observe(turn, role, text, loaded=(), **kwargs):
            result = learning.observe(self.project, 'hermes', session, turn, role, text,
                                      loaded, hermes_home=self.profile, **kwargs)
            self.assertTrue(result)
        observe('request', 'user', f'Sample meeting notes for {session}.')
        observe('request', 'assistant', 'The agenda was extracted.',
                learning.source_inventory(self.source, hermes=True), hermes_uid='reply')
        observe('review', 'user', verdict, hermes_uid='reply')

    def test_import_reaches_existing_worker_and_duplicates_do_not_requeue(self):
        self.experience('one')
        self.experience('two', 'It fails with an empty attendee list.')
        self.experience('three', 'Fix the deadline. token=private-string')
        journal = self.source['state'] / 'journal.sqlite'
        source_bytes = journal.read_bytes()

        imported = self.run_cli('import-hermes', str(self.project), '--from-profile', str(self.profile))
        self.assertEqual(imported, {'imported': 3, 'duplicates': 0, 'skipped': 0})
        self.wake.assert_called_once_with(self.target)
        with learning.connection(self.target) as db:
            rows = db.execute('SELECT * FROM evidence ORDER BY id').fetchall()
            self.assertEqual([x['kind'] for x in rows], ['success', 'failure', 'correction'])
            self.assertTrue(all(x['harness'] == 'hermes' and x['origin'] == 'session'
                                and x['version'] == tree_hash(self.skill) for x in rows))
            self.assertNotIn('private-string', rows[2]['note'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
            with patch.object(learning_worker, 'revise', return_value={'status': 'blocked'}) as revise:
                learning_worker.drain(self.target, db)
            revise.assert_called_once()
            self.assertEqual([x['id'] for x in revise.call_args.args[5]], [1, 2, 3])
            db.execute('UPDATE evidence SET consumed=1 WHERE id=1')

        copied_profile = self.profile.with_name('copied Hermes profile')
        shutil.copytree(self.profile, copied_profile)
        self.assertEqual(learning.import_hermes(self.project, copied_profile),
                         {'imported': 0, 'duplicates': 3, 'skipped': 0})
        self.assertEqual(journal.read_bytes(), source_bytes)
        self.wake.assert_called_once()
        with learning.connection(self.target) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT consumed FROM evidence WHERE id=1').fetchone()[0], 1)

    def test_only_current_version_and_verified_turn_are_imported(self):
        self.experience('stale')
        self.markdown.write_text(self.markdown.read_text() + 'Keep the agreed date.\n')
        self.experience('unbound')
        self.experience('missing-feedback')
        self.experience('current', 'Fix the owner.')
        with learning.connection(self.source) as db:
            db.execute("UPDATE messages SET bindings='{}' WHERE session='unbound' AND role='assistant'")
            db.execute("DELETE FROM messages WHERE session='missing-feedback' AND turn='review'")
            db.execute("UPDATE evidence SET reproducible=1 WHERE session='current'")
        self.assertEqual(learning.import_hermes(self.project, self.profile),
                         {'imported': 1, 'duplicates': 0, 'skipped': 3})
        with learning.connection(self.target) as db:
            rows = db.execute('SELECT * FROM evidence').fetchall()
        self.assertEqual([x['session'] for x in rows], ['current'])
        self.assertFalse(learning_worker.unique_batch(rows))

    def test_requires_both_enrollments_and_exact_project_root(self):
        with self.assertRaisesRegex(ValueError, 'Enable session learning'):
            learning.import_hermes(self.skill, self.profile)
        (self.source['state'] / 'enabled').unlink()
        with self.assertRaisesRegex(ValueError, 'Enable session learning'):
            learning.import_hermes(self.project, self.profile)
        self.wake.assert_not_called()


if __name__ == '__main__':
    unittest.main()
