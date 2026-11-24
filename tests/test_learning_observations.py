import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import learning
from scripts.learning_observations import SCHEMA as OBSERVATION_SCHEMA, declared_check, derived_cases, import_observations
from scripts.utils import tree_hash, write_json


class SessionObservationsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.project = self.home / 'project'
        self.project.mkdir()
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.enterContext(patch.dict('os.environ', {learning.INTERNAL: '0'}))
        self.wake = self.enterContext(patch.object(learning, 'wake'))

    def enroll(self, root, **settings):
        write_json(root / learning.CONFIG, {
            'schema_version': 1, 'enabled': True, 'selection': 'all',
            'automation': {'create': True}, **settings,
        })
        with redirect_stdout(io.StringIO()):
            self.assertEqual(learning.main(['enable', str(root)]), 0)
        return learning.config_for(root)

    def turn(self, root, session, turn, prompt, *, review=None, events=None, loaded=(), finish=True):
        learning.observe(root, 'codex', session, turn, 'user', prompt, [], target_turn=review)
        if finish:
            learning.observe(root, 'codex', session, turn, 'assistant', 'The output is ready.',
                             list(loaded), tool_events=events)

    def test_unbound_feedback_waits_for_a_completed_review_and_preserves_tool_status_only(self):
        config = self.enroll(self.project)
        self.turn(self.project, 's', 'ask', 'Create actions.csv from the notes. [skillcraft-topic:meeting-actions]',
                  events=[{'id': '1', 'tool': 'terminal', 'status': 'error', 'raw': 'ignored'},
                          {'id': '1', 'tool': 'terminal', 'status': 'error'},
                          {'id': '2', 'tool': 'remote', 'status': 'unavailable'}])
        self.turn(self.project, 's', 'review', 'It works. token=abcd', review='ask', finish=False)
        with learning.connection(config) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 2)
        self.assertTrue(learning.observe(self.project, 'codex', 's', 'review', 'assistant', 'Thanks', [],
                                        tool_events=[{'id': '3', 'tool': 'terminal', 'status': 'ok'}]))
        self.assertFalse(learning.observe(self.project, 'codex', 's', 'review', 'assistant', 'Thanks', []))
        with learning.connection(config) as db:
            feedback = db.execute("SELECT * FROM observations WHERE kind='success'").fetchone()
            self.assertIsNone(feedback['version'])
            self.assertEqual(feedback['topic'], 'meeting-actions')
            self.assertIn('[redacted]', feedback['excerpt'])
            self.assertEqual(json.loads(feedback['details'])['recovered_tools'], ['terminal'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observations WHERE kind='tool'").fetchone()[0], 3)

    def test_opt_in_journal_redacts_github_tokens_and_uri_userinfo(self):
        config = self.enroll(self.project)
        github_token = 'github_pat_' + 'A' * 40
        database_url = 'postgresql://alice:synthetic-password@localhost/db'
        private_values = (github_token, 'alice', 'synthetic-password')
        prompt = f'Create the report. {github_token} {database_url}'
        answer = f'Report saved. {github_token} {database_url}'

        learning.observe(self.project, 'codex', 'private', 'ask', 'user', prompt, [])
        learning.observe(self.project, 'codex', 'private', 'ask', 'assistant', answer, [])
        learning.observe(self.project, 'codex', 'private', 'review', 'user',
                         f'I verified it works. {github_token} {database_url}', [], target_turn='ask')
        learning.observe(self.project, 'codex', 'private', 'review', 'assistant', 'Thanks.', [])

        with learning.connection(config) as db:
            persisted = '\n'.join(str(value) for row in db.execute(
                'SELECT excerpt FROM messages UNION ALL '
                'SELECT prompt FROM observed_turns UNION ALL '
                'SELECT answer FROM observed_turns UNION ALL '
                'SELECT excerpt FROM observations UNION ALL '
                'SELECT details FROM observations'
            ).fetchall() for value in row)
            feedback = db.execute("SELECT details FROM observations WHERE kind='success'").fetchone()
        for secret in private_values:
            self.assertNotIn(secret, persisted)
        self.assertIn('[redacted]', persisted)
        derived = json.loads(feedback['details'])
        self.assertNotIn(github_token, json.dumps(derived))
        self.assertNotIn('synthetic-password', json.dumps(derived))

    def test_bound_teaching_keeps_the_verified_version(self):
        source = self.project / '.agents/skills/demo'
        source.mkdir(parents=True)
        (source / 'SKILL.md').write_text('---\nname: demo\ndescription: A demo skill.\n---\nHello\n')
        config = self.enroll(self.project, automation={'improve': True})
        self.turn(self.project, 's', 'ask', 'Run the demo.', loaded=learning.source_inventory(config))
        self.turn(self.project, 's', 'review', 'Как надо делать: сохраняй неизвестного владельца.', review='ask')
        with learning.connection(config) as db:
            observation = db.execute("SELECT skill,version,kind FROM observations").fetchone()
            evidence = db.execute("SELECT skill,version,kind FROM evidence").fetchone()
            self.assertEqual(tuple(observation), tuple(evidence))
            self.assertEqual((observation['skill'], observation['kind']), ('demo', 'correction'))

    def test_remember_and_save_rule_are_opt_in_reflection_signals(self):
        config = self.enroll(self.project, automation={'create': True, 'reflect': True})
        examples = (
            'Запомни: сохраняй порядок строк.',
            'Сохрани это как правило: оставляй повторы.',
            'Закрепи на будущее: пропускай пустые строки.',
            'Remember: keep the input order.',
            'Save this as a rule: keep duplicates.',
            'Сохрани отчёт в outputs/report.md.',
            'Save this to outputs/report.md.',
        )
        for index, text in enumerate(examples):
            session = str(index)
            self.turn(self.project, session, 'ask', 'Clean the rows.')
            self.turn(self.project, session, 'review', text, review='ask')
        with learning.connection(config) as db:
            signals = db.execute("SELECT excerpt FROM observations WHERE kind='correction' ORDER BY id").fetchall()
            self.assertEqual([row['excerpt'] for row in signals], list(examples[:5]))

        config = self.enroll(self.project, automation={'create': True})
        self.turn(self.project, 'off', 'ask', 'Clean the rows.')
        self.turn(self.project, 'off', 'review', examples[0], review='ask')
        with learning.connection(config) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM observations WHERE session='off'").fetchone())

    def test_fixture_checks_only_come_from_explicit_safe_user_literals(self):
        config = self.enroll(self.project)
        for index, prompt in enumerate(('Write actions.csv from Ann agrees to call.',
                                        'Write actions.csv from Lee agrees to check the schedule.')):
            session = f's-{index}'
            self.turn(self.project, session, 'ask', prompt)
            self.turn(self.project, session, 'review',
                      'I verified it works.\n[skillcraft-check:{"file":"actions.csv","contains":"Owner,Due"}]',
                      review='ask')
        with learning.connection(config) as db:
            train, holdout = derived_cases(db.execute('SELECT * FROM observations ORDER BY id').fetchall())
        self.assertEqual((len(train), len(holdout)), (1, 1))
        self.assertNotEqual(train[0]['group'], holdout[0]['group'])
        self.assertEqual(holdout[0]['checks'][0]['value'], 'Owner,Due')
        self.assertIsNone(declared_check('[skillcraft-check:{"file":"../actions.csv","contains":"Owner"}]'))

    def test_manual_verify_returns_report_and_only_explicit_apply_publishes(self):
        draft = self.home / 'drafts' / 'meeting-actions'
        draft.mkdir(parents=True)
        (draft / 'SKILL.md').write_text(
            '---\nname: meeting-actions\ndescription: Turn meeting notes into an action list.\n---\nDraft instructions.\n')
        target = self.project / '.agents/skills/meeting-actions'

        def action(*words):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(learning.main(list(words)), 0)
            return json.loads(out.getvalue())

        candidate = action('stage', str(self.project), str(draft))
        with patch('scripts.learning_candidates.verify_candidate',
                   return_value={'candidate': candidate, 'trusted_pass': False, 'reason': 'test'}) as verify:
            result = action('verify-now', str(self.project), candidate['id'], '--evals', str(self.home / 'eval.json'),
                            '--harness', 'codex', '--model', 'demo')
        verify.assert_called_once()
        self.assertFalse(result['trusted_pass'])
        self.assertFalse(target.exists())
        self.assertEqual(action('apply', str(self.project), candidate['id'])['status'], 'applied_manual_unverified')
        self.assertTrue((target / 'SKILL.md').is_file())
        action('rollback', str(self.project), '--candidate', candidate['id'])
        self.assertFalse(target.exists())

    def test_home_and_project_observations_never_mix(self):
        home_config = self.enroll(self.home, scope='home')
        project_config = self.enroll(self.project)
        self.turn(self.project, 's', '1', 'Create a report.', events=[{'id': 'x', 'tool': 'shell', 'status': 'timeout'}])
        with learning.connection(home_config) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
        with learning.connection(project_config) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)

    def test_hermes_observation_import_passes_sqlite_parameter_sequences(self):
        source = sqlite3.connect(':memory:')
        self.enterContext(_Close(source))
        source.row_factory = sqlite3.Row
        source.executescript(OBSERVATION_SCHEMA)
        source.execute('INSERT INTO observed_turns VALUES (?,?,?,?,?)',
                       ('hermes', 'session-1', 'turn-1', 'Prompt', 'Answer'))
        source.execute('INSERT INTO observations(witness,harness,session,turn,kind,topic,independent,excerpt,details) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       ('w', 'hermes', 'session-1', 'turn-1', 'tool', 'topic', 'family', '', '{}'))
        target = sqlite3.connect(':memory:')
        self.enterContext(_Close(target))
        target.row_factory = sqlite3.Row
        target.executescript(OBSERVATION_SCHEMA)
        self.assertEqual(import_observations(target, source, None), 1)
        self.assertEqual(import_observations(target, source, None), 0)
        self.assertEqual(target.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)

    def test_hermes_artifact_proof_import_requires_current_bound_skill_and_deduplicates(self):
        skill = self.project / '.agents/skills/demo'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: demo\ndescription: A demo skill.\n---\nCurrent.\n')
        config = self.enroll(self.project)
        config['automation_inputs'] = {'cases': {'demo-topic': {'train': {'sha256': 'dataset-hash'}}}}
        version = tree_hash(skill)
        source = sqlite3.connect(':memory:')
        self.enterContext(_Close(source))
        source.row_factory = sqlite3.Row
        source.executescript(OBSERVATION_SCHEMA)
        source.execute('INSERT INTO observed_turns VALUES (?,?,?,?,?)',
                       ('hermes', 'session-1', 'turn-1', 'Prompt', 'Answer'))
        details = json.dumps({'proof': {'provenance': 'native',
                                        'config_sha256': config['fingerprint'],
                                        'dataset_sha256': 'dataset-hash',
                                        'case_id': 'case-a',
                                        'skill': 'demo', 'version': version}})
        source.execute('INSERT INTO observations(witness,harness,session,turn,kind,topic,skill,version,'
                       'independent,excerpt,case_id,details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('proof-1', 'hermes', 'session-1', 'turn-1', 'success', 'demo-topic',
                        'demo', version, 'artifact:family', 'ok', 'case-a', details))

        target = sqlite3.connect(':memory:')
        self.enterContext(_Close(target))
        target.row_factory = sqlite3.Row
        target.executescript(learning.SCHEMA)
        target.executescript(OBSERVATION_SCHEMA)
        self.assertEqual(import_observations(target, source, None, config), 1)
        self.assertEqual(import_observations(target, source, None, config), 0)
        self.assertEqual(target.execute("SELECT COUNT(*) FROM evidence WHERE origin='tool_check'").fetchone()[0], 1)

        stale_target = sqlite3.connect(':memory:')
        self.enterContext(_Close(stale_target))
        stale_target.row_factory = sqlite3.Row
        stale_target.executescript(learning.SCHEMA)
        stale_target.executescript(OBSERVATION_SCHEMA)
        (skill / 'SKILL.md').write_text('---\nname: demo\ndescription: A demo skill.\n---\nChanged.\n')
        self.assertEqual(import_observations(stale_target, source, None, config), 0)
        self.assertEqual(stale_target.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
        self.assertEqual(stale_target.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 0)


class _Close:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_):
        self.value.close()
