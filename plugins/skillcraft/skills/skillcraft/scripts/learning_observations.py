"""Store bounded, completed-turn observations separately from version-bound evidence."""
from hashlib import sha256
import json
import re
import stat
from pathlib import Path, PurePosixPath

from scripts.learning import CASE_TAG, classify, prompt_key, sanitize


SCHEMA = """
CREATE TABLE IF NOT EXISTS observed_turns (
    harness TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
    prompt TEXT NOT NULL, answer TEXT NOT NULL,
    PRIMARY KEY(harness, session, turn)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY, witness TEXT NOT NULL UNIQUE,
    harness TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
    kind TEXT NOT NULL, topic TEXT NOT NULL, skill TEXT, version TEXT,
    independent TEXT NOT NULL, excerpt TEXT NOT NULL, case_id TEXT,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS protected_session_cases (
    skill TEXT NOT NULL, version TEXT NOT NULL, family TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(skill, version, family)
);
CREATE TABLE IF NOT EXISTS artifact_proofs (
    id INTEGER PRIMARY KEY, witness TEXT NOT NULL UNIQUE, provenance TEXT NOT NULL,
    topic TEXT NOT NULL, case_id TEXT NOT NULL, input_hash TEXT NOT NULL,
    artifacts TEXT NOT NULL, passed INTEGER NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_turn_snapshots (
    harness TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
    topic TEXT NOT NULL, case_id TEXT NOT NULL, dataset_sha256 TEXT NOT NULL,
    outputs TEXT NOT NULL, config_sha256 TEXT NOT NULL,
    PRIMARY KEY(harness, session, turn)
);
"""

TOPIC_TAG = re.compile(r'\[skillcraft-topic:([a-z0-9]+(?:-[a-z0-9]+)*)\]', re.I)
TEACH = re.compile(r'^(?:как надо(?: делать)?\s*:|правильный способ\s*:|'
                   r'делай так\s*:|how to do it\s*:|the correct way is\b)', re.I)
REFLECT_TEACH = re.compile(
    r'^(?:в следующий раз|всегда|на будущее|впредь|запомни(?:те)?|'
    r'(?:сохрани|закрепи)(?:те)?\s+(?:это\s+)?(?:правило|как\s+правило|на\s+будущее)|'
    r'next time|from now on|always|for future|remember|save\s+(?:this\s+)?as\s+a\s+rule)\b',
    re.I,
)
ARTIFACT = re.compile(r'\b([a-z0-9][a-z0-9_-]{2,35})\.(csv|json|md|txt|tsv)\b', re.I)
WORD = re.compile(r'[^\W\d_]{3,}', re.UNICODE)
STOP = set('the and for from this that with please create generate write file output there '
           'для это как мне надо пожалуйста сделай напиши создай файл получи'.split())
CHECK_TAG = re.compile(r'^\[skillcraft-check:(\{[^\n]{2,500}\})\]$', re.M)
SAFE_FILE = re.compile(r'[a-zA-Z0-9_-]{1,64}\.(?:csv|json|md|txt|tsv)')
TOOL_NAME = re.compile(r'^[a-zA-Z0-9_.:-]{1,96}$')
OUTCOMES = {'ok', 'error', 'unknown', 'timeout', 'denied', 'unavailable'}
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


def topic_for(prompt, feedback=''):
    """Prefer the operator's topic ID. Untagged prompts get a conservative grouping key."""
    tagged = TOPIC_TAG.search(feedback) or TOPIC_TAG.search(prompt)
    if tagged and len(tagged[1]) <= 64:
        return tagged[1].lower()
    output = re.search(r'\boutputs/([\w./-]+)', prompt, re.I)
    artifact = (ARTIFACT.search(output[1]) if output else None) or ARTIFACT.search(prompt)
    if artifact:
        return re.sub(r'[-_]+', '-', artifact[1].lower())
    # Multiline inputs and examples after a colon are often the variable part
    # of a recurring request.
    intent = re.split(r'[:\n]', prompt, maxsplit=1)[0]
    words = sorted({word.casefold() for word in WORD.findall(intent)} - STOP)
    if len(words) < 3:
        return ''
    # Preserve a human-readable hint for ASCII topics and disambiguate exact word sets.
    hint = '-'.join(word for word in words if word.isascii())[:31].rstrip('-') or 'workflow'
    return f'{hint}-{sha256(" ".join(words).encode()).hexdigest()[:10]}'


