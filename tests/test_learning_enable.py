import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins import skillcraft
from scripts import learning
from scripts.utils import write_json


class Terminal(io.StringIO):
    def isatty(self):
        return True


class LearningEnableTests(unittest.TestCase):
    def setUp(self):
        parent = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.home = parent / 'home'
        self.root = self.home / 'workspace'
        self.first = parent / 'first'
        self.second = parent / 'second'
        self.root.mkdir(parents=True)
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.config = self.root / learning.CONFIG
        write_json(self.config, {'schema_version': 1, 'enabled': True, 'selection': 'all',
                                 'defaults': {'auto_apply': True}})
        self.alpha = self.link('alpha', self.first)
        self.link('beta', self.second)

    def make_skill(self, parent, name):
        skill = parent / name
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Example.\n---\n\nInstructions.\n')
        return skill

    def link(self, name, parent):
        alias = self.root / '.agents/skills' / name
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(self.make_skill(parent, name), target_is_directory=True)
        return alias

    def command(self, *flags, home=None, terminal=None, expected=0):
        output = io.StringIO()
        error = Terminal() if terminal is not None else io.StringIO()
        profile = ['--hermes-home', str(home)] if home else []
        with redirect_stdout(output), redirect_stderr(error), patch('sys.stdin', Terminal(terminal or '')):
            code = learning.main([*profile, 'enable', str(self.root), *flags])
        self.assertEqual(code, expected, error.getvalue())
        return json.loads(output.getvalue()) if code == 0 else error.getvalue()

    def approve(self, preview, *roots, **kwargs):
        options = [arg for root in roots for arg in ('--approve-linked-root', str(root))]
        return self.command(*options, '--linked-roots-preview-sha256', preview['preview_sha256'], **kwargs)

    def test_preview_is_read_only_and_approval_enrolls_only_requested_root(self):
        original = self.config.read_bytes()
        preview = self.command('--preview-linked-roots')
        self.assertEqual([row['root'] for row in preview['candidates']], [str(self.first), str(self.second)])
        self.assertEqual(preview['candidates'][0]['links'], [
            {'source': '.agents/skills/alpha', 'target': str(self.first / 'alpha')},
        ])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(learning.learning_state(self.root).exists())
        self.approve(preview, self.first)
        config = learning.config_for(self.root)
        self.assertEqual(config['linked_skill_roots'], (self.first,))
        self.assertEqual(set(config['skills']), {'alpha'})
        self.assertIs(config['skills']['alpha']['auto_apply'], False)
        self.link('later', self.first)
        self.assertEqual(set(learning.config_for(self.root)['skills']), {'alpha', 'later'})
        self.assertEqual([row['root'] for row in self.command('--preview-linked-roots')['candidates']],
                         [str(self.second)])

    def test_interactive_skip_select_and_invalid_selection(self):
        original = self.config.read_bytes()
        self.command(terminal='\n')
        marker = learning.learning_state(self.root) / 'enabled'
        approved = marker.read_bytes()
        self.assertEqual(self.config.read_bytes(), original)
        self.command(terminal='3\n', expected=1)
        self.command(terminal='', expected=1)
        self.assertEqual(marker.read_bytes(), approved)
        self.assertEqual(self.config.read_bytes(), original)
        self.command(terminal='2\n')
        self.assertEqual(learning.config_for(self.root)['linked_skill_roots'], (self.second,))

    def test_noninteractive_enable_without_flags_preserves_config(self):
        original = self.config.read_bytes()
        self.command()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(learning.config_for(self.root)['skills'], {})

    def test_stale_preview_requires_new_confirmation_even_if_root_remains(self):
        self.link('same-root', self.first)
        preview = self.command('--preview-linked-roots')
        original = self.config.read_bytes()
        self.alpha.unlink()
        self.alpha.symlink_to(self.make_skill(self.second, 'alpha'), target_is_directory=True)
        message = self.approve(preview, self.first, expected=1)
        self.assertIn('preview changed', message)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(learning.learning_state(self.root).exists())

    def test_config_changed_while_prompting_is_preserved(self):
        def choose(_entries):
            self.config.write_bytes(self.config.read_bytes() + b'\n')
            return [str(self.first)]

        original = self.config.read_bytes()
        with patch.object(learning, 'prompt_linked_roots', side_effect=choose):
            self.assertIn('configuration changed', self.command(terminal='1\n', expected=1))
        self.assertEqual(self.config.read_bytes(), original + b'\n')
        self.assertFalse(learning.learning_state(self.root).exists())

    def test_hermes_shared_approval_is_preserved_without_reapproving_inputs(self):
        settings = json.loads(self.config.read_bytes())
        settings['skills'] = {'local': {'train': 'train.json', 'holdout': 'holdout.json'}}
        write_json(self.config, settings)
        self.make_skill(self.root / '.agents/skills', 'local')
        for split in ('train', 'holdout'):
            write_json(self.root / f'{split}.json', {
                'evals': [{'id': split, 'prompt': f'Case {split}', 'expectations': []}],
            })
        self.command()
        approval = learning.config_for(self.root)['approval']
        self.assertIn('local', approval)
        with (self.root / 'train.json').open('a') as file:
            file.write('\n')
        other = self.home / 'other-hermes'
        self.command(home=other)
        profile = self.home / 'hermes'
        preview = self.command('--preview-linked-roots', home=profile)
        self.approve(preview, self.first, home=profile)
        shared = learning.config_for(self.root)
        self.assertEqual(shared['approval'], approval)
        self.assertFalse(learning.approval_still_current(shared, approval, name='local'))
        self.assertEqual(set(shared['skills']), {'alpha', 'local'})
        self.assertIsNone(learning.config_for(self.root, hermes_home=other))
        self.assertFalse(learning.config_for(self.root, hermes_home=profile)['auto_import'])

    def test_hermes_auto_import_preserves_both_enrollments_and_source(self):
        profile = self.home / 'hermes'
        preview = self.command('--preview-linked-roots', home=profile)
        flags = ('--auto-import', '--approve-linked-root', str(self.first),
                 '--linked-roots-preview-sha256', preview['preview_sha256'])
        original = self.config.read_bytes()
        self.assertIn('Enable regular', self.command(*flags, home=profile, expected=1))
        self.assertEqual(self.config.read_bytes(), original)
        self.command()
        self.command(*flags, home=profile)
        config = learning.config_for(self.root, hermes_home=profile)
        self.assertTrue(config['auto_import'])
        before = learning.tree_hash(self.alpha.resolve())
        loaded = learning.source_inventory(config, hermes=True)
        self.assertEqual([x['snapshot_hash'] for x in loaded], [before])
        context = Mock()
        context.session_cwd.return_value = str(self.root)
        context.session_surface.return_value = 'cli'
        constants = SimpleNamespace(get_hermes_home=lambda: profile)
        with patch.dict(os.environ, HOME=str(self.home)), patch.dict('sys.modules', hermes_constants=constants):
            skillcraft.register(context)
            hooks = dict(call.args for call in context.register_hook.call_args_list)
            skill = self.alpha.resolve()
            hooks['pre_llm_call'](session_id='session', turn_id='first', user_message='Use alpha')
            hooks['post_tool_call'](session_id='session', turn_id='first', tool_name='skill_view', status='ok',
                                    args={'name': 'alpha'}, result=json.dumps({
                                        'success': True, 'skill_dir': str(skill),
                                        'content': (skill / 'SKILL.md').read_text(),
                                    }))
            reply = {'role': 'assistant', 'content': 'Output', 'message_uid': 'reply-1'}
            hooks['post_llm_call'](session_id='session', turn_id='first', assistant_response='Output',
                                   conversation_history=[reply])
            text = 'I verified it works.'
            hooks['pre_llm_call'](session_id='session', turn_id='feedback', user_message=text,
                                  conversation_history=[reply, {'role': 'user', 'content': text}])
            hooks['post_llm_call'](session_id='session', turn_id='feedback', assistant_response='Thanks',
                                   conversation_history=[
                                       {'role': 'assistant', 'content': 'Thanks', 'message_uid': 'reply-2'},
                                   ])
        for journal in (learning.config_for(self.root), config):
            with learning.connection(journal) as db:
                self.assertEqual([tuple(r) for r in db.execute('SELECT skill, version, kind FROM evidence')],
                                 [('alpha', before, 'success')])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 0)
        self.assertEqual(learning.tree_hash(self.alpha.resolve()), before)

    def test_failed_profile_write_restores_existing_config_and_markers(self):
        profile = self.home / 'hermes'
        self.command()
        self.command('--auto-import', home=profile)
        shared = learning.learning_state(self.root) / 'enabled'
        enrolled = learning.learning_state(self.root, profile) / 'enabled'
        originals = {path: path.read_bytes() for path in (self.config, shared, enrolled)}
        preview = self.command('--preview-linked-roots', home=profile)
        replace = Path.replace

        def fail_on_profile(path, destination):
            if destination == enrolled:
                raise OSError('Profile write failed')
            return replace(path, destination)

        flags = ('--auto-import', '--approve-linked-root', str(self.first),
                 '--linked-roots-preview-sha256', preview['preview_sha256'])
        with patch.object(Path, 'replace', fail_on_profile):
            self.assertIn('Profile write failed', self.command(*flags, home=profile, expected=1))
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
