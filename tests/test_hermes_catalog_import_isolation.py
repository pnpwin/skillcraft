import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
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
            response = {
                "matches": [str(target)],
                "listed": 1,
                "success": True,
                "directory": str(target),
                "content_hash": content_hash,
            }
            completed = type("Completed", (), {"returncode": 0, "stdout": json.dumps(response)})()
            hermes_runner = ModuleType("scripts.hermes_runner")
            hermes_runner.binary = lambda: "/trusted/hermes/bin/hermes"
            with (patch.dict(os.environ, {
                    "SKILLCRAFT_HERMES_PYTHON": "/trusted/hermes/bin/python",
                    "SKILLCRAFT_HERMES_HOME": str(Path(temporary) / "profile"),
                    "PYTHONPATH": str(root),
                }),
                  patch.dict(sys.modules, {"scripts.hermes_runner": hermes_runner}),
                  patch("subprocess.run", return_value=completed) as run):
                result = learning_candidates._probe_hermes(root, target)

            command = run.call_args.args[0]
            self.assertEqual(command[:3], ["/trusted/hermes/bin/python", "-I", "-B"])
            self.assertEqual(run.call_args.kwargs["cwd"], Path("/"))
            environment = run.call_args.kwargs["env"]
            self.assertNotIn("PYTHONPATH", environment)
            self.assertEqual(environment["TERMINAL_CWD"], str(root))
            self.assertEqual(environment["HERMES_HOME"], str((Path(temporary) / "profile").resolve()))
            self.assertEqual(result["enabled"], True)


if __name__ == "__main__":
    unittest.main()
