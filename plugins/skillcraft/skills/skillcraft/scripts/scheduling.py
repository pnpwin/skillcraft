"""Bounded dispatch with durable states and repeated infrastructure-error stops."""
from collections import Counter
import contextvars
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import re


def failure_fingerprint(stage, status, error):
    # Invocation-specific paths/IDs must not hide the same infrastructure fault.
    text = re.sub(r'/[^\s,;]+', '<path>', str(error))
    text = re.sub(r'\b[0-9a-f]{16,}\b|\b\d+\b', '<id>', text)
    return f'{stage}:{status}:{text[:240]}'


def bounded_dispatch(jobs, execute, *, workers, threshold, persist):
    dispatch_context = contextvars.copy_context()
    states = {key: {'status': 'planned'} for key, _ in jobs}
    results, counts, stop = {}, Counter(), None
    iterator = iter(jobs)
    active = {}

    def save():
        persist({'jobs': states, 'stop': stop, 'failure_counts': dict(counts)})

    save()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def fill():
            while stop is None and len(active) < workers:
                item = next(iterator, None)
                if item is None:
                    break
                key, job = item
                states[key] = {'status': 'started'}
                save()
                context = dispatch_context.copy()
                active[pool.submit(context.run, execute, job)] = key
        fill()
        while active:
            try:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                stop = {'stage': 'dispatch', 'reason': 'interrupted; active jobs drained'}
                save()
                continue
            # Process the whole completed batch before any refill.
            for future in done:
                key = active.pop(future)
                try:
                    outcome = future.result()
                except Exception as error:
                    outcome = {'complete': False, 'stage': 'orchestration', 'status': type(error).__name__, 'error': str(error)}
                results[key] = outcome
                states[key] = {'status': 'completed' if outcome['complete'] else 'error',
                               'stage': outcome.get('stage'), 'error': outcome.get('error')}
                if not outcome['complete']:
                    fingerprint = failure_fingerprint(outcome.get('stage'), outcome.get('status'), outcome.get('error'))
                    counts[fingerprint] += 1
                    if counts[fingerprint] >= threshold and stop is None:
                        stop = {'stage': outcome.get('stage'), 'reason': 'repeated infrastructure failure',
                                'fingerprint': fingerprint, 'threshold': threshold}
            save()
            fill()
    for state in states.values():
        if state['status'] == 'planned':
            state['status'] = 'not_started_after_stop'
    save()
    return results, {'jobs': states, 'stop': stop, 'failure_counts': dict(counts)}
