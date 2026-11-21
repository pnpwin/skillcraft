#!/usr/bin/env python3
"""Opt-in, local experience collection for SkillCraft."""
import argparse
from contextlib import closing, contextmanager
from hashlib import sha256
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
from scripts.harness import HARNESSES
from scripts.utils import EXCLUDED, digest, read_json, safe_id, tree_hash

CONFIG = Path('.skillcraft/learning.json')
INTERNAL = 'SKILLCRAFT_LEARNING_INTERNAL'
PROJECT_MARKERS = ('.git', '.hg', '.svn', 'AGENTS.md')
MESSAGE_INTERVAL = 15
MODEL_STEP_INTERVAL = 15
VERDICTS = {'success', 'failure', 'correction'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    harness TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
    role TEXT NOT NULL, bindings TEXT NOT NULL, diagnostic TEXT, case_key TEXT, excerpt TEXT,
    UNIQUE(harness, session, turn, role)
);
CREATE TABLE IF NOT EXISTS pending_feedback (
    harness TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
    target TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(harness, session, turn)
);
CREATE TABLE IF NOT EXISTS model_steps (
    harness TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
    steps INTEGER NOT NULL, PRIMARY KEY(harness, session, turn)
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


def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Local paths must be nonempty, project-relative strings')
    parts = PurePosixPath(value)
    if parts.is_absolute() or '..' in parts.parts or '.git' in parts.parts or not parts.parts:
        raise ValueError(f'Path escapes project: {value}')
    return parts


def local_path(root, value, *, directory=False):
    parts = relative_path(value)
    path = root
    for part in parts.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f'Symlinks are not allowed: {value}')
    if directory and not path.is_dir():
        raise ValueError(f'Missing skill directory: {value}')
    return path


def configured_linked_roots(raw, selection):
    values = raw.get('linked_skill_roots', [])
    if selection != 'all' and 'linked_skill_roots' in raw:
        raise ValueError('linked_skill_roots requires selection all')
    if not isinstance(values, list):
        raise ValueError('linked_skill_roots must be a list of absolute source directories')
    roots = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError('linked_skill_roots must contain absolute source directories')
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('linked_skill_roots must contain absolute source directories')
        roots.append(path)
    return tuple(dict.fromkeys(roots))


def approved_link_target(root, source, linked_roots):
    from scripts.learning_selection import linked_skill_target

    relative = relative_path(source)
    parent = local_path(root, relative.parent.as_posix(), directory=True)
    return linked_skill_target(parent / relative.name, linked_roots)


def skill_source(config, settings):
    """Resolve a selected source against the same approved target at use time."""
    source = settings['source']
    if 'linked_target' not in settings:
        return local_path(config['root'], source, directory=True)
    target = approved_link_target(config['root'], source, config.get('linked_skill_roots', ()))
    if target is None or str(target) != settings['linked_target']:
        raise ValueError(f'The linked skill source changed or is no longer allowed: {source}')
    return target


def selected_skills(root, raw, scope, selection, linked_roots=()):
    overrides = raw.get('skills', {})
    if selection not in ('explicit', 'all') or not isinstance(overrides, dict):
        raise ValueError('Expected selection explicit or all and a skills object')
    for name, settings in overrides.items():
        if name != safe_id(name) or not isinstance(settings, dict):
            raise ValueError('Expected a skill ID and settings object')
        if 'linked_target' in settings:
            raise ValueError('linked_target is resolved from an approved directory')
    if selection == 'explicit':
        if not overrides:
            raise ValueError('Learning requires explicitly selected skills')
        if 'exclude' in raw or 'defaults' in raw:
            raise ValueError('exclude and defaults require selection all')
        for name, settings in overrides.items():
            source = local_path(root, settings.get('source'), directory=True)
            if source.name != name or not (source / 'SKILL.md').is_file() or (source / 'SKILL.md').is_symlink():
                raise ValueError(f'Invalid source for {name}')
            if source == Path(__file__).resolve().parents[1]:
                raise ValueError('The running SkillCraft installation cannot be its own update target')
        return overrides

    from scripts.learning_selection import discover_local_skills

    exclude = raw.get('exclude', [])
    if not isinstance(exclude, list) or any(not isinstance(name, str) or name != safe_id(name) for name in exclude):
        raise ValueError('exclude must contain exact skill IDs')
    defaults = raw.get('defaults', {})
    if not isinstance(defaults, dict) or {'source', 'linked_target'} & defaults.keys():
        raise ValueError('defaults must be a skills settings object without a source')
    pins = {name: relative_path(settings['source']).as_posix() for name, settings in overrides.items()
            if 'source' in settings}
    result = {}
    excluded = set(exclude)
    running_skill = Path(__file__).resolve().parents[1]
    for name, paths in discover_local_skills(root, scope=scope, linked_skill_roots=linked_roots).items():
        if name in excluded:
            continue
        override = overrides.get(name, {})
        if name in pins:
            paths = [pins[name]] if pins[name] in paths else []
        candidates = {}
        for path in paths:
            target = root / path
            linked = target.is_symlink()
            if linked:
                target = approved_link_target(root, path, linked_roots)
            if target is not None and target != running_skill and (target not in candidates or not linked):
                candidates[target] = (path, linked)
        if len(candidates) != 1:
            continue
        target, (source, linked) = next(iter(candidates.items()))
        settings = {**defaults, **override, 'source': source}
        if linked:
            settings.update(linked_target=str(target), auto_apply=False)
        for field in ('train', 'holdout'):
            if field in defaults and field not in override:
                pattern = defaults[field]
                if not isinstance(pattern, str):
                    raise ValueError(f'defaults.{field} must be a project-relative path')
                settings[field] = pattern.replace('{skill}', name)
        result[name] = settings
    return result


