import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.compliance import assess, acceptance_exit, runner_hash, skill_hash
from scripts.utils import copy_tree, digest, write_json
from scripts.improve_description import improve_description
from scripts.run_eval import run_eval
from scripts.run_quality_eval import run_quality
from scripts.package_skill import _normalize_runs


class Compliance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / 'candidate' / 'demo-skill'
        copy_tree(ROOT / 'tests/fixtures/demo-skill', self.skill, skill=True)
        self.run = self.root / 'run'
        self.run.mkdir()
        self.installed = self.root / 'installed' / 'demo-skill'
        installed_hash = copy_tree(self.skill, self.installed, skill=True)
        self.assertEqual(installed_hash, skill_hash(self.skill))
        self.request = {'model': {'providerID': 'test', 'id': 'model'}, 'run_id': 'run1', 'operation': 'run',
            'mode': 'quality', 'attach_skill': True, 'skill_id': 'demo-skill',
            'skill_path': str(self.installed / 'SKILL.md'), 'skill_sha256': digest(self.installed / 'SKILL.md')}
        environment = {'server_version': '2.2.7', 'background': [], 'plugins': [], 'configuration_fingerprint': 'config',
            'controlled': False, 'candidate': {'id': 'demo-skill', 'path': str(self.installed / 'SKILL.md')}}
        self.record = {'runner_hash': runner_hash(), 'status': 'completed', 'run_id': 'run1', 'prompt_id': 'prompt1',
            'model': self.request['model'], 'profile': {'controlled': False}, 'installed_hash': installed_hash,
            'environment': environment, 'attached': True}
        write_json(self.run / 'request.json', self.request)
        write_json(self.run / 'profile.json', self.record['profile'])
        write_json(self.run / 'profile-source.json', {})
        write_json(self.run / 'environment.json', environment)
        write_json(self.run / 'messages.json', [{'type': 'user', 'id': 'prompt1', 'skills': [{'id': 'demo-skill'}]}])
        (self.run / 'response.txt').write_text('Task output')
        self.persist()

    def persist(self):
        self.record['evidence_hashes'] = {name: digest(self.run / name) for name in
            ['request.json', 'profile.json', 'profile-source.json', 'messages.json', 'response.txt', 'environment.json']}
        write_json(self.run / 'run.json', self.record)

    def tearDown(self):
        self.temp.cleanup()

    def test_status_matrix_only_two_passes_accept(self):
        for a, b in itertools.product(['PASS', 'FAIL', 'NOT_RUN', 'ERROR'], repeat=2):
            check = {'agent_skills': {'status': a}, 'opencode': {'status': b}, 'accepted': True}
            self.assertEqual(acceptance_exit(check) == 0, a == b == 'PASS')

    def test_packager_accepts_dual_runs_and_preserves_single_run_api(self):
        self.assertEqual(_normalize_runs(run_dir=Path('legacy-run')), {'opencode': Path('legacy-run')})
        self.assertEqual(_normalize_runs(run_dirs={'opencode': Path('open'), 'codex': Path('codex')}),
                         {'opencode': Path('open'), 'codex': Path('codex')})
        with self.assertRaises(ValueError):
            _normalize_runs(run_dirs={'opencode': Path('same'), 'codex': Path('same')})

    def test_matching_evidence_and_legacy_absence(self):
        self.assertTrue(assess(self.skill, self.run)['accepted'])
        self.assertEqual(assess(self.skill)['opencode']['status'], 'NOT_RUN')
        del self.record['runner_hash']
        self.persist()
        self.assertEqual(assess(self.skill, self.run)['opencode']['status'], 'NOT_RUN')

    def test_corruption_is_error_and_changed_body_is_fail(self):
        (self.run / 'response.txt').write_text('Corrupted')
        self.assertEqual(assess(self.skill, self.run)['opencode']['status'], 'ERROR')
        self.persist()
        (self.skill / 'SKILL.md').write_text((self.skill / 'SKILL.md').read_text() + '\nNew instructions\n')
        check = assess(self.skill, self.run)
        self.assertEqual(check['agent_skills']['status'], 'PASS')
        self.assertEqual(check['opencode']['status'], 'FAIL')
        self.assertFalse(check['accepted'])

    def test_runtime_success_cannot_promote_invalid_standard(self):
        for directory in [self.skill, self.installed]:
            p = directory / 'SKILL.md';p.write_text(p.read_text().replace('name: demo-skill', 'name: Demo Skill'))
        self.record['installed_hash'] = skill_hash(self.installed)
        self.request['skill_sha256'] = digest(self.installed / 'SKILL.md')
        write_json(self.run / 'request.json', self.request)
        self.persist()
        check = assess(self.skill, self.run)
        self.assertEqual(check['agent_skills']['status'], 'FAIL')
        self.assertEqual(check['opencode']['status'], 'PASS')
        self.assertFalse(check['accepted'])

    def test_current_environment_required_and_drift_rejected(self):
        self.assertEqual(assess(self.skill, self.run, require_fresh=True)['opencode']['status'], 'NOT_RUN')
        probe = self.root / 'probe'
        copy_tree(self.run, probe)
        req = json.loads((probe / 'request.json').read_text());req['operation'] = 'probe'
        write_json(probe / 'request.json', req)
        run = json.loads((probe / 'run.json').read_text());run['evidence_hashes']['request.json'] = digest(probe / 'request.json')
        write_json(probe / 'run.json', run)
        self.assertTrue(assess(self.skill, self.run, require_fresh=True, probe_dir=probe)['accepted'])
        run['environment']['configuration_fingerprint'] = 'new-global-config'
        write_json(probe / 'environment.json', run['environment'])
        run['evidence_hashes']['environment.json'] = digest(probe / 'environment.json')
        write_json(probe / 'run.json', run)
        self.assertEqual(assess(self.skill, self.run, require_fresh=True, probe_dir=probe)['opencode']['status'], 'FAIL')

    def test_error_trigger_only_and_false_attachment(self):
        self.record['status'] = 'timeout';self.persist()
        self.assertEqual(assess(self.skill, self.run)['opencode']['status'], 'ERROR')
        self.record['status'] = 'completed';self.request['mode'] = 'trigger'
        write_json(self.run / 'request.json', self.request);self.persist()
        self.assertEqual(assess(self.skill, self.run)['opencode']['status'], 'NOT_RUN')
        self.request['mode'] = 'quality';write_json(self.run / 'request.json', self.request)
        write_json(self.run / 'messages.json', [{'type': 'user', 'id': 'wrong', 'skills': [{'id': 'other'}]}]);self.persist()
        self.assertEqual(assess(self.skill, self.run)['opencode']['status'], 'ERROR')

    def test_codex_requires_explicit_hash_bound_attachment_and_harness_match(self):
        self.record['harness'] = 'codex'
        self.record.update(thread_id='thread', turn_id='turn')
        self.request['harness'] = 'codex'
        self.request['model'] = {'id': 'model', 'effort': 'high'}
        self.record['model'] = self.request['model']
        self.record['environment']['server_version'] = '0.170.0'
        write_json(self.run / 'environment.json', self.record['environment'])
        write_json(self.run / 'request.json', self.request)
        proof = {'schema_version': 1, 'confirmed': True,
            'evidence_kind': 'explicit_input_and_durable_user_item', 'thread_id': 'thread',
            'turn_id': 'turn', 'user_message_id': 'message',
            'skill': {'name': 'demo-skill', 'path': str(self.installed / 'SKILL.md'),
                      'sha256': digest(self.installed / 'SKILL.md')},
            'source_item': {'type': 'skill', 'id': 'item'}}
        write_json(self.run / 'codex_attachment.json', proof)
        self.record['runner_hash'] = runner_hash('codex')
        self.persist()
        self.record['evidence_hashes']['codex_attachment.json'] = digest(self.run / 'codex_attachment.json')
        write_json(self.run / 'run.json', self.record)
        check = assess(self.skill, self.run)
        self.assertTrue(check['accepted'])
        self.assertEqual(check['selected_runtime'], 'codex')
        self.assertEqual(check['codex']['status'], 'PASS')
        self.assertEqual(check['opencode']['status'], 'NOT_RUN')
        probe = self.root / 'codex-probe'
        copy_tree(self.run, probe)
        (probe / 'codex_attachment.json').unlink()
        probe_request = json.loads((probe / 'request.json').read_text())
        probe_request['operation'] = 'probe'
        write_json(probe / 'request.json', probe_request)
        probe_record = json.loads((probe / 'run.json').read_text())
        probe_record['evidence_hashes'].pop('codex_attachment.json')
        probe_record['evidence_hashes']['request.json'] = digest(probe / 'request.json')
        write_json(probe / 'run.json', probe_record)
        self.assertTrue(assess(self.skill, self.run, probe_dir=probe, require_fresh=True)['accepted'])
        mismatched = assess(self.skill, self.run, harness='opencode')
        self.assertEqual(mismatched['selected_runtime'], 'opencode')
        self.assertEqual(mismatched['opencode']['status'], 'FAIL')

        proof['confirmed'] = None
        write_json(self.run / 'codex_attachment.json', proof)
        self.record['evidence_hashes']['codex_attachment.json'] = digest(self.run / 'codex_attachment.json')
        write_json(self.run / 'run.json', self.record)
        self.assertEqual(assess(self.skill, self.run)['codex']['status'], 'ERROR')

    def test_real_cli_missing_runtime_and_package_block(self):
        p = subprocess.run([sys.executable, str(ROOT / 'scripts/compliance.py'), str(self.skill),
            '--workspace', str(self.root / 'acceptance')], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(json.loads(p.stdout)['opencode']['status'], 'NOT_RUN')
        p = subprocess.run([sys.executable, str(ROOT / 'scripts/package_skill.py'), str(self.skill), str(self.root / 'dist')], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertFalse((self.root / 'dist/demo-skill.skill').exists())

    def test_invalid_proposal_never_becomes_candidate(self):
        for index, description in enumerate(['', 'x' * 1025, 123]):
            output = self.root / f'optimizer-{index}'
            def model(prompt, target, *args, **kwargs):
                target.mkdir();write_json(target / 'response.txt', {'description': description, 'rationale': 'reason'})
                return {'status': 'completed'}
            with patch('scripts.improve_description.run_model', side_effect=model), self.assertRaises(ValueError):
                improve_description(self.skill, {'results': []}, output, 'test/model')
            self.assertFalse((output / 'proposal.json').exists())

    def test_standard_invalid_blocked_before_model_execution(self):
        p = self.skill / 'SKILL.md';p.write_text(p.read_text().replace('name: demo-skill', 'name: Demo Skill'))
        with patch('scripts.run_eval.run_model') as model, self.assertRaises(ValueError):
            run_eval(self.skill, ROOT / 'tests/fixtures/trigger.json', self.root / 'trigger', 'test/model')
        model.assert_not_called()
        with patch('scripts.run_quality_eval.run_model') as model, self.assertRaises(ValueError):
            run_quality(self.skill, ROOT / 'tests/fixtures/quality.json', self.root / 'quality', 'test/model')
        model.assert_not_called()
        for name in ['trigger', 'quality']:
            self.assertEqual(json.loads((self.root / name / 'compliance.json').read_text())['agent_skills']['status'], 'FAIL')

    def test_projection_matches_snapshot_and_resource_changes_invalidate(self):
        (self.skill / 'tests').mkdir();(self.skill / 'tests/private.json').write_text('Holdout stays out')
        (self.skill / '.venv').mkdir();(self.skill / '.venv/dependency').write_text('Local install stays out')
        snapshot = self.root / 'projection'
        self.assertEqual(skill_hash(self.skill), copy_tree(self.skill, snapshot, skill=True))
        (self.skill / 'references').mkdir();(self.skill / 'references/guide.md').write_text('Changed instructions')
        self.assertEqual(assess(self.skill, self.run)['opencode']['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
