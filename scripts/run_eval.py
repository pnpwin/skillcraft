#!/usr/bin/env python3
"""Natural triggering: advertise a native skill and observe successful tool loads."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.runtime import add_runtime_args, run_model
from scripts.compliance import require_standard, assess
from scripts.utils import copy_tree, digest, load_evals, read_json, safe_id, snapshot_fixtures, write_json


def summarize(results):
    counts = dict(tp=0, fp=0, tn=0, fn=0)
    valid = [r for r in results if r['status'] == 'completed' and r['triggered'] is not None]
    for row in valid:
        counts[('t' if row['triggered'] == row['should_trigger'] else 'f') + ('p' if row['triggered'] else 'n')] += 1
    tp, fp, fn = counts['tp'], counts['fp'], counts['fn']
    return {**counts, 'total': len(results), 'valid': len(valid), 'errors': len(results) - len(valid),
            'error_rate': (len(results) - len(valid)) / len(results) if results else None,
            'precision': tp / (tp + fp) if tp + fp else None,
            'recall': tp / (tp + fn) if tp + fn else None,
            'accuracy': (counts['tp'] + counts['tn']) / len(valid) if valid else None}


def run_eval(skill, evals, workspace, model, *, repeats=3, workers=1, timeout=180, profile=None, historical_baseline=None, judge_model=None, judge_profile=None):
    skill, workspace = Path(skill).resolve(), Path(workspace).resolve()
    if repeats < 1 or workers < 1:
        raise ValueError('repeats and workers must be positive')
    rows = load_evals(evals, trigger=True) if isinstance(evals, (str, Path)) else evals
    workspace.mkdir(parents=True, exist_ok=False)
    fixtures, fixture_hashes = snapshot_fixtures(rows, skill, workspace)
    if profile:
        fixed_profile = workspace / 'snapshots' / 'profile.json'
        write_json(fixed_profile, read_json(profile))
        profile = fixed_profile
    if judge_profile:
        fixed_judge_profile = workspace / 'snapshots' / 'judge-profile.json'
        write_json(fixed_judge_profile, read_json(judge_profile))
        judge_profile = fixed_judge_profile
    skill_id = safe_id(skill.name)
    snapshot = workspace / 'snapshots' / skill_id
    snapshot_hash = copy_tree(skill, snapshot, skill=True, readonly=True)
    write_json(workspace / 'compliance.json', assess(snapshot))
    if historical_baseline is None:
        require_standard(snapshot)
    else:
        from scripts.historical_baseline import verify
        write_json(workspace / 'historical-baseline.json', verify(snapshot, historical_baseline))
    write_json(workspace / 'manifest.json', {'schema_version': 1, 'kind': 'trigger', 'skill_id': skill_id,
               'snapshot_hash': snapshot_hash, 'model': model, 'evals': rows, 'repeats': repeats, 'timeout_seconds': timeout,
               'fixture_hashes': fixture_hashes, 'profile_hash': digest(profile) if profile else None,
               'judge_model': judge_model, 'judge_profile_hash': digest(judge_profile) if judge_profile else None})
    jobs = [(row, repeat) for row in rows for repeat in range(1, repeats + 1)]
    def execute(job):
        row, repeat = job
        target = workspace / f'eval-{row["id"]}' / 'natural' / f'run-{repeat:03d}'
        result = run_model(row['prompt'], target, model,
                           mode='trigger', skill=snapshot, skill_id=skill_id, timeout=timeout, profile=profile,
                           files=fixtures[row['id']], preparation=row.get('preparation'))
        from scripts.trigger_diagnostics import diagnose
        diagnostics = diagnose(target, result, skill_id)
        write_json(target / 'eval_metadata.json', {**row, 'eval_id': row['id'], 'snapshot_hash': snapshot_hash})
        if judge_model and row.get('expectations'):
            from scripts.grade import grade_run
            grade = grade_run(target, row, judge_model=judge_model, judge_profile=judge_profile, timeout=timeout)
            diagnostics['quality_status'] = 'completed' if grade['evaluation_complete'] else 'incomplete'
            if grade['evaluation_complete'] and grade['summary']['total']:
                diagnostics['task_correct'] = grade['summary']['failed'] == 0
        write_json(target / 'trigger-diagnostics.json', diagnostics)
        return {'eval_id': row['id'], 'group': row.get('group', row['id']), 'category': row.get('category', 'unspecified'),
                'capability': row.get('capability', 'unspecified'), 'repeat': repeat, 'prompt': row['prompt'],
                'should_trigger': row['should_trigger'], 'diagnostics': diagnostics, **result}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(execute, jobs))
    environments = [r['environment'] for r in results if r.get('environment')]
    signatures = {(str(e['background']), str(e['plugins']), e['configuration_fingerprint']) for e in environments}
    summary = summarize(results)
    summary.update(unique_cases=len(rows), unique_groups=len({row.get('group', row['id']) for row in rows}), repeats=repeats,
                   observed_loads=sum(r['diagnostics']['skill_loaded'] for r in results),
                   loads_with_terminal_error=sum(r['diagnostics']['skill_loaded'] and r['status'] != 'completed' for r in results),
                   scoped_cli_after_load=sum(r['diagnostics']['scoped_cli_after_load'] is True for r in results),
                   task_correct=sum(r['diagnostics']['task_correct'] is True for r in results),
                   loaded_and_correct=sum(r['diagnostics']['skill_loaded'] and r['diagnostics']['task_correct'] is True for r in results),
                   loaded_cli_and_correct=sum(r['diagnostics']['scoped_cli_after_load'] is True and r['diagnostics']['task_correct'] is True for r in results),
                   quality_unknown=sum(r['diagnostics']['task_correct'] is None for r in results),
                   quality_errors=sum(r['diagnostics']['quality_status'] == 'incomplete' for r in results),
                   selection_denominator='completed executions with known successful-load observation; errors excluded and separately counted')
    summary['environment_consistent'] = len(signatures) <= 1
    summary['controlled'] = (len(environments) == len(results) and bool(environments)
                             and all(e['controlled'] for e in environments) and len(signatures) == 1)
    report = {'schema_version': 1, 'summary': summary, 'results': results,
              'compliance': assess(snapshot)}
    if historical_baseline is not None:
        report['role'] = 'immutable_oversized_historical_baseline'
        report['eligible_for_promotion'] = False
    write_json(workspace / 'trigger-results.json', report)
    from scripts.generate_report import generate_html
    (workspace / 'report.html').write_text(generate_html(report), encoding='utf-8')
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--skill', type=Path, required=True)
    p.add_argument('--evals', type=Path, required=True)
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--judge-model', help='Optional independent task grading, separate from trigger counts')
    p.add_argument('--judge-profile', type=Path)
    add_runtime_args(p)
    a = p.parse_args()
    report = run_eval(a.skill, a.evals, a.workspace, a.model, repeats=a.repeats, workers=a.workers, timeout=a.timeout, profile=a.profile,
                      judge_model=a.judge_model, judge_profile=a.judge_profile)
    print(__import__('json').dumps(report['summary'], indent=2))
    return 1 if report['summary']['errors'] or report['summary']['quality_errors'] or not report['summary']['environment_consistent'] else 0


if __name__ == '__main__':
    sys.exit(main())
