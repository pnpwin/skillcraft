#!/usr/bin/env python3
"""Aggregate explicit configuration roles and real run metrics."""
import argparse
from collections import Counter
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.utils import read_json, write_json


def stats(values):
    values = [v for v in values if v is not None]
    return {'n': len(values), 'mean': statistics.mean(values) if values else None,
            'stddev': statistics.stdev(values) if len(values) > 1 else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}


def aggregate(workspace):
    workspace = Path(workspace)
    manifest = read_json(workspace / 'manifest.json')
    summary, rows = {}, []
    errors = 0
    for configuration in manifest['configurations']:
        runs = []
        for p in sorted(workspace.glob(f'eval-*/{configuration}/run-*/run.json')):
            run = read_json(p)
            parent = p.parent
            grading = read_json(parent / 'grading.json') if (parent / 'grading.json').exists() else {}
            usage = read_json(parent / 'usage.json') if (parent / 'usage.json').exists() else {}
            timing = read_json(parent / 'timing.json') if (parent / 'timing.json').exists() else {}
            executor = usage.get('executor') or {}
            judge_usage = read_json(parent / 'judge' / 'usage.json') if (parent / 'judge' / 'usage.json').exists() else {}
            maintenance = usage.get('maintenance') or {}
            critique = grading.get('eval_feedback') or {}
            row = {'run': str(parent.relative_to(workspace)), 'configuration': configuration,
                   'model': run.get('model'), 'agent': run.get('agent'), 'fixtures': run.get('fixtures'),
                   'critique_status': critique.get('status', 'not_run'),
                   'status': run['status'], 'grading_status': grading.get('status', 'missing'),
                   'pass_rate': (grading.get('summary') or {}).get('pass_rate') if grading.get('status') == 'completed' else None,
                   'time_seconds': timing.get('total_duration_seconds'), 'tokens': executor.get('total_tokens'),
                   'cost': executor.get('cost'), 'maintenance_tokens': maintenance.get('total_tokens'),
                   'judge_tokens': (judge_usage.get('executor') or {}).get('total_tokens'),
                   'output_chars': sum(len(f.read_text(errors='replace')) for f in (parent / 'outputs').glob('*.txt'))}
            if run['status'] != 'completed' or grading.get('status') != 'completed' or (critique.get('required') and critique.get('status') != 'completed'):
                errors += 1
            runs.append(row)
            rows.append(row)
        summary[configuration] = {key: stats(r[key] for r in runs) for key in ['pass_rate', 'time_seconds', 'tokens', 'cost', 'maintenance_tokens', 'judge_tokens']}
        summary[configuration].update(runs=len(runs), statuses=dict(Counter(r['status'] for r in runs)),
            grading_statuses=dict(Counter(r['grading_status'] for r in runs)),
            completion_rate=sum(r['status'] == 'completed' for r in runs) / len(runs) if runs else None)
    deltas = {}
    for baseline in ['without_skill', 'previous_skill']:
        if baseline not in summary or 'with_skill' not in summary:
            continue
        paired = {key: [] for key in ['pass_rate', 'time_seconds', 'tokens']}
        excluded = []
        lookup = {r['run'].replace('/' + baseline + '/', '/with_skill/'): r for r in rows if r['configuration'] == baseline}
        for r in rows:
            b = lookup.get(r['run']) if r['configuration'] == 'with_skill' else None
            if not b:
                continue
            compatible = (manifest.get('environment_consistent') is not False and
                          all(b[k] == r[k] for k in ['model', 'agent', 'fixtures']) and
                          b['status'] == r['status'] == 'completed')
            if not compatible:
                excluded.append(r['run'])
                continue
            for key in paired:
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
        planned = len([p for p in expected if p.split('/')[1] == configuration])
        completed = group['statuses'].get('completed', 0)
        group.update(planned_runs=planned, missing_runs=len([p for p in missing if p.split('/')[1] == configuration]),
                     planned_completion_rate=completed / planned if planned else None)
    validation_errors = []
    if missing:
        validation_errors.append('Experiment is incomplete: planned runs are missing')
    if manifest.get('environment_consistent') is False:
        validation_errors.append('Environment drift detected between configurations')
    result = {'schema_version': 1, 'metadata': manifest, 'run_summary': summary, 'deltas': deltas,
              'runs': rows, 'errors': errors + len(missing), 'missing_runs': missing, 'validation_errors': validation_errors,
              'compliance': manifest.get('compliance', {'agent_skills': {'status': 'NOT_RUN'}, 'opencode': {'status': 'NOT_RUN'}, 'accepted': False})}
    write_json(workspace / 'benchmark.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('workspace', type=Path)
    a = p.parse_args()
    try:
        report = aggregate(a.workspace)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Aggregation failed: {error}', file=sys.stderr)
        return 1
    print(__import__('json').dumps(report, indent=2, ensure_ascii=False))
    return int(bool(report['errors'] or report['validation_errors']))


if __name__ == '__main__':
    sys.exit(main())
