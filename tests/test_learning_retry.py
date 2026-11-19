import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import learning, learning_worker
from scripts.utils import tree_hash, write_json


class LearningRetryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.project = root / 'project'
        self.home = root / 'home'
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
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.enterContext(patch.dict(os.environ, {learning.INTERNAL: '0'}))
        self.write_cases()

    def seed(self):
        self.enable()
        with learning.connection(self.config) as db:
            learning.queue(db, 'restore')
            learning.record(db, 'seed-correction', 'demo', tree_hash(self.source), 'feedback', 'codex', 's', 't',
                            'case:fail', 'fail', 'correction', 'Retain the date.', True)
            learning.record(db, 'heldout', 'demo', tree_hash(self.source), 'feedback', 'codex', 's', 't2',
                            'case:secret-case', 'secret-case', 'success', 'DO NOT SEND TO OPTIMIZER', False)

    def enable(self):
        write_json(self.project / learning.CONFIG, self.configuration)
        self.assertEqual(learning.main(['enable', str(self.project)]), 0)
        self.config = learning.config_for(self.project)

    def disable_auto_apply(self):
        self.configuration['skills']['demo']['auto_apply'] = False
        self.enable()

    def write_cases(self):
        for name, case, hidden in [('train', 'fail', 'Training owner'),
                                   ('holdout', 'secret-case', 'Protected unseen portion')]:
            write_json(self.project / f'{name}.json', {'evals': [{
                'id': case, 'prompt': hidden, 'expectations': ['Stable date'],
                'checks': [{'text': 'Stable date', 'type': 'contains', 'file': '_response.txt', 'value': 'DATE'}],
            }]})

    def queue(self, witness):
        with learning.connection(self.config) as db:
            learning.queue(db, witness)

    def timeout(self, prompt, out, model, **kwargs):
        return {'status': 'timeout'}

    def no_change(self, prompt, out, model, **kwargs):
        write_json(out / 'response.txt', {'rationale': 'No justified change', 'changes': []})
        return {'status': 'completed'}

    def proposal(self, prompt, out, model, **kwargs):
        body = self.markdown.read_text().replace('Old behavior.', 'Preserve the supplied date.')
        write_json(out / 'response.txt', {'rationale': 'Preserve dates from confirmed evidence',
                                          'changes': [{'path': 'SKILL.md', 'content': body}]})
        return {'status': 'completed'}

    def quality(self, candidate, evals, workspace, model, *, repeats, previous, **kwargs):
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

    def test_timeout_keeps_evidence_and_suppresses_same_input_without_new_signal(self):
        self.seed()
        with patch.object(learning_worker, 'run_model', side_effect=self.timeout) as model:
            learning_worker.work(self.config)
            self.queue('empty-retry-window')
            learning_worker.work(self.config)

        model.assert_called_once()
        with learning.connection(self.config) as db:
            training = db.execute("SELECT consumed FROM evidence WHERE kind='correction'").fetchone()
            statuses = [row[0] for row in db.execute('SELECT status FROM revisions ORDER BY id')]
        self.assertEqual(training['consumed'], 0)
        self.assertEqual(statuses, ['blocked', 'retry_suppressed'])

    def test_new_evidence_unlocks_retry_and_reuses_prior_evidence(self):
        self.seed()
        prompts = []

        def no_change(prompt, out, model, **kwargs):
            prompts.append(prompt)
            write_json(out / 'response.txt', {'rationale': 'No justified change', 'changes': []})
            return {'status': 'completed'}

        with patch.object(learning_worker, 'run_model', side_effect=self.timeout) as model:
            learning_worker.work(self.config)
        model.assert_called_once()
        with learning.connection(self.config) as db:
            learning.record(db, 'new-signal', 'demo', tree_hash(self.source), 'feedback', 'codex', 's2', 't2',
                            'case:new', None, 'correction', 'Preserve the new validated behavior.', True)
            learning.queue(db, 'new-evidence-window')

        with patch.object(learning_worker, 'run_model', side_effect=no_change) as model:
            learning_worker.work(self.config)
        model.assert_called_once()
        self.assertEqual(len(prompts), 1)
        self.assertIn('Retain the date.', prompts[0])
        self.assertIn('Preserve the new validated behavior.', prompts[0])
        with learning.connection(self.config) as db:
            consumed = db.execute("SELECT COUNT(*) FROM evidence WHERE kind='correction' AND consumed=1").fetchone()[0]
            status = db.execute('SELECT status FROM revisions ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertEqual(consumed, 2)
        self.assertEqual(status, 'no_change')

    def test_changed_model_and_runtime_override_unlock_same_evidence(self):
        self.seed()
        calls = []

        def record_timeout(prompt, out, model, **kwargs):
            calls.append((model, kwargs.get('harness'), os.environ.get('SKILLCRAFT_CODEX_HOME')))
            return {'status': 'timeout'}

        with patch.object(learning_worker, 'run_model', side_effect=record_timeout):
            learning_worker.work(self.config)
            self.queue('changed-model-window')
            self.configuration['skills']['demo']['harnesses']['codex']['optimizer_model'] = 'gpt-new'
            self.enable()
            learning_worker.work(self.config)
            self.queue('changed-runtime-window')
            self.configuration['skills']['demo']['harnesses']['codex']['codex_home'] = str(self.home)
            self.enable()
            learning_worker.work(self.config)

        self.assertEqual([call[0] for call in calls], ['gpt-test', 'gpt-new', 'gpt-new'])
        self.assertEqual(calls[2][2], str(self.home))
        with learning.connection(self.config) as db:
            self.assertEqual(db.execute("SELECT consumed FROM evidence WHERE kind='correction'").fetchone()[0], 0)

    def test_changed_inherited_runner_unlocks_same_evidence(self):
        self.seed()
        with (patch.dict(os.environ, {'SKILLCRAFT_CODEX': '/first-codex'}),
              patch.object(learning_worker, 'run_model', side_effect=self.timeout) as model):
            learning_worker.work(self.config)
            self.queue('changed-inherited-runner')
            os.environ['SKILLCRAFT_CODEX'] = '/second-codex'
            learning_worker.work(self.config)
        self.assertEqual(model.call_count, 2)

    def test_explicit_codex_binary_is_bound_and_staged_after_enable(self):
        binary = self.project / 'fake-codex'
        binary.write_bytes(b'\xcf\xfa\xed\xfe harmless approved executable')
        binary.chmod(0o755)
        self.configuration['skills']['demo']['harnesses']['codex']['binary'] = str(binary)
        self.seed()
        approved_bytes = binary.read_bytes()
        calls = []

        def inspect_snapshot(*args, **kwargs):
            staged = Path(os.environ['SKILLCRAFT_CODEX'])
            self.assertNotEqual(staged, binary)
            self.assertEqual(staged.read_bytes(), approved_bytes)
            calls.append(staged)
            return {'status': 'timeout'}

        with patch.object(learning_worker, 'run_model', side_effect=inspect_snapshot) as model:
            learning_worker.work(self.config)
            self.queue('same-binary-retry')
            learning_worker.work(self.config)
        model.assert_called_once()
        self.assertEqual(len(calls), 1)

    def test_same_path_binary_reapproval_changes_optimizer_retry_identity(self):
        binary = self.project / 'fake-codex'
        binary.write_bytes(b'\xcf\xfa\xed\xfe approved executable revision one')
        binary.chmod(0o755)
        self.configuration['skills']['demo']['harnesses']['codex']['binary'] = str(binary)
        self.seed()
        with patch.object(learning_worker, 'run_model', side_effect=self.timeout) as model:
            learning_worker.work(self.config)
            self.queue('same-path-binary-reapproval')
            binary.write_bytes(b'\xcf\xfa\xed\xfe approved executable revision two')
            self.enable()
            learning_worker.work(self.config)
        self.assertEqual(model.call_count, 2)
        with learning.connection(self.config) as db:
            fingerprints = [json.loads((Path(row['path']) / 'report.json').read_text())
                            ['optimizer']['input_hash']
                            for row in db.execute('SELECT path FROM revisions ORDER BY id')]
        self.assertEqual(len(fingerprints), 2)
        self.assertNotEqual(*fingerprints)

    def test_replaced_explicit_codex_binary_stops_before_optimizer(self):
        binary = self.project / 'fake-codex'
        binary.write_bytes(b'\xcf\xfa\xed\xfe harmless approved executable')
        binary.chmod(0o755)
        self.configuration['skills']['demo']['harnesses']['codex']['binary'] = str(binary)
        self.seed()
        binary.write_bytes(b'replacement after owner enable')
        with patch.object(learning_worker, 'run_model') as model:
            learning_worker.work(self.config)
        model.assert_not_called()

    def test_explicit_binary_snapshot_is_used_for_ab_and_activation(self):
        binary = self.project / 'fake-codex'
        binary.write_bytes(b'\xcf\xfa\xed\xfe harmless approved executable')
        binary.chmod(0o755)
        self.configuration['skills']['demo']['harnesses']['codex']['binary'] = str(binary)
        self.seed()
        launched = []

        def quality(*args, **kwargs):
            path = Path(os.environ['SKILLCRAFT_CODEX'])
            self.assertNotEqual(path, binary)
            self.assertEqual(path.read_bytes(), b'\xcf\xfa\xed\xfe harmless approved executable')
            launched.append(path)
            return self.quality(*args, **kwargs)

        with (patch.object(learning_worker, 'run_model', side_effect=self.proposal),
              patch.object(learning_worker, 'run_quality', side_effect=quality)):
            learning_worker.work(self.config)
        self.assertEqual(len(launched), 1)
        self.assertIn('Preserve the supplied date.', self.markdown.read_text())

    def test_explicit_binary_change_during_ab_blocks_activation(self):
        binary = self.project / 'fake-codex'
        binary.write_bytes(b'\xcf\xfa\xed\xfe harmless approved executable')
        binary.chmod(0o755)
        self.configuration['skills']['demo']['harnesses']['codex']['binary'] = str(binary)
        self.seed()
        original = self.markdown.read_text()

        def change_during_quality(*args, **kwargs):
            result = self.quality(*args, **kwargs)
            binary.write_bytes(b'changed after optimizer approval')
            return result

        with (patch.object(learning_worker, 'run_model', side_effect=self.proposal),
              patch.object(learning_worker, 'run_quality', side_effect=change_during_quality)):
            learning_worker.work(self.config)
        self.assertEqual(self.markdown.read_text(), original)

    def test_final_report_write_failure_does_not_consume_evidence(self):
        self.seed()
        write_json_real = learning_worker.write_recovery_record

        def fail_terminal_report(path, body):
            if Path(path).name == 'report.json' and body.get('status') == 'no_change':
                raise OSError('simulated final report write failure')
            return write_json_real(path, body)

        with (patch.object(learning_worker, 'run_model', side_effect=self.no_change),
              patch.object(learning_worker, 'write_recovery_record', side_effect=fail_terminal_report)):
            with self.assertRaisesRegex(OSError, 'final report write failure'):
                learning_worker.work(self.config)

        with learning.connection(self.config) as db:
            revision = db.execute('SELECT status, path FROM revisions').fetchone()
            consumed = db.execute("SELECT consumed FROM evidence WHERE kind='correction'").fetchone()[0]
        report = json.loads((Path(revision['path']) / 'report.json').read_text())
        self.assertEqual(revision['status'], 'running')
        self.assertEqual(report['status'], 'validating')
        self.assertEqual(consumed, 0)

    def test_restart_reconciles_durable_terminal_report_and_evidence_atomically(self):
        self.seed()
        with patch.object(learning_worker, 'run_model', side_effect=self.no_change), \
                patch.object(learning_worker, 'record_revision_outcome', side_effect=OSError('simulated DB interruption')):
            with self.assertRaisesRegex(OSError, 'DB interruption'):
                learning_worker.work(self.config)

        with learning.connection(self.config) as db:
            revision = db.execute('SELECT status, path FROM revisions').fetchone()
            consumed = db.execute("SELECT consumed FROM evidence WHERE kind='correction'").fetchone()[0]
        report = json.loads((Path(revision['path']) / 'report.json').read_text())
        self.assertEqual((revision['status'], report['status'], consumed), ('running', 'no_change', 0))

        learning_worker.work(self.config)
        with learning.connection(self.config) as db:
            revision_status = db.execute('SELECT status FROM revisions').fetchone()[0]
            consumed = db.execute("SELECT consumed FROM evidence WHERE kind='correction'").fetchone()[0]
        self.assertEqual((revision_status, consumed), ('no_change', 1))

    def test_restart_recovers_interrupted_call_and_waits_for_new_evidence(self):
        self.seed()
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt()

        with patch.object(learning_worker, 'run_model', side_effect=interrupt) as model:
            with self.assertRaises(KeyboardInterrupt):
                learning_worker.work(self.config)
            learning_worker.work(self.config)
            self.queue('post-restart-empty-window')
            learning_worker.work(self.config)

        model.assert_called_once()
        with learning.connection(self.config) as db:
            statuses = [row[0] for row in db.execute('SELECT status FROM revisions ORDER BY id')]
            consumed = db.execute("SELECT consumed FROM evidence WHERE kind='correction'").fetchone()[0]
        self.assertEqual(statuses, ['interrupted', 'retry_suppressed'])
        self.assertEqual(consumed, 0)

    def test_consumed_successes_still_protect_their_versioned_cases(self):
        self.enable()
        version = tree_hash(self.source)
        with learning.connection(self.config) as db:
            learning.record(db, 'success-witness', 'demo', version, 'feedback', 'codex', 's', 't',
                            'case:confirmed', 'confirmed', 'success', 'The verified behavior was correct.', True)
            db.execute("UPDATE evidence SET consumed=1 WHERE kind='success'")
            db.commit()
            self.assertEqual(learning_worker.protected_success_cases(db, 'demo', version), {'confirmed'})

    def test_blocked_evaluator_recovery_reuses_proposal_after_validation_change(self):
        self.seed()
        self.disable_auto_apply()
        with patch.object(learning_worker, 'run_model', side_effect=self.proposal) as model, \
                patch.object(learning_worker, 'run_quality', side_effect=OSError('temporary evaluator outage')):
            learning_worker.work(self.config)
        model.assert_called_once()
        with learning.connection(self.config) as db:
            first = db.execute('SELECT path, status FROM revisions ORDER BY id DESC LIMIT 1').fetchone()
        first_report = json.loads((Path(first['path']) / 'report.json').read_text())
        self.assertEqual(first['status'], 'blocked')
        self.assertTrue(first_report['optimizer'].get('proposal_hash'))

        # An unchanged ordinary checkpoint performs neither another optimizer nor another A/B.
        self.queue('unchanged-empty-checkpoint')
        with patch.object(learning_worker, 'run_model', side_effect=AssertionError('optimizer must not repeat')), \
                patch.object(learning_worker, 'run_quality', side_effect=AssertionError('validation unchanged')):
            learning_worker.work(self.config)

        self.configuration['skills']['demo']['harnesses']['codex']['judge_model'] = 'gpt-judge-new'
        self.enable()
        with patch.object(learning_worker, 'run_model', side_effect=AssertionError('saved proposal should be reused')) as model, \
                patch.object(learning_worker, 'run_quality', side_effect=self.quality) as quality:
            learning_worker.work(self.config)
        model.assert_not_called()
        quality.assert_called_once()
        with learning.connection(self.config) as db:
            latest = db.execute('SELECT path, status FROM revisions ORDER BY id DESC LIMIT 1').fetchone()
        latest_report = json.loads((Path(latest['path']) / 'report.json').read_text())
        self.assertEqual(latest['status'], 'qualified')
        self.assertEqual(latest_report['optimizer']['reused_from'], first['path'])

    def test_manual_retry_without_saved_proposal_never_calls_optimizer(self):
        self.seed()
        with patch.object(learning_worker, 'run_model', side_effect=self.timeout) as model:
            learning_worker.work(self.config)
            model.assert_called_once()
        with learning.connection(self.config) as db:
            learning.record(db, 'manual-new-signal', 'demo', tree_hash(self.source), 'feedback', 'codex', 's3', 't3',
                            'case:manual-new', None, 'correction', 'Use the verified new constraint.', True)
        self.configuration['skills']['demo']['harnesses']['codex']['optimizer_model'] = 'gpt-new'
        self.enable()
        with patch.object(learning_worker, 'run_model', side_effect=AssertionError('manual retry must not optimize')) as model:
            self.assertEqual(learning.main(['work', str(self.project), '--retry-blocked']), 0)
        model.assert_not_called()
        with learning.connection(self.config) as db:
            statuses = [row[0] for row in db.execute('SELECT status FROM revisions ORDER BY id')]
        self.assertEqual(statuses, ['blocked', 'retry_suppressed'])

    def test_manual_retry_reuses_saved_proposal_without_optimizer_call(self):
        self.seed()
        self.disable_auto_apply()
        with patch.object(learning_worker, 'run_model', side_effect=self.proposal) as model, \
                patch.object(learning_worker, 'run_quality', side_effect=OSError('temporary evaluator outage')):
            learning_worker.work(self.config)
        model.assert_called_once()
        with patch.object(learning_worker, 'run_model', side_effect=AssertionError('manual retry must reuse')) as model, \
                patch.object(learning_worker, 'run_quality', side_effect=self.quality):
            self.assertEqual(learning.main(['work', str(self.project), '--retry-blocked']), 0)
        model.assert_not_called()
        with learning.connection(self.config) as db:
            status = db.execute('SELECT status FROM revisions ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertEqual(status, 'qualified')

    def test_latest_blocked_validation_plan_does_not_fall_back_to_older_plan(self):
        self.seed()
        self.disable_auto_apply()
        with patch.object(learning_worker, 'run_model', side_effect=self.proposal) as model, \
                patch.object(learning_worker, 'run_quality', side_effect=OSError('evaluator remains unavailable')) as quality:
            learning_worker.work(self.config)
            self.configuration['skills']['demo']['harnesses']['codex']['judge_model'] = 'gpt-judge-b'
            self.enable()
            learning_worker.work(self.config)
            learning_worker.work(self.config)
        model.assert_called_once()
        self.assertEqual(quality.call_count, 2)
        with learning.connection(self.config) as db:
            rows = db.execute('SELECT status, path FROM revisions ORDER BY id').fetchall()
        reports = [json.loads((Path(row['path']) / 'report.json').read_text()) for row in rows]
        self.assertEqual([row['status'] for row in rows], ['blocked', 'blocked'])
        self.assertNotEqual(reports[0]['validation_hash'], reports[1]['validation_hash'])


if __name__ == '__main__':
    unittest.main()
