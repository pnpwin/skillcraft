import json
import importlib.util
import os
import shutil
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
        self.write_cases()
        write_json(self.project / learning.CONFIG, self.configuration)
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.enterContext(patch.dict(os.environ, {learning.INTERNAL: '0'}))
        self.wake = self.enterContext(patch.object(learning, 'wake'))
        self.config = learning.config_for(self.project, enrolling=True)

    def enable(self):
        self.config['state'].mkdir(parents=True, exist_ok=True)
        write_json(self.config['state'] / 'enabled', learning.approval_record(self.config))
        self.config = learning.config_for(self.project)

    def pending_model_evidence(self):
        with learning.connection(self.config) as db:
            learning.queue(db, 'security-review')
            learning.record(db, 'repair-a', 'demo', tree_hash(self.source), 'feedback', 'codex', 's', '1',
                            'case:a', 'a', 'correction', 'Keep the supplied date.', True)

    def message(self, session, turn, role, text='', *, binds=False, harness='codex', target=None):
        loaded = learning.source_inventory(self.config) if binds else []
        return learning.observe(self.project, harness, session, turn, role, text, loaded, target_turn=target)

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
            self.message(session, 'next', 'user', text, harness=harness, target='old')
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

    def test_changed_dataset_blocks_model_until_owner_reenables(self):
        self.enable()
        self.pending_model_evidence()
        write_json(self.project / 'train.json', {'evals': [{
            'id': 'changed', 'prompt': 'New unauthorized training case', 'expectations': ['Stable date'],
        }]})
        with patch.object(learning_worker, 'run_model') as model:
            learning_worker.work(self.config)
        model.assert_not_called()
        self.enable()
        self.assertIsInstance(learning.require_model_approval(self.config), dict)

    def test_changed_fixture_blocks_model_until_owner_reenables(self):
        fixture = self.project / 'fixture.txt'
        fixture.write_text('Owner approved fixture')
        write_json(self.project / 'train.json', {'evals': [{
            'id': 'fail', 'prompt': 'Training owner', 'expectations': ['Stable date'],
            'files': [str(fixture)],
        }]})
        self.enable()
        self.pending_model_evidence()
        fixture.write_text('Changed fixture')
        with patch.object(learning_worker, 'run_model') as model:
            learning_worker.work(self.config)
        model.assert_not_called()

    def test_changed_frozen_fixture_is_rejected_before_optimizer(self):
        fixture = self.project / 'fixture.txt'
        fixture.write_text('Owner approved fixture')
        write_json(self.project / 'train.json', {'evals': [{
            'id': 'fail', 'prompt': 'Training owner', 'expectations': ['Stable date'],
            'files': [str(fixture)],
        }]})
        self.enable()
        self.pending_model_evidence()
        snapshot = learning_worker.snapshot_fixtures

        def mutate_frozen(rows, skill, workspace):
            frozen, hashes = snapshot(rows, skill, workspace)
            copied = Path(frozen['fail'][0])
            copied.write_text('Changed after snapshot hash')
            return frozen, hashes

        with (patch.object(learning_worker, 'snapshot_fixtures', side_effect=mutate_frozen),
              patch.object(learning_worker, 'run_model') as model):
            learning_worker.work(self.config)
        model.assert_not_called()

    def test_enable_rejects_out_of_project_fixture(self):
        outside = Path(self.temp.name) / 'outside.txt'
        outside.write_text('Harmless external fixture')
        write_json(self.project / 'train.json', {'evals': [{
            'id': 'fail', 'prompt': 'Training owner', 'expectations': ['Stable date'],
            'files': [str(outside)],
        }]})
        with self.assertRaisesRegex(ValueError, 'inside the approved project'):
            self.enable()

    def test_legacy_marker_allows_evidence_but_requires_reenable_for_models(self):
        marker = self.config['state'] / 'enabled'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(self.config['fingerprint'] + '\n')
        legacy = learning.config_for(self.project)
        self.assertIsNone(legacy['approval'])
        with self.assertRaisesRegex(ValueError, 're-enable'):
            learning.require_model_approval(legacy)
        self.enable()
        self.assertIsInstance(learning.require_model_approval(self.config), dict)

    def test_observe_only_configuration_without_datasets_stays_usable(self):
        settings = self.configuration['skills']['demo']
        settings.pop('train')
        settings.pop('holdout')
        write_json(self.project / learning.CONFIG, self.configuration)
        self.config = learning.config_for(self.project, enrolling=True)
        self.enable()
        self.assertTrue(self.message('observe-only', 'one', 'user', 'A harmless observation'))
        with learning.connection(self.config) as db:
            learning.queue(db, 'observe-only')
        with patch.object(learning_worker, 'run_model') as model:
            learning_worker.work(self.config)
        model.assert_not_called()

    def test_config_parse_and_fingerprint_use_the_same_open_buffer(self):
        original = (self.project / learning.CONFIG).read_bytes()
        replacement = original.replace(b'"enabled": true', b'"enabled": false')
        original_fdopen = learning.os.fdopen
        changed = False

        def replace_after_open(fd, *args, **kwargs):
            nonlocal changed
            if not changed:
                staged = self.project / 'replacement.json'
                staged.write_bytes(replacement)
                staged.replace(self.project / learning.CONFIG)
                changed = True
            return original_fdopen(fd, *args, **kwargs)

        with patch.object(learning.os, 'fdopen', side_effect=replace_after_open):
            config = learning.config_for(self.project, enrolling=True)
        self.assertTrue(config['fingerprint'] == __import__('hashlib').sha256(original).hexdigest())
        self.assertIsNotNone(config)

    def test_symlink_learning_config_is_refused(self):
        file = self.project / learning.CONFIG
        target = self.project / 'config-target.json'
        file.unlink()
        target.write_bytes(json.dumps(self.configuration).encode())
        file.symlink_to(target)
        with self.assertRaises(OSError):
            learning.config_for(self.project, enrolling=True)

    def test_same_request_across_sessions_is_one_case(self):
        self.enable()
        for session in ('a', 'b', 'c'):
            self.message(session, 'old', 'user', 'Verify the empty result.')
            self.message(session, 'old', 'assistant', binds=True)
            self.message(session, 'next', 'user', 'Всё работает', target='old')
        with learning.connection(self.config) as db:
            rows = db.execute('SELECT * FROM evidence').fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['independent'] for row in rows}), 1)
        self.assertFalse(learning_worker.unique_batch(rows))

    def test_feedback_without_a_proven_target_does_not_use_an_older_answer(self):
        self.enable()
        self.message('s', 'earlier', 'assistant', binds=True)
        self.message('s', 'next', 'user', 'Всё работает')
        with learning.connection(self.config) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT id FROM evidence").fetchone())

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

    def test_codex_rollout_binds_wrapped_output_by_exact_path_and_bytes(self):
        rollout = self.project / 'rollout.jsonl'
        versions = learning.source_inventory(self.config)
        contents = self.markdown.read_text()
        relative = self.markdown.relative_to(self.project)
        for parsed, args, output, expected in (
            ([{'type': 'unknown', 'cmd': f"rtk read '{relative}'"}], [], contents, versions),
            ([{'type': 'unknown', 'cmd': f"custom-wrapper show './{relative}'"}], [], contents, versions),
            ([], ['wrapper', 'show', str(self.markdown)], contents, versions),
            ([{'type': 'unknown', 'cmd': f"wrapper show '{relative}'"}], [], contents.rstrip(), []),
            ([{'type': 'unknown', 'cmd': f"wrapper other/SKILL.md # {relative}"}], [], contents, []),
            ([{'type': 'read', 'path': 'other/SKILL.md'}], ['wrapper', str(relative)], contents, []),
        ):
            with self.subTest(parsed=parsed, args=args, output_length=len(output)):
                record = {'type': 'event_msg', 'payload': {
                    'type': 'item_completed', 'thread_id': 'native-session', 'turn_id': 'native-turn',
                    'item': {'type': 'CommandExecution', 'status': 'completed', 'exit_code': 0,
                             'cwd': self.project.as_uri(), 'aggregated_output': output,
                             'command': args, 'parsed_cmd': parsed},
                }}
                rollout.write_text(json.dumps(record) + '\n')
                self.assertEqual(
                    codex_capture.confirmed_reads(str(rollout), 'native-session', 'native-turn', versions), expected,
                )

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

    def test_selected_review_import_uses_the_sealed_selected_grade(self):
        from scripts.generate_review import find_runs, save_feedback
        from scripts.grading_store import reserve_attempt, seal_attempt

        self.enable()
        workspace, snapshot = self.fake_eval()
        relative = 'eval-fail/with_skill/run-001'
        run = workspace / relative
        (run / 'outputs').mkdir()
        (workspace / 'pilot' / relative / 'outputs').mkdir(parents=True)
        write_json(run / 'messages.json', [])
        chosen, attempt = reserve_attempt(run, {}, self.project / 'regrade', None, None)
        grade = json.loads((run / 'grading.json').read_text())
        grade['expectations'][0]['passed'] = True
        write_json(run / 'grading.json', grade)
        grade['expectations'][0]['passed'] = False
        write_json(chosen / 'grading.json', grade)
        seal_attempt(chosen, attempt, grade)
        selection = {'schema_version': 1, 'source_workspace': str(workspace), 'rule': 'Verify regrade',
                     'runs': {relative: {'selected': str(chosen)}}}
        write_json(self.project / 'selection.json', selection)
        views = find_runs(workspace, selection)
        self.assertEqual([view['id'] for view in views], [f'r1@{attempt["attempt_id"]}'])
        feedback = self.project / 'feedback.json'
        save_feedback(feedback, {'schema_version': 1, 'status': 'complete', 'reviews': [{
            'run_id': views[0]['id'], 'snapshot_hash': snapshot, 'reviewed': True,
            'feedback': 'Preserve the due date.', 'verdict': 'correction',
        }]}, views)
        self.assertTrue(learning.notify_feedback(workspace, feedback, selection=selection))
        self.assertFalse(learning.notify_feedback(workspace, feedback, selection=selection))
        with learning.connection(self.config) as db:
            signal = db.execute('SELECT kind,reproducible FROM evidence').fetchone()
        self.assertEqual(tuple(signal), ('correction', 1))

    def test_ab_import_rejects_a_modified_sealed_grade(self):
        from scripts.grading_store import reserve_attempt, seal_attempt

        self.enable()
        workspace, _ = self.fake_eval()
        run = workspace / 'eval-fail/with_skill/run-001'
        grade_file = run / 'grading.json'
        grade = json.loads(grade_file.read_text())
        grade_file.unlink()
        destination, attempt = reserve_attempt(run, {}, None, None, None)
        write_json(grade_file, grade)
        seal_attempt(destination, attempt, grade)
        grade['expectations'][0]['passed'] = True
        write_json(grade_file, grade)
        with self.assertRaisesRegex(ValueError, 'grading result was modified'):
            learning.notify_eval(workspace)
        with learning.connection(self.config) as db:
            self.assertIsNone(db.execute('SELECT id FROM evidence').fetchone())

    def test_direct_import_rejects_a_truthy_string_reviewed_flag(self):
        self.enable()
        workspace, snapshot = self.fake_eval()
        feedback = workspace / 'feedback.json'
        write_json(feedback, {'schema_version': 1, 'status': 'complete', 'reviews': [{
            'run_id': 'r1', 'snapshot_hash': snapshot, 'reviewed': 'false',
            'feedback': 'Preserve the due date.', 'verdict': 'correction',
        }]})
        with self.assertRaisesRegex(ValueError, 'reviewed flag'):
            learning.notify_feedback(workspace, feedback)
        self.wake.assert_not_called()

    def test_new_ab_runs_can_queue_after_the_first_import_checkpoint(self):
        self.enable()
        workspace, _ = self.fake_eval()
        self.assertTrue(learning.notify_eval(workspace))
        with learning.connection(self.config) as db:
            db.execute("UPDATE checkpoints SET status='done'")
        run = workspace / 'eval-fail/with_skill/run-002'
        shutil.copytree(run.with_name('run-001'), run)
        data = json.loads((run / 'run.json').read_text())
        write_json(run / 'run.json', {**data, 'run_id': 'r2'})
        self.assertTrue(learning.notify_eval(workspace))
        self.assertFalse(learning.notify_eval(workspace))
        with learning.connection(self.config) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evidence').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM checkpoints WHERE status='pending'").fetchone()[0], 1)

    def write_cases(self):
        for name, case, hidden in [('train', 'fail', 'Training owner'), ('holdout', 'secret-case', 'Protected unseen portion')]:
            write_json(self.project / f'{name}.json', {'evals': [{
                'id': case, 'prompt': hidden, 'expectations': ['Stable date'],
                'checks': [{'text': 'Stable date', 'type': 'contains', 'file': '_response.txt', 'value': 'DATE'}],
            }]})

    def seed_worker(self):
        self.write_cases()
        self.enable()
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
        return {'errors': 0, 'validation_errors': [], 'deltas': {
            arm: {'excluded_pairs': [], 'paired_pass_rate': {'n': 2 * repeats}}
            for arm in ('previous_skill', 'without_skill')
        }}

    def test_incompatible_quality_pairs_cannot_activate_a_candidate(self):
        self.seed_worker()
        version = tree_hash(self.source)

        def incompatible(*args, **kwargs):
            result = self.fake_quality(*args, **kwargs)
            result['deltas']['previous_skill']['excluded_pairs'] = ['fixture mismatch']
            return result

        with (patch.object(learning_worker, 'run_model', side_effect=self.fake_proposal),
              patch.object(learning_worker, 'run_quality', side_effect=incompatible)):
            learning_worker.work(self.config)
        with learning.connection(self.config) as db:
            revision = db.execute('SELECT * FROM revisions').fetchone()
        report = json.loads((Path(revision['path']) / 'report.json').read_text())
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('incompatible pairs', report['reason'])
        self.assertEqual(tree_hash(self.source), version)

    def test_qualified_text_change_applies_and_rolls_back_after_worker_restart(self):
        self.seed_worker()
        before = self.markdown.read_text()
        def stop_after_swap(path, value):
            if path.name == 'report.json' and value.get('status') == 'applied':
                raise OSError('Test interruption before recording promotion')
            return write_json(path, value)

        with (patch.object(learning_worker, 'run_model', side_effect=self.fake_proposal) as model,
              patch.object(learning_worker, 'run_quality', side_effect=self.fake_quality)):
            with patch.object(learning_worker, 'write_recovery_record', side_effect=stop_after_swap):
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

    def test_changed_approval_blocks_activation(self):
        self.seed_worker()
        before = self.markdown.read_text()

        def change_approval(*args, **kwargs):
            result = self.fake_quality(*args, **kwargs)
            write_json(self.config['state'] / 'enabled', {
                'config_sha256': self.config['fingerprint'], 'validation': {},
            })
            return result

        with (patch.object(learning_worker, 'run_model', side_effect=self.fake_proposal),
              patch.object(learning_worker, 'run_quality', side_effect=change_approval)):
            learning_worker.work(self.config)
        self.assertEqual(self.markdown.read_text(), before)
        with learning.connection(self.config) as db:
            row = db.execute('SELECT * FROM revisions').fetchone()
        report = json.loads((Path(row['path']) / 'report.json').read_text())
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('Owner configuration changed', report['reason'])

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
