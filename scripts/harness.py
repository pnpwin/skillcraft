"""Resolve the calling harness once at the CLI boundary."""
import os
from pathlib import Path

from scripts.utils import parse_model


HARNESSES = ('codex', 'opencode')
SKILL_ROOT = Path('.agents') / 'skills'


def resolve_harness(override=None, *, environ=None):
    if override is not None:
        if override not in HARNESSES:
            raise ValueError(f'Unsupported harness: {override}')
        return override
    env = os.environ if environ is None else environ
    codex = bool(env.get('CODEX_THREAD_ID'))
    opencode = env.get('OPENCODE') == '1' and bool(env.get('OPENCODE_SESSION_ID'))
    if codex == opencode:
        reason = 'Both Codex and OpenCode session signals are present' if codex else 'No harness session signal was found'
        raise ValueError(f'{reason}. Specify --harness codex or --harness opencode.')
    return 'codex' if codex else 'opencode'


def add_harness_arg(parser):
    parser.add_argument('--harness', choices=HARNESSES,
                        help='Override session autodetection (for example in CI)')


def parse_harness_args(parser):
    args = parser.parse_args()
    try:
        args.harness = resolve_harness(args.harness)
    except ValueError as error:
        parser.error(str(error))
    return args


def model_ref(value, harness):
    if harness == 'opencode':
        return parse_model(value)
    if harness != 'codex':
        raise ValueError(f'Unsupported harness: {harness}')
    name, separator, effort = value.partition('#')
    if not name or any(character.isspace() for character in value) or '/' in name or (
            separator and (not effort or '#' in effort)):
        raise ValueError('Codex model must be model[#effort]')
    return {'id': name, **({'effort': effort} if effort else {})}
