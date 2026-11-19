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


def config_for(project, *, enrolling=False):
    if os.environ.get(INTERNAL) == '1':
        return None
    path = Path(project).resolve()
    for root in (path, *path.parents):
        file = root / CONFIG
        if not file.is_file():
            continue
        if file.is_symlink() or file.parent.is_symlink():
            raise ValueError('Learning config must be a regular project file')
        raw = read_json(file)
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
        state = Path.home() / '.local/share/skillcraft/learning' / sha256(str(root).encode()).hexdigest()
        fingerprint = digest(file)
        marker = state / 'enabled'
        if not enrolling and (not marker.is_file() or marker.read_text().strip() != fingerprint):
            return None
        return {'root': root, 'state': state, 'skills': skills, 'sources': sources, 'fingerprint': fingerprint}
    return None


@contextmanager
def connection(config):
    state = config['state']
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(state / 'journal.sqlite', timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
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


def source_inventory(config):
    versions = []
    if 'sessions' not in config['sources']:
        return versions
    for settings in config['skills'].values():
        try:
            source = local_path(config['root'], settings['source'], directory=True)
            markdown = source / 'SKILL.md'
            versions.append({'path': str(markdown), 'hash': digest(markdown), 'snapshot_hash': tree_hash(source)})
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


def observe(project, harness, session, turn, role, text, loaded, *, target_turn=None, diagnostic=None):
    config = config_for(project)
    if not config or 'sessions' not in config['sources']:
        return False
    if harness not in {'codex', 'opencode'} or role not in {'user', 'assistant'}:
        raise ValueError('Invalid message origin')
    if not all(isinstance(x, str) and x for x in (session, turn)) or not isinstance(text, str):
        raise ValueError('Messages need durable session and turn identities')
    labels = binding(config, loaded)
    case_key = prompt_key(text) if role == 'user' else None
    excerpt = sanitize(text)[:500] if role == 'assistant' and labels else None
    with connection(config) as db:
        inserted = db.execute(
            'INSERT OR IGNORE INTO messages(harness,session,turn,role,bindings,diagnostic,case_key,excerpt) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (harness, session, turn, role, json.dumps(labels), sanitize(diagnostic) if diagnostic else None,
             case_key, excerpt),
        ).rowcount
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
        if inserted:
            count = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            if count % MESSAGE_INTERVAL == 0:
                queue(db, f'messages:{count}')
        pending = db.execute('SELECT 1 FROM checkpoints WHERE status != \'done\' LIMIT 1').fetchone()
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


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
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
    ingest = sub.add_parser('ingest', help='Import a completed A/B or an explicit feedback file')
    ingest.add_argument('workspace', type=Path)
    ingest.add_argument('--project', type=Path)
    ingest.add_argument('--feedback', type=Path)
    ingest.add_argument('--selection', type=Path, help='Grading selection used for the reviewed report')
    args = cli.parse_args(argv)
    try:
        if args.command == 'observe':
            observe(**json.load(sys.stdin))
            return 0
        if args.command == 'ingest':
            if args.selection and not args.feedback:
                raise ValueError('--selection requires --feedback')
            changed = (notify_feedback(args.workspace, args.feedback, project=args.project, selection=args.selection) if args.feedback
                       else notify_eval(args.workspace, project=args.project))
            print(json.dumps({'new_evidence': changed}))
            return 0
        config = config_for(args.project, enrolling=args.command == 'enable')
        if not config:
            raise ValueError('Project learning is not enabled')
        if args.command == 'enable':
            if args.project.resolve() != config['root']:
                raise ValueError('Enable learning from the project root')
            config['state'].mkdir(parents=True, exist_ok=True, mode=0o700)
            from tempfile import NamedTemporaryFile
            with NamedTemporaryFile('w', dir=config['state'], delete=False) as marker:
                marker.write(config['fingerprint'] + '\n')
            Path(marker.name).replace(config['state'] / 'enabled')
            print(json.dumps({'enabled': str(config['root']), 'journal': str(config['state'])}))
        elif args.command == 'inventory':
            print(json.dumps(source_inventory(config), ensure_ascii=False))
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
