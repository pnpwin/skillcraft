import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from scripts.cli_state import prepare_tools, validate_tools, collect_state


class CLIState(unittest.TestCase):
    def test_server_process_receives_scoped_environment_and_parallel_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            def run(n):
                root = (Path(tmp) / str(n)).resolve()
                spec = prepare_tools(root, [{'id': 'python', 'executable': sys.executable,
                                             'environment': {'EXAMPLE_CACHE': '{state}/cache'}}])
                launcher = spec['tools'][0]['launcher']
                code = "import os,json,pathlib; p=pathlib.Path(os.environ['EXAMPLE_CACHE']); p.mkdir(); (p/'proof').write_text('ok'); print(json.dumps({'cache':str(p)}))"
                result = subprocess.run([launcher, '-c', code], check=True, capture_output=True, text=True)
                cache = Path(json.loads(result.stdout)['cache'])
                self.assertTrue(cache.is_relative_to(root))
                evidence = collect_state(spec, root)
                self.assertEqual(evidence['observations'][0]['status'], 'observed')
                self.assertEqual(evidence['observations'][0]['calls'][0]['environment']['EXAMPLE_CACHE'], str(cache))
                return cache
            with ThreadPoolExecutor(max_workers=2) as pool:
                caches = list(pool.map(run, range(2)))
            self.assertNotEqual(caches[0], caches[1])
            self.assertTrue(all((p / 'proof').read_text() == 'ok' for p in caches))

    def test_no_invocation_is_unverified_and_invalid_profiles_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = {'id': 'python', 'executable': sys.executable, 'environment': {}}
            manifest = prepare_tools(tmp, [tool])
            self.assertEqual(collect_state(manifest, tmp)['observations'][0]['status'], 'unverified')
            for tools in [[tool, tool], [{**tool, 'id': '../outside'}], [{**tool, 'executable': 'python'}],
                          [{**tool, 'environment': {'HOME': '/elsewhere'}}], [{**tool, 'environment': {'API_KEY': 'redacted'}}]]:
                with self.assertRaises(ValueError):
                    validate_tools(tools)
