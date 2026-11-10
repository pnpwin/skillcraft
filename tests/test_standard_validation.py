import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.quick_validate import validate_metadata, validate_standard, validate_skill, validate_opencode_static
from scripts.utils import frontmatter, parse_frontmatter, set_description, tree_hash


class StandardValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'example'
        self.root.mkdir()
        self.file = self.root / 'SKILL.md'
        self.data = {'name': 'example', 'description': 'Use for example tasks.'}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data=None, body='Instructions\n'):
        self.file.write_text('---\n' + yaml.safe_dump(self.data if data is None else data, allow_unicode=True) + '---\n' + body)

    def test_length_boundaries_count_unicode_not_bytes(self):
        for length, expected in [(1, True), (64, True), (65, False)]:
            name = 'é' * length
            self.assertEqual(not validate_metadata(dict(self.data, name=name), name), expected)
        for field, limit in [('description', 1024), ('compatibility', 500)]:
            for length in [1, limit, limit + 1]:
                with self.subTest(field=field, length=length):
                    issues = validate_metadata(dict(self.data, **{field: '文' * length}), 'example')
                    self.assertEqual(bool(issues), length > limit)
                    if issues:
                        self.assertEqual(issues[0]['actual_length'], length)

    def test_required_fields_empty_and_wrong_types(self):
        for key in ['name', 'description']:
            d = dict(self.data)
            del d[key]
            self.assertIn('required', [i['code'] for i in validate_metadata(d, 'example')])
            for value in ['', '  ', None, False, 123, [], {}]:
                self.assertTrue(validate_metadata(dict(self.data, **{key: value}), 'example'), (key, value))

    def test_name_rules_exact_match_without_repair(self):
        for name in ['Example', 'two words', '-example', 'example-', 'two--words', ' example', 'example ', 'other']:
            self.assertTrue(validate_metadata(dict(self.data, name=name), 'example'), name)
        for name in ['example', 'données', '数据', 'a-b2']:
            self.assertEqual(validate_metadata(dict(self.data, name=name), name), [])
        # The reference's normalization is not permission to silently repair names.
        self.assertTrue(validate_metadata(dict(self.data, name='ｅxample'), 'example'))

    def test_optional_fields_and_unknown_keys(self):
        self.assertEqual(validate_metadata(self.data, 'example'), [])
        for key in ['license', 'compatibility', 'allowed-tools']:
            for value in [None, True, 1, [], {}]:
                self.assertTrue(validate_metadata(dict(self.data, **{key: value}), 'example'))
        for value in ['', '   ']:
            self.assertTrue(validate_metadata(dict(self.data, compatibility=value), 'example'))
        d = dict(self.data, license='See LICENSE.txt', compatibility='Requires Python', **{'allowed-tools': 'Read Bash'})
        self.assertEqual(validate_metadata(d, 'example'), [])
        self.assertIn('unknown_field', [i['code'] for i in validate_metadata(dict(d, author='team'), 'example')])
        self.assertTrue(validate_metadata({**self.data, 1: 'not a string key'}, 'example'))

    def test_metadata_types_are_not_coerced(self):
        for value in [None, [], 'x', True, {'x': False}, {'x': 1.0}, {1: 'x'}, {'x': {'name': 'team'}}, {'x': ['v']}]:
            self.assertTrue(validate_metadata(dict(self.data, metadata=value), 'example'), value)
        self.assertEqual(validate_metadata(dict(self.data, metadata={'example.org/version': '1.0', 'enabled': 'true'}), 'example'), [])
        self.assertEqual(validate_metadata(dict(self.data, metadata={}), 'example'), [])

    def test_parser_failures_and_missing_file(self):
        self.assertEqual(validate_standard(self.root)['status'], 'FAIL')
        for text in ['Body', '---\nname: example\n', '---\n[bad\n---\nBody', '---\n[]\n---\nBody', '---\nfalse\n---\nBody',
                     '---\nname: example\nname: other\n---\nBody',
                     '---\nname: example\ndescription: ok\nmetadata:\n  a: one\n  a: two\n---\nBody',
                     '---\nname: !!python/object:secret.invalid {}\n---\nBody']:
            self.file.write_text(text)
            r = validate_standard(self.root)
            self.assertEqual(r['status'], 'FAIL', text)
            self.assertEqual(r['issues'][0]['field'], 'frontmatter')
        self.file.write_bytes(b'---\nname: \xff\n---\n')
        self.assertEqual(validate_standard(self.root)['status'], 'FAIL')

    def test_yaml_scalar_merge_and_set_description_preserve_values(self):
        self.file.write_text('---\nname: example\ndescription: >-\n  first\n  second\nmetadata:\n  version: "1.0"\n  enabled: "false"\n---\nBody unchanged\n')
        data, body = frontmatter(self.file, required=True)
        self.assertEqual(data['description'], 'first second')
        self.assertEqual(validate_standard(self.root)['status'], 'PASS')
        set_description(self.root, 'New description')
        after, after_body = frontmatter(self.file)
        self.assertEqual(after_body, body)
        self.assertEqual(after['metadata'], data['metadata'])
        merged, _ = parse_frontmatter('---\nname: example\ndescription: ok\nmetadata:\n  <<: &base {version: "1.0"}\n  version: "2.0"\n---\nBody', required=True)
        self.assertEqual(merged['metadata']['version'], '2.0')

    def test_read_error_is_not_nonconformance(self):
        self.write()
        with patch.object(Path, 'read_bytes', side_effect=PermissionError(13, 'denied')):
            result = validate_standard(self.root)
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['issues'][0]['error_type'], 'PermissionError')

    def test_runtime_acceptance_does_not_imply_standard_conformance(self):
        self.write(dict(self.data, name='Example Display', metadata={'opencode/autoinvoke': True}))
        before = self.file.read_bytes()
        self.assertEqual(validate_opencode_static(self.root)['status'], 'PASS')
        self.assertEqual(validate_opencode_static(self.root)['opencode_compatibility'], 'NOT_RUN')
        self.assertEqual(validate_standard(self.root)['status'], 'FAIL')
        self.assertFalse(validate_skill(self.root)[0])
        self.assertEqual(self.file.read_bytes(), before)

    def test_recommendations_are_nonblocking(self):
        self.write(body='\n'.join('Instruction' for _ in range(501)))
        result = validate_standard(self.root)
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(result['recommendations'])
        self.write(body='')
        self.assertEqual(validate_standard(self.root)['status'], 'PASS')

    def test_real_cli_and_json_exit_contract(self):
        for data, expected in [(self.data, 0), (dict(self.data, description='x'*1025), 1), ({}, 1)]:
            self.write(data)
            before = self.file.read_bytes()
            p = subprocess.run([sys.executable, str(ROOT/'scripts/quick_validate.py'), str(self.root), '--json'], capture_output=True, text=True)
            self.assertEqual(p.returncode, expected, p.stderr)
            result = json.loads(p.stdout)
            self.assertEqual(result['check'], 'agent_skills')
            self.assertEqual(result['status'], 'PASS' if expected == 0 else 'FAIL')
            self.assertNotIn('OpenCode compatibility: PASS', p.stdout)
            self.assertEqual(self.file.read_bytes(), before)

    def test_cli_io_error_and_useful_text_diagnostics(self):
        self.write(dict(self.data, description='x'*1025))
        command = [sys.executable, str(ROOT/'scripts/quick_validate.py'), str(self.root)]
        text = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(text.returncode, 1)
        self.assertIn('description [length]', text.stdout)
        self.assertIn('actual_length=1025', text.stdout)
        self.file.chmod(0)
        try:
            if os.access(self.file, os.R_OK):
                self.skipTest('Privileged process can read mode-000 files; unit I/O error coverage still runs')
            p = subprocess.run(command + ['--json'], capture_output=True, text=True)
            self.assertEqual(p.returncode, 3, p.stderr)
            self.assertEqual(json.loads(p.stdout)['status'], 'ERROR')
        finally:
            self.file.chmod(0o600)

    def test_dotdot_path_uses_actual_directory_name(self):
        self.write()
        (self.root/'child').mkdir()
        self.assertEqual(validate_standard(self.root/'child'/'..')['status'], 'PASS')

    def test_yaml_failures_do_not_echo_source_values(self):
        self.file.write_text('---\nname: example\ndescription: [PRIVATE_VALUE_CANARY\n---\nBody')
        result = validate_standard(self.root)
        self.assertEqual(result['status'], 'FAIL')
        self.assertNotIn('PRIVATE_VALUE_CANARY', json.dumps(result))

    def test_portable_filename_case_on_case_insensitive_filesystems(self):
        (self.root / 'skill.md').write_text('---\nname: example\ndescription: Example task\n---\nBody\n')
        result = validate_standard(self.root)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['issues'][0]['code'], 'missing_file')

    def test_unreadable_directory_is_error_not_missing_format(self):
        self.write()
        self.root.chmod(0)
        try:
            if os.access(self.root, os.R_OK | os.X_OK):
                self.skipTest('Privileged process can traverse mode-000 directories')
            self.assertEqual(validate_standard(self.root)['status'], 'ERROR')
        finally:
            self.root.chmod(0o700)

    def test_directory_case_alias_cannot_repair_name_mismatch(self):
        actual = Path(self.tmp.name) / 'Other'
        actual.mkdir()
        (actual / 'SKILL.md').write_text('---\nname: other\ndescription: Example task\n---\nBody\n')
        self.assertEqual(validate_standard(actual)['status'], 'FAIL')
        self.assertEqual(validate_standard(actual.parent / 'other')['status'], 'FAIL')

    def test_cli_does_not_create_bytecode_in_installed_skill(self):
        self.write()
        installed = Path(self.tmp.name) / 'installed-validator'
        (installed / 'scripts').mkdir(parents=True)
        for name in ['__init__.py', 'quick_validate.py', 'utils.py']:
            target = installed / 'scripts' / name
            shutil.copy2(ROOT / 'scripts' / name, target)
            target.chmod(0o444)
        before = tree_hash(installed)
        p = subprocess.run([sys.executable, str(installed / 'scripts/quick_validate.py'), str(self.root), '--json'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(tree_hash(installed), before)
        self.assertFalse((installed / 'scripts/__pycache__').exists())


if __name__ == '__main__':
    unittest.main()
