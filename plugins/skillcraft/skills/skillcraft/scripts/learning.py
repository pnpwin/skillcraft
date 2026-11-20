#!/usr/bin/env python3
"""Opt-in, local experience collection for SkillCraft."""
import argparse
from contextlib import closing, contextmanager
from hashlib import sha256
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.utils import EXCLUDED, digest, read_json, safe_id, tree_hash

CONFIG = Path('.skillcraft/learning.json')
INTERNAL = 'SKILLCRAFT_LEARNING_INTERNAL'
MESSAGE_INTERVAL = 15
VERDICTS = {'success', 'failure', 'correction'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    harness TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
    role TEXT NOT NULL, bindings TEXT NOT NULL, diagnostic TEXT, case_key TEXT, excerpt TEXT,
    UNIQUE(harness, session, turn, role)
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, witness TEXT NOT NULL UNIQUE,
    skill TEXT NOT NULL, version TEXT NOT NULL, origin TEXT NOT NULL,
    harness TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
    independent TEXT NOT NULL, case_id TEXT, kind TEXT NOT NULL, note TEXT NOT NULL,
    reproducible INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    witness TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, checkpoint INTEGER NOT NULL,
    skill TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
    path TEXT NOT NULL, UNIQUE(checkpoint, skill)
);
"""

# Require a recognizable first-person confirmation, report of failure, or correction.
# Other chat text is left unclassified. The checkout can still accept explicit HTML verdicts.
CORRECTION = re.compile(
    r'^(?:исправь(?:те)?\b|исправление\s*:|поправка\s*:|нужно\s+исправить\b|'
    r'fix\b|correction\s*:|should have\b)', re.I)
FAILURE = re.compile(
    r'^(?:не\s+(?:работает|сработало)\b|ошибка\b|результат\s+неверный\b|'
    r'it\s+(?:fails|does not work|doesn.t work)\b|incorrect\b|failed\b)', re.I)
SUCCESS = re.compile(
    r'^(?:подтверждаю\b|проверил(?:а)?[,:]?\s+(?:всё\s+)?работает\b|'
    r'вс[её]\s+работает\b|сработало(?:[.!\s]|$)|'
    r'(?:i\s+)?(?:confirmed|verified)[,:]?\s+(?:it\s+)?(?:works|works correctly)\b|'
    r'it works(?:[.!\s]|$))', re.I)
CASE_TAG = re.compile(r'\[skillcraft-case:([a-zA-Z0-9_-]+)\]')
SECRET = re.compile(
    r'-----BEGIN [^-\n]+ PRIVATE KEY-----[\s\S]*?(?:-----END [^-\n]+ PRIVATE KEY-----|$)'
    r'|\b(?:sk-[a-zA-Z0-9_-]{8,}|gh[pousr]_[a-zA-Z0-9_]{8,}|AKIA[0-9A-Z]{16})\b'
    r'|\bbearer\s+[^\s,;]+'
    r'''|\b(?:api[_ -]?key|pass(?:word)?|(?:access[_ -]?)?token|secret)["']?\s*[:=]\s*'''
    r'''(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''', re.I)


def sanitize(text):
    return SECRET.sub('[redacted]', text.strip())[:1200]


def prompt_key(text):
    normalized = re.sub(r'\W+', ' ', sanitize(text).casefold()).strip()
    return sha256(normalized.encode()).hexdigest() if normalized else None


def classify(text):
    text = text.strip()
    if not text or '?' in text.splitlines()[0]:
        return None
    text = re.sub(r'^(?:да[,:]?|это|теперь|yes[,]?)\s+', '', text, flags=re.I)
    if re.match(r'^(?:подтверждаю|confirmed)\b.*(?:не работает|неверн|does not work|doesn.t work|incorrect)', text, re.I):
        return 'failure'
    return next((kind for kind, pattern in [('correction', CORRECTION), ('failure', FAILURE), ('success', SUCCESS)]
                 if pattern.match(text)), None)


def local_path(root, value, *, directory=False):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Local paths must be nonempty, project-relative strings')
    parts = PurePosixPath(value)
    if parts.is_absolute() or '..' in parts.parts or '.git' in parts.parts or not parts.parts:
        raise ValueError(f'Path escapes project: {value}')
    path = root
    for part in parts.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f'Symlinks are not allowed: {value}')
    if directory and not path.is_dir():
        raise ValueError(f'Missing skill directory: {value}')
    return path


def config_for(project, *, enrolling=False, trusted_internal=False, hermes_home=None):
    if os.environ.get(INTERNAL) == '1' and not trusted_internal:
        return None
    path = Path(project).resolve()
    for root in (path, *path.parents):
        directory = root / CONFIG.parent
        try:
            dir_fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            continue
        try:
            fd = os.open(CONFIG.name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                         getattr(os, 'O_NONBLOCK', 0), dir_fd=dir_fd)
        except FileNotFoundError:
            os.close(dir_fd)
            continue
        except BaseException:
            os.close(dir_fd)
            raise
        try:
            with os.fdopen(fd, 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError('Learning config must be a regular project file')
                contents = stream.read()
        finally:
            os.close(dir_fd)
        fingerprint = hashlib.sha256(contents).hexdigest()
        raw = json.loads(contents)
        if not isinstance(raw, dict) or raw.get('schema_version') != 1:
            raise ValueError('Expected learning config schema_version 1')
        if raw.get('enabled') is not True:
            return None
        sources = raw.get('sources', ['sessions', 'evals'])
        if not isinstance(sources, list) or any(x not in {'sessions', 'evals'} for x in sources):
            raise ValueError('Learning sources must be sessions and/or evals')
        skills = raw.get('skills')
        if not isinstance(skills, dict) or not skills:
            raise ValueError('Learning requires explicitly selected skills')
        for name, settings in skills.items():
            if name != safe_id(name) or not isinstance(settings, dict):
                raise ValueError('Expected a skill ID and settings object')
            source = local_path(root, settings.get('source'), directory=True)
            if source.name != name or not (source / 'SKILL.md').is_file() or (source / 'SKILL.md').is_symlink():
                raise ValueError(f'Invalid source for {name}')
            if source == Path(__file__).resolve().parents[1]:
                raise ValueError('The running SkillCraft installation cannot be its own update target')
        state_root = (hermes_home / 'skillcraft/learning' if hermes_home is not None
                      else Path.home() / '.local/share/skillcraft/learning')
        state = state_root / sha256(str(root).encode()).hexdigest()
        marker = state / 'enabled'
        approval = None
        if not enrolling:
            try:
                marker_fd = os.open(marker, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                                    getattr(os, 'O_NONBLOCK', 0))
                with os.fdopen(marker_fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        return None
                    marker_raw = stream.read().decode('utf-8')
                try:
                    value = json.loads(marker_raw)
                except (ValueError, TypeError):
                    value = None
            except (OSError, ValueError, TypeError):
                marker_raw, value = '', None
            # Legacy markers remain sufficient for observation, but never authorize model work.
            if isinstance(value, str):
                if value.strip() != fingerprint:
                    return None
            elif isinstance(value, dict) and value.get('config_sha256') == fingerprint:
                approval = value.get('validation')
            else:
                if marker_raw.strip() != fingerprint:
                    return None
        return {'root': root, 'state': state, 'skills': skills, 'sources': sources,
                'fingerprint': fingerprint, 'approval': approval, 'hermes': hermes_home is not None}
    return None


def dataset_snapshot(path):
    from scripts.utils import parse_evals
    contents = path.read_bytes()
    data = json.loads(contents)
    return contents, parse_evals(data)


def approval_inputs(config):
    """Hash approved datasets and in-project fixtures only on explicit enable."""
    from scripts.approved_binary import describe
    from scripts.inputs import input_specs
    root = config['root'].resolve()
    result = {}
    for name, settings in config['skills'].items():
        approved = {'datasets': {}, 'fixtures': []}
        if all(key in settings for key in ('train', 'holdout')):
            source = local_path(root, settings['source'], directory=True)
            for split in ('train', 'holdout'):
                path = local_path(root, settings[split])
                raw, rows = dataset_snapshot(path)
                approved['datasets'][split] = {
                    'path': path.relative_to(root).as_posix(), 'sha256': sha256(raw).hexdigest(),
                }
                for row in rows:
                    _reject_out_of_project_fixtures(row.get('files', []), source, root)
                    for fixture in input_specs(row.get('files', []), source):
                        fixture_path = Path(fixture['source']).resolve(strict=True)
                        approved['fixtures'].append({
                            'split': split, 'case_id': row['id'], 'destination': fixture['destination'],
                            'path': fixture_path.relative_to(root).as_posix(), 'sha256': digest(fixture_path),
                        })
        approved['fixtures'].sort(key=lambda x: (x['split'], x['case_id'], x['destination'], x['path']))
        codex = settings.get('harnesses', {}).get('codex', {})
        if 'binary' in codex:
            approved['binary'] = describe(codex['binary'])
        if approved['datasets'] or 'binary' in approved:
            result[name] = approved
    return result


def _reject_out_of_project_fixtures(items, base, root):
    for item in items:
        raw = item.get('source') if isinstance(item, dict) else item
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw)
        resolved = (path if path.is_absolute() else base / path).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError(f'Fixture source must be inside the approved project: {resolved}')


def approval_record(config):
    return {'config_sha256': config['fingerprint'], 'validation': approval_inputs(config)}


def require_model_approval(config):
    approval = config.get('approval')
    if not isinstance(approval, dict):
        raise ValueError('Model learning requires re-enable with approved train, holdout, and fixtures')
    for name, settings in config['skills'].items():
        stored = approval.get(name)
        needs_datasets = all(key in settings for key in ('train', 'holdout'))
        needs_binary = 'binary' in settings.get('harnesses', {}).get('codex', {})
        if ((needs_datasets and (not isinstance(stored, dict)
                                 or set(stored.get('datasets', {})) != {'train', 'holdout'}))
                or (needs_binary and (not isinstance(stored, dict) or not stored.get('binary')))):
            raise ValueError('Model learning requires re-enable with approved train, holdout, and fixtures')
    return approval


def approval_still_current(config, approval):
    """Check only identities the owner approved; never hash newly referenced fixtures."""
    from scripts.approved_binary import describe
    from scripts.inputs import input_specs
    root = config['root'].resolve()
    for name, settings in config['skills'].items():
        codex = settings.get('harnesses', {}).get('codex', {})
        needs_binary = 'binary' in codex
        needs_datasets = all(key in settings for key in ('train', 'holdout'))
        if not needs_binary and not needs_datasets:
            continue
        expected = approval.get(name)
        if not isinstance(expected, dict):
            return False
        if needs_binary:
            try:
                if describe(codex['binary']) != expected.get('binary'):
                    return False
            except (OSError, TypeError, ValueError):
                return False
        if not all(key in settings for key in ('train', 'holdout')):
            continue
        source = local_path(root, settings['source'], directory=True)
        fixtures = {(item['split'], item['case_id'], item['destination']): item
                    for item in expected.get('fixtures', [])}
        seen = set()
        for split in ('train', 'holdout'):
            path = local_path(root, settings[split])
            dataset = expected.get('datasets', {}).get(split, {})
            raw, cases = dataset_snapshot(path)
            if (dataset.get('path') != path.relative_to(root).as_posix()
                    or sha256(raw).hexdigest() != dataset.get('sha256')):
                return False
            for row in cases:
                try:
                    _reject_out_of_project_fixtures(row.get('files', []), source, root)
                except ValueError:
                    return False
                for spec in input_specs(row.get('files', []), source):
                    fixture_path = Path(spec['source']).resolve(strict=True)
                    key = (split, row['id'], spec['destination'])
                    seen.add(key)
                    approved_fixture = fixtures.get(key)
                    if not fixture_path.is_relative_to(root):
                        return False
                    relative = fixture_path.relative_to(root).as_posix()
                    if not approved_fixture or approved_fixture.get('path') != relative:
                        return False
                    if digest(fixture_path) != approved_fixture.get('sha256'):
                        return False
        if seen != set(fixtures):
            return False
    return True


@contextmanager
def connection(config):
    state = config['state']
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(state / 'journal.sqlite', timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        if config.get('hermes'):
            db.execute("""CREATE TABLE IF NOT EXISTS hermes_replies (
                session TEXT NOT NULL, turn TEXT NOT NULL, uid TEXT NOT NULL,
                PRIMARY KEY (session, turn), UNIQUE (session, uid))""")
        with db:
            yield db


def queue(db, witness):
    return db.execute('INSERT OR IGNORE INTO checkpoints(witness) VALUES (?)', (witness,)).rowcount != 0


def wake(config):
    # Hand the lock to the child before returning, so repeated delivery while a
    # worker is active cannot create a backlog of redundant processes.
    import fcntl
    with (config['state'] / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with (config['state'] / 'worker.log').open('ab') as log:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), 'work', str(config['root']),
                 '--lock-fd', str(lock.fileno())],
                cwd=config['root'], stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),),
            )


def binding(config, loaded):
    bindings = {}
    for name, opts in config['skills'].items():
        source = local_path(config['root'], opts['source'], directory=True)
        markdown = source / 'SKILL.md'
        for entry in loaded:
            if isinstance(entry, dict) and Path(entry.get('path') or '.').resolve() == markdown:
                if entry.get('hash') == digest(markdown):
                    version = tree_hash(source)
                    if entry.get('snapshot_hash') == version:
                        bindings[name] = version
                break
    return bindings


def source_inventory(config, *, hermes=False):
    versions = []
    if 'sessions' not in config['sources']:
        return versions
    for settings in config['skills'].values():
        try:
            source = local_path(config['root'], settings['source'], directory=True)
            markdown = source / 'SKILL.md'
            content = markdown.read_bytes()
            version = {'path': str(markdown), 'hash': sha256(content).hexdigest(),
                       'snapshot_hash': tree_hash(source)}
            if hermes:
                # Hermes decodes SKILL.md with utf-8-sig and universal newlines.
                decoded = content.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
                version['content_hash'] = sha256(decoded.encode()).hexdigest()
            versions.append(version)
        except (OSError, ValueError):
            # A source being edited can still be observed as an unbound turn.
            continue
    return versions


def record(db, witness, skill, version, origin, harness, session, turn, independent, case_id, kind, note,
           reproducible=False):
    return db.execute(
        'INSERT OR IGNORE INTO evidence(witness,skill,version,origin,harness,session,turn,'
        'independent,case_id,kind,note,reproducible) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (witness, skill, version, origin, harness, session, turn, independent, case_id, kind,
         sanitize(note), int(reproducible)),
    ).rowcount != 0


def observe(project, harness, session, turn, role, text, loaded, *, target_turn=None,
            diagnostic=None, hermes_home=None, hermes_uid=None):
    config = config_for(project, hermes_home=hermes_home)
    if not config or 'sessions' not in config['sources']:
        return False
    allowed = {'hermes'} if hermes_home is not None else {'codex', 'opencode'}
    if harness not in allowed or role not in {'user', 'assistant'}:
        raise ValueError('Invalid message origin')
    if not all(isinstance(x, str) and x for x in (session, turn)) or not isinstance(text, str):
        raise ValueError('Messages need durable session and turn identities')
    labels = binding(config, loaded)
    case_key = prompt_key(text) if role == 'user' else None
    excerpt = sanitize(text)[:500] if role == 'assistant' and labels else None
    with connection(config) as db:
        uid = sha256(hermes_uid.encode()).hexdigest() if hermes_home and isinstance(hermes_uid, str) and hermes_uid else None
        if uid and role == 'user':
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute(
                "SELECT turn,role FROM messages WHERE harness='hermes' AND session=? ORDER BY seq DESC LIMIT 1",
                (session,),
            ).fetchone()
            prior = db.execute('SELECT turn FROM hermes_replies WHERE session=? AND uid=?',
                               (session, uid)).fetchone()
            target_turn = (prior['turn'] if latest and prior and latest['role'] == 'assistant'
                           and latest['turn'] == prior['turn'] else None)
        inserted = db.execute(
            'INSERT OR IGNORE INTO messages(harness,session,turn,role,bindings,diagnostic,case_key,excerpt) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (harness, session, turn, role, json.dumps(labels), sanitize(diagnostic) if diagnostic else None,
             case_key, excerpt),
        ).rowcount
        if inserted and uid and role == 'assistant' and labels:
            db.execute('INSERT OR IGNORE INTO hermes_replies(session,turn,uid) VALUES (?,?,?)',
                       (session, turn, uid))
        if inserted and role == 'user' and target_turn:
            kind = classify(text)
            if kind:
                prior = db.execute(
                    'SELECT turn,bindings,excerpt FROM messages WHERE harness=? AND session=? AND role=\'assistant\' '
                    'AND turn=?',
                    (harness, session, target_turn),
                ).fetchone()
                associated = json.loads(prior['bindings']) if prior else {}
                if len(associated) > 1:
                    named = {name: version for name, version in associated.items()
                             if re.search(r'(?<![\w-])' + re.escape(name) + r'(?![\w-])', text, re.I)}
                    if len(named) == 1:
                        associated = named
                if len(associated) == 1:
                    name, version = next(iter(associated.items()))
                    tagged = CASE_TAG.search(text)
                    case_id = tagged.group(1) if tagged else None
                    # A conversation cannot assert that a correction is reproducible.
                    # Durably graded evidence for the same case must already exist.
                    prior_case = db.execute(
                        'SELECT independent,kind,reproducible FROM evidence WHERE skill=? AND version=? AND case_id=? '
                        'ORDER BY (kind=\'failure\' AND reproducible=1) DESC, id LIMIT 1', (name, version, case_id),
                    ).fetchone()
                    reproducible = kind == 'correction' and prior_case and (
                        prior_case['kind'] == 'failure' and prior_case['reproducible'])
                    prompt = db.execute(
                        'SELECT case_key FROM messages WHERE harness=? AND session=? AND turn=? AND role=\'user\'',
                        (harness, session, prior['turn']),
                    ).fetchone()
                    family = f'session:{harness}:{session}'
                    if prompt and prompt['case_key']:
                        family = f'prompt:{prompt["case_key"]}'
                    witness = json.dumps([harness, session, turn, prior['turn'], name, kind])
                    note = text
                    if kind == 'success' and prior['excerpt']:
                        note = f'{text}\nConfirmed prior answer: {prior["excerpt"]}'
                    record(db, witness, name, version, 'session', harness, session, prior['turn'],
                           prior_case['independent'] if prior_case else family,
                           case_id, kind, note, bool(reproducible))
        if inserted and hermes_home is None:
            count = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            if count % MESSAGE_INTERVAL == 0:
                queue(db, f'messages:{count}')
        pending = hermes_home is None and db.execute(
            "SELECT 1 FROM checkpoints WHERE status != 'done' LIMIT 1").fetchone()
    if pending:
        wake(config)
    return bool(inserted)


def snapshot_hash(source):
    """The package projection used by copy_tree(skill=True) before A/B."""
    hash_value = sha256()
    for path in sorted(source.rglob('*')):
        rel = path.relative_to(source)
        if any(part in EXCLUDED for part in rel.parts) or rel.parts[0] in {'evals', 'tests'}:
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink in skill: {rel}')
        if path.is_file() and not path.name.endswith('.pyc'):
            hash_value.update(rel.as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return hash_value.hexdigest()


def eval_context(workspace, project=None):
    config = config_for(project or workspace) or (config_for(Path.cwd()) if not project else None)
    if not config or 'evals' not in config['sources']:
        return None
    manifest = read_json(workspace / 'manifest.json')
    name = manifest.get('skill_id')
    if manifest.get('kind') != 'quality' or name not in config['skills'] or manifest.get('learning_internal'):
        return None
    if manifest.get('harness') not in {'codex', 'opencode'}:
        return None
    source = local_path(config['root'], config['skills'][name]['source'], directory=True)
    version = tree_hash(source)
    candidate = snapshot_hash(source)
    if tree_hash(source) != version:
        return None
    variants = {arm for arm, entry in manifest['configurations'].items() if entry.get('snapshot_hash') == candidate}
    if not variants:
        return None
    return config, manifest, name, version, variants


def observed_runs(workspace, variants, *, include_pilot=False):
    phases = [workspace, workspace / 'pilot'] if include_pilot else [workspace]
    for phase in phases:
        for path in sorted(phase.glob('eval-*/*/run-*/run.json')):
            if path.parent.parent.name in variants:
                run = read_json(path)
                if run.get('status') != 'completed' or not run.get('attached') or not run.get('run_id'):
                    continue
                meta = read_json(path.parent / 'eval_metadata.json')
                yield path.parent, run, meta


def independence(meta):
    group = meta.get('group')
    return f'group:{group}' if group is not None else f'case:{meta["eval_id"]}'


def graded_checks(grade, *, judge=False):
    checks = grade.get('expectations', [])
    if grade.get('status') != 'completed' or not grade.get('evaluation_complete') or not checks:
        return None
    accepted_sources = {'deterministic', 'judge'} if judge else {'deterministic'}
    if not all(type(x.get('passed')) is bool and x.get('source') in accepted_sources for x in checks):
        return None
    return checks


def reproducible_failure(checks):
    return bool(checks and any(x['source'] == 'deterministic' and not x['passed'] for x in checks))


def notify_eval(workspace, *, project=None):
    from scripts.grading_store import read_grading

    workspace = Path(workspace).resolve()
    context = eval_context(workspace, project)
    if not context:
        return False
    config, manifest, name, version, variants = context
    report = read_json(workspace / 'benchmark.json')
    if (report.get('errors') != 0 or report.get('validation_errors') or not manifest.get('controlled')
            or not manifest.get('environment_consistent')):
        return False
    added = False
    with connection(config) as db:
        for path, run, meta in observed_runs(workspace, variants):
            if run.get('installed_hash') != meta.get('snapshot_hash'):
                continue
            grade, _, provenance = read_grading(path)
            checks = graded_checks(grade, judge=bool((provenance or manifest).get('judge_model')))
            if not checks:
                continue
            case_id = meta['eval_id']
            kind = 'success' if all(x['passed'] for x in checks) else 'failure'
            note = 'Independent evaluation: ' + '; '.join(
                f'{x["text"]}: {"pass" if x["passed"] else "fail"}' for x in checks)
            added |= record(db, f'ab:{run["run_id"]}:{name}', name, version, 'ab', manifest['harness'],
                            str(workspace), run['run_id'], independence(meta), case_id, kind, note,
                            reproducible_failure(checks))
        if added:
            last_id = db.execute('SELECT MAX(id) FROM evidence').fetchone()[0]
            queue(db, f'ab:{workspace}:{last_id}')
    if added:
        wake(config)
    return added


def notify_feedback(workspace, feedback_path, *, project=None, selection=None):
    from scripts.generate_review import validate_feedback_entries
    from scripts.grading_store import load_selection, read_grading

    workspace = Path(workspace).resolve()
    context = eval_context(workspace, project)
    if not context:
        return False
    config, manifest, name, version, variants = context
    feedback = read_json(feedback_path)
    if not isinstance(feedback, dict) or feedback.get('status') != 'complete' or feedback.get('schema_version') != 1:
        return False
    validate_feedback_entries(feedback)
    selected = load_selection(workspace, selection)
    runs = {}
    for path, run, meta in observed_runs(workspace, variants, include_pilot=selected is None):
        view_id = run['run_id']
        attempt = None
        if selected is not None:
            attempt = selected['runs'][path.relative_to(workspace).as_posix()]['selected']
        grade, _, provenance = read_grading(path, attempt)
        if selected is not None:
            view_id = f'{view_id}@{provenance["attempt_id"] if provenance else "legacy"}'
        checks = graded_checks(grade, judge=bool((provenance or manifest).get('judge_model')))
        runs[view_id] = (run, meta, checks)
    added = False
    with connection(config) as db:
        for entry in feedback['reviews']:
            kind = entry.get('verdict')
            match = runs.get(entry.get('run_id'))
            note = entry.get('feedback', '')
            if kind not in VERDICTS or not entry.get('reviewed') or not match:
                continue
            run, meta, checks = match
            if entry.get('snapshot_hash') != meta.get('snapshot_hash') or run.get('installed_hash') != meta.get('snapshot_hash'):
                continue
            if kind == 'correction' and not note.strip():
                continue
            case_id = meta['eval_id']
            reproducible = kind == 'correction' and reproducible_failure(checks)
            added |= record(db, f'feedback:{entry["run_id"]}:{name}:{kind}:{sanitize(note)}', name, version, 'feedback',
                            manifest['harness'], str(workspace), run['run_id'], independence(meta), case_id,
                            kind, note or f'Explicit reviewer verdict: {kind}', reproducible)
        if added:
            queue(db, 'feedback:' + digest(feedback_path))
    if added:
        wake(config)
    return added


def import_hermes(project, profile):
    """Import current-version conversation evidence from an enrolled Hermes profile."""
    project = Path(project).resolve()
    profile = Path(profile).expanduser().resolve(strict=True)
    target = config_for(project)
    source = config_for(project, hermes_home=profile)
    if not target or not source or target['root'] != project or 'sessions' not in target['sources']:
        raise ValueError('Enable session learning in both SkillCraft and the selected Hermes profile for this project')

    journal = source['state'] / 'journal.sqlite'
    if journal.resolve() != journal:
        raise ValueError('The Hermes journal must be inside the selected profile')
    counts = {'imported': 0, 'duplicates': 0, 'skipped': 0}
    if not journal.exists():
        return counts
    if not journal.is_file():
        raise ValueError('The Hermes journal must be a regular file')

    versions = {name: tree_hash(local_path(project, settings['source'], directory=True))
                for name, settings in target['skills'].items()}
    imported = sha256()
    with closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True, timeout=10)) as hermes:
        hermes.row_factory = sqlite3.Row
        rows = hermes.execute("""
            SELECT e.*, a.bindings FROM evidence e
            LEFT JOIN messages a
              ON a.harness=e.harness AND a.session=e.session AND a.turn=e.turn AND a.role='assistant'
            WHERE e.harness='hermes' AND e.origin='session'
            ORDER BY e.id
        """)
        with connection(target) as db:
            for entry in rows:
                try:
                    witness = json.loads(entry['witness'])
                    bound = json.loads(entry['bindings'] or '{}')
                except (TypeError, ValueError):
                    counts['skipped'] += 1
                    continue
                name, version, kind = entry['skill'], entry['version'], entry['kind']
                if (versions.get(name) != version or not isinstance(bound, dict) or bound.get(name) != version
                        or kind not in VERDICTS or not isinstance(witness, list) or len(witness) != 6
                        or not isinstance(witness[2], str) or not witness[2]
                        or witness != ['hermes', entry['session'], witness[2], entry['turn'], name, kind]
                        or not hermes.execute("SELECT 1 FROM messages WHERE harness='hermes' "
                                              "AND session=? AND turn=? AND role='user'",
                                              (entry['session'], witness[2])).fetchone()):
                    counts['skipped'] += 1
                    continue
                # The native session/turn witness survives profile moves and copies.
                identity = sha256(json.dumps(witness, separators=(',', ':')).encode()).hexdigest()
                if record(db, f'hermes-import:{identity}', name, version, 'session', 'hermes',
                          entry['session'], entry['turn'], entry['independent'], entry['case_id'],
                          kind, entry['note']):
                    counts['imported'] += 1
                    imported.update(identity.encode())
                else:
                    counts['duplicates'] += 1
            if counts['imported']:
                queue(db, 'hermes-import:' + imported.hexdigest())
    if counts['imported']:
        wake(target)
    return counts


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--hermes-home', type=Path, help=argparse.SUPPRESS)
    sub = cli.add_subparsers(dest='command', required=True)
    sub.add_parser('observe', help='Read one native message envelope from stdin')
    for verb in ('enable', 'work', 'status', 'rollback', 'inventory'):
        parser = sub.add_parser(verb)
        parser.add_argument('project', type=Path)
        if verb == 'rollback':
            parser.add_argument('revision', type=int)
        if verb == 'work':
            parser.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
            parser.add_argument('--retry-blocked', action='store_true',
                                help='Queue one durable validation-only retry for saved blocked proposals')
    hermes_import = sub.add_parser('import-hermes', help='Import verified conversations from an enrolled Hermes profile')
    hermes_import.add_argument('project', type=Path)
    hermes_import.add_argument('--from-profile', type=Path, required=True, help='Hermes profile home directory')
    ingest = sub.add_parser('ingest', help='Import a completed A/B or an explicit feedback file')
    ingest.add_argument('workspace', type=Path)
    ingest.add_argument('--project', type=Path)
    ingest.add_argument('--feedback', type=Path)
    ingest.add_argument('--selection', type=Path, help='Grading selection used for the reviewed report')
    args = cli.parse_args(argv)
    try:
        home = args.hermes_home
        if home is not None:
            if not home.is_absolute() or args.command not in {'enable', 'inventory', 'observe', 'status'}:
                raise ValueError('Hermes supports journal collection only and requires an absolute profile path')
        if args.command == 'observe':
            inserted = observe(**json.load(sys.stdin), hermes_home=home)
            if home is not None:
                print(json.dumps({'inserted': inserted}))
            return 0
        if args.command == 'import-hermes':
            print(json.dumps(import_hermes(args.project, args.from_profile)))
            return 0
        if args.command == 'ingest':
            if args.selection and not args.feedback:
                raise ValueError('--selection requires --feedback')
            changed = (notify_feedback(args.workspace, args.feedback, project=args.project, selection=args.selection) if args.feedback
                       else notify_eval(args.workspace, project=args.project))
            print(json.dumps({'new_evidence': changed}))
            return 0
        config = config_for(args.project, enrolling=args.command == 'enable', hermes_home=home)
        if not config:
            raise ValueError('Project learning is not enabled')
        if home is not None and 'sessions' not in config['sources']:
            raise ValueError('Hermes requires sessions in the learning sources')
        if args.command == 'enable':
            if args.project.resolve() != config['root']:
                raise ValueError('Enable learning from the project root')
            config['state'].mkdir(parents=True, exist_ok=True, mode=0o700)
            from tempfile import NamedTemporaryFile
            with NamedTemporaryFile('w', encoding='utf-8', dir=config['state'], delete=False) as marker:
                approval = (approval_record(config) if home is None
                            else {'config_sha256': config['fingerprint']})
                json.dump(approval, marker, ensure_ascii=False)
                marker.write('\n')
                marker.flush()
                os.fsync(marker.fileno())
            Path(marker.name).replace(config['state'] / 'enabled')
            print(json.dumps({'enabled': str(config['root']), 'journal': str(config['state'])}))
        elif args.command == 'inventory':
            versions = source_inventory(config, hermes=home is not None)
            print(json.dumps({'root': str(config['root']), 'versions': versions} if home else versions,
                             ensure_ascii=False))
        elif args.command == 'work':
            from scripts.learning_worker import work
            if args.retry_blocked:
                with connection(config) as db:
                    queue(db, f'manual-retry:{secrets.token_hex(16)}')
            work(config, lock_fd=args.lock_fd)
        elif args.command == 'rollback':
            from scripts.learning_worker import rollback_revision
            print(json.dumps(rollback_revision(config, args.revision), ensure_ascii=False))
        else:
            with connection(config) as db:
                print(json.dumps({
                    'messages': db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
                    'evidence': {r[0]: r[1] for r in db.execute('SELECT kind,COUNT(*) FROM evidence GROUP BY kind')},
                    'checkpoints': {r[0]: r[1] for r in db.execute('SELECT status,COUNT(*) FROM checkpoints GROUP BY status')},
                    'revisions': [dict(r) for r in db.execute('SELECT * FROM revisions ORDER BY id DESC LIMIT 20')],
                }, ensure_ascii=False))
        return 0
    except (OSError, TypeError, ValueError, KeyError, RuntimeError, sqlite3.Error) as error:
        if args.command != 'observe':
            print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