def observation_kind(text, config):
    automation = config.get('automation') or {}
    teaching = (config.get('automation') and TEACH.match(text.strip())
                or automation.get('reflect') and REFLECT_TEACH.match(text.strip()))
    return classify(text) or ('correction' if teaching else None)


def retained_text(text):
    return sanitize(text)[:700]


def declared_check(feedback):
    match = CHECK_TAG.search(feedback)
    if not match:
        return None
    try:
        check = json.loads(match[1])
    except ValueError:
        return None
    if (not isinstance(check, dict) or set(check) != {'file', 'contains'}
            or not isinstance(check['file'], str) or not SAFE_FILE.fullmatch(check['file'])
            or not isinstance(check['contains'], str) or not 0 < len(check['contains']) <= 200
            or '[redacted]' in check['contains'] or sanitize(check['contains']) != check['contains']):
        return None
    return {'type': 'contains', 'text': 'The output contains the user-confirmed literal',
            'file': check['file'], 'value': check['contains']}


def derived_cases(observations):
    """Make file checks only from explicit, literal user expectations on distinct prompts."""
    by_family = {}
    ranked = sorted(observations, key=lambda row: (
        {'correction': 0, 'success': 1, 'failure': 2}.get(row['kind'], 3), row['id']))
    for row in ranked:
        if row['kind'] not in {'success', 'failure', 'correction'} or row['independent'] in by_family:
            continue
        details = json.loads(row['details'])
        proof = details.get('proof')
        if isinstance(proof, dict) and isinstance(details.get('case'), dict):
            case = details['case']
            prompt = case.get('prompt')
            checks = case.get('checks')
            if not isinstance(prompt, str) or not isinstance(checks, list) or not checks:
                continue
            prompt = CASE_TAG.sub('', TOPIC_TAG.sub('', prompt)).strip()
            if not prompt:
                continue
            by_family[row['independent']] = {
                'id': f"session-{row['id']}", 'group': row['independent'], 'prompt': prompt,
                'expectations': case.get('expectations', []), 'checks': checks,
                'files': case.get('files', []),
                'session_fixture_hashes': proof.get('session_fixture_hashes', {}),
                'protect': row['kind'] == 'success',
            }
            continue
        check = details.get('check')
        prompt = details.get('prompt')
        if not check or not isinstance(prompt, str) or '[redacted]' in prompt:
            continue
        prompt = CASE_TAG.sub('', TOPIC_TAG.sub('', prompt)).strip()
        if not prompt:
            continue
        by_family[row['independent']] = {
            'id': f'session-{row["id"]}', 'group': row['independent'], 'prompt': prompt,
            'expectations': [check['text']], 'checks': [check], 'protect': row['kind'] == 'success',
        }
    cases = list(by_family.values())
    if len(cases) < 2:
        return cases, []
    return cases[:-1], cases[-1:]


def save_protected_cases(db, skill, version, cases):
    """Preserve verified success checks and their frozen fixture hashes for the new version."""
    db.executemany(
        'INSERT OR IGNORE INTO protected_session_cases VALUES(?,?,?,?)',
        ((skill, version, case['group'], json.dumps(case, ensure_ascii=False))
         for case in cases if case.get('protect') and case.get('group')
         and (not case.get('files') or case.get('session_fixture_hashes'))),
    )


def protected_session_cases(db, skill, version):
    rows = db.execute('SELECT payload FROM protected_session_cases WHERE skill=? AND version=? ORDER BY rowid',
                      (skill, version))
    return [json.loads(row['payload']) for row in rows]


def write_observation(db, harness, session, turn, event, kind, topic, independent,
                      *, bindings=None, excerpt='', case_id=None, details=None):
    skill, version = next(iter(bindings.items())) if bindings and len(bindings) == 1 else (None, None)
    return db.execute(
        'INSERT OR IGNORE INTO observations(witness,harness,session,turn,kind,topic,skill,version,'
        'independent,excerpt,case_id,details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (json.dumps([harness, session, turn, event, kind]), harness, session, turn, kind, topic,
         skill, version, independent, excerpt, case_id, json.dumps(details or {}, ensure_ascii=False)),
    ).rowcount


