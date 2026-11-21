import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins import skillcraft
from plugins.skillcraft import hermes_learning


class HermesEnableCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.home = base / 'profile'
        self.project = base / 'project'
        self.project.mkdir()
        self.constants = SimpleNamespace(get_hermes_home=lambda: self.home)
        self.parser = argparse.ArgumentParser()
        skillcraft.cli_arguments(self.parser)

    def invoke(self, argv, *, stdin_tty=False, stderr_tty=False):
        args = self.parser.parse_args(argv)
        streams = (patch.object(sys, 'stdin', SimpleNamespace(isatty=lambda: stdin_tty)),
                   patch.object(sys, 'stderr', SimpleNamespace(isatty=lambda: stderr_tty)))
        for stream in streams:
            stream.start()
            self.addCleanup(stream.stop)
        output = StringIO()
        with redirect_stdout(output), patch.dict('sys.modules', hermes_constants=self.constants):
            skillcraft.journal_command(args)
        return json.loads(output.getvalue())

    def test_enable_forwards_linked_root_approval_options_and_json(self):
        result = {'enabled': str(self.project), 'journal': '/journal'}
        with patch.object(skillcraft, 'run_learning', return_value=result) as run:
            self.assertEqual(self.invoke([
                'enable', str(self.project), '--auto-import', '--preview-linked-roots',
                '--approve-linked-root', '/linked/one', '--approve-linked-root', '/linked/two',
                '--linked-roots-preview-sha256', 'a' * 64,
            ]), result)
        run.assert_called_once_with(
            self.home, 'enable', str(self.project.resolve()), '--auto-import', '--preview-linked-roots',
            '--approve-linked-root', '/linked/one', '--approve-linked-root', '/linked/two',
            '--linked-roots-preview-sha256', 'a' * 64, interactive=False,
        )

    def test_approval_digest_must_match_approval_paths(self):
        with patch.object(skillcraft, 'run_learning') as run:
            with self.assertRaisesRegex(SystemExit, 'required with --approve-linked-root'):
                self.invoke(['enable', str(self.project), '--approve-linked-root', '/linked'])
            with self.assertRaisesRegex(SystemExit, 'requires --approve-linked-root'):
                self.invoke(['enable', str(self.project), '--linked-roots-preview-sha256', 'a' * 64])
        run.assert_not_called()

    def test_only_unqualified_interactive_enable_uses_prompt_terminal(self):
        with patch.object(skillcraft, 'run_learning', return_value={}) as run:
            self.invoke(['enable', str(self.project)], stdin_tty=True, stderr_tty=True)
        self.assertTrue(run.call_args.kwargs['interactive'])
        with patch.object(skillcraft, 'run_learning', return_value={}) as run:
            self.invoke(['enable', str(self.project)], stdin_tty=True, stderr_tty=False)
        self.assertFalse(run.call_args.kwargs['interactive'])
        with patch.object(skillcraft, 'run_learning', return_value={}) as run:
            self.invoke(['status', str(self.project)], stdin_tty=True, stderr_tty=True)
        self.assertFalse(run.call_args.kwargs['interactive'])

    def test_interactive_subprocess_inherits_stdin_and_stderr_without_timeout(self):
        completed = subprocess.CompletedProcess([], 0, stdout='{}')
        with patch.object(hermes_learning.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(hermes_learning.run_learning(self.home, 'enable', str(self.project),
                                                          interactive=True), {})
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs['stdout'], subprocess.PIPE)
        self.assertNotIn('stdin', kwargs)
        self.assertNotIn('stderr', kwargs)
        self.assertNotIn('timeout', kwargs)

    def test_noninteractive_subprocess_keeps_json_capture_and_timeout(self):
        completed = subprocess.CompletedProcess([], 0, stdout='{}', stderr='')
        with patch.object(hermes_learning.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(hermes_learning.run_learning(self.home, 'enable', str(self.project)), {})
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs['capture_output'])
        self.assertEqual(kwargs['timeout'], 15)
        self.assertEqual(kwargs['input'], '')


if __name__ == '__main__':
    unittest.main()
