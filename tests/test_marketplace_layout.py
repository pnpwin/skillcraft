import unittest

from tests import REPO, SKILL
from scripts.compliance import runner_hash
from scripts.harness import HARNESSES
from scripts.utils import ROOT, read_json


class MarketplaceLayoutTests(unittest.TestCase):
    def test_both_loaders_resolve_one_skill_in_the_plugin_package(self):
        catalog = read_json(REPO / '.agents/plugins/marketplace.json')
        [entry] = catalog['plugins']
        package_root = REPO / entry['source']['path']
        codex = read_json(package_root / '.codex-plugin/plugin.json')
        opencode = read_json(package_root / 'package.json')

        self.assertEqual(entry['name'], codex['name'])
        self.assertEqual(codex['version'], opencode['version'])
        self.assertTrue((package_root / opencode['exports']['.']).is_file())
        self.assertIn('skills/', opencode['files'])
        self.assertIn('src/opencode/', opencode['files'])
        self.assertTrue((package_root / 'src/opencode/learning.js').is_file())
        self.assertIn('hooks/', opencode['files'])
        capture = 'src/codex/learning_capture.py'
        self.assertIn(capture, opencode['files'])
        self.assertTrue((package_root / capture).is_file())
        self.assertEqual((package_root / codex['skills']).resolve(), SKILL.parent)
        self.assertEqual(set(read_json(package_root / codex['hooks'])['hooks']), {'PostToolUse', 'PostModelRequest', 'UserPromptSubmit', 'Stop'})
        self.assertEqual(list(package_root.rglob('SKILL.md')), [SKILL / 'SKILL.md'])
        self.assertEqual(ROOT, SKILL)

    def test_runner_fingerprints_resolve_inside_the_shared_skill(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                self.assertRegex(runner_hash(harness), r'^[0-9a-f]{64}$')
