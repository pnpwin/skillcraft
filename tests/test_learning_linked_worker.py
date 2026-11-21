from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import unittest
from unittest.mock import patch

from scripts import learning, learning_worker
from scripts.learning_promotion import tree_modes
from scripts.utils import tree_hash, write_json


class LearningLinkedWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.link_root = self.root / 'approved-skills'
        self.target = self.link_root / 'demo'
        self.target.mkdir(parents=True)
        (self.target / 'SKILL.md').write_text(
            '---\nname: demo\ndescription: A demo skill.\n---\n\nOriginal linked behavior.\n',
            encoding='utf-8')
        self.alias = self.project / '.agents/skills/demo'
        self.alias.parent.mkdir(parents=True)
        self.alias.symlink_to(self.target, target_is_directory=True)
        self.settings = {
            'source': '.agents/skills/demo', 'linked_target': str(self.target),
            'train': 'train.json', 'holdout': 'holdout.json',
            'harnesses': {'codex': {'model': 'gpt-test'}}, 'repeats': 1,
            'auto_apply': True,
        }
        self.config = {
            'root': self.project, 'state': self.project / '.skillcraft/state',
            'selection': 'all', 'linked_skill_roots': (self.link_root,),
            'skills': {'demo': self.settings},
        }

    def test_linked_skill_is_read_for_qualification_but_never_promoted(self):
        attempt = self.config['state'] / 'revisions' / '1' / 'demo'
        train = [{'id': 'train-case', 'prompt': 'training', 'expectations': ['Keep dates'],
                  'files': [], 'case_id': 'train-case'}]
        holdout = [{'id': 'holdout-case', 'prompt': 'held out', 'expectations': ['Keep dates'],
                    'files': [], 'case_id': 'holdout-case'}]
        evals = attempt / 'evals.json'
        fixture_hashes = {'train-case': {}, 'holdout-case': {}}
        rows = [{'id': 1, 'kind': 'correction', 'case_id': 'train-case',
                 'independent': 'case:train-case', 'note': 'Keep the supplied date.',
                 'reproducible': True}]
        seen_prompts = []

        def optimizer(prompt, out, model, **_kwargs):
            seen_prompts.append(prompt)
            replacement = (self.target / 'SKILL.md').read_text(encoding='utf-8').replace(
                'Original linked behavior.', 'Keep the supplied date.')
            write_json(out / 'response.txt', {
                'rationale': 'Preserve the validated date behavior.',
                'changes': [{'path': 'SKILL.md', 'content': replacement}],
            })
            return {'status': 'completed'}

        def quality(_candidate, _evals, workspace, _model, *, repeats, **_kwargs):
            write_json(workspace / 'manifest.json', {'controlled': True, 'environment_consistent': True})
            return {'errors': 0, 'validation_errors': [], 'deltas': {
                arm: {'excluded_pairs': [], 'paired_pass_rate': {'n': 2 * repeats}}
                for arm in ('previous_skill', 'without_skill')
            }}

        def configured(*_args, **_kwargs):
            write_json(evals, {'evals': train + holdout})
            return ({'codex': {'model': 'gpt-test'}}, (train, holdout), evals,
                    hashlib.sha256(evals.read_bytes()).hexdigest(), 1, 10, set(), fixture_hashes)

        before = (self.target / 'SKILL.md').read_text(encoding='utf-8')
        config_snapshot = dict(self.config)
        config_snapshot['state'].mkdir(parents=True, exist_ok=True)
        with learning.connection(config_snapshot) as db, \
                patch.object(learning_worker, 'require_model_approval', return_value={}), \
                patch.object(learning_worker, 'configured_runs', side_effect=configured), \
                patch.object(learning_worker, 'validation_fingerprint', return_value='validation'), \
                patch('scripts.learning_promotion.verify_code'), \
                patch.object(learning_worker, 'run_model', side_effect=optimizer), \
                patch.object(learning_worker, 'run_quality', side_effect=quality), \
                patch.object(learning_worker, 'grade_matrix', return_value={
                    ('train-case', 'Keep dates'): {
                        'previous_skill': [False], 'with_skill': [True], 'deterministic': [True],
                    },
                }), \
                patch('scripts.learning_promotion.promote') as promote:
            report = learning_worker.revise(
                config_snapshot, 'demo', self.settings, self.target, tree_hash(self.target),
                rows, attempt, db)

        self.assertEqual(report['status'], 'qualified')
        self.assertEqual(report['promotion']['status'], 'manual_required')
        self.assertTrue(report['linked_source'])
        self.assertEqual(report['source_canonical'], str(self.target))
        self.assertIn('Original linked behavior.', seen_prompts[0])
        self.assertEqual((self.target / 'SKILL.md').read_text(encoding='utf-8'), before)
        promote.assert_not_called()

    def test_recovery_and_rollback_refuse_linked_sources(self):
        attempt = self.config['state'] / 'revisions' / '1' / 'demo'
        attempt.mkdir(parents=True)
        report = {'skill': 'demo', 'source': str(self.alias), 'source_canonical': str(self.target),
                  'before': tree_hash(self.target), 'status': 'validating'}
        write_json(attempt / 'report.json', report)
        write_json(attempt / 'promotion.json', {'phase': 'prepared'})
        with patch('scripts.learning_promotion.recover_exchange') as recover:
            status = learning_worker.recover_attempt(attempt, self.target, self.config['state'], linked=True)
        self.assertEqual(status, 'blocked')
        recover.assert_not_called()

        self.config['state'].mkdir(parents=True, exist_ok=True)
        with learning.connection(self.config) as db:
            db.execute("INSERT INTO revisions(checkpoint, skill, version, status, path) VALUES (1, ?, ?, 'applied', ?)",
                       ('demo', report['before'], str(attempt)))
            revision = db.execute('SELECT id FROM revisions').fetchone()['id']
            db.commit()
        with self.assertRaisesRegex(ValueError, 'read-only'):
            learning_worker.rollback_revision(self.config, revision)

    def test_linked_proposal_provenance_cannot_be_reused_after_retarget(self):
        other = self.link_root / 'other-demo'
        other.mkdir()
        (other / 'SKILL.md').write_text((self.target / 'SKILL.md').read_text(encoding='utf-8'))
        old_attempt = self.config['state'] / 'old-attempt'
        old_attempt.mkdir(parents=True)
        proposal = {'rationale': 'Preserve behavior.', 'changes': []}
        report = {
            'skill': 'demo', 'before': tree_hash(self.target), 'source_canonical': str(self.target),
            'source_modes': tree_modes(self.target), 'optimizer': {
                'input_hash': 'same-input', 'proposal_hash': learning_worker.proposal_hash(proposal),
            },
        }
        write_json(learning_worker.proposal_record(old_attempt),
                   learning_worker.proposal_archive(proposal, report))
        self.assertFalse(learning_worker.proposal_source_matches(old_attempt, report, other,
                                                                  report['before']))

    def test_changed_or_removed_link_marker_fails_closed(self):
        settings = dict(self.settings)
        settings.pop('linked_target')
        with self.assertRaisesRegex(ValueError, 'Symlinks are not allowed'):
            learning_worker.source_identity(self.config, settings)

        other = self.link_root / 'other-demo'
        other.mkdir()
        (other / 'SKILL.md').write_text((self.target / 'SKILL.md').read_text(encoding='utf-8'))
        self.alias.unlink()
        self.alias.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'changed or is no longer allowed'):
            learning_worker.source_identity(self.config, self.settings)


if __name__ == '__main__':
    unittest.main()