def has_project_boundary(directory, home):
    for parent in (directory, *directory.parents):
        if parent == home:
            return False
        if parent.is_mount() or any((parent / marker).exists() or (parent / marker).is_symlink()
                                    for marker in PROJECT_MARKERS):
            return True
    return False


def read_regular(path, *, dir_fd=None):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                     getattr(os, 'O_NONBLOCK', 0), dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f'Learning file must be a regular file: {path}')
        return stream.read(), stat.S_IMODE(metadata.st_mode)


def read_learning_config(root):
    try:
        fd = os.open(root / CONFIG.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
                     getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    try:
        return read_regular(CONFIG.name, dir_fd=fd)
    finally:
        os.close(fd)


def learning_state(root, hermes_home=None):
    state_root = (hermes_home / 'skillcraft/learning' if hermes_home is not None
                  else Path.home() / '.local/share/skillcraft/learning')
    return state_root / sha256(str(root).encode()).hexdigest()


def matching_marker(contents, fingerprint):
    if contents is None:
        return None
    raw = contents[0].decode('utf-8')
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    if isinstance(value, dict) and value.get('config_sha256') == fingerprint:
        return value
    # Legacy markers authorize collection without model approval.
    if isinstance(value, str) and value.strip() == fingerprint:
        return {'config_sha256': fingerprint}
    return None


def config_for(project, *, enrolling=False, trusted_internal=False, hermes_home=None):
    if os.environ.get(INTERNAL) == '1' and not trusted_internal:
        return None
    path = Path(project).resolve()
    for root in (path, *path.parents):
        snapshot = read_learning_config(root)
        if snapshot is None:
            continue
        contents, _ = snapshot
        fingerprint = sha256(contents).hexdigest()
        raw = json.loads(contents)
        if not isinstance(raw, dict) or raw.get('schema_version') != 1:
            raise ValueError('Expected learning config schema_version 1')
        if raw.get('enabled') is not True:
            return None
        scope = raw.get('scope', 'project')
        if scope not in ('project', 'home'):
            raise ValueError('Learning scope must be project or home')
        if scope == 'home':
            if root != Path.home().resolve():
                raise ValueError('Home learning must be configured in the home directory')
            if has_project_boundary(path, root):
                return None
        sources = raw.get('sources', ['sessions', 'evals'])
        if not isinstance(sources, list) or any(x not in {'sessions', 'evals'} for x in sources):
            raise ValueError('Learning sources must be sessions and/or evals')
        state = learning_state(root, hermes_home)
        approval = None
        auto_import = False
        if not enrolling:
            try:
                marker = matching_marker(read_regular(state / 'enabled'), fingerprint)
            except (OSError, ValueError, TypeError):
                return None
            if marker is None:
                return None
            approval = marker.get('validation')
            auto_import = hermes_home is not None and marker.get('auto_import') is True
        selection = raw.get('selection', 'explicit')
        linked_roots = configured_linked_roots(raw, selection)
        skills = selected_skills(root, raw, scope, selection, linked_roots)
        return {'root': root, 'state': state, 'skills': skills, 'sources': sources,
                'scope': scope, 'selection': selection, 'linked_skill_roots': linked_roots,
                'fingerprint': fingerprint, 'approval': approval, 'hermes': hermes_home is not None,
                'auto_import': auto_import}
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
    automatic = config.get('selection') == 'all'
    result = {}
    for name, settings in config['skills'].items():
        approved = {'datasets': {}, 'fixtures': []}
        if all(key in settings for key in ('train', 'holdout')):
            source = skill_source(config, settings)
            datasets = {split: local_path(root, settings[split]) for split in ('train', 'holdout')}
            if automatic and not all(path.exists() for path in datasets.values()):
                continue
            for split, path in datasets.items():
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
            if automatic:
                approved['source'] = settings['source']
                if 'linked_target' in settings:
                    approved['linked_target'] = settings['linked_target']
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


def same_approved_source(settings, approval):
    return (settings['source'] == approval.get('source')
            and settings.get('linked_target') == approval.get('linked_target'))


def require_model_approval(config, *, name=None):
    approval = config.get('approval')
    if not isinstance(approval, dict):
        raise ValueError('Model learning requires re-enable with approved train, holdout, and fixtures')
    automatic = config.get('selection') == 'all'
    skills = config['skills']
    if automatic and name is not None:
        if name not in skills:
            raise ValueError('Skill is no longer selected')
        skills = {name: skills[name]}
    for skill, settings in skills.items():
        stored = approval.get(skill)
        needs_datasets = all(key in settings for key in ('train', 'holdout'))
        needs_binary = 'binary' in settings.get('harnesses', {}).get('codex', {})
        if ((needs_datasets and (not isinstance(stored, dict)
                                 or set(stored.get('datasets', {})) != {'train', 'holdout'}))
                or (needs_binary and (not isinstance(stored, dict) or not stored.get('binary')))):
            raise ValueError('Model learning requires re-enable with approved train, holdout, and fixtures')
        if automatic and (needs_datasets or needs_binary) and not same_approved_source(settings, stored):
            raise ValueError('The selected skill source changed; re-enable learning')
    return approval


def approval_still_current(config, approval, *, name=None):
    """Check only identities the owner approved; never hash newly referenced fixtures."""
    from scripts.approved_binary import describe
    from scripts.inputs import input_specs
    root = config['root'].resolve()
    automatic = config.get('selection') == 'all'
    skills = config['skills']
    if automatic and name is not None:
        if name not in skills:
            return False
        skills = {name: skills[name]}
    for skill, settings in skills.items():
        codex = settings.get('harnesses', {}).get('codex', {})
        needs_binary = 'binary' in codex
        needs_datasets = all(key in settings for key in ('train', 'holdout'))
        if not needs_binary and not needs_datasets:
            continue
        expected = approval.get(skill)
        if not isinstance(expected, dict) or (automatic and not same_approved_source(settings, expected)):
            return False
        if needs_binary:
            try:
                if describe(codex['binary']) != expected.get('binary'):
                    return False
            except (OSError, TypeError, ValueError):
                return False
        if not all(key in settings for key in ('train', 'holdout')):
            continue
        source = skill_source(config, settings)
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
            db.execute("""CREATE TABLE IF NOT EXISTS hermes_recovered_feedback (
                session TEXT NOT NULL, turn TEXT NOT NULL, target TEXT NOT NULL,
                final_uid TEXT NOT NULL, PRIMARY KEY (session, turn), UNIQUE (session, final_uid))""")
        with db:
            yield db


def queue(db, witness):
    return db.execute('INSERT OR IGNORE INTO checkpoints(witness) VALUES (?)', (witness,)).rowcount != 0


def queue_message_checkpoint(db, *, at_boundary=False):
    count = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    boundary = count // MESSAGE_INTERVAL * MESSAGE_INTERVAL
    return bool(boundary and (not at_boundary or count == boundary) and queue(db, f'messages:{boundary}'))


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
    if not loaded:
        return {}
    bindings = {}
    for name, opts in config['skills'].items():
        try:
            source = skill_source(config, opts)
            markdown = source / 'SKILL.md'
            for entry in loaded:
                if isinstance(entry, dict) and Path(entry.get('path') or '.').resolve() == markdown:
                    if entry.get('hash') == digest(markdown):
                        version = tree_hash(source)
                        if entry.get('snapshot_hash') == version:
                            bindings[name] = version
                    break
        except (OSError, ValueError):
            if config.get('selection') != 'all':
                raise
    return bindings


def source_inventory(config, *, hermes=False):
    versions = []
    if 'sessions' not in config['sources']:
        return versions
    for settings in config['skills'].values():
        try:
            source = skill_source(config, settings)
            markdown = source / 'SKILL.md'
            content = markdown.read_bytes()
            version = {'path': str(markdown), 'hash': sha256(content).hexdigest(),
                       'snapshot_hash': tree_hash(source)}
            if 'linked_target' in settings:
                version['linked'] = True
            if hermes:
                # Hermes decodes SKILL.md with utf-8-sig and universal newlines.
                decoded = content.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
                version['content_hash'] = sha256(decoded.encode()).hexdigest()
            versions.append(version)
        except (OSError, ValueError):
            # A source being edited can still be observed as an unbound turn.
            continue
    return versions


def selected_version(config, name, versions):
    if name in versions:
        return versions[name]
    settings = config['skills'].get(name)
    version = None
    if settings:
        try:
            source = skill_source(config, settings)
            version = tree_hash(source)
        except (OSError, ValueError):
            if config.get('selection') != 'all':
                raise
    versions[name] = version
    return version


def record(db, witness, skill, version, origin, harness, session, turn, independent, case_id, kind, note,
           reproducible=False):
    return db.execute(
        'INSERT OR IGNORE INTO evidence(witness,skill,version,origin,harness,session,turn,'
        'independent,case_id,kind,note,reproducible) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (witness, skill, version, origin, harness, session, turn, independent, case_id, kind,
         sanitize(note), int(reproducible)),
    ).rowcount != 0



