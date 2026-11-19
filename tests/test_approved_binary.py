from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json

from scripts import approved_binary


class ApprovedBinaryTests(unittest.TestCase):
    def native(self, path, contents=b'\xcf\xfa\xed\xfe' + b'harmless stub'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        path.chmod(0o755)
        return path

    def test_standalone_snapshot_uses_approved_bytes_after_source_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.native(root / 'codex')
            approved = approved_binary.describe(source)
            launch = approved_binary.stage(approved, root / 'private' / 'runner')
            source.write_bytes(b'changed after approval')
            self.assertEqual(launch.read_bytes(), b'\xcf\xfa\xed\xfeharmless stub')
            self.assertEqual(approved_binary._regular_info(launch)[0], approved['sha256'])

    def test_changed_source_is_rejected_during_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.native(root / 'codex')
            approved = approved_binary.describe(source)
            source.write_bytes(b'new contents')
            with self.assertRaisesRegex(ValueError, 'differs from owner approval'):
                approved_binary.stage(approved, root / 'private' / 'runner')

    def test_app_snapshot_preserves_bundle_layout_and_sidecars(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'CodexCLI.app'
            executable = self.native(app / 'Contents/MacOS/codex')
            (app / 'Contents/Resources').mkdir(parents=True)
            (app / 'Contents/Resources/runtime.dat').write_bytes(b'approved sidecar')
            approved = approved_binary.describe(executable)
            launch = approved_binary.stage(approved, root / 'private' / 'runner')
            self.assertEqual(launch.read_bytes(), executable.read_bytes())
            self.assertEqual((launch.parents[1] / 'Resources/runtime.dat').read_bytes(), b'approved sidecar')
            snapshot_bundle = launch.parents[2]
            self.assertEqual(approved_binary._bundle_digest(snapshot_bundle), approved['bundle_sha256'])
            (launch.parents[1] / 'Resources/runtime.dat').write_bytes(b'changed')
            self.assertNotEqual(approved_binary._bundle_digest(snapshot_bundle), approved['bundle_sha256'])

    def test_official_chatgpt_launcher_snapshot_keeps_relative_bundle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / 'codex-cli'
            launcher = cli / 'bin/codex'
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(approved_binary.CHATGPT_CODEX_WRAPPER)
            launcher.chmod(0o755)
            app = cli / 'CodexCLI.app'
            self.native(app / 'Contents/MacOS/codex')
            self.native(cli / 'bin/codex-exec-server')
            self.native(cli / 'bin/codex-code-mode-host')
            (cli / 'codex-resources').mkdir()
            self.native(cli / 'codex-resources/codex-code-mode-host')
            (cli / 'codex-path').mkdir()
            self.native(cli / 'codex-path/rg')
            (cli / 'codex-package.json').write_text(json.dumps({
                'layoutVersion': 1, 'entrypoint': 'bin/codex', 'execServer': 'bin/codex-exec-server',
                'resourcesDir': 'codex-resources', 'pathDir': 'codex-path',
            }))
            approved = approved_binary.describe(launcher)
            staged = approved_binary.stage(approved, root / 'private' / 'runner')
            self.assertTrue(staged.is_file())
            self.assertEqual(staged.name, 'codex')
            self.assertTrue((staged.parents[2].parent / 'bin/codex').is_file())
            self.assertTrue((staged.parents[2].parent / 'bin/codex-exec-server').is_file())
            self.assertTrue((staged.parents[2].parent / 'bin/codex-code-mode-host').is_file())
            self.assertTrue((staged.parents[2].parent / 'codex-resources/codex-code-mode-host').is_file())
            self.assertTrue((staged.parents[2].parent / 'codex-path/rg').is_file())
            snapshot_bundle = staged.parents[2]
            package_root = snapshot_bundle.parent
            self.assertEqual(approved_binary._bundle_digest(package_root), approved['package_sha256'])
            launcher.write_bytes(b'#!/bin/sh\nexit 0\n')
            with self.assertRaisesRegex(ValueError, 'not a supported native executable'):
                approved_binary.describe(launcher)
            native = app / 'Contents/MacOS/codex'
            direct = approved_binary.describe(native)
            self.assertEqual(direct['launch_path'], native.relative_to(cli).as_posix())

    def test_package_sidecar_change_invalidates_owner_approval(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'codex-package'
            (root / 'bin').mkdir(parents=True)
            binary = self.native(root / 'bin/codex')
            self.native(root / 'bin/codex-exec-server')
            (root / 'codex-resources').mkdir()
            (root / 'codex-path').mkdir()
            (root / 'codex-package.json').write_text(json.dumps({
                'layoutVersion': 1, 'entrypoint': 'bin/codex', 'execServer': 'bin/codex-exec-server',
                'resourcesDir': 'codex-resources', 'pathDir': 'codex-path',
            }))
            approved = approved_binary.describe(binary)
            sidecar = root / 'bin/codex-exec-server'
            sidecar.write_bytes(b'changed after owner approval')
            with self.assertRaisesRegex(ValueError, 'differs from owner approval'):
                approved_binary.stage(approved, root.parent / 'private' / 'runner')

    def test_native_manifest_entrypoint_preserves_declared_runtime_layout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / '0.999.0'
            binary = self.native(root / 'bin/codex')
            standalone = approved_binary.describe(binary)
            self.native(root / 'bin/codex-exec-server')
            (root / 'codex-resources').mkdir(parents=True)
            (root / 'codex-path').mkdir()
            (root / 'codex-package.json').write_text(json.dumps({
                'layoutVersion': 1, 'entrypoint': 'bin/codex', 'execServer': 'bin/codex-exec-server',
                'resourcesDir': 'codex-resources', 'pathDir': 'codex-path',
            }))
            with self.assertRaisesRegex(ValueError, 're-enable learning'):
                approved_binary.stage(standalone, root.parent / 'private' / 'old')
            approved = approved_binary.describe(binary)
            staged = approved_binary.stage(approved, root.parent / 'private' / 'runner')
            self.assertEqual(staged.relative_to(staged.parents[1]).as_posix(), 'bin/codex')
            self.assertTrue((staged.parent / 'codex-exec-server').is_file())
            self.assertEqual(approved['package_root'], str(root.resolve()))

    def test_symlink_replacement_does_not_change_launched_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.native(root / 'first')
            second = self.native(root / 'second', b'\xcf\xfa\xed\xfeother')
            link = root / 'codex'
            link.symlink_to(first)
            approved = approved_binary.describe(link)
            link.unlink()
            link.symlink_to(second)
            with self.assertRaisesRegex(ValueError, 'changed'):
                approved_binary.stage(approved, root / 'private' / 'runner')


if __name__ == '__main__':
    unittest.main()
