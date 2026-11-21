from contextlib import redirect_stdout
from hashlib import sha256
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
        observe('review', 'assistant', 'Thanks for checking.')

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
        self.experience('interrupted-feedback')
        self.experience('current', 'Fix the owner.')
        with learning.connection(self.source) as db:
            db.execute("UPDATE messages SET bindings='{}' WHERE session='unbound' AND role='assistant'")
            db.execute("DELETE FROM messages WHERE session='missing-feedback' AND turn='review'")
            db.execute("DELETE FROM messages WHERE session='interrupted-feedback' AND turn='review' AND role='assistant'")
            db.execute("UPDATE evidence SET reproducible=1 WHERE session='current'")
        self.assertEqual(learning.import_hermes(self.project, self.profile),
                         {'imported': 1, 'duplicates': 0, 'skipped': 4})
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

    def test_hermes_correction_uses_reproduction_in_the_destination_journal(self):
        with learning.connection(self.target) as db:
            version = tree_hash(self.skill)
            learning.record(db, 'ab:case-1', 'demo', version, 'ab', 'hermes', 'ab-run', 'case-1',
                            'case:deadline', 'deadline', 'failure', 'Wrong deadline', True)
            learning.record(db, 'ab:old-version', 'demo', 'old-version', 'ab', 'hermes', 'old-run', 'c2',
                            'case:owner', 'owner', 'failure', 'Wrong owner in old version', True)
        self.experience('reproduced', 'Correction: Keep the due date. [skillcraft-case:deadline]')
        self.experience('unproven', 'Correction: Keep the original owner. [skillcraft-case:owner]')

        self.assertEqual(learning.import_hermes(self.project, self.profile)['imported'], 2)
        with learning.connection(self.target) as db:
            signals = db.execute("SELECT case_id,independent,reproducible FROM evidence "
                                 "WHERE origin='session' ORDER BY id").fetchall()
        self.assertEqual(tuple(signals[0]), ('deadline', 'case:deadline', 1))
        self.assertEqual(signals[1]['case_id'], 'owner')
        self.assertEqual(signals[1]['reproducible'], 0)

    def test_automatic_sync_only_imports_finished_feedback_and_model_steps_once(self):
        self.run_cli('--hermes-home', str(self.profile), 'enable', str(self.project), '--auto-import')
        def observe(turn, role, text, **kwargs):
            return learning.observe(self.project, 'hermes', 'session', turn, role, text,
                                    kwargs.pop('loaded', []), hermes_home=self.profile, **kwargs)

        observe('request', 'user', 'Summarize these meeting notes.')
        observe('request', 'assistant', 'Meeting notes summarized.',
                loaded=learning.source_inventory(self.source, hermes=True), hermes_uid='reply',
                model_steps=14)
        observe('review', 'user', 'I verified it works. password=private', hermes_uid='reply',
                defer_feedback=True)

        def sync():
            return self.run_cli('--hermes-home', str(self.profile), 'sync-hermes', str(self.project), 'session')

        self.assertEqual(sync()['imported'], 0)
        self.wake.assert_not_called()
        observe('review', 'assistant', 'Thank you.', model_steps=1)
        self.assertEqual(sync()['imported'], 1)
        self.assertEqual(sync()['duplicates'], 1)
        self.wake.assert_called_once()
        with learning.connection(self.target) as db:
            row = db.execute('SELECT kind,note FROM evidence').fetchone()
            self.assertEqual(row['kind'], 'success')
            self.assertNotIn('private', row['note'])
            self.assertEqual(db.execute('SELECT SUM(steps) FROM model_steps').fetchone()[0], 15)
            witnesses = [row[0] for row in db.execute('SELECT witness FROM checkpoints')]
        self.assertEqual(len(witnesses), 2)
        self.assertIn('model-steps:15', witnesses)

    def test_automatic_import_requires_profile_opt_in(self):
        self.experience('local-only')
        with self.assertRaisesRegex(ValueError, 'explicit enrollment'):
            learning.import_hermes(self.project, self.profile, completed_session='local-only', automatic=True)
        self.wake.assert_not_called()

    def test_hermes_message_budget_counts_only_complete_pairs(self):
        self.run_cli('--hermes-home', str(self.profile), 'enable', str(self.project), '--auto-import')
        for index in range(8):
            learning.observe(self.project, 'hermes', 'work', str(index), 'user',
                             'Do some work.', [], hermes_home=self.profile)
            if index < 7:
                learning.observe(self.project, 'hermes', 'work', str(index), 'assistant',
                                 'Done.', [], hermes_home=self.profile)
        learning.import_hermes(self.project, self.profile, completed_session='work', automatic=True)
        self.wake.assert_not_called()
        learning.observe(self.project, 'hermes', 'work', '7', 'assistant',
                         'Done.', [], hermes_home=self.profile)
        learning.import_hermes(self.project, self.profile, completed_session='work', automatic=True)
        self.wake.assert_called_once()
        with learning.connection(self.target) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 16)
            self.assertEqual([r[0] for r in db.execute('SELECT witness FROM checkpoints')], ['messages:15'])

    def test_recovery_reuses_verified_binding_without_appending_live_messages(self):
        def observe(turn, role, text, loaded=(), **kwargs):
            return learning.observe(self.project, 'hermes', 'old-chat', turn, role, text, loaded,
                                    hermes_home=self.profile, **kwargs)

        observe('original', 'user', 'Apply demo.')
        observe('original', 'assistant', 'Done.', learning.source_inventory(self.source, hermes=True),
                hermes_uid='native-original')
        item = {
            'target_turn': 'original', 'feedback_turn': 'native-review', 'feedback': 'I verified it works.',
            'reply_uid_sha256': sha256(b'native-original').hexdigest(), 'final_reply_uid': 'native-final',
        }
        result = learning.recover_hermes(self.project, self.profile, session='old-chat',
                                         items=[{**item, 'reply_uid_sha256': '0' * 64}, item, item])
        self.assertEqual(result, {'recorded': 1, 'duplicates': 1, 'skipped': 1,
                                  'import': {'imported': 1, 'duplicates': 0, 'skipped': 0}})
        with learning.connection(self.source) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        with learning.connection(self.target) as db:
            self.assertEqual(db.execute('SELECT kind FROM evidence').fetchone()[0], 'success')


if __name__ == '__main__':
    unittest.main()