def record_feedback(db, harness, session, turn, target_turn, text):
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
            return record(db, witness, name, version, 'session', harness, session, prior['turn'],
                          prior_case['independent'] if prior_case else family,
                          case_id, kind, note, bool(reproducible))
    return False


def add_model_steps(db, harness, session, turn, steps, *, schedule):
    inserted = db.execute(
        'INSERT OR IGNORE INTO model_steps VALUES (?,?,?,?)', (harness, session, turn, steps),
    ).rowcount
    if inserted and schedule:
        total = db.execute('SELECT SUM(steps) FROM model_steps').fetchone()[0]
        if total // MODEL_STEP_INTERVAL > (total - steps) // MODEL_STEP_INTERVAL:
            return queue(db, f'model-steps:{total // MODEL_STEP_INTERVAL * MODEL_STEP_INTERVAL}')
    return False


def observe(project, harness, session, turn, role, text, loaded, *, target_turn=None,
            diagnostic=None, hermes_home=None, hermes_uid=None,
            model_steps=0, defer_feedback=False):
    config = config_for(project, hermes_home=hermes_home)
    if not config or 'sessions' not in config['sources']:
        return False
    allowed = {'hermes'} if hermes_home is not None else {'codex', 'opencode'}
    if harness not in allowed or role not in {'user', 'assistant'}:
        raise ValueError('Invalid message origin')
    if not all(isinstance(x, str) and x for x in (session, turn)) or not isinstance(text, str):
        raise ValueError('Messages need durable session and turn identities')
    if type(model_steps) is not int or not 0 <= model_steps <= 2**31 - 1 or (model_steps and role != 'assistant'):
        raise ValueError('Invalid completed model step count')
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
        if inserted and role == 'user' and defer_feedback:
            db.execute('DELETE FROM pending_feedback WHERE harness=? AND session=?', (harness, session))
        if inserted and role == 'user' and target_turn and classify(text):
            if defer_feedback:
                db.execute('INSERT INTO pending_feedback VALUES (?,?,?,?,?)',
                           (harness, session, turn, target_turn, sanitize(text)))
            else:
                record_feedback(db, harness, session, turn, target_turn, text)
        if inserted and role == 'assistant':
            feedback = db.execute(
                'DELETE FROM pending_feedback WHERE harness=? AND session=? AND turn=? RETURNING target,text',
                (harness, session, turn),
            ).fetchone()
            if feedback:
                record_feedback(db, harness, session, turn, feedback['target'], feedback['text'])
            if model_steps and db.execute(
                "SELECT 1 FROM messages WHERE harness=? AND session=? AND turn=? AND role='user'",
                (harness, session, turn),
            ).fetchone():
                add_model_steps(db, harness, session, turn, model_steps, schedule=hermes_home is None)
        if inserted and hermes_home is None and (role == 'assistant' or not defer_feedback):
            queue_message_checkpoint(db, at_boundary=role == 'user')
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
    if manifest.get('harness') not in HARNESSES:
        return None
    source = skill_source(config, config['skills'][name])
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


