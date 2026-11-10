#!/usr/bin/env python3
"""Repeated, paired quality evaluation using native skill attachments."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.runtime import add_runtime_args, run_model
from scripts.compliance import require_standard, assess
from scripts.utils import copy_tree, digest, load_evals, read_json, safe_id, snapshot_fixtures, tree_hash, write_json


def run_quality(skill, evals, workspace, model, *, repeats=3, workers=1, previous=None, timeout=180, profile=None, judge_model=None, judge_profile=None, historical_baseline=None):
    skill, workspace = Path(skill).resolve(), Path(workspace).resolve()
    if repeats < 1 or workers < 1:
        raise ValueError('repeats and workers must be positive')
    workspace.mkdir(parents=True, exist_ok=False)
    rows = load_evals(evals)
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
    candidate = workspace / 'snapshots' / 'candidate' / skill_id
    candidate_hash = copy_tree(skill, candidate, skill=True, readonly=True)
    write_json(workspace / 'compliance.json', assess(candidate))
    require_standard(candidate)
    configs = {'without_skill': None, 'with_skill': candidate}
    if previous:
        old = workspace / 'snapshots' / 'previous' / skill_id
        copy_tree(previous, old, skill=True, readonly=True)
        if historical_baseline is None:
            require_standard(old)
        else:
            from scripts.historical_baseline import verify
            write_json(workspace / 'historical-baseline.json', verify(old, historical_baseline))
        configs['previous_skill'] = old
    manifest = {'schema_version': 1, 'kind': 'quality', 'skill_id': skill_id, 'model': model,
                'judge_model': judge_model, 'repeats': repeats, 'workers': workers,
                'configurations': {k: {'role': k, 'snapshot_hash': tree_hash(v) if v else None} for k, v in configs.items()},
                'candidate_hash': candidate_hash, 'evals': rows, 'controlled_requested': profile is None,
                'fixture_hashes': fixture_hashes, 'profile_hash': digest(profile) if profile else None,
                'judge_profile_hash': digest(judge_profile) if judge_profile else None,
                'runtime': {'opencode': '2.2.6', 'client': '2.2.6', 'runner': '1.0.0'}, 'timeout_seconds': timeout}
    write_json(workspace / 'manifest.json', manifest)
    jobs = []
    for row in rows:
        metadata = {'eval_id': row['id'], **row}
        write_json(workspace / f'eval-{row["id"]}' / 'eval_metadata.json', metadata)
        # Input paths are skill-relative, matching the upstream eval contract.
        files = fixtures[row['id']]
        for config, snapshot in configs.items():
            for repeat in range(1, repeats + 1):
                target = workspace / f'eval-{row["id"]}' / config / f'run-{repeat:03d}'
                jobs.append((row, snapshot, files, target, config))
    def execute(job):
        row, snapshot, files, target, config = job
        result = run_model(row['prompt'], target, model, skill=snapshot, skill_id=skill_id,
                           attach=snapshot is not None, files=files, timeout=timeout, profile=profile)
        write_json(target / 'eval_metadata.json', {'eval_id': row['id'], **row, 'configuration': config,
                   'snapshot_hash': manifest['configurations'][config]['snapshot_hash']})
        from scripts.grade import grade_run
        grade_run(target, row, judge_model=judge_model, timeout=timeout, judge_profile=judge_profile)
        return result
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(execute, jobs))
    # Detect environmental drift across successful preflights.
    environments = [r['environment'] for r in results if r.get('environment')]
    signatures = {(str(e['background']), str(e['plugins']), e['configuration_fingerprint']) for e in environments}
    manifest['environment_consistent'] = len(signatures) <= 1
    manifest['controlled'] = (len(environments) == len(results) and bool(environments)
                               and all(e['controlled'] for e in environments) and len(signatures) == 1)
    manifest['compliance'] = {config: [assess(snapshot, job[3]) for job in jobs if job[4] == config]
                              for config, snapshot in configs.items() if snapshot}
    write_json(workspace / 'manifest.json', manifest)
    from scripts.aggregate_benchmark import aggregate
    report = aggregate(workspace)
    report['compliance'] = manifest['compliance']
    write_json(workspace / 'benchmark.json', report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--skill', type=Path, required=True)
    p.add_argument('--evals', type=Path, required=True)
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--previous', type=Path)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--judge-model')
    p.add_argument('--judge-profile', type=Path)
    add_runtime_args(p)
    args = p.parse_args()
    report = run_quality(args.skill, args.evals, args.workspace, args.model, repeats=args.repeats,
                        workers=args.workers, previous=args.previous, timeout=args.timeout,
                        profile=args.profile, judge_model=args.judge_model, judge_profile=args.judge_profile)
    print(__import__('json').dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['errors'] or report['validation_errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
