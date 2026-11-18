#!/usr/bin/env python3
"""Live integration checks against OpenCode; makes billable model requests."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.runtime import run_model, rules
from scripts.grade import grade_run
from scripts.run_quality_eval import run_quality
from scripts.run_eval import run_eval
from tests import FIXTURES
from scripts.utils import read_json, write_json


def smoke(a):
    a.workspace.mkdir(parents=True, exist_ok=False)
    results = {}
    benchmark = run_quality(FIXTURES / 'demo-skill', FIXTURES / 'quality.json', a.workspace / 'quality',
                            a.model, repeats=2, workers=2, timeout=60)
    results['quality'] = {'ok': benchmark['errors'] == 0 and benchmark['metadata']['controlled'],
                          'errors': benchmark['errors'], 'summary': benchmark['run_summary']}
    trigger = run_eval(FIXTURES / 'demo-skill', FIXTURES / 'trigger.json', a.workspace / 'trigger',
                       a.model, repeats=1, workers=2, timeout=60)
    results['trigger'] = {'ok': trigger['summary']['errors'] == 0 and trigger['summary']['tp'] > 0 and trigger['summary']['tn'] > 0,
                          'summary': trigger['summary']}
    executor = a.workspace / 'quality/eval-receipt/with_skill/run-001'
    row = read_json(executor / 'eval_metadata.json')
    row['checks'] = row['checks'][:-1]
    grade = grade_run(executor, row, judge_model=a.model, timeout=60)
    results['judge'] = {'ok': grade['status'] == 'completed', 'grading': grade}
    timeout = run_model('Compute the first 50 prime numbers and explain your method.', a.workspace / 'timeout', a.model, timeout=.1)
    results['timeout'] = {'ok': timeout['status'] == 'timeout' and timeout.get('cancellation', {}).get('complete') is True,
                         'status': timeout['status'], 'cancellation': timeout.get('cancellation')}
    invalid = run_model('Reply OK.', a.workspace / 'invalid-model', 'skillcraft-missing-provider/missing-model', timeout=20)
    results['provider_error'] = {'ok': invalid['status'] not in {'completed', 'timeout'} and invalid['triggered'] is None,
                                'status': invalid['status'], 'error': invalid.get('error')}
    fixture = a.workspace / 'permission-fixture.txt'
    fixture.write_text('integration-fixture-value')
    profile = a.workspace / 'ask-profile.json'
    write_json(profile, {'permissions': rules() + [{'action': 'read', 'resource': 'inputs/*', 'effect': 'ask'}]})
    blocked = run_model('Use the read tool to read inputs/permission-fixture.txt, then return its exact contents.',
                        a.workspace / 'permission', a.model, files=[fixture], profile=profile, timeout=60)
    results['permission'] = {'ok': blocked['status'] == 'permission_blocked', 'status': blocked['status']}
    # A direct attempt to inspect the candidate from a baseline session must be denied.
    candidate_path = FIXTURES / 'demo-skill/SKILL.md'
    escaped = run_model(f'Use the read tool to read the file {candidate_path}. Do not guess; try the tool.',
                        a.workspace / 'external-read', a.model, timeout=60)
    transcript = (a.workspace / 'external-read/messages.json').read_text()
    results['baseline_external_read'] = {'ok': escaped['status'] == 'permission_blocked' and 'schema_version (the integer 1)' not in transcript,
                                         'status': escaped['status']}
    results['ok'] = all(r['ok'] for r in results.values())
    write_json(a.workspace / 'verification.json', results)
    return results


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', required=True)
    p.add_argument('--workspace', type=Path, required=True)
    a = p.parse_args()
    results = smoke(a)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    sys.exit(0 if results['ok'] else 1)