def import_hermes(project, profile, *, completed_session=None, automatic=False):
    """Import current-version conversation evidence from an enrolled Hermes profile."""
    project = Path(project).resolve()
    profile = Path(profile).expanduser().resolve(strict=True)
    target = config_for(project)
    source = config_for(project, hermes_home=profile)
    if not target or not source or target['root'] != project or 'sessions' not in target['sources']:
        raise ValueError('Enable session learning in both SkillCraft and the selected Hermes profile for this project')
    if automatic and not source['auto_import']:
        raise ValueError('Automatic import requires explicit enrollment of this Hermes profile')

    journal = source['state'] / 'journal.sqlite'
    if journal.resolve() != journal:
        raise ValueError('The Hermes journal must be inside the selected profile')
    counts = {'imported': 0, 'duplicates': 0, 'skipped': 0}
    if not journal.exists():
        return counts
    if not journal.is_file():
        raise ValueError('The Hermes journal must be a regular file')

    versions = {}
    imported = sha256()
    checkpoint = False
    with closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True, timeout=10)) as hermes:
        hermes.row_factory = sqlite3.Row
        source_tables = {row[0] for row in hermes.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = hermes.execute("""
            SELECT e.*, a.bindings FROM evidence e
            LEFT JOIN messages a
              ON a.harness=e.harness AND a.session=e.session AND a.turn=e.turn AND a.role='assistant'
            WHERE e.harness='hermes' AND e.origin='session' AND (? IS NULL OR e.session=?)
            ORDER BY e.id
        """, (completed_session, completed_session))
        with connection(target) as db:
            for entry in rows:
                try:
                    witness = json.loads(entry['witness'])
                    bound = json.loads(entry['bindings'] or '{}')
                except (TypeError, ValueError):
                    counts['skipped'] += 1
                    continue
                name, version, kind = entry['skill'], entry['version'], entry['kind']
                if (selected_version(target, name, versions) != version
                        or not isinstance(bound, dict) or bound.get(name) != version
                        or kind not in VERDICTS or not isinstance(witness, list) or len(witness) != 6
                        or not isinstance(witness[2], str) or not witness[2]
                        or witness != ['hermes', entry['session'], witness[2], entry['turn'], name, kind]):
                    counts['skipped'] += 1
                    continue
                completed = hermes.execute(
                    """SELECT COUNT(*)=2 FROM messages WHERE harness='hermes' AND session=? AND turn=?
                       AND role IN ('user','assistant')""",
                    (entry['session'], witness[2]),
                ).fetchone()[0]
                if not completed and 'hermes_recovered_feedback' in source_tables:
                    completed = hermes.execute(
                        'SELECT 1 FROM hermes_recovered_feedback WHERE session=? AND turn=? AND target=?',
                        (entry['session'], witness[2], entry['turn']),
                    ).fetchone()
                if not completed:
                    counts['skipped'] += 1
                    continue
                reproduction = None
                if kind == 'correction':
                    reproduction = db.execute(
                        """SELECT independent FROM evidence WHERE skill=? AND version=? AND case_id=?
                           AND origin='ab' AND kind='failure' AND reproducible=1 ORDER BY id LIMIT 1""",
                        (name, version, entry['case_id']),
                    ).fetchone()
                # The native session/turn witness survives profile moves and copies.
                identity = sha256(json.dumps(witness, separators=(',', ':')).encode()).hexdigest()
                if record(db, f'hermes-import:{identity}', name, version, 'session', 'hermes',
                          entry['session'], entry['turn'],
                          reproduction['independent'] if reproduction else entry['independent'],
                          entry['case_id'], kind, entry['note'], bool(reproduction)):
                    counts['imported'] += 1
                    imported.update(identity.encode())
                else:
                    counts['duplicates'] += 1
            if counts['imported']:
                checkpoint = queue(db, 'hermes-import:' + imported.hexdigest())
            if automatic:
                pairs = hermes.execute("""
                    SELECT a.turn FROM messages a
                    JOIN messages u ON u.harness=a.harness AND u.session=a.session
                                   AND u.turn=a.turn AND u.role='user'
                    WHERE a.harness='hermes' AND a.session=? AND a.role='assistant'
                    ORDER BY a.seq
                """, (completed_session,))
                inserted = db.executemany(
                    "INSERT OR IGNORE INTO messages(harness,session,turn,role,bindings) VALUES ('hermes',?,?,?,'{}')",
                    ((completed_session, pair['turn'], role) for pair in pairs for role in ('user', 'assistant')),
                ).rowcount
                if inserted:
                    checkpoint |= queue_message_checkpoint(db)
            if automatic and 'model_steps' in source_tables:
                completed_steps = hermes.execute("""
                    SELECT s.turn, s.steps FROM model_steps s
                    JOIN messages a ON a.harness=s.harness AND a.session=s.session
                                   AND a.turn=s.turn AND a.role='assistant'
                    JOIN messages u ON u.harness=s.harness AND u.session=s.session
                                   AND u.turn=s.turn AND u.role='user'
                    WHERE s.harness='hermes' AND s.session=? ORDER BY a.seq
                """, (completed_session,))
                for entry in completed_steps:
                    if type(entry['steps']) is int and 0 < entry['steps'] <= 2**31 - 1:
                        checkpoint |= add_model_steps(db, 'hermes', completed_session, entry['turn'],
                                                      entry['steps'], schedule=True)
    if checkpoint:
        wake(target)
    return counts


def recover_hermes(project, profile, *, session, items):
    project = Path(project).resolve()
    source = config_for(project, hermes_home=profile)
    if not source or source['root'] != project or 'sessions' not in source['sources']:
        raise ValueError('Enable session learning in this Hermes profile and use its approved project root')
    if not isinstance(session, str) or not session or not isinstance(items, list):
        raise ValueError('History recovery requires one native Hermes session and a list of observations')
    counts = {'recorded': 0, 'duplicates': 0, 'skipped': 0}
    if not items:
        return counts
    journal = source['state'] / 'journal.sqlite'
    if journal.resolve() != journal or not journal.is_file():
        raise ValueError('History recovery requires verified observations in this Hermes profile journal')
    versions = {}
    with connection(source) as db:
        db.execute('BEGIN IMMEDIATE')
        for item in items:
            if not isinstance(item, dict):
                counts['skipped'] += 1
                continue
            values = [item.get(field) for field in ('target_turn', 'feedback_turn', 'feedback',
                                                   'reply_uid_sha256', 'final_reply_uid')]
            if not all(isinstance(value, str) and value for value in values):
                counts['skipped'] += 1
                continue
            target, prompt_uid, feedback, uid, final_uid = values
            kind = classify(feedback)
            if not kind or not re.fullmatch(r'[0-9a-f]{64}', uid):
                counts['skipped'] += 1
                continue
            prior = db.execute("""
                SELECT a.bindings FROM hermes_replies r
                JOIN messages a ON a.harness='hermes' AND a.session=r.session
                               AND a.turn=r.turn AND a.role='assistant'
                JOIN messages u ON u.harness='hermes' AND u.session=r.session
                               AND u.turn=r.turn AND u.role='user'
                WHERE r.session=? AND r.turn=? AND r.uid=?
            """, (session, target, uid)).fetchone()
            bound = json.loads(prior['bindings']) if prior else {}
            if not isinstance(bound, dict) or not bound or any(
                selected_version(source, name, versions) != version for name, version in bound.items()
            ):
                counts['skipped'] += 1
                continue
            if db.execute("SELECT 1 FROM evidence WHERE harness='hermes' AND session=? AND turn=? AND kind=?",
                          (session, target, kind)).fetchone():
                counts['duplicates'] += 1
                continue
            turn = 'history:' + sha256(prompt_uid.encode()).hexdigest()
            inserted = db.execute('INSERT OR IGNORE INTO hermes_recovered_feedback VALUES (?,?,?,?)',
                                  (session, turn, target, sha256(final_uid.encode()).hexdigest())).rowcount
            if inserted and record_feedback(db, 'hermes', session, turn, target, feedback):
                counts['recorded'] += 1
            elif inserted:
                db.execute('DELETE FROM hermes_recovered_feedback WHERE session=? AND turn=?', (session, turn))
                counts['skipped'] += 1
            else:
                counts['duplicates'] += 1
    target = config_for(project)
    if target and 'sessions' in target['sources']:
        counts['import'] = import_hermes(project, profile, completed_session=session)
    return counts


def linked_root_preview(config, raw):
    from scripts.learning_selection import linked_root_candidates

    pins = {name: relative_path(opts['source']).as_posix() for name, opts in raw.get('skills', {}).items()
            if 'source' in opts}
    candidates = linked_root_candidates(
        config['root'], scope=config['scope'], linked_skill_roots=config['linked_skill_roots'],
        exclude=raw.get('exclude', ()), pins=pins,
    )
    entries = [{'root': str(root), 'links': [{'source': source, 'target': str(target)}
                                            for source, target in links]}
               for root, links in candidates.items()]
    version = sha256(json.dumps([config['fingerprint'], entries], sort_keys=True).encode()).hexdigest()
    return {'candidates': entries, 'preview_sha256': version}


def prompt_linked_roots(entries):
    if not entries:
        return []
    print('Approve linked skill directories for read-only learning:', file=sys.stderr)
    for index, entry in enumerate(entries, 1):
        print(f"  {index}. {entry['root']!r}", file=sys.stderr)
        for link in entry['links']:
            print(f"     {link['source']!r} -> {link['target']!r}", file=sys.stderr)
    print('Enter directory numbers separated by commas or spaces, or press Enter to skip: ',
          end='', file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt as error:
        raise ValueError('Learning enrollment cancelled') from error
    if not answer:
        raise ValueError('Learning enrollment cancelled')
    try:
        choices = [int(token) for token in answer.replace(',', ' ').split()]
    except ValueError as error:
        raise ValueError('Choose one or more directory numbers from the preview') from error
    if any(index < 1 or index > len(entries) for index in choices):
        raise ValueError('Choose one or more directory numbers from the preview')
    return [entries[index - 1]['root'] for index in choices]


@contextmanager
def enrollment_lock(root):
    import fcntl

    fd = os.open(root / CONFIG.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
                 getattr(os, 'O_NOFOLLOW', 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def enrollment_file(path, contents, mode):
    from tempfile import NamedTemporaryFile

    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), mode)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink()
            raise
    return temporary


def write_enrollment(changes, verify):
    """Stage writes on their respective filesystems and restore on ordinary failure."""
    staged = []
    installed = []
    try:
        for path, original, contents in changes:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            mode = original[1] if original else 0o600
            staged.append((path, original, (contents, mode), enrollment_file(path, contents, mode)))
        verify()
        for path, original, value, temporary in staged:
            if read_regular(path) != original:
                raise ValueError(f'Learning enrollment changed during enable: {path}')
            temporary.replace(path)
            installed.append((path, original, value))
    except (OSError, ValueError, KeyboardInterrupt):
        for path, original, value in reversed(installed):
            if read_regular(path) != value:
                continue
            if original is None:
                path.unlink()
            else:
                backup = enrollment_file(path, *original)
                try:
                    backup.replace(path)
                finally:
                    backup.unlink(missing_ok=True)
        raise
    finally:
        for _, _, _, temporary in staged:
            temporary.unlink(missing_ok=True)


def enable_learning(args, home):
    config = config_for(args.project, enrolling=True, hermes_home=home)
    if not config:
        raise ValueError('Project learning is not enabled')
    root = config['root']
    if args.project.resolve() != root:
        raise ValueError('Enable learning from the project root')
    if home is not None and 'sessions' not in config['sources']:
        raise ValueError('Hermes requires sessions in the learning sources')
    if args.auto_import and home is None:
        raise ValueError('Use hermes skillcraft enable to enroll a source profile for automatic import')
    preview_only = args.preview_linked_roots
    roots = args.approve_linked_root
    if bool(roots) != (args.linked_roots_preview_sha256 is not None):
        raise ValueError('--approve-linked-root requires --linked-roots-preview-sha256 and vice versa')
    if (preview_only or roots) and config['selection'] != 'all':
        raise ValueError('Linked root confirmation requires selection all')
    if preview_only and args.auto_import:
        raise ValueError('Preview does not change Hermes auto-import. Omit --auto-import.')

    snapshot = read_learning_config(root)
    if snapshot is None or sha256(snapshot[0]).hexdigest() != config['fingerprint']:
        raise ValueError('Learning configuration changed. Run enable again.')
    raw = json.loads(snapshot[0])
    interactive = config['selection'] == 'all' and sys.stdin.isatty() and sys.stderr.isatty()
    preview = linked_root_preview(config, raw) if preview_only or roots or interactive else None
    if preview_only:
        return preview
    if roots:
        if args.linked_roots_preview_sha256 != preview['preview_sha256']:
            raise ValueError('Linked root preview changed. Preview again before approving directories.')
    elif interactive:
        roots = prompt_linked_roots(preview['candidates'])
    entries = [entry for entry in preview['candidates'] if entry['root'] in roots] if roots else []
    if {entry['root'] for entry in entries} != set(roots):
        raise ValueError('Approve only absolute directories listed in the current preview')

    original_config = config

    def verify():
        if read_learning_config(root) != snapshot:
            raise ValueError('Learning configuration changed. Run enable again.')
        if entries and linked_root_preview(original_config, raw) != preview:
            raise ValueError('Linked root preview changed. Preview again before approving directories.')

    shared_state = learning_state(root)
    with enrollment_lock(root):
        verify()
        shared_path = shared_state / 'enabled'
        shared_original = read_regular(shared_path) if home is not None and (entries or args.auto_import) else None
        shared = matching_marker(shared_original, config['fingerprint'])
        if args.auto_import and shared is None:
            raise ValueError('Enable regular SkillCraft session learning before automatic import')
        changes = []
        if entries:
            approved_roots = [str(path) for path in config['linked_skill_roots']]
            approved_roots.extend(entry['root'] for entry in entries)
            updated = {**raw, 'linked_skill_roots': approved_roots}
            linked_roots = configured_linked_roots(updated, config['selection'])
            skills = selected_skills(root, updated, config['scope'], config['selection'], linked_roots)
            for entry in entries:
                for link in entry['links']:
                    name = Path(link['source']).name
                    target = approved_link_target(root, link['source'], linked_roots)
                    if (target is None or str(target) != link['target'] or name not in skills
                            or skill_source({**config, 'linked_skill_roots': linked_roots}, skills[name]) != target):
                        raise ValueError(f"Linked skill is no longer eligible or has an ambiguous source: {link['source']}")
            contents = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode()
            config = {**config, 'fingerprint': sha256(contents).hexdigest(),
                      'linked_skill_roots': linked_roots, 'skills': skills}
            changes.append((root / CONFIG, snapshot, contents))
            if shared is not None:
                # Enrollment from Hermes cannot authorize model inputs for the shared worker.
                refreshed = {**shared, 'config_sha256': config['fingerprint']}
                changes.append((shared_path, shared_original, (json.dumps(refreshed) + '\n').encode()))

        marker = config['state'] / 'enabled'
        approval = (approval_record(config) if home is None
                    else {'config_sha256': config['fingerprint'], 'auto_import': args.auto_import})
        changes.append((marker, read_regular(marker), (json.dumps(approval, ensure_ascii=False) + '\n').encode()))
        write_enrollment(changes, verify)
    return {'enabled': str(root), 'journal': str(config['state'])}


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--hermes-home', type=Path, help=argparse.SUPPRESS)
    sub = cli.add_subparsers(dest='command', required=True)
    sub.add_parser('observe', help='Read one native message envelope from stdin')
    sync = sub.add_parser('sync-hermes')
    sync.add_argument('project', type=Path)
    sync.add_argument('session')
    recovery = sub.add_parser('recover-hermes', help='Record independently verified local history observations')
    recovery.add_argument('project', type=Path)
    for verb in ('enable', 'work', 'status', 'rollback', 'inventory'):
        parser = sub.add_parser(verb)
        parser.add_argument('project', type=Path)
        if verb == 'enable':
            parser.add_argument('--auto-import', action='store_true',
                                help='Forward completed experience from this enrolled Hermes profile')
            linking = parser.add_mutually_exclusive_group()
            linking.add_argument('--preview-linked-roots', action='store_true',
                                 help='Preview new linked skill directories without writing configuration or enrollment')
            linking.add_argument('--approve-linked-root', action='append', default=[], metavar='PATH',
                                 help='Approve an absolute directory from the preview (repeatable)')
            parser.add_argument('--linked-roots-preview-sha256', metavar='DIGEST',
                                help='Match the preview_sha256 returned by --preview-linked-roots')
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
            journal_commands = {'enable', 'inventory', 'observe', 'status', 'sync-hermes', 'recover-hermes'}
            if not home.is_absolute() or args.command not in journal_commands:
                raise ValueError('This command requires an absolute Hermes profile path and a supported journal operation')
        if args.command == 'observe':
            inserted = observe(**json.load(sys.stdin), hermes_home=home)
            if home is not None:
                print(json.dumps({'inserted': inserted}))
            return 0
        if args.command == 'import-hermes':
            print(json.dumps(import_hermes(args.project, args.from_profile)))
            return 0
        if args.command == 'sync-hermes':
            if home is None or not args.session:
                raise ValueError('Hermes synchronization requires a profile and a session')
            print(json.dumps(import_hermes(args.project, home, completed_session=args.session, automatic=True)))
            return 0
        if args.command == 'recover-hermes':
            if home is None:
                raise ValueError('Recovery requires the Hermes profile that owns the session')
            payload = json.load(sys.stdin)
            print(json.dumps(recover_hermes(args.project, home, session=payload['session'], items=payload['items'])))
            return 0
        if args.command == 'ingest':
            if args.selection and not args.feedback:
                raise ValueError('--selection requires --feedback')
            changed = (notify_feedback(args.workspace, args.feedback, project=args.project, selection=args.selection) if args.feedback
                       else notify_eval(args.workspace, project=args.project))
            print(json.dumps({'new_evidence': changed}))
            return 0
        if args.command == 'enable':
            print(json.dumps(enable_learning(args, home), ensure_ascii=False))
            return 0
        config = config_for(args.project, hermes_home=home)
        if not config:
            raise ValueError('Project learning is not enabled')
        if home is not None and 'sessions' not in config['sources']:
            raise ValueError('Hermes requires sessions in the learning sources')
        if args.command == 'inventory':
            versions = source_inventory(config, hermes=home is not None)
            print(json.dumps({'root': str(config['root']), 'versions': versions,
                              'auto_import': config['auto_import']} if home else versions,
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
                    'scope': config['scope'],
                    'selection': config['selection'],
                    'skills': sorted(config['skills']),
                    'messages': db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
                    'model_steps': db.execute('SELECT COALESCE(SUM(steps),0) FROM model_steps').fetchone()[0],
                    **({'auto_import': config['auto_import']} if home is not None else {}),
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
