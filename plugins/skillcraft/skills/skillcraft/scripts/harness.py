"""Resolve the calling harness once at the CLI boundary."""
import os
from pathlib import Path

from scripts.utils import parse_model


HARNESSES = ('codex', 'opencode', 'hermes')
HARNESS_CHOICES = ('codex', 'chatgpt', 'opencode', 'hermes')
SKILL_ROOT = Path('.agents') / 'skills'


def resolve_harness(override=None, *, environ=None):
    if override is not None:
        if override not in HARNESS_CHOICES:
            raise ValueError(f'Unsupported harness: {override}')
        return override
    env = os.environ if environ is None else environ
    signals = {
        'codex': bool(env.get('CODEX_THREAD_ID')),
        'opencode': env.get('OPENCODE') == '1' and bool(env.get('OPENCODE_SESSION_ID')),
        'hermes': env.get('AI_AGENT') == 'hermes-agent' and env.get('HERMES_AGENT') == 'true',
    }
    detected = [name for name, present in signals.items() if present]
    if len(detected) != 1:
        reason = 'Multiple harness signals are present' if detected else 'No harness session signal was found'
        raise ValueError(f'{reason}. Specify --harness codex, chatgpt, opencode, or hermes.')
    return detected[0]


def add_harness_arg(parser):
    parser.add_argument('--harness', choices=HARNESS_CHOICES,
                        help='Override autodetection. chatgpt is an alias for the local Codex runner.')


def parse_harness_args(parser):
    args = parser.parse_args()
    try:
        args.harness = resolve_harness(args.harness)
    except ValueError as error:
        parser.error(str(error))
    return args


def model_ref(value, harness):
    harness = runtime_harness(harness)
    if harness == 'opencode':
        return parse_model(value)
    if harness == 'hermes':
        provider, separator, name = value.partition(':')
        if not separator or not provider or not name or any(c.isspace() for c in value):
            raise ValueError('Hermes model must be provider:model')
        return {'provider': provider, 'id': name}
    if harness != 'codex':
        raise ValueError(f'Unsupported harness: {harness}')
    name, separator, effort = value.partition('#')
    if not name or any(character.isspace() for character in value) or '/' in name or (
            separator and (not effort or '#' in effort)):
        raise ValueError('Codex model must be model[#effort]')
    return {'id': name, **({'effort': effort} if effort else {})}


def runtime_harness(harness):
    return 'codex' if harness == 'chatgpt' else harness


def harness_metadata(harness):
    runtime = runtime_harness(harness)
    metadata = {'harness': runtime}
    if runtime != harness:
        metadata['requested_harness'] = harness
    return metadata