def save_completed_turn(db, harness, session, turn, answer, bindings, tool_events, config=None):
    user = db.execute('SELECT excerpt FROM messages WHERE harness=? AND session=? AND turn=? AND role=?',
                      (harness, session, turn, 'user')).fetchone()
    if not user or user['excerpt'] is None:
        return
    prompt = user['excerpt']
    db.execute('INSERT OR IGNORE INTO observed_turns VALUES (?,?,?,?,?)',
               (harness, session, turn, prompt, retained_text(answer)))
    if not isinstance(tool_events, list):
        return
    observed = []
    for event in tool_events[:128]:
        if not isinstance(event, dict):
            continue
        event_id, tool, status = (event.get(field) for field in ('id', 'tool', 'status'))
        if (not isinstance(event_id, str) or not event_id or len(event_id) > 256
                or not isinstance(tool, str) or not TOOL_NAME.fullmatch(tool)):
            continue
        if not isinstance(status, str) or status not in OUTCOMES:
            status = 'unknown'
        write_observation(
            db, harness, session, turn, sha256(event_id.encode()).hexdigest(), 'tool',
            topic_for(prompt), f'prompt:{prompt_key(prompt)}', bindings=bindings,
            details={'tool': tool, 'status': status},
        )
        observed.append(event)
    if observed:
        verify_artifact_turn(db, harness, session, turn, prompt, bindings, observed, config=config)


def verify_saved_artifact(db, config, harness, session, turn, *, case_id=None):
    """Manual CLI path: verify one already captured completed turn against current outputs."""
    row = db.execute('SELECT prompt FROM observed_turns WHERE harness=? AND session=? AND turn=?',
                     (harness, session, turn)).fetchone()
    if row is None:
        return {'verified': False, 'reason': 'completed_turn_not_found'}
    message = db.execute("SELECT bindings FROM messages WHERE harness=? AND session=? AND turn=? "
                         "AND role='assistant'", (harness, session, turn)).fetchone()
    bindings = json.loads(message['bindings']) if message else {}
    events = []
    for event in db.execute("SELECT witness,details FROM observations WHERE harness=? AND session=? "
                            "AND turn=? AND kind='tool' ORDER BY id", (harness, session, turn)):
        detail = json.loads(event['details'])
        try:
            witness = json.loads(event['witness'])
            event_id = witness[3] if isinstance(witness, list) and len(witness) > 3 else event['witness']
        except (TypeError, ValueError):
            event_id = event['witness']
        events.append({'id': event_id,
                       'tool': detail.get('tool', 'tool'), 'status': detail.get('status', 'unknown')})
    if not events:
        return {'verified': False, 'reason': 'completed_tool_event_not_found'}
    completion_proofs = [
        json.loads(artifact['details']).get('proof') or {}
        for artifact in db.execute("SELECT details FROM observations WHERE harness=? AND session=? AND turn=? "
                                   "AND json_type(details, '$.proof')='object'", (harness, session, turn))
    ]
    result = verify_artifact_turn(db, harness, session, turn, row['prompt'], bindings, events,
                                  config=config, case_id=case_id, event_ids_are_hashed=True,
                                  completion_proofs=completion_proofs)
    if result is None:
        return {'verified': False, 'reason': 'approved_case_or_artifact_checks_not_verified'}
    return {'verified': True, 'inserted': result['inserted'], 'kind': result['kind'],
            'case_id': result['case_id'], 'proof': result['proof']}


def _case_for_prompt(config, prompt, case_id=None, topic=None, *, dataset_sha256=None):
    """Return one exact owner-approved training case, never a holdout case."""
    from scripts.learning import dataset_snapshot, local_path

    automation = config.get('automation') or {}
    approved = (config.get('automation_inputs') or {}).get('cases') or {}
    tagged = case_id
    if not tagged and isinstance(prompt, str):
        tagged_match = CASE_TAG.search(prompt)
        tagged = tagged_match.group(1) if tagged_match else None
    matches = []
    for case_topic, splits in automation.get('cases', {}).items():
        if topic is not None and case_topic != topic:
            continue
        if 'train' not in splits:
            continue
        snapshot = approved.get(case_topic, {}).get('train')
        if not isinstance(snapshot, dict):
            continue
        if dataset_sha256 is not None and snapshot.get('sha256') != dataset_sha256:
            continue
        try:
            path = local_path(config['root'], splits['train'])
            raw, rows = dataset_snapshot(path)
        except (OSError, KeyError, TypeError, ValueError):
            continue
        if sha256(raw).hexdigest() != snapshot.get('sha256'):
            continue
        for row in rows:
            if tagged and row['id'] != tagged:
                continue
            if not tagged and (not isinstance(prompt, str) or row['prompt'].strip() != prompt.strip()):
                continue
            matches.append((case_topic, snapshot, row))
    return matches[0] if len(matches) == 1 else None


