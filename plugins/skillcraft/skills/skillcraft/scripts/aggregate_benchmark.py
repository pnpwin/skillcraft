#!/usr/bin/env python3
"""Aggregate explicit configuration roles and real run metrics."""
import argparse
from collections import Counter
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.utils import read_json, write_json, digest
from scripts.grading_store import load_selection, read_grading, grading_signature
from scripts.reporting import resource_summary, concise, print_report


def stats(values):
    values = [v for v in values if v is not None]
    return {'n': len(values), 'mean': statistics.mean(values) if values else None,
            'stddev': statistics.stdev(values) if len(values) > 1 else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}


def aggregate(workspace, *, selection=None, output=None):
    workspace = Path(workspace).resolve()
    selected = load_selection(workspace, selection)
    if selected and (output is None or Path(output).resolve().is_relative_to(workspace)):
        raise ValueError('Selected grading reports require an output directory outside the source workspace')
    report_root = Path(output).resolve() if output else workspace
    if output and (report_root / 'benchmark.json').exists():
        raise ValueError('Report output already exists; choose a new report directory')
    manifest = read_json(workspace / 'manifest.json')
    summary, rows = {}, []
    errors = 0
    for configuration in manifest['configurations']:
        runs = []
        for p in sorted(workspace.glob(f'eval-*/{configuration}/run-*/run.json')):
            run = read_json(p)
            parent = p.parent
            if run.get('harness') == 'hermes' and run.get('status') == 'completed':
                from scripts.hermes_runner import verify_saved_run
                try:
                    verify_saved_run(parent, run)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    run.update(status='artifact_error', error=str(error))
            key = parent.relative_to(workspace).as_posix()
            spec = selected['runs'][key] if selected else {'selected': 'legacy', 'history': []}
            grading, grading_root, provenance = read_grading(parent, spec['selected'], allow_incomplete=selected is None)
            usage = read_json(parent / 'usage.json') if (parent / 'usage.json').exists() else {}
            timing = read_json(parent / 'timing.json') if (parent / 'timing.json').exists() else {}
            executor = usage.get('executor') or {}
            judge_usage = read_json(grading_root / 'judge/usage.json') if (grading_root / 'judge/usage.json').exists() else {}
            attempts = set(spec.get('history', [])) | {spec['selected']}
            if (parent / 'grading.json').exists():
                attempts.add('legacy')
            attempt_usage, attempt_cost, seen_attempts = [], [], set()
            for item in attempts:
                _, folder, attempt_manifest = read_grading(parent, item, allow_incomplete=True)
                if folder in seen_attempts or (not attempt_manifest and not (folder / 'grading.json').exists()):
                    continue
                seen_attempts.add(folder)
                attempt_usage.append((read_json(folder / 'judge/usage.json').get('executor') or {}).get('total_tokens')
                                     if (folder / 'judge/usage.json').exists() else None)
                attempt_cost.append((read_json(folder / 'judge/usage.json').get('executor') or {}).get('cost')
                                    if (folder / 'judge/usage.json').exists() else None)
            maintenance = usage.get('maintenance') or {}
            critique = grading.get('eval_feedback') or {}
            quality_complete = (run.get('status') == 'completed' and grading.get('status') == 'completed' and
                                (not critique.get('required') or critique.get('status') == 'completed'))
            row = {'run': str(parent.relative_to(workspace)), 'configuration': configuration,
                    'run_id': run.get('run_id'), 'included': spec.get('included', True), 'exclusion_reason': spec.get('reason'),
                    'execution_hash': provenance['execution']['sha256'] if provenance else None,
                    'grading_attempt_id': provenance.get('attempt_id') if provenance else 'legacy',
                    'rubric_hash': provenance.get('rubric_hash') if provenance else None,
                    'grading_signature': grading_signature(provenance),
                    'judge_attempts': len(seen_attempts),
                    'all_judge_tokens': sum(attempt_usage) if attempt_usage and all(x is not None for x in attempt_usage) else None,
                    'all_judge_cost': sum(attempt_cost) if attempt_cost and all(x is not None for x in attempt_cost) else None,
                   'model': run.get('model'), 'response_models': run.get('response_models'), 'agent': run.get('agent'), 'fixtures': run.get('fixtures'),
                   'critique_status': critique.get('status', 'not_run'),
                    'status': run['status'], 'grading_status': grading.get('status', 'missing'),
                    'error': run.get('error'), 'grading_error': grading.get('error'), 'failure_stage': grading.get('failure_stage'),
                    'pass_rate': (grading.get('summary') or {}).get('pass_rate') if quality_complete else None,
                    'quality_complete': quality_complete, 'grading_coverage': grading.get('coverage'),
                    'pending_expectations': grading.get('pending_expectations', []),
                    'partial_summary': grading.get('partial_summary'),
                   'time_seconds': timing.get('total_duration_seconds'), 'tokens': executor.get('total_tokens'),
                   'cost': executor.get('cost'), 'maintenance_tokens': maintenance.get('total_tokens'),
                    'judge_tokens': (judge_usage.get('executor') or {}).get('total_tokens'),
                    'judge_cost': (judge_usage.get('executor') or {}).get('cost'), 'maintenance_cost': maintenance.get('cost'),
                   'output_chars': sum(len(f.read_text(errors='replace')) for f in (parent / 'outputs').glob('*.txt'))}
            if run['status'] != 'completed' or grading.get('status') != 'completed' or (critique.get('required') and critique.get('status') != 'completed'):
                errors += 1
            if row['included']:
                runs.append(row)
            rows.append(row)
        summary[configuration] = {key: stats(r[key] for r in runs) for key in ['pass_rate', 'time_seconds', 'tokens', 'cost', 'maintenance_tokens', 'judge_tokens']}
        summary[configuration].update(runs=len(runs), statuses=dict(Counter(r['status'] for r in runs)),
            evaluation_completion_rate=sum(r['status'] == 'completed' and r['quality_complete'] for r in runs) / len(runs) if runs else None,
            grading_statuses=dict(Counter(r['grading_status'] for r in runs)),
            completion_rate=sum(r['status'] == 'completed' for r in runs) / len(runs) if runs else None)
    deltas = {}
    for baseline in ['without_skill', 'previous_skill']:
        if baseline not in summary or 'with_skill' not in summary:
            continue
        paired = {key: [] for key in ['pass_rate', 'time_seconds', 'tokens']}
        excluded = []
        lookup = {r['run'].replace('/' + baseline + '/', '/with_skill/'): r for r in rows if r['configuration'] == baseline and r['included']}
        for r in rows:
            b = lookup.get(r['run']) if r['configuration'] == 'with_skill' and r['included'] else None
            if not b:
                if r['configuration'] == 'with_skill' and r['included']:
                    excluded.append(r['run'] + ': matching baseline missing or excluded')
                continue
            compatible = (manifest.get('environment_consistent') is not False and
                          all(b[k] == r[k] for k in ['model', 'agent', 'fixtures']) and
                          b['status'] == r['status'] == 'completed')
            if not compatible:
                excluded.append(r['run'])
                continue
            for key in paired:
                if key == 'pass_rate' and (b['grading_signature'] != r['grading_signature'] or (selected and not b['rubric_hash'])):
                    excluded.append(r['run'] + ': incompatible/unknown grading contract')
                    continue
                if b[key] is not None and r[key] is not None:
                    paired[key].append(r[key] - b[key])
        deltas[baseline] = {**{f'paired_{key}': stats(values) for key, values in paired.items()},
                            'excluded_pairs': excluded,
                            'direction': 'with_skill minus baseline; positive time/tokens means more consumption'}
    actual = {r['run'] for r in rows}
    expected = {f'eval-{e["id"]}/{c}/run-{i:03d}' for e in manifest.get('evals', [])
                for c in manifest['configurations'] for i in range(1, manifest.get('repeats', 0) + 1)}
    missing = sorted(expected - actual)
    for configuration, group in summary.items():
        source_planned = len([p for p in expected if p.split('/')[1] == configuration])
        planned = len([p for p in expected if p.split('/')[1] == configuration and
                       (not selected or selected['runs'].get(p, {}).get('included', True))])
        completed = group['statuses'].get('completed', 0)
        group.update(planned_runs=planned, missing_runs=len([p for p in missing if p.split('/')[1] == configuration]),
                      source_planned_runs=source_planned, excluded_runs=sum(r['configuration'] == configuration and not r['included'] for r in rows),
                     planned_completion_rate=completed / planned if planned else None)
    validation_errors = []
    queue = manifest.get('queue') or {}
    queue_jobs = queue.get('jobs', {})
    not_started = [key for key in missing if queue_jobs.get(key, {}).get('status') == 'not_started_after_stop']
    unexpected_missing = [key for key in missing if key not in not_started]
    if queue.get('stop'):
        validation_errors.append('Dispatch stopped: ' + str(queue['stop'].get('reason')))
    if missing:
        validation_errors.append('Experiment is incomplete: planned runs are missing')
    if manifest.get('environment_consistent') is False:
        validation_errors.append('Environment drift detected between configurations')
    if rows and not any(r['included'] for r in rows):
        validation_errors.append('No runs selected for summary')
    if any('incompatible/unknown grading contract' in x for d in deltas.values() for x in d['excluded_pairs']):
        validation_errors.append('Selected comparison contains incompatible or unknown grading contracts (rubric/judge/profile/renderer)')
    result = {'schema_version': 1, 'metadata': manifest, 'run_summary': summary, 'deltas': deltas,
              'runs': rows, 'errors': errors + len(missing), 'missing_runs': missing, 'validation_errors': validation_errors,
              'compliance': manifest.get('compliance', {'agent_skills': {'status': 'NOT_RUN'}, 'opencode': {'status': 'NOT_RUN'}, 'accepted': False})}
    result['grading_selection'] = selected
    result['provenance'] = {'source_workspace': str(workspace), 'source_manifest_sha256': digest(workspace / 'manifest.json'),
                            'candidate_hash': manifest.get('candidate_hash'), 'profile_hash': manifest.get('profile_hash'),
                            'judge_profile_hash': manifest.get('judge_profile_hash'), 'fixture_hashes': manifest.get('fixture_hashes'),
                            'composition': 'explicit selection' if selected else 'all planned runs'}
    result['resources'] = {'source_executions': resource_summary(rows),
                           'selected_executions': resource_summary(r for r in rows if r['included'])}
    result['pilot'] = manifest.get('pilot_summary')
    result['scheduling'] = {'planned': len(expected), 'started': sum(s.get('status') in {'started', 'completed', 'error'} for s in queue_jobs.values()),
                             'pipeline_completed': sum(s.get('status') == 'completed' for s in queue_jobs.values()),
                             'not_started_after_stop': not_started, 'missing_unexpected': unexpected_missing, 'stop': queue.get('stop')}
    write_json(report_root / 'benchmark.json', result)
    (report_root / 'summary.md').write_text('# Evaluation summary\n\n```text\n' + concise(result, report_root / 'benchmark.json') + '\n```\n', encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('workspace', type=Path)
    p.add_argument('--selection', type=Path)
    p.add_argument('--output', type=Path, help='Separate report output root; source remains unchanged')
    p.add_argument('--json', action='store_true', help='Print the full machine-readable report instead of the concise summary')
    a = p.parse_args()
    try:
        report = aggregate(a.workspace, selection=a.selection, output=a.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Aggregation failed: {error}', file=sys.stderr)
        return 1
    print_report(report, (a.output or a.workspace) / 'benchmark.json', as_json=a.json)
    return int(bool(report['errors'] or report['validation_errors']))


if __name__ == '__main__':
    sys.exit(main())
