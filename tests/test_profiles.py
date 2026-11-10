from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.runtime import profile_settings
from scripts.utils import write_json


class Profiles(unittest.TestCase):
    def test_default_and_opt_in_worker(self):
        default = profile_settings(None, 'candidate')
        self.assertTrue(default['controlled'])
        self.assertFalse(default['allow_delegation'])
        custom = profile_settings(ROOT / 'tests/fixtures/delegation-profile.json', 'candidate')
        self.assertFalse(custom['controlled'])
        self.assertEqual(custom['permissions'][-1], {'action': 'subagent', 'resource': 'skillcraft-worker', 'effect': 'allow'})
        self.assertTrue(any(p['action'] == 'skill' and p['resource'] == 'candidate' for p in custom['permissions']))
        base = profile_settings(ROOT / 'tests/fixtures/shell-profile.json')
        self.assertFalse(any(p['action'] == 'skill' and p['effect'] == 'allow' for p in base['permissions']))
        self.assertEqual(base['steps'], 48)

    def test_bad_config_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.json'
            for bad in [{'allow_delegation': 'true'}, {'steps': 0}, {'image_generation': True}, {'permissions': [{'action': 'shell'}]}]:
                write_json(path, bad)
                with self.assertRaises(ValueError):
                    profile_settings(path)


if __name__ == '__main__':
    unittest.main()