def _output_artifacts(root, checks):
    """Hash only regular, non-symlink files explicitly named by deterministic checks."""
    outputs = Path(root) / 'outputs'
    if outputs.is_symlink() or not outputs.is_dir():
        return None
    artifacts = {}
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('file'), str):
            return None
        raw = check['file']
        if not raw or '\\' in raw or '\0' in raw:
            return None
        relative = PurePosixPath(raw)
        if relative.is_absolute() or any(part in {'', '.', '..'} for part in raw.split('/')):
            return None
        path = outputs
        for part in relative.parts:
            path = path / part
            try:
                mode = path.lstat().st_mode
            except OSError:
                return None
            if stat.S_ISLNK(mode):
                return None
        if not stat.S_ISREG(mode):
            return None
        try:
            with path.open('rb') as handle:
                value = handle.read(MAX_ARTIFACT_BYTES + 1)
            if len(value) > MAX_ARTIFACT_BYTES:
                return None
        except OSError:
            return None
        artifacts[relative.as_posix()] = sha256(value).hexdigest()
    return artifacts or None


def _output_snapshot(root, checks):
    """Capture bounded output signatures, including files absent at prompt time."""
    outputs = Path(root) / 'outputs'
    if outputs.is_symlink():
        return None
    signatures = {}
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('file'), str):
            return None
        raw = check['file']
        if not raw or '\\' in raw or '\0' in raw:
            return None
        relative = PurePosixPath(raw)
        if relative.is_absolute() or any(part in {'', '.', '..'} for part in raw.split('/')):
            return None
        path = outputs
        missing = False
        for part in relative.parts:
            path = path / part
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                missing = True
                break
            except OSError:
                return None
            if stat.S_ISLNK(mode):
                return None
        if missing:
            signatures[relative.as_posix()] = None
            continue
        if not stat.S_ISREG(mode):
            return None
        try:
            metadata = path.stat()
            with path.open('rb') as handle:
                content = handle.read(MAX_ARTIFACT_BYTES + 1)
            if len(content) > MAX_ARTIFACT_BYTES or len(content) != metadata.st_size:
                return None
            content_hash = sha256(content).hexdigest()
        except OSError:
            return None
        signatures[relative.as_posix()] = {'sha256': content_hash, 'mtime_ns': metadata.st_mtime_ns,
                                           'size': metadata.st_size}
    return signatures or None


def save_artifact_snapshot(db, config, harness, session, turn, prompt):
    """Remember checked output state before a native turn can create artifacts."""
    config = _artifact_config(config)
    if config is None:
        return False
    match = _case_for_prompt(config, prompt)
    if not match:
        return False
    topic, snapshot, row = match
    checks = row.get('checks')
    if not isinstance(checks, list) or not checks:
        return False
    outputs = _output_snapshot(config['root'], checks)
    if outputs is None:
        return False
    db.execute('INSERT OR IGNORE INTO artifact_turn_snapshots VALUES (?,?,?,?,?,?,?,?)',
               (harness, session, turn, topic, row['id'], snapshot['sha256'],
                json.dumps(outputs, sort_keys=True), config['fingerprint']))
    return True


def _fresh_output_snapshot(config, baseline, verified):
    before = json.loads(baseline['outputs'])
    after = _output_snapshot(config['root'], verified['case'].get('checks', []))
    if not after or set(before) != set(after) or set(after) != set(verified['artifacts']):
        return False
    return all(after[name] is not None and after[name]['sha256'] == verified['artifacts'][name]
               and before[name] != after[name] for name in after)


