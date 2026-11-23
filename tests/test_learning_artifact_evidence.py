import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import learning
from scripts.learning_observations import derived_cases
from scripts.utils import write_json


class ArtifactEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.project = self.home / 'project'
        self.project.mkdir()
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.enterContext(patch.dict('os.environ', {learning.INTERNAL: '0'}))
        self.enterContext(patch.object(learning, 'wake'))
        self.skill = self.project / '.agents/skills/demo'
        self.skill.mkdir(parents=True)
        (self.skill / 'SKILL.md').write_text(
            '---\nname: demo\ndescription: A demo skill.\n---\nUse the supplied notes.\n')
        (self.project / 'evals').mkdir()
        (self.project / 'evals/notes.md').write_text('Approved fixture.\n')
        write_json(self.project / 'evals/train.json', {'evals': [{
            'id': 'case-a', 'prompt': 'Create a summary from notes.md.',
            'expectations': ['The summary matches the owner checks.'],
            'files': [{'source': 'evals/notes.md', 'destination': 'notes.md'}],
            'checks': [
                {'text': 'The summary includes the marker.', 'type': 'contains',
                 'file': 'summary.txt', 'value': 'APPROVED'},
                {'text': 'The metadata file exists.', 'type': 'exists', 'file': 'meta.json'},
            ],
        }]})
        write_json(self.project / 'evals/holdout.json', {'evals': [{
            'id': 'case-h', 'prompt': 'Create a private holdout.', 'checks': [
                {'text': 'Never used by session proof.', 'type': 'exists', 'file': 'private.txt'},
            ],
        }]})
        write_json(self.project / learning.CONFIG, {
            'schema_version': 1, 'enabled': True, 'selection': 'all',
            'automation': {'create': True, 'cases': {
                'demo-topic': {'train': 'evals/train.json', 'holdout': 'evals/holdout.json'},
            }},
        })
        with redirect_stdout(io.StringIO()):
            self.assertEqual(learning.main(['enable', str(self.project)]), 0)
        self.config = learning.config_for(self.project)
        self.bindings = learning.binding(self.config, learning.source_inventory(self.config))

    def record_turn(self, *, status='error', prompt=None, session='s1', turn='t1', bound=True):
        prompt = prompt or 'Create a summary from notes.md.'
        learning.observe(self.project, 'codex', session, turn, 'user', prompt, [])
        outputs = self.project / 'outputs'
        if outputs.is_dir():
            for artifact in outputs.rglob('*'):
                if artifact.is_file() and not artifact.is_symlink():
                    metadata = artifact.stat()
                    os.utime(artifact, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
        learning.observe(self.project, 'codex', session, turn, 'assistant', 'The summary is ready.',
                         learning.source_inventory(self.config) if bound else [],
                         tool_events=[{'id': f'{session}-tool', 'tool': 'terminal', 'status': status}])

    def test_completed_tool_turn_records_multi_check_proof_and_idempotent_evidence(self):
        outputs = self.project / 'outputs'
        outputs.mkdir()
        (outputs / 'summary.txt').write_text('APPROVED content\n')
        (outputs / 'meta.json').write_text('{}\n')
        self.record_turn()
        with learning.connection(self.config) as db:
            row = db.execute("SELECT * FROM observations WHERE kind='success'").fetchone()
            evidence = db.execute("SELECT * FROM evidence WHERE origin='tool_check'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(evidence['origin'], 'tool_check')
            self.assertEqual(evidence['reproducible'], 1)
            details = json.loads(row['details'])
            self.assertEqual(len(details['case']['checks']), 2)
            self.assertEqual(set(details['proof']['artifacts']), {'summary.txt', 'meta.json'})
            self.assertNotIn('APPROVED content', row['details'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observations WHERE kind='tool'").fetchone()[0], 1)
        # Duplicate native completion does not add a second proof.
        self.record_turn()
        with learning.connection(self.config) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observations WHERE kind='success'").fetchone()[0], 1)
            cases, _ = derived_cases(db.execute('SELECT * FROM observations ORDER BY id').fetchall())
        self.assertEqual(len(cases), 1)
        self.assertEqual(len(cases[0]['checks']), 2)
        self.assertEqual(cases[0]['files'][0]['source'], str((self.project / 'evals/notes.md').resolve()))
        proof = json.loads(row['details'])['proof']
        self.assertEqual(proof['dataset_sha256'], self.config['automation_inputs']['cases']['demo-topic']['train']['sha256'])

    def test_manual_recheck_cannot_attribute_changed_outputs_to_earlier_turn(self):
        outputs = self.project / 'outputs'
        outputs.mkdir()
        (outputs / 'summary.txt').write_text('wrong content\n')
        (outputs / 'meta.json').write_text('{}\n')
        self.record_turn(status='timeout')
        failure = learning.verify_artifact(self.project, 'codex', 's1', 't1', case_id='case-a')
        self.assertEqual((failure['verified'], failure['kind']), (True, 'failure'))
        self.assertFalse(failure['inserted'])  # The automatic completion already stored this exact proof.
        (outputs / 'summary.txt').write_text('APPROVED content\n')
        stale = learning.verify_artifact(self.project, 'codex', 's1', 't1', case_id='case-a')
        self.assertFalse(stale['verified'])
        self.record_turn(turn='t2', status='ok')
        correction = learning.verify_artifact(self.project, 'codex', 's1', 't2', case_id='case-a')
        self.assertEqual((correction['verified'], correction['kind']), (True, 'correction'))
        (outputs / 'summary.txt').unlink()
        stale = learning.verify_artifact(self.project, 'codex', 's1', 't1', case_id='case-a')
        self.assertFalse(stale['verified'])
        with learning.connection(self.config) as db:
            kinds = [row[0] for row in db.execute(
                "SELECT kind FROM evidence WHERE origin='tool_check' ORDER BY id")]
        self.assertEqual(kinds, ['failure', 'correction'])

    def test_no_case_match_no_symlink_and_changed_fixture_are_not_evidence(self):
        outputs = self.project / 'outputs'
        outputs.mkdir()
        (outputs / 'summary.txt').write_text('APPROVED\n')
        (outputs / 'meta.json').write_text('{}')
        self.record_turn(prompt='Create a summary from another input.')
        self.assertFalse(learning.verify_artifact(self.project, 'codex', 's1', 't1')['verified'])
        (outputs / 'summary.txt').unlink()
        (outputs / 'summary.txt').symlink_to(self.project / 'evals/notes.md')
        self.record_turn(session='s2', turn='t2')
        self.assertFalse(learning.verify_artifact(self.project, 'codex', 's2', 't2')['verified'])
        (self.project / 'evals/notes.md').write_text('Changed fixture.\n')
        (outputs / 'summary.txt').unlink()
        (outputs / 'summary.txt').write_text('APPROVED\n')
        self.record_turn(session='s3', turn='t3')
        self.assertFalse(learning.verify_artifact(self.project, 'codex', 's3', 't3')['verified'])
        with learning.connection(self.config) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence WHERE origin='tool_check'").fetchone()[0], 0)

    def test_unbound_native_check_is_observed_but_not_credited_to_a_skill(self):
        outputs = self.project / 'outputs'
        outputs.mkdir()
        (outputs / 'summary.txt').write_text('APPROVED content\n')
        (outputs / 'meta.json').write_text('{}\n')
        self.record_turn(bound=False)
        with learning.connection(self.config) as db:
            row = db.execute("SELECT * FROM observations WHERE kind='success'").fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row['skill'])
            self.assertIsNone(row['version'])
            proof = json.loads(row['details'])['proof']
            self.assertNotIn('skill', proof)
            self.assertNotIn('version', proof)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence WHERE origin='tool_check'").fetchone()[0], 0)

    def test_cli_standalone_path_records_manual_proof_without_fake_session_or_skill(self):
        outputs = self.project / 'outputs'
        outputs.mkdir()
        (outputs / 'summary.txt').write_text('APPROVED content\n')
        (outputs / 'meta.json').write_text('{}\n')

        def verify():
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(learning.main(['verify-artifact', str(self.project), '--topic',
                                                'demo-topic', '--case', 'case-a']), 0)
            return json.loads(stream.getvalue())

        first = verify()
        self.assertEqual((first['verified'], first['provenance'], first['passed']), (True, 'manual', True))
        self.assertTrue(first['inserted'])
        self.assertFalse(verify()['inserted'])
        train_path = self.project / 'evals/train.json'
        train_path.write_text(json.dumps(json.loads(train_path.read_text()), separators=(',', ':')))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(learning.main(['enable', str(self.project)]), 0)
        self.config = learning.config_for(self.project)
        reformatted = verify()
        self.assertFalse(reformatted['inserted'])
        self.assertEqual(reformatted['proof']['input_hash'], first['proof']['input_hash'])
        self.assertNotEqual(reformatted['proof']['dataset_sha256'], first['proof']['dataset_sha256'])
        with learning.connection(self.config) as db:
            row = db.execute('SELECT * FROM artifact_proofs').fetchone()
            self.assertEqual(row['provenance'], 'manual')
            details = json.loads(row['details'])
            proof = details['proof']
            self.assertEqual(proof['topic'], 'demo-topic')
            self.assertEqual(proof['case_id'], 'case-a')
            self.assertNotIn('session', proof)
            self.assertNotIn('skill', proof)
            self.assertNotIn('version', proof)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
