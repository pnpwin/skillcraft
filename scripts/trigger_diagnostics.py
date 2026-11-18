"""Observed selection and tool use are separate from completion and correctness."""
from pathlib import Path
from scripts.utils import read_json


def diagnose(run_dir, result, skill_id):
    root = Path(run_dir)
    messages = read_json(root / 'messages.json') if (root / 'messages.json').exists() else []
    calls = [part for message in messages if message.get('type') == 'assistant'
             for part in message.get('content', []) if part.get('type') == 'tool']
    loaded, after_load, shell_calls = False, 0, []
    for part in calls:
        state = part.get('state', {})
        if part.get('name') == 'skill' and state.get('status') == 'completed' and (state.get('input') or {}).get('id') == skill_id:
            loaded = True
        if part.get('name') in ('shell', 'commandExecution') and state.get('status') == 'completed':
            shell_calls.append(part)
            if loaded:
                after_load += 1
    cli = [call for tool in result.get('cli_state', {}).get('observations', []) for call in tool['calls']
           if call.get('phase', 'model') == 'model']
    load_times = [message.get('time', {}).get('completed') for message in messages
                  if any(part.get('type') == 'tool' and part.get('name') == 'skill'
                         and part.get('state', {}).get('status') == 'completed'
                         and (part.get('state', {}).get('input') or {}).get('id') == skill_id
                         for part in message.get('content', []))]
    load_times = [t for t in load_times if isinstance(t, (int, float))]
    scoped_after_load = False if not loaded or not cli else None
    if load_times and cli and all(isinstance(call.get('started_ms'), (int, float)) for call in cli):
        scoped_after_load = any(call['started_ms'] >= min(load_times) for call in cli)
    status = result.get('status')
    skill_load_evidence = 'successful skill tool event' if loaded else 'no successful load observed'
    if result.get('harness') == 'codex':
        loaded = result.get('triggered') is True
        skill_load_evidence = {True: 'native Codex implicit invocation event',
                               False: 'no native invocation observed',
                               None: 'Codex observation unavailable'}.get(result.get('triggered'))
        after_load = None
        scoped_after_load = None
    return {'skill_loaded': loaded, 'skill_load_evidence': skill_load_evidence,
            'shell_calls': len(shell_calls), 'shell_calls_after_load': after_load,
            'scoped_cli_calls': len(cli), 'scoped_cli_after_load': scoped_after_load,
            'cli_observation_scope': 'configured launcher invocation attempts, not command success; bare commands are not inferred',
            'terminal_status': status, 'stop_reason': result.get('error') or (status if status != 'completed' else None),
            'selection_valid': status == 'completed' and result.get('triggered') is not None,
            'task_correct': None, 'quality_status': 'not_run'}