def _artifact_config(config):
    """Resolve Hermes artifact approval only through its explicitly shared project enrollment."""
    if not config.get('hermes'):
        return config
    if config.get('auto_import') is not True:
        return None
    from scripts.learning import config_for
    shared = config_for(config['root'])
    if (not shared or shared.get('fingerprint') != config.get('fingerprint')
            or shared.get('automation') is None or shared.get('automation_inputs') is None):
        return None
    return shared


def _verified_case(config, prompt, case_id=None, topic=None, *, dataset_sha256=None):
    """Check approved fixture provenance and deterministic output checks locally."""
    from scripts.grade import check_one
    from scripts.inputs import input_specs
    from scripts.learning import sanitize

    config = _artifact_config(config)
    if config is None:
        return None
    match = _case_for_prompt(config, prompt, case_id, topic, dataset_sha256=dataset_sha256)
    if not match:
        return None
    topic, snapshot, row = match
    # The selected row came from an approved dataset hash. Check that row's
    # fixture bytes below without reopening the dataset or reading holdout material.
    checks = row.get('checks')
    if not isinstance(checks, list) or not checks:
        return None
    def contains_secret(value):
        if isinstance(value, str):
            return sanitize(value) != value
        if isinstance(value, list):
            return any(contains_secret(item) for item in value)
        if isinstance(value, dict):
            return any(contains_secret(item) for item in value.values())
        return False
    if contains_secret({key: row.get(key) for key in ('prompt', 'expectations', 'checks', 'files')}):
        return None
    try:
        specs = input_specs(row.get('files', []), config['root'])
        fixture_hashes = {}
        for spec in specs:
            source = Path(spec['source'])
            relative = source.relative_to(config['root']).as_posix()
            expected = snapshot.get('fixtures', {}).get(relative)
            if not expected or sha256(source.read_bytes()).hexdigest() != expected:
                return None
            fixture_hashes[relative] = expected
        artifacts = _output_artifacts(config['root'], checks)
        if not artifacts:
            return None
        grades = [check_one(Path(config['root']) / 'outputs', check) for check in checks]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    input_identity = {'topic': topic, 'case_id': row['id'], 'prompt': row['prompt'],
                      'expectations': row.get('expectations', []), 'checks': checks,
                      'fixtures': [
                          {'destination': item['destination'], 'role': item['role'],
                           'sha256': fixture_hashes[Path(item['source']).relative_to(config['root']).as_posix()]}
                          for item in specs
                      ]}
    input_hash = sha256(json.dumps(input_identity, sort_keys=True, ensure_ascii=False,
                                   separators=(',', ':')).encode()).hexdigest()
    proof_case = {key: row[key] for key in ('id', 'prompt', 'expectations', 'checks') if key in row}
    if specs:
        proof_case['files'] = [
            {'source': spec['source'], 'destination': spec['destination'], 'role': spec['role']}
            for spec in specs
        ]
    session_fixture_hashes = {spec['destination']: fixture_hashes[
        Path(spec['source']).relative_to(config['root']).as_posix()] for spec in specs}
    return {'topic': topic, 'case_id': row['id'], 'input_hash': input_hash,
            'artifacts': artifacts, 'passed': all(item['passed'] for item in grades),
            'check_results': [{'text': item['text'], 'passed': item['passed']} for item in grades],
            'case': proof_case, 'fixture_hashes': fixture_hashes,
            'session_fixture_hashes': session_fixture_hashes, 'dataset_sha256': snapshot['sha256']}


def verify_standalone_artifact(db, config, topic, case_id):
    """Manually verify an explicitly approved case without inventing session identity."""
    verified = _verified_case(config, None, case_id, topic)
    if not verified:
        return {'verified': False, 'reason': 'approved_case_or_artifact_checks_not_verified'}
    proof = {'provenance': 'manual', 'topic': topic, 'case_id': case_id,
             'input_hash': verified['input_hash'], 'artifacts': verified['artifacts'],
             'dataset_sha256': verified['dataset_sha256'],
             'session_fixture_hashes': verified['session_fixture_hashes'],
             'config_sha256': config['fingerprint']}
    identity = {key: value for key, value in proof.items()
                if key not in {'dataset_sha256', 'config_sha256'}}
    witness = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    details = {'proof': proof, 'checks': verified['check_results'],
               'fixtures': verified['fixture_hashes']}
    inserted = db.execute(
        'INSERT OR IGNORE INTO artifact_proofs(witness,provenance,topic,case_id,input_hash,artifacts,passed,details) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (witness, 'manual', topic, case_id, verified['input_hash'],
         json.dumps(verified['artifacts'], sort_keys=True), int(verified['passed']),
         json.dumps(details, ensure_ascii=False, sort_keys=True)),
    ).rowcount > 0
    return {'verified': True, 'inserted': inserted, 'provenance': 'manual', 'topic': topic,
            'case_id': case_id, 'passed': verified['passed'], 'proof': proof}


