import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import learning
from scripts.utils import write_json


class LearningScopeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / 'home'
        self.project = self.home / 'workspace'
        self.project.mkdir(parents=True)
        self.enterContext(patch.object(Path, 'home', return_value=self.home))

    def configure(self, root, **settings):
        write_json(root / learning.CONFIG, {'schema_version': 1, 'enabled': True, **settings})

    def enable(self, root, *options):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(learning.main([*options, 'enable', str(root)]), 0)

    def make_skill(self, root, name='demo', directory='.agents/skills'):
        source = root / directory / name
        source.mkdir(parents=True)
        (source / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test.\n---\n\nInstructions.\n')
        return source

    def test_empty_automatic_scope_accepts_new_skills_without_reenrollment(self):
        self.configure(self.project, selection='all')
        self.enable(self.project)
        initial = learning.config_for(self.project)
        self.assertEqual(initial['skills'], {})
        self.make_skill(self.project)
        current = learning.config_for(self.project)
        self.assertEqual(current['fingerprint'], initial['fingerprint'])
        self.assertEqual(set(current['skills']), {'demo'})
        loaded = learning.source_inventory(current)
        self.assertTrue(learning.observe(self.project, 'codex', 'session', 'read', 'user', 'Use demo', []))
        self.assertTrue(learning.observe(self.project, 'codex', 'session', 'read', 'assistant', 'Output', loaded))
        self.assertTrue(learning.observe(self.project, 'codex', 'session', 'feedback', 'user',
                                         'It works.', [], target_turn='read'))
        with learning.connection(current) as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT skill,kind FROM evidence')],
                             [('demo', 'success')])

    def test_exclusion_applies_to_later_skills_and_source_pin_resolves_collisions(self):
        self.configure(self.project, selection='all', exclude=['skip'],
                       skills={'demo': {'source': './.codex/skills/demo'}})
        self.enable(self.project)
        self.make_skill(self.project)
        self.make_skill(self.project, directory='.codex/skills')
        self.make_skill(self.project, 'skip')
        self.make_skill(self.project, 'ambiguous')
        self.make_skill(self.project, 'ambiguous', '.codex/skills')
        self.make_skill(self.project, 'included')
        config = learning.config_for(self.project)
        self.assertEqual(set(config['skills']), {'demo', 'included'})
        self.assertEqual(config['skills']['demo']['source'], '.codex/skills/demo')
        path = self.project / '.codex/skills/demo'
        path.rename(path.with_name('moved'))
        path.symlink_to(self.project / '.agents/skills/demo', target_is_directory=True)
        self.assertEqual(set(learning.config_for(self.project)['skills']), {'included'})

    def test_linked_skill_added_after_enable_records_canonical_version_without_auto_apply(self):
        checkout = self.root / 'shared-checkout'
        skill = self.make_skill(checkout, directory='skills/team')
        (checkout / '.git').mkdir()
        self.configure(self.project, selection='all', linked_skill_roots=[str(skill.parent)],
                       defaults={'auto_apply': True})
        self.enable(self.project)
        config = learning.config_for(self.project)
        self.assertFalse(config['skills'])

        link = self.project / '.hermes/skills/demo'
        link.parent.mkdir(parents=True)
        link.symlink_to(skill, target_is_directory=True)
        second_link = self.project / '.agents/skills/demo'
        second_link.parent.mkdir(parents=True)
        second_link.symlink_to(skill, target_is_directory=True)
        config = learning.config_for(self.project)
        settings = config['skills']['demo']
        self.assertEqual(learning.skill_source(config, settings), skill)
        self.assertIs(settings['auto_apply'], False)
        loaded = learning.source_inventory(config)
        self.assertEqual([entry['path'] for entry in loaded], [str(skill / 'SKILL.md')])
        version = loaded[0]['snapshot_hash']
        loaded[0]['path'] = str(link / 'SKILL.md')
        self.assertTrue(learning.observe(self.project, 'codex', 'session', 'read', 'user', 'Use demo', []))
        self.assertTrue(learning.observe(self.project, 'codex', 'session', 'read', 'assistant', 'Output', loaded))
        self.assertTrue(learning.observe(self.project, 'codex', 'session', 'confirm', 'user',
                                         'I verified it works.', [], target_turn='read'))
        with learning.connection(config) as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT skill, version, kind FROM evidence')],
                             [('demo', version, 'success')])
        self.assertEqual(learning.tree_hash(skill), version)

    def test_link_retarget_requires_approval_for_new_source_even_with_identical_contents(self):
        first = self.make_skill(self.root / 'first')
        second = self.make_skill(self.root / 'second')
        link = self.project / '.agents/skills/demo'
        link.parent.mkdir(parents=True)
        link.symlink_to(first, target_is_directory=True)
        self.configure(self.project, selection='all', linked_skill_roots=[str(first.parent), str(second.parent)],
                       defaults={'train': 'train.json', 'holdout': 'holdout.json'})
        for split in ('train', 'holdout'):
            write_json(self.project / f'{split}.json', {
                'evals': [{'id': split, 'prompt': f'Use demo for {split}', 'expectations': []}],
            })
        self.enable(self.project)
        prior = learning.config_for(self.project)
        approved = learning.require_model_approval(prior, name='demo')
        self.assertTrue(learning.approval_still_current(prior, approved, name='demo'))

        link.unlink()
        link.symlink_to(second, target_is_directory=True)
        self.assertEqual(learning.tree_hash(first), learning.tree_hash(second))
        self.assertEqual(learning.source_inventory(prior), [])
        current = learning.config_for(self.project)
        self.assertEqual(learning.skill_source(current, current['skills']['demo']), second)
        self.assertFalse(learning.approval_still_current(current, approved, name='demo'))
        with self.assertRaisesRegex(ValueError, 'source changed'):
            learning.require_model_approval(current, name='demo')

    def test_home_journal_stays_separate_from_project_and_repository_sessions(self):
        self.configure(self.home, scope='home', selection='all')
        self.make_skill(self.home, 'global-skill')
        self.enable(self.home)
        local_chat = self.home / 'scratch' / 'chat'
        local_chat.mkdir(parents=True)
        home = learning.config_for(local_chat)
        self.assertEqual(home['root'], self.home)
        self.assertEqual(set(home['skills']), {'global-skill'})
        mounted = self.home / 'mounted-workspace'
        mounted.mkdir()
        with patch.object(Path, 'is_mount', lambda path: path == mounted):
            self.assertIsNone(learning.config_for(mounted))
        repo = self.home / 'repo'
        repo.mkdir()
        (repo / '.git').write_text('gitdir: /worktrees/repo\n')
        self.assertIsNone(learning.config_for(repo))
        (self.project / 'AGENTS.md').touch()
        self.assertIsNone(learning.config_for(self.project))
        self.configure(self.project, selection='all')
        self.make_skill(self.project, 'project-skill')
        self.enable(self.project)
        project = learning.config_for(self.project)
        self.assertNotEqual(project['state'], home['state'])
        self.assertEqual(set(project['skills']), {'project-skill'})
        self.configure(self.project, selection='all', enabled=False)
        self.assertIsNone(learning.config_for(self.project))

    def test_hermes_home_auto_import_can_start_empty(self):
        profile = self.root / 'hermes'
        profile.mkdir()
        self.configure(self.home, scope='home', selection='all', sources=['sessions'])
        self.enable(self.home)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(learning.main(['--hermes-home', str(profile), 'enable', str(self.home),
                                            '--auto-import']), 0)
        self.make_skill(self.home, 'later')
        entry = learning.config_for(self.home, hermes_home=profile)
        self.assertTrue(entry['auto_import'])
        loaded = learning.source_inventory(entry, hermes=True)
        self.assertTrue(learning.observe(self.home, 'hermes', 'session', 'read', 'user', 'Use later', [],
                                         hermes_home=profile))
        self.assertTrue(learning.observe(self.home, 'hermes', 'session', 'read', 'assistant', 'Output', loaded,
                                         hermes_home=profile))
        self.assertTrue(learning.observe(self.home, 'hermes', 'session', 'feedback', 'user', 'It works.', [],
                                         target_turn='read', hermes_home=profile))
        self.assertTrue(learning.observe(self.home, 'hermes', 'session', 'feedback', 'assistant', 'Thanks', [],
                                         hermes_home=profile))
        with patch.object(learning, 'wake'):
            stats = learning.import_hermes(self.home, profile, completed_session='session', automatic=True)
        self.assertEqual(stats['imported'], 1)

    def test_defaults_allow_bulk_model_setup_while_pinning_approval_to_each_skill_source(self):
        self.configure(self.project, selection='all', defaults={
            'train': 'evals/{skill}/train.json', 'holdout': 'evals/{skill}/holdout.json',
            'harnesses': {'codex': {'model': 'gpt-test'}},
        })
        first = self.make_skill(self.project, 'first')

        def datasets(name):
            for split in ('train', 'holdout'):
                write_json(self.project / 'evals' / name / f'{split}.json', {
                    'evals': [{'id': split, 'prompt': f'Use {name} for {split}', 'expectations': []}],
                })

        datasets('first')
        self.make_skill(self.project, 'without-data')
        self.enable(self.project)
        self.make_skill(self.project, 'later')
        datasets('later')
        config = learning.config_for(self.project)
        self.assertEqual(set(config['approval']), {'first'})
        self.assertEqual(config['skills']['later']['train'], 'evals/later/train.json')
        old = learning.require_model_approval(config, name='first')
        self.assertTrue(learning.approval_still_current(config, old, name='first'))
        with self.assertRaisesRegex(ValueError, 're-enable'):
            learning.require_model_approval(config, name='later')
        new_location = self.project / '.codex/skills/first'
        new_location.parent.mkdir(parents=True)
        first.rename(new_location)
        moved = learning.config_for(self.project)
        self.assertEqual(moved['skills']['first']['source'], '.codex/skills/first')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            learning.require_model_approval(moved, name='first')
