#!/usr/bin/env python3
"""Repeated, paired quality evaluation using native skill attachments."""
import argparse
import os
import sqlite3
import subprocess
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.runtime import add_runtime_args, run_model, profile_settings
from scripts.harness import harness_metadata, model_ref, parse_harness_args
from scripts.compliance import require_standard, assess
from scripts.utils import copy_tree, digest, load_evals, read_json, safe_id, snapshot_fixtures, tree_hash, write_json
from scripts.reporting import print_report


def run_quality(skill, evals, workspace, model, *, repeats=3, workers=1, previous=None, timeout=180, profile=None, judge_model=None, judge_profile=None, historical_baseline=None, pilot_ids=None, stop_threshold=2, harness='opencode'):
    skill, workspace = Path(skill).resolve(), Path(workspace).resolve()
    model_ref(model, harness)
    if harness == 'hermes' and (judge_model or judge_profile):
        raise ValueError('Hermes file evaluation uses deterministic graders')
    if judge_model:
        model_ref(judge_model, harness)
    if repeats < 1 or workers < 1 or stop_threshold < 1:
        raise ValueError('repeats, workers and stop_threshold must be positive')
    workspace.mkdir(parents=True, exist_ok=False)
    rows = load_evals(evals)
    pilot_ids = list(dict.fromkeys(pilot_ids or [rows[0]['id']]))
    if not set(pilot_ids) <= {row['id'] for row in rows}:
        raise ValueError('Unknown pilot case ID')
    profile_settings(profile, safe_id(skill.name), harness=harness)
    profile_settings(judge_profile, harness=harness)
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
    write_json(workspace / 'compliance.json', assess(candidate, harness=harness))
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
    selection = harness_metadata(harness)
    runtime = selection['harness']
    manifest = {'schema_version': 1, 'kind': 'quality', 'skill_id': skill_id, **selection, 'model': model,
                'judge_model': judge_model, 'repeats': repeats, 'workers': workers,
                'configurations': {k: {'role': k, 'snapshot_hash': tree_hash(v) if v else None} for k, v in configs.items()},
                'candidate_hash': candidate_hash, 'evals': rows, 'controlled_requested': profile is None,
                'learning_internal': os.environ.get('SKILLCRAFT_LEARNING_INTERNAL') == '1',
                'fixture_hashes': fixture_hashes, 'profile_hash': digest(profile) if profile else None,
                'judge_profile_hash': digest(judge_profile) if judge_profile else None,
                'runtime': {runtime: None}, 'timeout_seconds': timeout,
                'pilot_policy': {'case_ids': pilot_ids, 'selection': 'explicit' if pilot_ids != [rows[0]['id']] else 'first case or explicitly selected first',
                                 'configurations': list(configs), 'stop_threshold': stop_threshold, 'max_in_flight': workers}}
    write_json(workspace / 'manifest.json', manifest)
    def make_jobs(root, cases, count):
        jobs = []
        for row in cases:
            write_json(root / f'eval-{row["id"]}' / 'eval_metadata.json', {'eval_id': row['id'], **row})
            for config, snapshot in configs.items():
                for repeat in range(1, count + 1):
                    key = f'eval-{row["id"]}/{config}/run-{repeat:03d}'
                    jobs.append((key, (row, snapshot, fixtures[row['id']], root / key, config)))
        return jobs

    def execute(job):
        row, snapshot, files, target, config = job
        stage, result = 'executor', {}
        try:
            result = run_model(row['prompt'], target, model, skill=snapshot, skill_id=skill_id,
                               attach=snapshot is not None, files=files, timeout=timeout, profile=profile,
                               preparation=row.get('preparation'), harness=harness)
            write_json(target / 'eval_metadata.json', {'eval_id': row['id'], **row, 'configuration': config,
                       'snapshot_hash': manifest['configurations'][config]['snapshot_hash']})
            if harness == 'hermes' and result['status'] != 'completed':
                return {'complete': False, 'run': result, 'stage': 'executor',
                        'status': result['status'], 'error': result.get('error')}
            stage = 'grading'
            from scripts.grade import grade_run
            grade = grade_run(target, row, judge_model=judge_model, timeout=timeout, judge_profile=judge_profile, harness=harness)
            complete = result['status'] == 'completed' and grade['evaluation_complete']
            return {'complete': complete, 'run': result,
                    'stage': 'executor' if result['status'] != 'completed' else grade.get('failure_stage', 'grading'),
                    'status': result['status'] if result['status'] != 'completed' else grade['eval_feedback']['status'] if grade['status'] == 'completed' and not complete else grade['status'],
                    'error': result.get('error') or grade.get('error') or grade['eval_feedback'].get('reason')}
        except Exception as error:
            write_json(target / 'orchestration-error.json', {'stage': stage, 'error': str(error)})
            if not (target / 'run.json').exists():
                write_json(target / 'run.json', {'status': 'orchestration_error', 'error': str(error)})
            return {'complete': False, 'run': result, 'stage': stage, 'status': type(error).__name__, 'error': str(error)}

    from scripts.aggregate_benchmark import aggregate
    from scripts.scheduling import bounded_dispatch

    def phase(root, phase_manifest, jobs, threshold):
        def persist(queue):
            phase_manifest['queue'] = queue
            write_json(root / 'manifest.json', phase_manifest)
        outcomes, queue = bounded_dispatch(jobs, execute, workers=workers, threshold=threshold, persist=persist)
        results = [o.get('run', {}) for o in outcomes.values()]
        environments = [r['environment'] for r in results if r.get('environment')]
        signatures = {(e.get('server_version'), str(e['background']), str(e['plugins']),
                       e['configuration_fingerprint']) for e in environments}
        versions = sorted({e['server_version'] for e in environments if e.get('server_version')})
        phase_manifest['runtime'] = {runtime: versions[0] if len(versions) == 1 else None}
        phase_manifest['runtime_versions'] = versions
        phase_manifest['environment_consistent'] = len(signatures) <= 1
        phase_manifest['controlled'] = (len(environments) == len(results) and bool(environments)
                                       and all(e['controlled'] for e in environments) and len(signatures) == 1)
        persist(queue)
        try:
            report = aggregate(root)
        except (OSError, ValueError, KeyError, TypeError) as error:
            phase_manifest['report_error'] = {'stage': 'report', 'error': str(error)}
            write_json(root / 'manifest.json', phase_manifest)
            report = {'errors': 1, 'validation_errors': [str(error)], 'resources': None}
        return report

    pilot_root = workspace / 'pilot'
    pilot_rows = [row for row in rows if row['id'] in pilot_ids]
    pilot_manifest = {**manifest, 'phase': 'pilot', 'repeats': 1, 'evals': pilot_rows}
    pilot_report = phase(pilot_root, pilot_manifest, make_jobs(pilot_root, pilot_rows, 1), 1)
    pilot_ok = not pilot_report['errors'] and not pilot_report['validation_errors']
    review_error = pilot_manifest.get('report_error')
    if pilot_ok:
        review = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/generate_review.py'),
                                 str(pilot_root), '--static', str(pilot_root / 'review.html')], capture_output=True, text=True)
        (pilot_root / 'review.log').write_text(review.stdout + review.stderr)
        if review.returncode or not (pilot_root / 'review.html').is_file():
            pilot_ok = False
            review_error = 'Pilot review generation failed; see pilot/review.log'
    manifest['pilot_summary'] = {'status': 'completed' if pilot_ok else 'error', 'root': str(pilot_root),
                                 'resources': pilot_report['resources'], 'error': review_error,
                                 'queue': pilot_manifest['queue'], 'repair': None if pilot_ok else
                                 'Repair the recorded failing stage and rerun in a new workspace; main repeats were not dispatched.'}
    jobs = make_jobs(workspace, rows, repeats)
    if pilot_ok:
        phase(workspace, manifest, jobs, stop_threshold)
    else:
        manifest['queue'] = {'jobs': {key: {'status': 'not_started_after_stop'} for key, _ in jobs},
                             'stop': {'stage': 'pilot', 'reason': review_error or 'Pilot pipeline incomplete'}}
    manifest['compliance'] = {config: [assess(snapshot, job[3], harness=harness) for _, job in jobs
                                      if job[4] == config and (job[3] / 'run.json').exists()]
                              for config, snapshot in configs.items() if snapshot}
    write_json(workspace / 'manifest.json', manifest)
    report = aggregate(workspace)
    report['compliance'] = manifest['compliance']
    write_json(workspace / 'benchmark.json', report)
    if not manifest['learning_internal'] and runtime != 'hermes':
        try:
            from scripts.learning import notify_eval
            notify_eval(workspace)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
            print(f'SkillCraft experience import skipped: {type(error).__name__}', file=sys.stderr)
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
    p.add_argument('--json', action='store_true', help='Print full JSON; default output is a concise summary')
    p.add_argument('--pilot-case', action='append', help='Representative case ID; repeat for distinct capabilities. Default: first case')
    p.add_argument('--stop-threshold', type=int, default=2, help='Stop dispatch after this many matching infrastructure errors')
    add_runtime_args(p)
    args = parse_harness_args(p)
    report = run_quality(args.skill, args.evals, args.workspace, args.model, repeats=args.repeats,
                        workers=args.workers, previous=args.previous, timeout=args.timeout,
                         profile=args.profile, judge_model=args.judge_model, judge_profile=args.judge_profile,
                         pilot_ids=args.pilot_case, stop_threshold=args.stop_threshold, harness=args.harness)
    print_report(report, args.workspace / 'benchmark.json', as_json=args.json)
    return 1 if report['errors'] or report['validation_errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
