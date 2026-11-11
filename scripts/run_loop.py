#!/usr/bin/env python3
"""Train → validation selection → one final test → quality regression."""
import argparse
from difflib import SequenceMatcher
import json
from pathlib import Path
import random
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.improve_description import improve_description, training_evidence
from scripts.compliance import require_standard, assess
from scripts.run_eval import run_eval
from scripts.run_quality_eval import run_quality
from scripts.inputs import serial_inputs, input_specs
from scripts.utils import copy_tree, load_evals, parse_skill_md, read_json, set_description, snapshot_fixtures, tree_hash, write_json


def split_evals(rows, seed=42):
    # Connected components cover explicit paraphrase groups, exact and near duplicates.
    parents = list(range(len(rows)))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    normalized = [re.sub(r'\W+', ' ', r['prompt'].casefold()).strip() for r in rows]
    for i in range(len(rows)):
        for j in range(i):
            explicit = rows[i].get('group') is not None and rows[i].get('group') == rows[j].get('group')
            if explicit or SequenceMatcher(None, normalized[i], normalized[j]).ratio() >= .90:
                parents[find(i)] = find(j)
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(find(i), []).append(row)
    buckets = {True: [], False: []}
    for group in groups.values():
        labels = {r['should_trigger'] for r in group}
        if len(labels) != 1:
            raise ValueError('Duplicate/paraphrase group has conflicting labels')
        buckets[group[0]['should_trigger']].append(group)
    rng = random.Random(seed)
    split = {'train': [], 'validation': [], 'test': []}
    for label, groups in buckets.items():
        rng.shuffle(groups)
        if len(groups) < 2:
            raise ValueError(f'Need at least two independent groups for label {label}')
        split['validation'].extend(groups.pop())
        if len(groups) >= 2:
            split['test'].extend(groups.pop())
        split['train'].extend(r for group in groups for r in group)
    # A one-class holdout is not an independent classification test.
    if len({r['should_trigger'] for r in split['test']}) != 2:
        split['train'].extend(split['test'])
        split['test'] = []
    return split


def selection_score(report):
    summary = report['summary']
    if summary['errors'] or not summary['valid'] or summary.get('environment_consistent') is False:
        return None
    c = summary
    positive = c['tp'] / (c['tp'] + c['fn']) if c['tp'] + c['fn'] else 0
    negative = c['tn'] / (c['tn'] + c['fp']) if c['tn'] + c['fp'] else 0
    return (positive + negative) / 2


