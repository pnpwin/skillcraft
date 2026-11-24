"""Configuration and input approval for opt-in scheduled SkillCraft Learning."""
from hashlib import sha256
from pathlib import Path
import re

from scripts.learning import dataset_snapshot, local_path
from scripts.inputs import input_specs
from scripts.utils import digest

SKILL_NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


def parse_automation(raw):
    if 'automation' not in raw:
        return None
    value = raw['automation']
    defaults = raw.get('defaults', {})
    if not isinstance(value, dict) or not isinstance(defaults, dict):
        raise ValueError('automation must be an object')
    accepted = {'create', 'improve', 'auto_apply', 'reflect', 'interval_days', 'max_candidates_per_batch',
                'cases', 'harnesses'}
    if set(value) - accepted:
        raise ValueError('Unsupported automation settings')
    policy = {'create': False, 'improve': False, 'auto_apply': False, 'interval_days': 7,
              'max_candidates_per_batch': 3, 'cases': {}, 'harnesses': defaults.get('harnesses', {}),
              **value}
    if any(type(policy[key]) is not bool for key in ('create', 'improve', 'auto_apply', 'reflect') if key in policy):
        raise ValueError('automation.create, improve, auto_apply and reflect must be booleans')
    if policy.get('reflect') and not (policy['create'] or policy['improve']):
        raise ValueError('automation.reflect requires create or improve')
    if any(type(policy[key]) is not int or policy[key] < 1
           for key in ('interval_days', 'max_candidates_per_batch')):
        raise ValueError('automation interval and candidate limit must be positive integers')
    if policy['create'] and raw.get('selection') != 'all':
        raise ValueError('Automatic creation requires selection all')
    if policy['auto_apply'] and not (policy['create'] or policy['improve']):
        raise ValueError('automation.auto_apply requires create or improve')
    if not isinstance(policy['harnesses'], dict) or set(policy['harnesses']) - {'codex', 'opencode', 'hermes'}:
        raise ValueError('automation.harnesses must contain supported harness settings')
    for settings in policy['harnesses'].values():
        if not isinstance(settings, dict) or not isinstance(settings.get('model'), str) or not settings['model']:
            raise ValueError('Each automation harness requires a model')
    cases = policy['cases']
    if not isinstance(cases, dict):
        raise ValueError('automation.cases must map topics to train and holdout paths')
    for name, paths in cases.items():
        if (not SKILL_NAME.fullmatch(name) or len(name) > 64 or not isinstance(paths, dict)
                or set(paths) != {'train', 'holdout'} or not all(isinstance(path, str) for path in paths.values())):
            raise ValueError('automation.cases requires a safe topic and train and holdout paths')
    return policy


def automation_approval(config):
    from scripts.approved_binary import describe

    root = config['root']
    automation = config['automation']
    cases = {}
    for name, splits in automation['cases'].items():
        approved = cases[name] = {}
        for split, relative in splits.items():
            path = local_path(root, relative)
            contents, rows = dataset_snapshot(path)
            fixtures = {}
            for row in rows:
                for entry in input_specs(row.get('files', []), root):
                    source = Path(entry['source'])
                    if not source.resolve(strict=True).is_relative_to(root):
                        raise ValueError('Automation fixtures must be within the approved scope')
                    fixtures[source.relative_to(root).as_posix()] = digest(source)
            approved[split] = {'path': relative, 'sha256': sha256(contents).hexdigest(), 'fixtures': fixtures}
    codex = automation['harnesses'].get('codex', {})
    return {'cases': cases, 'binary': describe(codex['binary']) if 'binary' in codex else None}