def verify_artifact_turn(db, harness, session, turn, prompt, bindings, tool_events,
                         config=None, case_id=None, event_ids_are_hashed=False, completion_proofs=None):
    """Record deterministic artifact proof for a completed, tool-observed turn."""
    if config is None or not tool_events or len(bindings) > 1:
        return None
    config = _artifact_config(config)
    if config is None:
        return None
    baseline = db.execute('SELECT * FROM artifact_turn_snapshots WHERE harness=? AND session=? AND turn=?',
                          (harness, session, turn)).fetchone()
    if (baseline is None or baseline['config_sha256'] != config['fingerprint']
            or (case_id is not None and case_id != baseline['case_id'])):
        return None
    # Only the full, live prompt can bind a case. The retained excerpt is metadata.
    verified = _verified_case(config, None, baseline['case_id'], baseline['topic'],
                              dataset_sha256=baseline['dataset_sha256'])
    if not verified:
        return None
    if completion_proofs is not None and not any(
        proof.get('input_hash') == verified['input_hash'] and proof.get('artifacts') == verified['artifacts']
        for proof in completion_proofs
    ):
        return None
    if not _fresh_output_snapshot(config, baseline, verified):
        return None
    skill, version = next(iter(bindings.items())) if bindings else (None, None)
    independent = f"artifact:{verified['topic']}:{verified['case_id']}:{verified['input_hash']}"
    proof = {'provenance': 'native', 'case_id': verified['case_id'], 'input_hash': verified['input_hash'],
             'artifacts': verified['artifacts'],
             'tool_event_hashes': sorted(item['id'] if event_ids_are_hashed
                                         else sha256(item['id'].encode()).hexdigest()
                                         for item in tool_events if isinstance(item.get('id'), str)),
             'session_fixture_hashes': verified['session_fixture_hashes'],
             'dataset_sha256': verified['dataset_sha256'],
             'config_sha256': config['fingerprint']}
    if skill is not None:
        proof.update(skill=skill, version=version)
    details = {'proof': proof, 'prompt': prompt, 'case': verified['case'],
               'checks': verified['check_results'], 'fixtures': verified['fixture_hashes']}
    if len(verified['case'].get('checks', [])) == 1:
        details['check'] = verified['case']['checks'][0]
    identity = {key: value for key, value in proof.items()
                if key not in {'dataset_sha256', 'config_sha256'}}
    proof_key = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    reported_kind = 'success' if verified['passed'] else 'failure'
    if verified['passed']:
        prior = db.execute("SELECT 1 FROM observations WHERE kind='failure' AND skill IS ? AND version IS ? "
                           "AND case_id=? AND json_extract(details,'$.proof.input_hash')=? LIMIT 1",
                           (skill, version, verified['case_id'], verified['input_hash'])).fetchone()
        if prior:
            reported_kind = 'correction'
    witness = f"artifact:{harness}:{session}:{turn}:{proof_key}:{reported_kind}"
    inserted = write_observation(db, harness, session, turn, proof_key + ':' + reported_kind,
                                 reported_kind, verified['topic'], independent, bindings=bindings,
                                 case_id=verified['case_id'], details=details)
    if skill is not None:
        from scripts.learning import record
        record(db, witness, skill, version, 'tool_check', harness, session, turn, independent,
               verified['case_id'], reported_kind, f"Deterministic artifact checks: {reported_kind}", True)
    return {'inserted': inserted, 'kind': reported_kind, 'case_id': verified['case_id'], 'proof': proof}


