import json
import importlib.util
import os
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import learning, learning_worker
from scripts.utils import digest, tree_hash, write_json
from tests import REPO


capture_spec = importlib.util.spec_from_file_location(
    'codex_learning_capture', REPO / 'plugins/skillcraft/hooks/learning_capture.py')
codex_capture = importlib.util.module_from_spec(capture_spec)
capture_spec.loader.exec_module(codex_capture)


class LearningTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name).resolve()
        self.project = directory / 'project'
        self.home = directory / 'home'
        self.source = self.project / '.agents/skills/demo'
        self.source.mkdir(parents=True)
        self.markdown = self.source / 'SKILL.md'
        self.markdown.write_text('---\nname: demo\ndescription: A demo skill.\n---\n\nOld behavior.\n')
        self.configuration = {
            'schema_version': 1, 'enabled': True, 'skills': {'demo': {
                'source': '.agents/skills/demo', 'train': 'train.json', 'holdout': 'holdout.json',
                'harnesses': {'codex': {'model': 'gpt-test'}}, 'repeats': 2, 'auto_apply': True,
            }},
        }
        write_json(self.project / learning.CONFIG, self.configuration)
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.enterContext(patch.dict(os.environ, {learning.INTERNAL: '0'}))
        self.wake = self.enterContext(patch.object(learning, 'wake'))
        self.config = learning.config_for(self.project, enrolling=True)

    def enable(self):
        self.config['state'].mkdir(parents=True, exist_ok=True)
        (self.config['state'] / 'enabled').write_text(self.config['fingerprint'] + '\n')

    def message(self, session, turn, role, text='', *, binds=False, harness='codex'):
        loaded = [{'path': str(self.markdown), 'hash': digest(self.markdown)}] if binds else []
        return learning.observe(self.project, harness, session, turn, role, text, loaded)

    def test_message_threshold_dedup_and_opt_in(self):
        self.assertFalse(self.message('s', 'pre', 'user'))
        self.enable()
        for index in range(7):
            self.assertTrue(self.message('s', str(index), 'user'))
            self.assertFalse(self.message('s', str(index), 'user'))
            self.assertTrue(self.message('s', str(index), 'assistant'))
        self.wake.assert_not_called()
        self.assertTrue(self.message('s', '15', 'user'))
        self.wake.assert_called_once()
        worker = patch.object(learning_worker, 'run_model', side_effect=AssertionError('No optimizer call expected'))
        with worker:
            learning_worker.work(self.config)
        with learning.connection(self.config) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 15)
            self.assertEqual(db.execute('SELECT status FROM checkpoints').fetchone()[0], 'done')
        self.configuration['skills']['demo']['auto_apply'] = False
        write_json(self.project / learning.CONFIG, self.configuration)
        self.assertFalse(self.message('s', 'changed', 'user'))

    def test_both_harnesses_attribute_corrections_to_loaded_revision(self):
        self.enable()
        for session, harness, text in [('a', 'codex', 'Проверил, всё работает'),
                                       ('b', 'opencode', 'Это не работает'),
                                       ('c', 'opencode', 'Исправь: неверный срок')]:
            self.message(session, 'old', 'assistant', binds=True, harness=harness)
            self.message(session, 'next', 'user', text, harness=harness)
        self.message('no_skill', '1', 'assistant')
        self.message('no_skill', '2', 'user', 'Всё работает')
        with learning.connection(self.config) as db:
            rows = db.execute('SELECT * FROM evidence ORDER BY id').fetchall()
        self.assertEqual([row['kind'] for row in rows], ['success', 'failure', 'correction'])
        self.assertEqual([row['harness'] for row in rows], ['codex', 'opencode', 'opencode'])
        self.assertTrue(all(row['turn'] == 'old' and row['version'] == tree_hash(self.source) for row in rows))
        self.assertTrue(learning_worker.unique_batch(rows))

    def test_redacts_credentials_and_ignores_questions(self):
        self.assertIsNone(learning.classify('Всё работает?'))
        self.assertEqual(learning.classify('Подтверждаю, что не работает'), 'failure')
        self.assertNotIn('sk-abcdefghijk', learning.sanitize('token: sk-abcdefghijk'))

    def test_same_request_across_sessions_is_one_case(self):
        self.enable()
        for session in ('a', 'b', 'c'):
            self.message(session, 'old', 'user', 'Verify the empty result.')
            self.message(session, 'old', 'assistant', binds=True)
            self.message(session, 'next', 'user', 'Всё работает')
        with learning.connection(self.config) as db:
            rows = db.execute('SELECT * FROM evidence').fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['independent'] for row in rows}), 1)
        self.assertFalse(learning_worker.unique_batch(rows))

    def test_codex_rollout_proves_only_complete_read_in_requested_turn(self):
        path = self.project / 'rollout.jsonl'
        record = {'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'thread_id': 'native-session', 'turn_id': 'native-turn',
            'item': {'type': 'CommandExecution', 'status': 'completed', 'exit_code': 0,
                     'cwd': self.project.as_uri(), 'aggregated_output': self.markdown.read_text(),
                     'parsed_cmd': [{'type': 'read', 'path': str(self.markdown.relative_to(self.project))}]},
        }}
        versions = learning.source_inventory(self.config)
        path.write_text(json.dumps(record) + '\n')
        self.assertEqual(codex_capture.confirmed_reads(str(path), 'native-session', 'native-turn', versions), versions)
        self.assertEqual(codex_capture.confirmed_reads(str(path), 'native-session', 'another-turn', versions), [])
        record['payload']['item']['aggregated_output'] = 'description: A demo skill.'
        path.write_text(json.dumps(record) + '\n')
        self.assertEqual(codex_capture.confirmed_reads(str(path), 'native-session', 'native-turn', versions), [])

    def fake_eval(self):
        workspace = self.project / 'iteration'
        snapshot = learning.snapshot_hash(self.source)
        write_json(workspace / 'manifest.json', {
            'kind': 'quality', 'skill_id': 'demo', 'harness': 'codex', 'controlled': True,
            'environment_consistent': True, 'configurations': {'with_skill': {'snapshot_hash': snapshot}},
        })
        write_json(workspace / 'benchmark.json', {'errors': 0, 'validation_errors': []})
        run = workspace / 'eval-fail/with_skill/run-001'
        write_json(run / 'run.json', {'run_id': 'r1', 'attached': True, 'status': 'completed', 'installed_hash': snapshot})
        write_json(run / 'eval_metadata.json', {'eval_id': 'fail', 'snapshot_hash': snapshot})
        write_json(run / 'grading.json', {
            'status': 'completed', 'evaluation_complete': True,
            'expectations': [{'text': 'Stable date', 'source': 'deterministic', 'passed': False}],
        })
        return workspace, snapshot

    def test_import_idempotence_and_reproducible_reviewer_correction(self):
        self.enable()
        workspace, snapshot = self.fake_eval()
        self.assertTrue(learning.notify_eval(workspace))
        self.assertFalse(learning.notify_eval(workspace))
        feedback = workspace / 'feedback.json'
        row = {'run_id': 'r1', 'snapshot_hash': snapshot, 'reviewed': True, 'feedback': 'Preserve the due date.'}
        body = {'schema_version': 1, 'status': 'complete', 'reviews': [row]}
        write_json(feedback, body)
        self.assertFalse(learning.notify_feedback(workspace, feedback))
        row['verdict'] = 'correction'
        write_json(feedback, body)
        self.assertTrue(learning.notify_feedback(workspace, feedback))
        self.assertFalse(learning.notify_feedback(workspace, feedback))
        self.assertEqual(self.wake.call_count, 2)
        with learning.connection(self.config) as db:
            rows = db.execute('SELECT * FROM evidence ORDER BY id').fetchall()
        self.assertEqual([item['kind'] for item in rows], ['failure', 'correction'])
        self.assertTrue(all(item['reproducible'] for item in rows))
        self.assertEqual(len({item['independent'] for item in rows}), 1)
        self.assertTrue(learning_worker.unique_batch(rows))
        manifest = workspace / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['learning_internal'] = True
        write_json(manifest, data)
        self.assertFalse(learning.notify_feedback(workspace, feedback))

    def write_cases(self):
        for name, case, hidden in [('train', 'fail', 'Training owner'), ('holdout', 'secret-case', 'Protected unseen portion')]:
            write_json(self.project / f'{name}.json', {'evals': [{
                'id': case, 'prompt': hidden, 'expectations': ['Stable date'],
                'checks': [{'text': 'Stable date', 'type': 'contains', 'file': '_response.txt', 'value': 'DATE'}],
            }]})

    def seed_worker(self):
        self.enable()
        self.write_cases()
        with learning.connection(self.config) as db:
            learning.queue(db, 'restore')
            learning.record(db, 'correction', 'demo', tree_hash(self.source), 'feedback', 'codex', 's', 't',
                            'case:fail', 'fail', 'correction', 'Retain the date.', True)
            learning.record(db, 'heldout', 'demo', tree_hash(self.source), 'feedback', 'codex', 's', 't2',
                            'case:secret-case', 'secret-case', 'success', 'DO NOT SEND TO OPTIMIZER', False)

    def fake_proposal(self, prompt, out, model, **kwargs):
        self.assertIn('Retain the date', prompt)
        self.assertNotIn('Protected unseen portion', prompt)
        self.assertNotIn('DO NOT SEND TO OPTIMIZER', prompt)
        body = self.markdown.read_text().replace('Old behavior.', 'Preserve the supplied date.')
        write_json(out / 'response.txt', {'rationale': 'Preserve dates based on observed failure',
                                         'changes': [{'path': 'SKILL.md', 'content': body}]})
        return {'status': 'completed'}

    def fake_quality(self, candidate, evals, workspace, model, *, repeats, previous, **kwargs):
        write_json(workspace / 'manifest.json', {'controlled': True, 'environment_consistent': True})
        for case in ('fail', 'secret-case'):
            for arm in ('previous_skill', 'with_skill'):
                for repeat in range(1, repeats + 1):
                    write_json(workspace / f'eval-{case}' / arm / f'run-{repeat:03d}' / 'grading.json', {
                        'evaluation_complete': True,
                        'expectations': [{'text': 'Stable date', 'source': 'deterministic',
                                          'passed': arm == 'with_skill' or case == 'secret-case'}],
                    })
        return {'errors': 0, 'validation_errors': []}

    def test_qualified_text_change_applies_and_rolls_back_after_worker_restart(self):
        self.seed_worker()
        before = self.markdown.read_text()
        def stop_after_swap(path, value):
            if path.name == 'report.json' and value.get('status') == 'applied':
                raise OSError('Test interruption before recording promotion')
            return write_json(path, value)

        with (patch.object(learning_worker, 'run_model', side_effect=self.fake_proposal) as model,
              patch.object(learning_worker, 'run_quality', side_effect=self.fake_quality)):
            with patch.object(learning_worker, 'write_json', side_effect=stop_after_swap):
                with self.assertRaisesRegex(OSError, 'interruption'):
                    learning_worker.work(self.config)
            learning_worker.work(self.config)
        model.assert_called_once()
        with learning.connection(self.config) as db:
            row = db.execute("SELECT * FROM revisions WHERE status='applied'").fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(json.loads((Path(row['path']) / 'report.json').read_text())['recovered'])
        self.assertIn('Preserve the supplied date', self.markdown.read_text())
        learning_worker.rollback_revision(self.config, row['id'])
        self.assertEqual(self.markdown.read_text(), before)

    def test_regression_and_improvement_are_independent(self):
        matrix = {
            ('repair', 'one'): defaultdict(list, previous_skill=[False, False], with_skill=[True, True],
                                            deterministic=[True] * 4),
            ('protected', 'other'): defaultdict(list, previous_skill=[True, True], with_skill=[True, False],
                                                 deterministic=[True] * 4),
        }
        gain, loss = learning_worker.compare(matrix, 2)
        self.assertEqual(gain[0]['case_id'], 'repair')
        self.assertEqual(loss[0]['case_id'], 'protected')

    def test_regression_keeps_source_and_records_candidate(self):
        self.seed_worker()
        version = tree_hash(self.source)

        def regress(candidate, evals, workspace, *args, **kwargs):
            quality = self.fake_quality(candidate, evals, workspace, *args, **kwargs)
            grade = workspace / 'eval-secret-case/with_skill/run-001/grading.json'
            value = json.loads(grade.read_text())
            value['expectations'][0]['passed'] = False
            write_json(grade, value)
            return quality

        with (patch.object(learning_worker, 'run_model', side_effect=self.fake_proposal),
              patch.object(learning_worker, 'run_quality', side_effect=regress)):
            learning_worker.work(self.config)
        with learning.connection(self.config) as db:
            revision = db.execute('SELECT * FROM revisions').fetchone()
        report = json.loads((Path(revision['path']) / 'report.json').read_text())
        self.assertEqual(report['status'], 'rejected')
        self.assertEqual(report['validations']['codex']['regressed'][0]['case_id'], 'secret-case')
        self.assertTrue(Path(report['candidate']).is_dir())
        self.assertEqual(tree_hash(self.source), version)


if __name__ == '__main__':
    unittest.main()
