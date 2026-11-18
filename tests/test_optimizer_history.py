import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from scripts.improve_description import improve_description, optimizer_payload


class History(unittest.TestCase):
    def test_previous_attempts_reach_actual_request_without_holdout(self):
        train = {'results': [{'eval_id': 'train', 'repeat': 1, 'prompt': 'Training prompt', 'should_trigger': True, 'triggered': False, 'status': 'completed'}]}
        attempts = [{'candidate_id': f'v{i}', 'snapshot_hash': f'h{i}', 'description': f'desc {i}', 'rationale': f'reason {i}',
                     'validation': 'VALIDATION_CANARY', 'final_test': 'TEST_CANARY',
                     'training_results': train['results'] + [{'eval_id': 'holdout', 'prompt': 'HOLDOUT_CANARY'}]} for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            def model(prompt, output, *args, **kwargs):
                self.assertEqual(kwargs.get('profile'), Path('/explicit/optimizer-profile.json'))
                self.assertIn('desc 0', prompt)
                self.assertIn('desc 1', prompt)
                self.assertIn('reason 0', prompt)
                for canary in ['VALIDATION_CANARY', 'TEST_CANARY', 'HOLDOUT_CANARY']:
                    self.assertNotIn(canary, prompt)
                output.mkdir()
                (output / 'response.txt').write_text(json.dumps({'description': 'Use for receipts', 'rationale': 'Cover indirect requests'}))
                return {'status': 'completed'}
            target = Path(tmp) / 'optimizer'
            with patch('scripts.improve_description.run_model', side_effect=model):
                improve_description(ROOT / 'tests/fixtures/demo-skill', train, target, 'test/model', history=attempts, profile=Path('/explicit/optimizer-profile.json'))
            payload = json.loads((target / 'optimizer-input.json').read_text())
            self.assertEqual([a['candidate_id'] for a in payload['previous_attempts']], ['v0', 'v1'])
            self.assertEqual(len(payload['previous_attempts'][0]['training_results']), 1)

    def test_legacy_missing_fields_are_not_invented(self):
        p = optimizer_payload(ROOT / 'tests/fixtures/demo-skill', {'results': []}, [{'description': 'old'}])
        entry = p['previous_attempts'][0]
        self.assertIsNone(entry['rationale'])
        self.assertIn('training_results', entry['missing_fields'])

    def test_nested_metadata_cannot_bypass_allowlist(self):
        with self.assertRaises(ValueError):
            optimizer_payload(ROOT / 'tests/fixtures/demo-skill', {'results': [
                {'eval_id': 'train', 'prompt': {'validation': 'CANARY'}, 'status': 'completed'}]})


if __name__ == '__main__':
    unittest.main()
