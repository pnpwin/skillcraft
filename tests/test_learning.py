import json
import importlib.util
from contextlib import redirect_stdout
from io import StringIO
import os
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import learning, learning_worker
from scripts.utils import digest, tree_hash, write_json
from tests import REPO


capture_spec = importlib.util.spec_from_file_location(
    'codex_learning_capture', REPO / 'plugins/skillcraft/src/codex/learning_capture.py')
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
        self.assertFalse(learning_worker.unique_batch(rows))

    def test_redacts_credentials_and_ignores_questions(self):
        self.assertIsNone(learning.classify('Всё работает?'))
        self.assertEqual(learning.classify('Подтверждаю, что не работает'), 'failure')
        self.assertNotIn('sk-abcdefghijk', learning.sanitize('token: sk-abcdefghijk'))
        self.assertNotIn('github_pat_', learning.sanitize('github_pat_abcdefghijk1234567890'))
        self.assertNotIn('synthetic-password', learning.sanitize(
            'postgresql://alice:synthetic-password@localhost/db'))
        self.assertNotIn('secret', learning.sanitize('https://user:secret@example.test/path'))

    def test_preview_groups_evidence_by_version_and_independent_case_without_notes(self):
        self.enable()
        version = tree_hash(self.source)
        with learning.connection(self.config) as db:
            learning.record(db, 'one', 'demo', version, 'feedback', 'codex', 'session-a', 'turn-a',
                            'case-a', 'train-1', 'failure', 'Private transcript excerpt')
            learning.record(db, 'two', 'demo', version, 'feedback', 'codex', 'session-b', 'turn-b',
                            'case-a', 'train-1', 'correction', 'Another private transcript excerpt')
            learning.record(db, 'old', 'demo', 'old-version', 'feedback', 'codex', 'session-c', 'turn-c',
                            'case-b', 'train-2', 'success', 'Old version evidence')
        view = learning.learning_view(self.config)
        current = [item for item in view['groups'] if item['version'] == version]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['independent_case'], 'case-a')
        self.assertEqual(current[0]['evidence_count'], 2)
        self.assertEqual(current[0]['verdicts'], {'failure': 1, 'correction': 1})
        self.assertEqual(len(view['groups']), 2)
        self.assertEqual(view['preview'][0]['state'], 'insufficient_evidence')
        self.assertEqual(view['preview'][0]['next_action'], 'collect_independent_evidence')
        self.assertNotIn('Private transcript', json.dumps(view))
        with learning.connection(self.config) as db:
            learning.record(db, 'verified-defect', 'demo', version, 'tool_check', 'codex', 's',
                            't', 'case-new', 'train-2', 'failure', 'Reproduced defect', True)
        self.assertEqual(learning.learning_view(self.config)['preview'][0]['state'], 'waiting_for_checkpoint')

    def test_status_history_reports_only_redacted_metadata(self):
        self.enable()
        attempt = self.config['state'] / 'revisions' / '1' / 'demo'
        attempt.mkdir(parents=True)
        write_json(attempt / 'report.json', {
            'status': 'blocked', 'reason': f'Fixture changed at {self.project}/secret.txt',
            'evidence': [1, 2], 'optimizer': {'reused_from': str(self.project / 'prior-proposal')},
            'transcript': 'must not appear',
        })
        with learning.connection(self.config) as db:
            db.execute('INSERT INTO revisions(checkpoint,skill,version,status,path) VALUES (1,?,?,?,?)',
                       ('demo', tree_hash(self.source), 'blocked', str(attempt)))
        view = learning.learning_view(self.config)
        history = view['history'][0]
        self.assertEqual(history['reason'], 'Evaluation fixture changed')
        self.assertEqual(history['evidence_count'], 2)
        self.assertEqual(history['retry'], 'reused_verified_proposal')
        self.assertNotIn(str(self.project), json.dumps(view))
        self.assertNotIn('prior-proposal', json.dumps(view))
        self.assertNotIn('transcript', json.dumps(view))
        write_json(attempt / 'report.json', ['Invalid report shape'])
        self.assertNotIn('reason', learning.learning_view(self.config)['history'][0])

    def test_learning_view_never_calls_model_for_insufficient_evidence(self):
        self.enable()
        with patch.object(learning_worker, 'run_model', side_effect=AssertionError('status must stay local')) as model:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(learning.main(['status', str(self.project)]), 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary['next_actions'][0]['state'], 'insufficient_evidence')
            self.assertIn('evidence_groups', summary)
        model.assert_not_called()

    def test_preview_reports_missing_prerequisites_for_explicit_skills(self):
        options = self.configuration['skills']['demo']
        for removed, reason in (
            (('train', 'holdout'), 'Capture only until train and holdout data are configured'),
            (('harnesses',), 'Required harness model settings are incomplete'),
        ):
            with self.subTest(removed=removed):
                self.configuration['skills']['demo'] = {k: v for k, v in options.items() if k not in removed}
                write_json(self.project / learning.CONFIG, self.configuration)
                self.config = learning.config_for(self.project, enrolling=True)
                self.enable()
                if removed == ('train', 'holdout'):
                    with learning.connection(self.config) as db:
                        for i in range(3):
                            learning.record(db, f'capture-{i}', 'demo', tree_hash(self.source), 'session',
                                            'codex', 's', str(i), f'case-{i}', None, 'success', 'Verified')
                preview = learning.learning_view(self.config)['preview'][0]
                self.assertEqual(preview['evidence_count'], 3)
                self.assertEqual(preview['state'], 'blocked')
                self.assertEqual(preview['reason'], reason)
                self.assertEqual(preview['next_action'], 'review_learning_configuration')

    def test_preview_reports_stale_approval_for_explicit_skills(self):
        self.enable()
        self.pending_model_evidence()
        self.assertEqual(learning.learning_view(self.config)['preview'][0]['state'], 'queued')
        write_json(self.project / 'train.json', {'evals': [{
            'id': 'changed', 'prompt': 'Changed training data', 'expectations': ['Stable date'],
        }]})
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(learning.main(['status', str(self.project)]), 0)
        preview = json.loads(output.getvalue())['next_actions'][0]
        self.assertEqual(preview['state'], 'blocked')
        self.assertIn('re-enable learning', preview['reason'])
        self.assertEqual(preview['next_action'], 'review_learning_configuration')

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

    def fake_eval(self, harness='codex'):
        workspace = self.project / 'iteration'
        snapshot = learning.snapshot_hash(self.source)
        write_json(workspace / 'manifest.json', {
            'kind': 'quality', 'skill_id': 'demo', 'harness': harness, 'controlled': True,
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

    def test_hermes_quality_supplies_a_reproducible_failure(self):
        self.enable()
        workspace, _ = self.fake_eval('hermes')
        self.assertTrue(learning.notify_eval(workspace))
        self.assertFalse(learning.notify_eval(workspace))
        with learning.connection(self.config) as db:
            signal = db.execute('SELECT harness,origin,case_id,kind,reproducible FROM evidence').fetchone()
        self.assertEqual(tuple(signal), ('hermes', 'ab', 'fail', 'failure', 1))

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

    def test_baseline_feedback_is_neutral_deduplicated_and_keeps_review_identity(self):
        self.enable()
        workspace, _ = self.fake_eval()
        manifest_path = workspace / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['configurations']['without_skill'] = {'snapshot_hash': None}
        write_json(manifest_path, manifest)
        baseline = workspace / 'eval-fail/without_skill/run-001'
        write_json(baseline / 'run.json', {'run_id': 'baseline-r1', 'status': 'completed'})
        write_json(baseline / 'eval_metadata.json', {'eval_id': 'fail', 'snapshot_hash': None})
        self.markdown.write_text('---\nname: demo\ndescription: A demo skill.\n---\n\nNew behavior.\n')
        feedback = workspace / 'feedback.json'
        write_json(feedback, {'schema_version': 1, 'status': 'complete', 'reviews': [{
            'run_id': 'baseline-r1', 'snapshot_hash': None,
            'reviewed': True, 'feedback': 'Keep the requested date in the answer.', 'verdict': 'failure',
        }]})

        imported = learning.ingest_feedback(workspace, feedback, project=self.project)

        self.assertEqual(imported, {'new_evidence': False, 'new_baseline_feedback': True})
        self.assertEqual(learning.ingest_feedback(workspace, feedback, project=self.project),
                         {'new_evidence': False, 'new_baseline_feedback': False})
        self.assertFalse(learning.notify_feedback(workspace, feedback))
        self.wake.assert_not_called()
        with learning.connection(self.config) as db:
            rows = db.execute('SELECT * FROM baseline_feedback').fetchall()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['run_id'], rows[0]['review_id'], rows[0]['harness'],
                          rows[0]['skill'], rows[0]['case_id'], rows[0]['note']),
                         ('baseline-r1', 'baseline-r1', 'codex', 'demo', 'fail',
                          'Keep the requested date in the answer.'))
        self.assertNotIn('verdict', rows[0].keys())
        self.assertNotIn('version', rows[0].keys())

    def test_baseline_feedback_requires_a_finished_review_and_exact_original_run(self):
        self.enable()
        workspace, _ = self.fake_eval()
        manifest_path = workspace / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['configurations']['without_skill'] = {'snapshot_hash': None}
        write_json(manifest_path, manifest)
        baseline = workspace / 'eval-fail/without_skill/run-001'
        write_json(baseline / 'run.json', {'run_id': 'baseline-r1', 'status': 'completed'})
        write_json(baseline / 'eval_metadata.json', {'eval_id': 'fail', 'snapshot_hash': None})
        feedback = workspace / 'feedback.json'

        def import_review(review, *, status='complete'):
            write_json(feedback, {'schema_version': 1, 'status': status, 'reviews': [review]})
            return learning.ingest_feedback(workspace, feedback, project=self.project)

        self.assertEqual(import_review({'run_id': 'baseline-r1', 'snapshot_hash': None,
                                        'reviewed': False, 'feedback': 'Draft note'}, status='in_progress'),
                         {'new_evidence': False, 'new_baseline_feedback': False})
        for review in (
            {'run_id': 'another-run', 'snapshot_hash': None,
             'reviewed': True, 'feedback': 'Note for another execution'},
            {'run_id': 'baseline-r1', 'snapshot_hash': 'different',
             'reviewed': True, 'feedback': 'Note with stale binding'},
            {'run_id': 'baseline-r1', 'snapshot_hash': None,
             'reviewed': True, 'feedback': '   '},
        ):
            with self.subTest(review=review):
                self.assertEqual(import_review(review),
                                 {'new_evidence': False, 'new_baseline_feedback': False})
        self.wake.assert_not_called()
        with learning.connection(self.config) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM baseline_feedback').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 0)

    def test_selected_baseline_review_keeps_original_run_and_selected_view_id(self):
        from scripts.generate_review import find_runs
        from scripts.grading_store import reserve_attempt, seal_attempt

        self.enable()
        workspace, _ = self.fake_eval()
        manifest_path = workspace / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['configurations'] = {
            'with_skill': {'snapshot_hash': 'another-snapshot'},
            'without_skill': {'snapshot_hash': None},
        }
        write_json(manifest_path, manifest)
        shutil.rmtree(workspace / 'eval-fail/with_skill/run-001')
        relative = 'eval-fail/without_skill/run-001'
        baseline = workspace / relative
        (baseline / 'outputs').mkdir(parents=True)
        write_json(baseline / 'run.json', {'run_id': 'baseline-r1', 'status': 'completed'})
        write_json(baseline / 'eval_metadata.json', {'eval_id': 'fail', 'snapshot_hash': None})
        write_json(baseline / 'messages.json', [])
        grade = {'status': 'completed', 'evaluation_complete': True,
                 'expectations': [{'text': 'Stable date', 'source': 'deterministic', 'passed': True}]}
        write_json(baseline / 'grading.json', grade)
        selected_path, attempt = reserve_attempt(baseline, {}, self.project / 'regrade', None, None)
        write_json(selected_path / 'grading.json', grade)
        seal_attempt(selected_path, attempt, grade)
        selection = {'schema_version': 1, 'source_workspace': str(workspace), 'rule': 'Verify selected review',
                     'runs': {relative: {'selected': str(selected_path)}}}
        write_json(self.project / 'selection.json', selection)
        view_id = find_runs(workspace, selection)[0]['id']
        self.assertEqual(view_id, f'baseline-r1@{attempt["attempt_id"]}')
        feedback = self.project / 'feedback.json'
        write_json(feedback, {'schema_version': 1, 'status': 'complete', 'reviews': [{
            'run_id': view_id, 'snapshot_hash': None, 'reviewed': True,
            'feedback': 'Keep the requested date in the answer.',
        }]})

        result = learning.ingest_feedback(workspace, feedback, project=self.project, selection=selection)

        self.assertEqual(result, {'new_evidence': False, 'new_baseline_feedback': True})
        with learning.connection(self.config) as db:
            row = db.execute('SELECT run_id,review_id FROM baseline_feedback').fetchone()
        self.assertEqual(tuple(row), ('baseline-r1', view_id))

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

    def test_scheduled_revision_checks_catalog_before_finalizing_and_can_rollback(self):
        self.configuration['automation'] = {'improve': True, 'auto_apply': True}
        write_json(self.project / learning.CONFIG, self.configuration)
        self.config = learning.config_for(self.project, enrolling=True)
        self.seed_worker()
        before = tree_hash(self.source)

        def runtime_probe(_root, harness, _models):
            return lambda path, **kwargs: {'harness': harness, 'path': str(path), 'hash': tree_hash(path)}

        with (patch.object(learning_worker, 'run_model', side_effect=self.fake_proposal) as model,
              patch.object(learning_worker, 'run_quality', side_effect=self.fake_quality),
              patch('scripts.learning_candidates.native_catalog_probe', side_effect=runtime_probe),
              patch('scripts.learning_candidates.render_quality_review', side_effect=lambda path: path / 'review.html')):
            report = learning_worker.work(self.config)
            self.assertEqual(report['candidate_count'], 1)
            self.assertEqual(report['items'][0]['result']['status'], 'applied')
            self.assertEqual(learning_worker.work(self.config)['status'], 'not_due')
        model.assert_called_once()
        with learning.connection(self.config) as db:
            revision = db.execute("SELECT * FROM revisions WHERE status='applied'").fetchone()
        report = json.loads((Path(revision['path']) / 'report.json').read_text())
        self.assertTrue(report['catalogs']['codex']['verified'])
        self.assertNotEqual(tree_hash(self.source), before)
        learning_worker.rollback_revision(self.config, revision['id'])
        self.assertEqual(tree_hash(self.source), before)

    def test_interrupted_scheduled_revision_does_not_block_future_batches(self):
        from scripts.learning_batch import due, run_batch

        self.configuration['automation'] = {'improve': True}
        write_json(self.project / learning.CONFIG, self.configuration)
        self.config = learning.config_for(self.project, enrolling=True)
        self.seed_worker()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        next_window = now + timedelta(days=7)
        original = tree_hash(self.source)

        with learning.connection(self.config) as db:
            def update(name, settings, rows, batch_id):
                return learning_worker.scheduled_candidate(self.config, db, name, settings, rows, batch_id)

            with patch.object(learning_worker, 'revise', side_effect=KeyboardInterrupt) as revise:
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(self.config, db, update=update, now=now)
                self.assertTrue(due(db, self.config['automation'], now=now))
                recovered = run_batch(self.config, db, update=update, now=now)
                self.assertEqual('interrupted', recovered['status'])
                result = recovered['items'][0]['result']
                self.assertEqual('interrupted', result['status'])
                self.assertTrue(result['candidate']['reason'])
                self.assertEqual('interrupted', db.execute('SELECT status FROM revisions').fetchone()[0])
                self.assertFalse(due(db, self.config['automation'], now=now))
                revise.assert_called_once()

            learning.record(db, 'new-evidence', 'demo', original, 'feedback', 'codex', 's', 't3',
                            'case:fail-new', 'fail', 'correction', 'Retain the date again.', True)
            db.commit()
            with patch.object(learning_worker, 'revise', return_value={'status': 'no_change'}) as revise:
                report = run_batch(self.config, db, update=update, now=next_window)
            self.assertNotEqual(recovered['batch_id'], report['batch_id'])
            self.assertEqual('done', report['status'])
            self.assertEqual('no_change', report['items'][0]['result']['status'])
            revise.assert_called_once()
        self.assertEqual(original, tree_hash(self.source))

    def test_worker_reloads_enrollment_before_the_next_batch_item(self):
        self.configuration['selection'] = 'all'
        self.configuration['automation'] = {'create': True, 'improve': True}
        write_json(self.project / learning.CONFIG, self.configuration)
        self.config = learning.config_for(self.project, enrolling=True)
        self.enable()
        with learning.connection(self.config) as db:
            db.executemany(
                "INSERT INTO observations(witness,harness,session,turn,kind,topic,independent,excerpt,details) "
                "VALUES(?,'codex','session',?,'success',?,?,?,?)",
                [(topic, topic, topic, topic, 'Confirmed output', '{"check":{"file":"result.txt"}}')
                 for topic in ('new-topic', 'other-topic')],
            )
            db.commit()

        def withdraw_after_first(_config, _db, name, _settings, _rows, _batch_id):
            self.assertEqual('new-topic', name)
            self.configuration['automation']['create'] = False
            write_json(self.project / learning.CONFIG, self.configuration)
            write_json(self.config['state'] / 'enabled', learning.approval_record(
                learning.config_for(self.project, enrolling=True, trusted_internal=True)))
            return {'status': 'staged'}

        with patch.object(learning_worker, 'scheduled_candidate', side_effect=withdraw_after_first) as callback:
            report = learning_worker.work(self.config)
        callback.assert_called_once()
        self.assertEqual(['staged', 'approval_changed'], [item['result']['status'] for item in report['items']])

    def test_session_holdout_fixture_cannot_be_an_optimizer_resource(self):
        settings = self.config['skills']['demo']
        train = {'id': 'train', 'prompt': 'Training prompt'}
        for directory in ('references', 'evals'):
            with self.subTest(directory=directory):
                relative = f'{directory}/heldout.txt'
                fixture = self.source / relative
                fixture.parent.mkdir()
                fixture.write_text('Private holdout fixture')
                holdout = {'id': 'holdout', 'prompt': 'Unseen prompt', 'files': [str(fixture)],
                           'session_fixture_hashes': {fixture.name: digest(fixture)}}
                args = (self.config, 'demo', settings, self.source, self.home / directory, {}, None)
                if directory == 'references':
                    with self.assertRaisesRegex(ValueError, 'Place holdout material outside'):
                        learning_worker.configured_runs(*args, session_inputs=([train], [holdout]))
                else:
                    _, (_, frozen), _, _, _, _, excluded, _ = learning_worker.configured_runs(
                        *args, session_inputs=([train], [holdout]))
                    self.assertEqual({relative}, excluded)
                    self.assertEqual(fixture.read_text(), Path(frozen[0]['files'][0]).read_text())
                    files = learning_worker.selected_files(self.source, ['SKILL.md', 'evals/*'], excluded)
                    self.assertEqual({'SKILL.md'}, files.keys())

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
