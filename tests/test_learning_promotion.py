import os
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import learning_promotion
from scripts.learning_promotion import _tree_hash, promote, recover_exchange, rollback, tree_modes, verify_code


class LearningPromotion(unittest.TestCase):
    def make_tree(self, path, files):
        path.mkdir(parents=True)
        for name, value in files.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        return path

    def test_text_changes_skip_container_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.make_tree(root / 'original', {'SKILL.md': b'old\n'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'new\n'})
            verify_code(original, candidate, [], root / 'out')
            self.assertEqual((candidate / 'SKILL.md').read_bytes(), b'new\n')

    def test_scripts_and_manifests_require_code_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.make_tree(root / 'original', {
                'scripts/settings.json': b'{"mode":"old"}',
                'package.json': b'{"scripts":{}}',
            })
            candidate = self.make_tree(root / 'candidate', {
                'scripts/settings.json': b'{"mode":"new"}',
                'package.json': b'{"scripts":{"postinstall":"run"}}',
            })
            with self.assertRaisesRegex(ValueError, 'verification_required'):
                verify_code(original, candidate, [], root / 'out')

    def test_tree_hash_rejects_nonregular_entries(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO creation is unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.mkfifo(root / 'fifo')
            with self.assertRaisesRegex(ValueError, 'Unsupported filesystem entry'):
                _tree_hash(root)

    def test_executable_change_requires_tests_and_uses_isolated_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.make_tree(root / 'original', {'runner.py': b'print(1)\n'})
            candidate = self.make_tree(root / 'candidate', {'runner.py': b'print(2)\n'})
            with self.assertRaisesRegex(ValueError, 'verification_required'):
                verify_code(original, candidate, [], root / 'out')
            command = [['python', '-m', 'unittest', 'verify_candidate']]
            staged_hashes = []
            def fake_gate(staged, frozen, commands):
                staged_hashes.append(_tree_hash(staged))
                self.assertEqual(_tree_hash(frozen), _tree_hash(original))
                self.assertEqual(commands, command)
            with patch('scripts.learning_promotion._run_isolated_tests') as gate:
                gate.side_effect = fake_gate
                verify_code(original, candidate, command, root / 'out')
            gate.assert_called_once()
            self.assertNotEqual(gate.call_args.args[0], candidate.resolve())
            self.assertEqual(staged_hashes, [_tree_hash(candidate)])
            self.assertEqual(gate.call_args.args[2], command)
            self.assertEqual((candidate / 'runner.py').read_bytes(), b'print(2)\n')

    def test_promote_and_rollback_preserve_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_tree(root / 'source', {'SKILL.md': b'old\n', 'nested/data.txt': b'keep'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'new\n', 'nested/data.txt': b'keep'})
            before, candidate_hash = _tree_hash(source), _tree_hash(candidate)
            source_mode = stat.S_IMODE((source / 'SKILL.md').stat().st_mode)
            candidate_mode = stat.S_IMODE((candidate / 'SKILL.md').stat().st_mode)
            record_path = root / 'state' / 'promotion.json'
            record = promote(source, candidate, before, root / 'state',
                             expected_candidate_hash=candidate_hash, record_path=record_path)
            self.assertEqual(_tree_hash(source), candidate_hash)
            self.assertEqual(_tree_hash(Path(record['backup'])), before)
            self.assertEqual(_tree_hash(Path(record['candidate_snapshot'])), candidate_hash)
            self.assertEqual(_tree_hash(candidate), candidate_hash)
            self.assertEqual(json.loads(record_path.read_text()), record)
            self.assertEqual(stat.S_IMODE((Path(record['backup']) / 'SKILL.md').stat().st_mode), source_mode)
            self.assertEqual(stat.S_IMODE((Path(record['candidate_snapshot']) / 'SKILL.md').stat().st_mode), candidate_mode)
            restored = rollback(source, record, root / 'state', record_path=record_path)
            self.assertEqual(_tree_hash(source), before)
            self.assertEqual(_tree_hash(Path(restored['backup'])), candidate_hash)
            self.assertEqual(stat.S_IMODE((source / 'SKILL.md').stat().st_mode), source_mode)

    def test_exchange_and_rollback_do_not_leave_duplicate_skills_in_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            skills = root / 'project/.agents/skills'
            source = self.make_tree(skills / 'demo', {'SKILL.md': b'old'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'new'})
            state = root / 'state'
            before = _tree_hash(source)
            record = promote(source, candidate, before, state)
            self.assertEqual([source / 'SKILL.md'], list(skills.rglob('SKILL.md')))
            self.assertTrue(learning_promotion.recover_exchange(source, record, state))
            rollback(source, record, state)
            self.assertEqual(before, _tree_hash(source))
            self.assertEqual([source / 'SKILL.md'], list(skills.rglob('SKILL.md')))

    def test_bad_hash_and_symlink_leave_live_tree_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_tree(root / 'source', {'SKILL.md': b'old'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'new'})
            with self.assertRaisesRegex(ValueError, 'source_hash_mismatch'):
                promote(source, candidate, 'not-the-hash', root / 'state')
            with self.assertRaisesRegex(ValueError, 'candidate_hash_mismatch'):
                promote(source, candidate, _tree_hash(source), root / 'state',
                        expected_candidate_hash='not-the-candidate-hash')
            self.assertEqual((source / 'SKILL.md').read_bytes(), b'old')
            self.assertEqual((candidate / 'SKILL.md').read_bytes(), b'new')
            symlink = root / 'symlink'
            symlink.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                promote(symlink, candidate, _tree_hash(candidate), root / 'state')
            self.assertEqual((source / 'SKILL.md').read_bytes(), b'old')

    def test_exchange_error_keeps_source_and_candidate_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_tree(root / 'source', {'SKILL.md': b'old'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'new'})
            with patch('scripts.learning_promotion._atomic_exchange', side_effect=RuntimeError('unsupported')):
                with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                    promote(source, candidate, _tree_hash(source), root / 'state',
                            expected_candidate_hash=_tree_hash(candidate))
            self.assertEqual((source / 'SKILL.md').read_bytes(), b'old')
            self.assertEqual((candidate / 'SKILL.md').read_bytes(), b'new')
            snapshots = list((root / 'state' / 'snapshots').iterdir())
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(all(path.stat().st_mode & 0o700 == 0o700 for path in snapshots))

    def test_record_is_durable_before_exchange_and_preserves_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_tree(root / 'source', {'SKILL.md': b'old'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'new'})
            record_path = root / 'state' / 'worker-record.json'
            real_exchange = __import__('scripts.learning_promotion', fromlist=['_atomic_exchange'])._atomic_exchange
            def check_record_first(first, second):
                staged = Path(second)
                self.assertNotEqual(staged, candidate)
                self.assertEqual(_tree_hash(candidate), _tree_hash(staged))
                saved = json.loads(record_path.read_text())
                self.assertEqual(saved['before_hash'], _tree_hash(source))
                self.assertEqual(saved['after_hash'], _tree_hash(staged))
                real_exchange(first, second)
            with patch('scripts.learning_promotion._atomic_exchange', side_effect=check_record_first):
                record = promote(source, candidate, _tree_hash(source), root / 'state',
                                 expected_candidate_hash=_tree_hash(candidate), record_path=record_path)
            self.assertEqual(json.loads(record_path.read_text()), record)
            self.assertEqual((candidate / 'SKILL.md').read_bytes(), b'new')
            self.assertEqual(_tree_hash(source), record['after_hash'])

    def test_rollback_hash_guard_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_tree(root / 'source', {'SKILL.md': b'old'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'new'})
            record = promote(source, candidate, _tree_hash(source), root / 'state',
                             expected_candidate_hash=_tree_hash(candidate))
            (source / 'SKILL.md').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'rollback_hash_mismatch'):
                rollback(source, record, root / 'state')

    def test_source_edit_during_candidate_staging_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.make_tree(root / 'source', {'SKILL.md': b'original'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'qualified'})
            original_snapshot = learning_promotion._snapshot

            def edit_during_staging(path, destination):
                result = original_snapshot(path, destination)
                if destination.name.startswith('.source.promotion-'):
                    (source / 'SKILL.md').write_bytes(b'live edit')
                return result

            with patch.object(learning_promotion, '_snapshot', side_effect=edit_during_staging), \
                    patch.object(learning_promotion, '_atomic_exchange') as exchange:
                with self.assertRaisesRegex(ValueError, 'source changed before exchange'):
                    promote(source, candidate, _tree_hash(source), root / 'state')
            exchange.assert_not_called()
            self.assertEqual((source / 'SKILL.md').read_bytes(), b'live edit')

    def test_edit_at_exchange_and_after_worker_stop_is_restored(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source = self.make_tree(root / 'source', {'SKILL.md': b'original'})
                candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'qualified'})
                record_path = root / 'state/promotion.json'
                real_exchange = learning_promotion._atomic_exchange

                def edit_at_exchange(first, second):
                    if (first / 'SKILL.md').read_bytes() == b'original':
                        (first / 'SKILL.md').write_bytes(b'last-moment edit')
                        real_exchange(first, second)
                        if interrupted:
                            raise InterruptedError('worker stopped after directory exchange')
                    else:
                        real_exchange(first, second)

                with patch.object(learning_promotion, '_atomic_exchange', side_effect=edit_at_exchange):
                    failure = InterruptedError if interrupted else ValueError
                    with self.assertRaises(failure):
                        promote(source, candidate, _tree_hash(source), root / 'state', record_path=record_path)
                if interrupted:
                    with self.assertRaisesRegex(ValueError, 'concurrent edit was restored'):
                        recover_exchange(source, json.loads(record_path.read_text()), root / 'state')
                self.assertEqual((source / 'SKILL.md').read_bytes(), b'last-moment edit')

    def test_mode_only_edit_since_qualification_prevents_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.make_tree(root / 'source', {'SKILL.md': b'original', 'runner.py': b'print(1)'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'qualified', 'runner.py': b'print(1)'})
            before, modes = _tree_hash(source), tree_modes(source)
            (source / 'runner.py').chmod(0o755)
            self.assertEqual(_tree_hash(source), before)
            with self.assertRaisesRegex(ValueError, 'source_mode_mismatch'):
                promote(source, candidate, before, root / 'state', expected_source_modes=modes)
            self.assertEqual(stat.S_IMODE((source / 'runner.py').stat().st_mode), 0o755)

    def test_post_exchange_sync_failure_keeps_a_verified_rollback_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.make_tree(root / 'source', {'SKILL.md': b'original'})
            candidate = self.make_tree(root / 'candidate', {'SKILL.md': b'qualified'})
            original_sync = os.fsync

            def fail_after_exchange(fd):
                if (source / 'SKILL.md').read_bytes() == b'qualified':
                    raise OSError('injected directory sync failure')
                return original_sync(fd)

            before = _tree_hash(source)
            record_path = root / 'state/promotion.json'
            with patch.object(learning_promotion.os, 'fsync', side_effect=fail_after_exchange):
                record = promote(source, candidate, before, root / 'state', record_path=record_path)
            self.assertEqual(record['durability'], 'unknown')
            self.assertEqual(_tree_hash(source), _tree_hash(candidate))
            self.assertTrue(recover_exchange(source, json.loads(record_path.read_text()), root / 'state'))
            rollback(source, record, root / 'state')
            self.assertEqual(_tree_hash(source), before)


if __name__ == '__main__':
    unittest.main()
