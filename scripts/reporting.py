"""Bounded human CLI output; full machine evidence remains in benchmark.json."""
from collections import Counter
import json
from pathlib import Path


def resource_totals(values):
    values = list(values)
    known = [v for v in values if v is not None]
    return {'known_sum': sum(known) if known else None, 'known_count': len(known),
            'unknown_count': len(values) - len(known),
            'total': sum(known) if values and len(known) == len(values) else None}


def resource_summary(rows):
    rows = list(rows)
    return {key: resource_totals(r.get(key) for r in rows) for key in
            ['tokens', 'cost', 'judge_tokens', 'judge_cost', 'all_judge_tokens', 'all_judge_cost',
             'maintenance_tokens', 'maintenance_cost']}


def concise(report, benchmark_path):
    rows = report.get('runs', [])
    included = [r for r in rows if r.get('included', True)]
    metadata = report.get('metadata', {})
    complete = not report.get('errors') and not report.get('validation_errors')
    lines = [('COMPLETE' if complete else 'INCOMPLETE') + ' — execution/grading pipeline; quality verdicts are separate.',
             f"Cases: {len(metadata.get('evals', []))}; repeats: {metadata.get('repeats', 'unknown')}; source runs: {len(rows)}; selected: {len(included)}.",
             f"Executor: {dict(Counter(r.get('status', 'unknown') for r in rows))}",
             f"Grading: {dict(Counter(r.get('grading_status', 'missing') for r in rows))}",
             f"Critique: {dict(Counter(r.get('critique_status', 'not_run') for r in rows))}",
             f"Errors: {report.get('errors', 0)}; missing planned runs: {len(report.get('missing_runs', []))}."]
    for name, group in list(report.get('run_summary', {}).items())[:8]:
        score = (group.get('pass_rate') or {}).get('mean')
        shown = 'unknown' if score is None else f'{score:.1%}'
        lines.append(f"{name}: {group.get('runs', 0)} selected runs; mean graded pass rate {shown}.")
    for error in report.get('validation_errors', [])[:5]:
        lines.append('Validation: ' + str(error).replace('\n', ' ')[:220])
    failures = [r for r in rows if r.get('status') != 'completed' or not r.get('quality_complete')]
    for row in failures[:5]:
        reason = row.get('error') or row.get('grading_error') or row.get('failure_stage') or 'see grading/critique evidence'
        lines.append('Failure ' + row['run'] + ': ' + str(reason).replace('\n', ' ')[:180])
    if len(failures) > 5:
        lines.append(f'{len(failures)-5} further failures are in the full report.')
    selection = report.get('grading_selection')
    lines.append('Selection: ' + (selection['rule'].replace('\n', ' ')[:180] if selection else 'all planned runs; no score-based filtering'))
    if report.get('pilot'):
        lines.append('Pilot: ' + str(report['pilot'].get('status', 'unknown')) + '; costs recorded separately.')
    path = Path(benchmark_path).resolve()
    lines.extend([f'Benchmark: {path}', f'Summary: {path.parent / "summary.md"}',
                  'Scope: unique cases, repeated observations and rubric coverage limit generalization; unknown usage remains null.'])
    return '\n'.join(lines)


def print_report(report, benchmark_path, *, as_json=False):
    print(json.dumps(report, ensure_ascii=False, indent=2) if as_json else concise(report, benchmark_path))
