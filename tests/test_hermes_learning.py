import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from venv import EnvBuilder

from plugins import skillcraft
from scripts import learning
from scripts.utils import tree_hash, write_json


class HermesLearningTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        base = Path(directory).resolve()
        self.home = base / 'profile'
        self.project = base / 'project'
        self.source = self.project / '.agents/skills/demo'
        self.source.mkdir(parents=True)
        self.markdown = self.source / 'SKILL.md'
        self.content = '---\nname: demo\ndescription: A demo skill.\n---\n\nRead the notes.\n'
        self.markdown.write_text(self.content)
        write_json(self.project / learning.CONFIG, {
            'schema_version': 1, 'enabled': True, 'sources': ['sessions'],
            'skills': {'demo': {'source': '.agents/skills/demo'}},
        })
        self.enterContext(patch.dict(os.environ, {learning.INTERNAL: '0'}))
        self.context = Mock()
        self.context.session_surface.return_value = 'cli'
        self.context.session_cwd.return_value = str(self.project)
        constants = SimpleNamespace(get_hermes_home=lambda: self.home)
        with patch.dict('sys.modules', hermes_constants=constants):
            skillcraft.register(self.context)
        self.hooks = dict(call.args for call in self.context.register_hook.call_args_list)
        self.constants = constants

    def command(self, name):
        _, _, setup, run = self.context.register_cli_command.call_args.args
        parser = argparse.ArgumentParser()
        setup(parser)
        output = StringIO()
        with redirect_stdout(output), patch.dict('sys.modules', hermes_constants=self.constants):
            run(parser.parse_args([name, str(self.project)]))
        return json.loads(output.getvalue())

    def emit(self, name, turn, **extra):
        self.hooks[name](session_id='same-session', turn_id=turn, **extra)

    def start(self, turn, text='Apply demo to these notes.', **extra):
        self.emit('pre_llm_call', turn, user_message=text, **extra)

    def view(self, turn, *, source=None, content=None, file_path=None):
        self.emit('post_tool_call', turn, tool_name='skill_view', status='ok',
                  args={'name': 'demo', 'file_path': file_path},
                  result=json.dumps({'success': True, 'skill_dir': str(source or self.source),
                                     'content': self.content if content is None else content}))

    def finish(self, turn):
        text = 'Minutes recorded.'
        self.emit('post_llm_call', turn, assistant_response=text,
                  conversation_history=[{'role': 'assistant', 'content': text}])

    def test_registered_skill_and_confirmed_success_are_deduplicated(self):
        name, path = self.context.register_skill.call_args.args
        self.assertEqual(name, 'skillcraft')
        self.assertTrue(path.is_file())
        self.assertTrue((path.parent / 'references/learning.md').is_file())
        self.assertEqual(set(self.hooks), {'pre_llm_call', 'post_tool_call', 'post_llm_call'})
        self.command('enable')
        self.start('one')
        self.view('one')
        self.finish('one')
        self.finish('one')
        self.start('two', 'Подтверждаю: всё правильно. [skillcraft-case:minutes]')
        self.start('two', 'Подтверждаю: всё правильно. [skillcraft-case:minutes]')
        self.finish('two')
        self.assertEqual(self.command('status')['messages'], 4)
        cfg = learning.config_for(self.project, hermes_home=self.home)
        with learning.connection(cfg) as db:
            rows = db.execute('SELECT skill,version,kind,harness,origin,case_id FROM evidence').fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ('demo', tree_hash(self.source), 'success', 'hermes', 'session', 'minutes'),
        ])

    def test_missing_consent_and_other_profile_leave_no_journal(self):
        self.start('without-consent')
        self.assertFalse(self.home.exists())
        self.command('enable')
        other = self.home.with_name('other-profile')
        saved_home = self.home
        self.home = other
        self.start('wrong-profile')
        self.assertFalse(other.exists())
        self.home = saved_home
        self.context.session_surface.return_value = 'telegram'
        self.start('remote')
        self.context.session_surface.return_value = 'cli'
        self.start('subagent', parent_session_id='parent')
        self.assertEqual(self.command('status')['messages'], 0)

    def test_journal_child_can_use_hermes_managed_yaml(self):
        # The Hermes launcher can add its managed packages only to the host process.
        bare = self.home.with_name('bare-python')
        EnvBuilder(with_pip=False).create(bare)
        with patch('plugins.skillcraft.hermes_learning.sys.executable', str(bare / 'bin/python')):
            self.command('enable')
            self.assertEqual(self.command('status')['messages'], 0)

    def test_wrong_path_reference_partial_or_changed_version_is_unbound(self):
        self.command('enable')
        other = self.project / 'other/demo'
        other.mkdir(parents=True)
        (other / 'SKILL.md').write_text(self.content)
        for turn, options in {
            'other': {'source': other},
            'reference': {'file_path': 'references/note.md'},
            'partial': {'content': self.content[:25]},
            'changed': {},
        }.items():
            self.start(turn)
            self.view(turn, **options)
            if turn == 'changed':
                (self.source / 'instructions.txt').write_text('New version.')
            self.finish(turn)
            self.start(turn + '-feedback', 'Подтверждаю: всё правильно.')
            self.finish(turn + '-feedback')
        self.assertEqual(self.command('status')['evidence'], {})

    def test_interruption_and_delayed_events_do_not_reuse_an_older_answer(self):
        self.command('enable')
        self.start('finished')
        self.view('finished')
        self.finish('finished')
        self.start('interrupted')
        self.view('interrupted')
        self.start('next', 'Подтверждаю: всё правильно.')
        self.start('finished')
        self.finish('interrupted')
        self.finish('next')
        status = self.command('status')
        self.assertEqual(status['messages'], 5)
        self.assertEqual(status['evidence'], {})

    def test_many_hermes_observations_only_use_the_profile_journal(self):
        self.command('enable')
        cfg = learning.config_for(self.project, hermes_home=self.home)
        versions = learning.source_inventory(cfg)
        for i in range(10):
            for role, text in [('user', 'Подтверждаю: всё правильно.'), ('assistant', 'Minutes recorded.')]:
                learning.observe(self.project, 'hermes', 's', str(i), role, text, versions,
                                 target_turn=str(i - 1), hermes_home=self.home)
        status = self.command('status')
        self.assertEqual(status['messages'], 20)
        self.assertEqual(status['evidence'], {'success': 9})
        self.assertEqual(status['checkpoints'], {})
        self.assertEqual(status['revisions'], [])
        self.assertFalse((cfg['state'] / 'worker.lock').exists())
        other = learning.config_for(self.project, enrolling=True)
        self.assertFalse(other['state'].exists())
