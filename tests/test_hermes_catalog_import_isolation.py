import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import learning_candidates


class HermesCatalogImportIsolationTest(unittest.TestCase):
    def test_probe_uses_isolated_python_and_trusted_cwd(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "enrolled-project"
            target = root / ".agents" / "skills" / "demo"
            target.mkdir(parents=True)
            skill_file = target / "SKILL.md"
            skill_file.write_text("---\nname: demo\n---\n\nRead the fixture.\n", encoding="utf-8")
            content_hash = hashlib.sha256(skill_file.read_text(encoding="utf-8-sig").encode()).hexdigest()
            runtime = Path(temporary) / "hermes-install" / "venv" / "bin"
            runtime.mkdir(parents=True)
            interpreter = runtime / "python"
            interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            interpreter.chmod(0o700)
            launcher = runtime / "hermes"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o700)
            response = {
                "matches": [str(target)],
                "listed": 1,
                "success": True,
                "directory": str(target),
                "content_hash": content_hash,
            }
            completed = type("Completed", (), {"returncode": 0, "stdout": json.dumps(response)})()
            with (patch.dict(os.environ, {
                    "SKILLCRAFT_HERMES_BIN": str(launcher),
                    "SKILLCRAFT_HERMES_PYTHON": str(interpreter),
                    "SKILLCRAFT_HERMES_HOME": str(Path(temporary) / "profile"),
                    "PYTHONPATH": str(root),
                }),
                  patch("subprocess.run", return_value=completed) as run):
                result = learning_candidates._probe_hermes(root, target)

            command = run.call_args.args[0]
            self.assertEqual(command[:3], [str(interpreter), "-I", "-B"])
            self.assertEqual(run.call_args.kwargs["cwd"], Path("/"))
            environment = run.call_args.kwargs["env"]
            self.assertNotIn("PYTHONPATH", environment)
            self.assertNotIn("DYLD_INSERT_LIBRARIES", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertEqual(environment["TERMINAL_CWD"], str(root.resolve()))
            self.assertEqual(environment["HERMES_HOME"], str((Path(temporary) / "profile").resolve()))
            self.assertEqual(environment["HOME"], str(Path.home()))
            self.assertEqual(result["enabled"], True)

    def test_probe_rejects_project_local_interpreter_before_execution(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            target = root / ".agents" / "skills" / "demo"
            target.mkdir(parents=True)
            interpreter = root / "python"
            interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            interpreter.chmod(0o700)
            launcher = Path(temporary) / "hermes"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o700)
            with (patch.dict(os.environ, {"SKILLCRAFT_HERMES_BIN": str(launcher),
                                          "SKILLCRAFT_HERMES_PYTHON": str(interpreter)}),
                  patch("subprocess.run") as run):
                with self.assertRaisesRegex(RuntimeError, "hermes_native_catalog_probe_unavailable"):
                    learning_candidates._probe_hermes(root, target)
            run.assert_not_called()

    def test_probe_rejects_project_local_launcher_before_execution(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            target = root / ".agents" / "skills" / "demo"
            target.mkdir(parents=True)
            launcher = root / "hermes"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o700)
            with (patch.dict(os.environ, {"SKILLCRAFT_HERMES_BIN": str(launcher),
                                          "SKILLCRAFT_HERMES_PYTHON": ""}),
                  patch("subprocess.run") as run):
                with self.assertRaisesRegex(RuntimeError, "hermes_native_catalog_probe_unavailable"):
                    learning_candidates._probe_hermes(root, target)
            run.assert_not_called()

    def test_trusted_runtime_rejects_collaborator_writable_parent(self):
        with TemporaryDirectory() as temporary:
            shared = Path(temporary) / "shared"
            shared.mkdir(mode=0o777)
            shared.chmod(0o777)
            interpreter = shared / "python"
            interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            interpreter.chmod(0o700)
            with self.assertRaisesRegex(ValueError, "untrusted parent"):
                learning_candidates._trusted_hermes_executable(interpreter, Path(temporary) / "project")

            protected = Path(temporary) / "private-runtime"
            protected.mkdir(mode=0o700)
            actual_interpreter = protected / "base-python"
            actual_interpreter.write_text("Synthetic executable fixture.\n", encoding="utf-8")
            actual_interpreter.chmod(0o700)
            intermediate = shared / "runtime-link"
            intermediate.symlink_to(actual_interpreter)
            alias = protected / "python"
            alias.symlink_to(intermediate)
            self.assertEqual(alias.resolve(), actual_interpreter.resolve())
            with self.assertRaisesRegex(ValueError, "untrusted parent"):
                learning_candidates._trusted_hermes_executable(alias, Path(temporary) / "project")


if __name__ == "__main__":
    unittest.main()