def optimize(a):
    workspace, skill = a.workspace.resolve(), a.skill.resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    if a.iterations < 1:
        raise ValueError('iterations must be positive')
    rows = load_evals(a.evals, trigger=True)
    frozen, _ = snapshot_fixtures(rows, skill, workspace)
    for row in rows:
        row['files'] = serial_inputs(frozen[row['id']])
    if a.profile:
        profile = workspace / 'snapshots' / 'profile.json'
        write_json(profile, read_json(a.profile))
        a.profile = profile
    for role in ['judge', 'optimizer']:
        original = getattr(a, f'{role}_profile', None)
        if original:
            frozen_profile = workspace / 'snapshots' / f'{role}-profile.json'
            write_json(frozen_profile, read_json(original))
            setattr(a, f'{role}_profile', frozen_profile)
    splits = split_evals(rows, a.seed)
    write_json(workspace / 'split.json', {'seed': a.seed, 'sets': splits, 'independent_test': bool(splits['test'])})
    history = {'schema_version': 1, 'executor_model': a.model, 'optimizer_model': a.optimizer_model,
               'selection_metric': 'balanced_accuracy', 'iterations': [], 'best': None,
               'repeats': a.repeats,
               'independent_test': bool(splits['test'])}
    current = workspace / 'candidates' / 'v000' / skill.name
    copy_tree(skill, current, skill=True)
    write_json(workspace / 'compliance.json', assess(current))
    require_standard(current)
    best, best_score = None, -1
    training_history = []
    rationale = 'Original candidate before optimization'
    for iteration in range(a.iterations):
        standard = require_standard(current)
        iteration_dir = workspace / f'iteration-{iteration:03d}'
        train = run_eval(current, splits['train'], iteration_dir / 'train', a.model, repeats=a.repeats, workers=a.workers, timeout=a.timeout, profile=a.profile)
        validation = run_eval(current, splits['validation'], iteration_dir / 'validation', a.model, repeats=a.repeats, workers=a.workers, timeout=a.timeout, profile=a.profile)
        score = selection_score(validation)
        if score is not None and score > best_score:
            best, best_score = current, score
        description = parse_skill_md(current)[1]
        history['iterations'].append({'iteration': iteration, 'candidate': str(current.relative_to(workspace)),
                                     'description': description, 'rationale': rationale,
                                     'snapshot_hash': tree_hash(current), 'train': train['summary'],
                                     'train_results': train['results'],
                                     'validation_results': validation['results'],
                                      'validation': validation['summary'], 'score': score})
        history['iterations'][-1]['agent_skills'] = standard
        training_history.append({'candidate_id': f'v{iteration:03d}', 'snapshot_hash': tree_hash(current),
                                 'description': description, 'rationale': rationale,
                                 'training_results': training_evidence(train)})
        write_json(workspace / 'training-history.json', training_history)
        history['best'] = str(best.relative_to(workspace)) if best else None
        write_json(workspace / 'history.json', history)
        from scripts.generate_report import generate_html
        (workspace / 'report.html').write_text(generate_html(history), encoding='utf-8')
        if iteration + 1 < a.iterations:
            if train['summary']['errors']:
                raise ValueError('Training errors must be resolved before description optimization')
            proposal = improve_description(current, train, iteration_dir / 'optimizer', a.optimizer_model, a.timeout,
                                           history=training_history, profile=getattr(a, 'optimizer_profile', None))
            following = workspace / 'candidates' / f'v{iteration + 1:03d}' / skill.name
            copy_tree(current, following, skill=True)
            set_description(following, proposal['description'])
            require_standard(following)
            current = following
            rationale = proposal['rationale']
    if best is None:
        raise ValueError('No valid validation result; no candidate selected')
    selected = workspace / 'selected' / skill.name
    copy_tree(best, selected, skill=True, readonly=True)
    require_standard(selected)
    if splits['test']:
        final = run_eval(selected, splits['test'], workspace / 'final-test', a.model, repeats=a.repeats, workers=a.workers, timeout=a.timeout, profile=a.profile)
        history['final_test'] = final['summary']
        history['final_test_results'] = final['results']
    else:
        history['final_test'] = None
        history['test_limitation'] = 'Insufficient independent groups; no final test'
    # Use the original skill as file-resolution base for quality fixtures.
    if a.quality_evals:
        rows = load_evals(a.quality_evals)
        for row in rows:
            row['files'] = input_specs(row.get('files', []), base=skill)
        resolved = workspace / 'quality-evals.json'
        write_json(resolved, {'evals': rows})
        quality = run_quality(selected, resolved, workspace / 'quality-regression', a.model,
                              repeats=a.repeats, workers=a.workers, previous=skill, timeout=a.timeout,
                              profile=a.profile, judge_model=a.judge_model, judge_profile=getattr(a, 'judge_profile', None))
        history['quality_regression'] = {'errors': quality['errors'], 'validation_errors': quality['validation_errors'], 'deltas': quality['deltas'], 'run_summary': quality['run_summary']}
    else:
        history['quality_regression'] = None
    history['promotion_ready'] = False
    runs = sorted((workspace / 'quality-regression').glob('eval-*/with_skill/run-*'))
    history['compliance'] = [assess(selected, p) for p in runs] if runs else [assess(selected)]
    history['promotion_note'] = 'Review final test, quality regression and human feedback before installing selected/.'
    write_json(workspace / 'history.json', history)
    from scripts.generate_report import generate_html
    (workspace / 'report.html').write_text(generate_html(history), encoding='utf-8')
    return history


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--skill', type=Path, required=True)
    p.add_argument('--evals', type=Path, required=True)
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--optimizer-model', required=True)
    p.add_argument('--judge-model')
    p.add_argument('--quality-evals', type=Path, required=True)
    p.add_argument('--profile', type=Path)
    p.add_argument('--judge-profile', type=Path)
    p.add_argument('--optimizer-profile', type=Path)
    p.add_argument('--iterations', type=int, default=3)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--timeout', type=float, default=180)
    p.add_argument('--seed', type=int, default=42)
    a = p.parse_args()
    history = optimize(a)
    print(json.dumps(history, ensure_ascii=False, indent=2))
    sys.exit(1 if (history.get('final_test') or {}).get('errors') or (history.get('quality_regression') or {}).get('errors') or (history.get('quality_regression') or {}).get('validation_errors') else 0)
