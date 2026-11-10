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


def run_eval(skill, evals, workspace, model, *, repeats=3, workers=1, timeout=180, profile=None, historical_baseline=None):
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
               'fixture_hashes': fixture_hashes, 'profile_hash': digest(profile) if profile else None})
    jobs = [(row, repeat) for row in rows for repeat in range(1, repeats + 1)]
    def execute(job):
        row, repeat = job
        result = run_model(row['prompt'], workspace / f'eval-{row["id"]}' / 'natural' / f'run-{repeat:03d}', model,
                           mode='trigger', skill=snapshot, skill_id=skill_id, timeout=timeout, profile=profile,
                           files=fixtures[row['id']])
        return {'eval_id': row['id'], 'repeat': repeat, 'prompt': row['prompt'], 'should_trigger': row['should_trigger'], **result}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(execute, jobs))
    environments = [r['environment'] for r in results if r.get('environment')]
    signatures = {(str(e['background']), str(e['plugins']), e['configuration_fingerprint']) for e in environments}
    summary = summarize(results)
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
    add_runtime_args(p)
    a = p.parse_args()
    report = run_eval(a.skill, a.evals, a.workspace, a.model, repeats=a.repeats, workers=a.workers, timeout=a.timeout, profile=a.profile)
    print(__import__('json').dumps(report['summary'], indent=2))
    return 1 if report['summary']['errors'] or not report['summary']['environment_consistent'] else 0


if __name__ == '__main__':
    sys.exit(main())
