"""Summarize persisted runner attempts for one automatic Learning window."""
from collections import defaultdict
from datetime import datetime
from html import escape
import json
from pathlib import Path
import sqlite3

METRICS = ('tokens', 'cost', 'cached_tokens', 'wall_time_seconds')
HARNESSES = {'codex', 'opencode', 'hermes'}


def resource_totals(values):
    values = list(values)
    known = [value for value in values if type(value) in {int, float}]
    return {
        'known_sum': sum(known) if known else None,
        'known_count': len(known),
        'unknown_count': len(values) - len(known),
        'total': sum(known) if values and len(known) == len(values) else None,
    }


def _inside_state(path, state):
    try:
        path = Path(path).resolve(strict=True)
        path.relative_to(state)
        return path
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _read(path, state):
    path = _inside_state(path, state)
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _usage(values):
    token_parts = values.get('tokens')
    token_parts = token_parts if isinstance(token_parts, dict) else {}
    cache = token_parts.get('cache')
    cache = cache if isinstance(cache, dict) else {}
    return {'tokens': values.get('total_tokens'),
            'cost': values.get('cost'),
            'cached_tokens': values.get('cached_tokens', values.get('cache_read_tokens', cache.get('read')))}


def _attempts(folder, state, phase, harness):
    run = _read(folder / 'run.json', state)
    request = _read(folder / 'request.json', state)
    usage = _read(folder / 'usage.json', state)
    if not (run or usage or request) or run.get('status') in {'not_started', 'skipped'}:
        return
    timing = _read(folder / 'timing.json', state)
    harness = run.get('harness') or request.get('harness') or harness
    harness = harness if isinstance(harness, str) and harness in HARNESSES else 'unknown'
    executor = usage.get('executor') or {}
    yield {'path': str(folder), 'stage': phase, 'harness': harness,
           'status': run.get('status', 'unknown'),
           'model_run': run.get('status') != 'preflight_error',
           **_usage(executor), 'wall_time_seconds': timing.get('total_duration_seconds')}
    maintenance = usage.get('maintenance')
    if isinstance(maintenance, dict) and maintenance:
        yield {'path': str(folder), 'stage': f'{phase}.maintenance', 'harness': harness,
               'status': 'unknown', **_usage(maintenance), 'wall_time_seconds': None}


def _eval_attempts(workspace, state, harness, *, trigger=False):
    for case in sorted(workspace.glob('eval-*')):
        for folder in sorted(case.glob('*/run-*')):
            if not folder.is_dir():
                continue
            phase = 'trigger' if trigger else 'main_ab'
            yield from _attempts(folder, state, phase, harness)
            yield from _attempts(folder / 'judge', state, f'{phase}.judge', harness)
    if not trigger and (workspace / 'pilot').is_dir():
        for sample in _eval_attempts(workspace / 'pilot', state, harness):
            yield {**sample, 'stage': sample['stage'].replace('main_ab', 'pilot', 1)}


