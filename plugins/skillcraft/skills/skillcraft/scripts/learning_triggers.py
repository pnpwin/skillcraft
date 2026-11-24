"""Shared natural selection gate for scheduled skill publication."""
from hashlib import sha256
import json
from pathlib import Path

from scripts.utils import read_json, tree_hash, write_json


def qualify(skill, holdout, attempt, harnesses, *, repeats, timeout, runner):
    from scripts.compliance import runner_hash
    from scripts.run_eval import run_eval
    from scripts.runtime import require_model_consent

    skill = Path(skill)
    positive = {key: value for key, value in holdout[0].items()
                if key in {'prompt', 'files', 'preparation'}}
    positive.update(id='heldout-positive', should_trigger=True)
    negatives = {}
    for case in holdout:
        prompts = case.get('trigger_negatives', [])
        if not isinstance(prompts, list) or any(not isinstance(prompt, str) or not prompt.strip() for prompt in prompts):
            raise ValueError('Holdout trigger_negatives must be a list of nonempty prompts')
        negatives.update(dict.fromkeys(prompt.strip() for prompt in prompts))
    if not negatives:
        repeats = 1
        topic = skill.name.replace('-', ' ')
        negatives[(f"For a glossary, explain the ordinary meaning of the words '{topic}' in one sentence. "
                   'Give only a definition. No project-specific convention, tool use, or deliverable is needed.')] = None
    probes = [positive, *({'id': f'heldout-close-negative-{index}', 'prompt': prompt, 'should_trigger': False}
                         for index, prompt in enumerate(negatives, start=1))]
    document = attempt / 'trigger-evals.json'
    write_json(document, {'evals': probes})

    candidate = tree_hash(skill)
    reports = {}
    for harness, settings in harnesses.items():
        key = sha256(json.dumps([candidate, probes, repeats, settings['model'], timeout, runner_hash(harness)],
                                sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        workspace = attempt / f'trigger-{harness}-{key}'
        result = workspace / 'trigger-results.json'
        html = workspace / 'report.html'
        if result.is_file() and html.is_file():
            evaluation = read_json(result)
        else:
            require_model_consent()
            with runner(harness, settings):
                evaluation = run_eval(skill, document, workspace, settings['model'],
                                      repeats=repeats, timeout=timeout, harness=harness)
        summary = evaluation.get('summary', {})
        if (summary.get('controlled') is not True or summary.get('errors') != 0 or
                summary.get('fp') != 0 or summary.get('fn') != 0 or
                summary.get('tp') != repeats or summary.get('tn') != len(negatives) * repeats or
                summary.get('valid') != len(probes) * repeats or not html.is_file()):
            return {'trigger_validation': 'failed', 'trigger_harness': harness,
                    'trigger_counts': {key: summary[key] for key in ('tp', 'fp', 'tn', 'fn', 'valid', 'errors')
                                       if key in summary},
                    'trigger_report': str(html) if html.is_file() else None,
                    'trigger_reports': reports,
                    'reason': f'Native {harness} trigger positive/negative gate failed'}
        reports[harness] = str(html)
    return {'trigger_validation': 'passed', 'trigger_reports': reports}
