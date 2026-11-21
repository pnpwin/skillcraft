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

import yaml

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

    def command(self, name, *options):
        _, _, setup, run = self.context.register_cli_command.call_args.args
        parser = argparse.ArgumentParser()
        setup(parser)
        output = StringIO()
        with redirect_stdout(output), patch.dict('sys.modules', hermes_constants=self.constants):
            run(parser.parse_args([name, str(self.project), *options]))
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
        reply = {'role': 'assistant', 'content': text, 'message_uid': f'reply-{turn}'}
        self.emit('post_llm_call', turn, assistant_response=text,
                  conversation_history=[reply])
        return reply

    def feedback(self, turn, prior):
        text = 'I verified it works.'
        self.start(turn, text, conversation_history=[prior, {'role': 'user', 'content': text}])

    def test_registered_skill_and_confirmed_success_are_deduplicated(self):
        name, path = self.context.register_skill.call_args.args
        self.assertEqual(name, 'skillcraft')
        self.assertTrue(path.is_file())
        self.assertTrue((path.parent / 'references/learning.md').is_file())
        self.assertEqual(set(self.hooks), {'pre_llm_call', 'post_tool_call', 'post_api_request', 'post_llm_call'})
        manifest = yaml.safe_load(Path(skillcraft.__file__).with_name('plugin.yaml').read_text())
        self.assertCountEqual(manifest['provides_hooks'], self.hooks)
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
        with patch('plugins.skillcraft.src.hermes.hermes_learning.sys.executable', str(bare / 'bin/python')):
            self.command('enable')
            self.assertEqual(self.command('status')['messages'], 0)

    def test_hermes_normalized_skill_read_preserves_raw_version(self):
        self.markdown.write_bytes(b'\xef\xbb\xbf' + self.content.replace('\n', '\r\n').encode())
        self.command('enable')
        self.start('one')
        self.view('one')
        self.finish('one')
        self.start('two', 'Подтверждаю: всё правильно.')
        self.assertEqual(self.command('status')['evidence'], {})
        self.finish('two')
        cfg = learning.config_for(self.project, hermes_home=self.home)
        with learning.connection(cfg) as db:
            row = db.execute('SELECT kind,version FROM evidence').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(tuple(row), ('success', tree_hash(self.source)))

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
        self.start('0')
        self.view('0')
        reply = self.finish('0')
        for i in range(1, 10):
            self.feedback(str(i), reply)
            self.view(str(i))
            reply = self.finish(str(i))
        status = self.command('status')
        self.assertEqual(status['messages'], 20)
        self.assertEqual(status['evidence'], {'success': 9})
        self.assertEqual(status['checkpoints'], {})
        self.assertEqual(status['revisions'], [])
        self.assertFalse((cfg['state'] / 'worker.lock').exists())
        other = learning.config_for(self.project, enrolling=True)
        self.assertFalse(other['state'].exists())

    def test_restart_recovers_verified_reply_across_project_subdirectories(self):
        self.command('enable')
        self.start('one')
        self.view('one')
        reply = self.finish('one')
        self.hooks['pre_llm_call'].__self__.sessions.clear()
        self.context.session_cwd.return_value = str(self.source.parent)
        self.feedback('two', reply)
        self.feedback('two', reply)
        self.assertEqual(self.command('status')['evidence'], {})
        self.finish('two')
        self.assertEqual(self.command('status')['evidence'], {'success': 1})

    def test_restart_cannot_skip_interrupted_turn_or_guess_missing_native_id(self):
        self.command('enable')
        self.start('one')
        self.view('one')
        reply = self.finish('one')
        self.hooks['pre_llm_call'].__self__.sessions.clear()
        self.feedback('two', {'role': 'assistant', 'content': reply['content']})
        self.hooks['pre_llm_call'].__self__.sessions.clear()
        self.feedback('three', reply)
        self.assertEqual(self.command('status')['evidence'], {})

    def test_hooks_sync_only_completed_turns_and_count_each_model_step_once(self):
        owner = self.home.with_name('skillcraft-owner')
        with patch.dict(os.environ, HOME=str(owner)), redirect_stdout(StringIO()):
            self.assertEqual(learning.main(['enable', str(self.project)]), 0)
            self.command('enable', '--auto-import')
            self.start('first')
            self.view('first')
            for step in range(1, 15):
                self.emit('post_api_request', 'first', api_call_count=step)
                self.emit('post_api_request', 'first', api_call_count=step)
            reply = self.finish('first')

            self.feedback('interrupted', reply)
            self.emit('post_api_request', 'interrupted', api_call_count=1)
            target = learning.config_for(self.project)
            with learning.connection(target) as db:
                self.assertEqual(db.execute('SELECT SUM(steps) FROM model_steps').fetchone()[0], 14)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 0)

            self.start('new-task')
            self.finish('interrupted')
            self.view('new-task')
            reply = self.finish('new-task')
            self.feedback('review', reply)
            self.emit('post_api_request', 'review', api_call_count=1)
            self.finish('review')
            self.finish('review')
            with learning.connection(target) as db:
                self.assertEqual(db.execute('SELECT SUM(steps) FROM model_steps').fetchone()[0], 15)
                self.assertEqual([r[0] for r in db.execute('SELECT kind FROM evidence')], ['success'])
                self.assertIn('model-steps:15', [r[0] for r in db.execute('SELECT witness FROM checkpoints')])