def _revision_attempt(item, state, batch_id, db):
    if db is None:
        return None
    try:
        row = db.execute(
            'SELECT r.checkpoint, r.path FROM revisions r '
            'JOIN checkpoints c ON c.id=r.checkpoint '
            'WHERE c.witness=? AND r.skill=?',
            (f'automation:{batch_id}:{item["name"]}', item['name']),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    attempt = state / 'revisions' / str(row[0]) / item['name']
    persisted = _inside_state(row[1], state)
    if persisted is None or persisted != attempt or not persisted.is_dir():
        return None
    return attempt


def _item_attempts(item, state, batch_id, db=None):
    result = item.get('result') or {}
    result = result.get('candidate', result) if 'candidate' in result and 'status' in result else result
    if item['operation'] == 'create':
        attempt = state / 'batches' / str(batch_id) / item['name']
        yield from _attempts(attempt / 'optimizer', state, 'optimizer', None)
        for workspace in sorted(attempt.glob('trigger-*')):
            yield from _eval_attempts(workspace, state, workspace.name.removeprefix('trigger-'), trigger=True)
        for workspace in sorted(attempt.glob('verification-*')):
            yield from _eval_attempts(workspace, state, workspace.name.removeprefix('verification-').split('-')[0])
        return
    if result.get('report') is not None:
        report_path = _inside_state(result.get('report'), state)
        if report_path is None:
            return
        attempt = report_path.parent
    else:
        attempt = _revision_attempt(item, state, batch_id, db)
    if attempt is None:
        return
    yield from _attempts(attempt / 'optimizer', state, 'optimizer', None)
    for workspace in sorted(attempt.glob('ab-*')):
        yield from _eval_attempts(workspace, state, workspace.name.removeprefix('ab-'))


def _summary(samples):
    model = [row for row in samples if row.get('model_run') is not False]
    result = {metric: resource_totals(row.get(metric) for row in model) for metric in METRICS}
    result.update(model_runs=sum(row.get('model_run') is True for row in samples),
                  runner_attempts=sum('model_run' in row for row in samples),
                  failed_runs=sum(row['status'] not in {'completed', 'unknown'} for row in samples),
                  unknown_outcomes=sum(row['status'] == 'unknown' for row in samples))
    if not model:
        for metric in METRICS:
            result[metric].update(known_sum=0, total=0)
    return result


def _groups(samples, key):
    groups = defaultdict(list)
    for row in samples:
        groups[row[key]].append(row)
    return {name: _summary(rows) for name, rows in sorted(groups.items())}


def _elapsed(start, end):
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return None


def _format_metric(value):
    total = value['total']
    if total is not None:
        return str(round(total, 6))
    if value['known_count']:
        return f"{round(value['known_sum'], 6)} + unknown ({value['unknown_count']})"
    return f"unknown ({value['unknown_count']})"


def render_html(report):
    sections = []
    summary = report['summary']
    sections_by_title = [('Batch', {'Total': summary}), ('Stages', report['phases']),
                         ('Harnesses', report['harnesses'])]
    for item in report['candidates']:
        sections_by_title.append((f"{item['operation']} · {item['name']}",
                                  {'Total': item['summary'], **item['harnesses'], **item['phases']}))
    for title, rows in sections_by_title:
        table = []
        for name, row in rows.items():
            values = [name, row['model_runs'], row['runner_attempts'], row['failed_runs'], row['unknown_outcomes'],
                      *(_format_metric(row[metric]) for metric in METRICS)]
            table.append('<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in values) + '</tr>')
        sections.append(f'<h2>{escape(title)}</h2><table>'
                        '<thead><tr><th>Name</th><th>Model runs</th><th>Runner attempts</th><th>Failures</th><th>Unknown outcomes</th>'
                        '<th>Tokens</th><th>Reported cost</th><th>Cached input tokens</th><th>Run seconds</th>'
                        '</tr></thead><tbody>' + ''.join(table) + '</tbody></table>')
    return ('<!doctype html><meta charset="utf-8"><title>Learning resource report</title>'
            '<h1>Automatic Learning resources</h1>'
            f"<p>Batch elapsed seconds: {escape(str(report['elapsed_seconds']))}</p>"
            '<p>Cached tokens are shown separately. Missing provider measurements are unknown.</p>'
            + ''.join(sections))


def aggregate_batch_resources(config, batch_id, items, *, created_at=None, updated_at=None, db=None):
    state = Path(config['state']).resolve()
    all_samples = []
    candidates = []
    seen = set()
    for item in items:
        samples = []
        for row in _item_attempts(item, state, batch_id, db):
            identity = (row['path'], row['stage'])
            if identity not in seen:
                seen.add(identity)
                samples.append(row)
        all_samples.extend(samples)
        candidates.append({'name': item['name'], 'operation': item['operation'], 'summary': _summary(samples),
                           'phases': _groups(samples, 'stage'), 'harnesses': _groups(samples, 'harness')})
    report = {'schema_version': 1, 'summary': _summary(all_samples), 'phases': _groups(all_samples, 'stage'),
              'harnesses': _groups(all_samples, 'harness'), 'candidates': candidates,
              'elapsed_seconds': _elapsed(created_at, updated_at)}
    html_path = state / 'batches' / str(batch_id) / 'resource-report.html'
    html_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    html_path.write_text(render_html(report), encoding='utf-8')
    return {**report, 'report_html': str(html_path)}