def save_feedback(db, harness, session, turn, target, text, kind, *, reflect=False):
    prior = db.execute(
        'SELECT t.prompt,t.answer,m.bindings FROM observed_turns t JOIN messages m '
        'ON m.harness=t.harness AND m.session=t.session AND m.turn=t.turn AND m.role=? '
        'WHERE t.harness=? AND t.session=? AND t.turn=?',
        ('assistant', harness, session, target),
    ).fetchone()
    if prior is None:
        return
    bindings = json.loads(prior['bindings'])
    # Competing bindings are ambiguous rather than evidence for a new skill.
    if len(bindings) > 1 and not reflect:
        return
    prompt = prior['prompt']
    topic = next(iter(bindings)) if len(bindings) == 1 else topic_for(prompt, text)
    if reflect and not topic and (identity := prompt_key(prompt)):
        topic = f'conversation-{identity[:16]}'
    case = CASE_TAG.search(text)
    failed_tools = set()
    recovered_tools = set()
    for event in db.execute(
        "SELECT turn,details FROM observations WHERE harness=? AND session=? "
        "AND turn IN (?,?) AND kind='tool' ORDER BY id", (harness, session, target, turn),
    ):
        detail = json.loads(event['details'])
        if event['turn'] == target and detail['status'] == 'error':
            failed_tools.add(detail['tool'])
        elif detail['status'] == 'ok' and detail['tool'] in failed_tools:
            recovered_tools.add(detail['tool'])
    details = {'prompt': prompt, 'answer': prior['answer'], 'review_turn': turn,
               'recovered_tools': sorted(recovered_tools)}
    if len(bindings) > 1:
        details['ambiguous_use'] = True
    if len(bindings) <= 1 and kind in {'success', 'correction'} and (check := declared_check(text)):
        details['check'] = check
    write_observation(
        db, harness, session, target, turn, kind, topic, f'prompt:{prompt_key(prompt)}',
        bindings=bindings, excerpt=retained_text(text), case_id=case[1] if case else None,
        details=details,
    )


def import_observations(db, source, session, config=None):
    """Copy completed observations from an enrolled Hermes profile to its shared scope."""
    rows = source.execute("""
        SELECT o.* FROM observations o
        JOIN observed_turns t ON t.harness=o.harness AND t.session=o.session AND t.turn=o.turn
        WHERE o.harness='hermes' AND (? IS NULL OR o.session=?) ORDER BY o.id
    """, (session, session))
    columns = ('witness', 'harness', 'session', 'turn', 'kind', 'topic', 'skill', 'version',
               'independent', 'excerpt', 'case_id', 'details')
    imported = 0
    for row in rows:
        try:
            detail = json.loads(row['details'] or '{}')
        except (TypeError, ValueError):
            continue
        proof = detail.get('proof') if isinstance(detail, dict) else None
        if isinstance(proof, dict):
            if (not config or proof.get('provenance') != 'native'
                    or proof.get('config_sha256') != config.get('fingerprint')
                    or not isinstance(proof.get('dataset_sha256'), str)
                    or row['kind'] not in {'success', 'failure', 'correction'}):
                continue
            approved_cases = (config.get('automation_inputs') or {}).get('cases', {})
            current_case = approved_cases.get(row['topic'], {}).get('train', {})
            if (proof.get('case_id') != row['case_id']
                    or proof.get('dataset_sha256') != current_case.get('sha256')):
                continue
            if row['skill'] is not None:
                if (proof.get('skill') != row['skill'] or proof.get('version') != row['version']
                        or row['skill'] not in config.get('skills', {})):
                    continue
                try:
                    from scripts.learning import skill_source
                    from scripts.utils import tree_hash
                    current = tree_hash(skill_source(config, config['skills'][row['skill']]))
                except (OSError, KeyError, TypeError, ValueError, RuntimeError):
                    continue
                if current != row['version']:
                    continue
            elif 'skill' in proof or 'version' in proof:
                continue
        inserted = db.execute(
            'INSERT OR IGNORE INTO observations(' + ','.join(columns) + ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            tuple(row[key] for key in columns),
        ).rowcount
        imported += inserted
        if isinstance(proof, dict) and row['skill'] is not None:
            from scripts.learning import record
            record(db, 'hermes-tool-check:' + row['witness'], row['skill'], row['version'],
                   'tool_check', row['harness'], row['session'], row['turn'], row['independent'],
                   row['case_id'], row['kind'], f"Imported deterministic artifact check: {row['kind']}", True)
    return imported
