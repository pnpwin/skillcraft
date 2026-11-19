"""Hermes registration for the shared SkillCraft package."""

import json
from pathlib import Path

from .hermes_learning import Learning, run_learning


def cli_arguments(parser):
    commands = parser.add_subparsers(dest='learning_command', required=True)
    for name in ('enable', 'status'):
        commands.add_parser(name).add_argument('project', type=Path)


def journal_command(args):
    from hermes_constants import get_hermes_home

    try:
        result = run_learning(get_hermes_home(), args.learning_command, str(args.project.resolve()))
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


def register(ctx):
    skill = Path(__file__).resolve().parent / 'skills/skillcraft/SKILL.md'
    ctx.register_skill('skillcraft', skill,
                       description='Create, evaluate, and refine Agent Skills.')
    ctx.register_cli_command('skillcraft', 'Manage the SkillCraft learning journal',
                             cli_arguments, journal_command)

    from hermes_constants import get_hermes_home

    learning = Learning(ctx, get_hermes_home)
    ctx.register_hook('pre_llm_call', learning.start)
    ctx.register_hook('post_tool_call', learning.tool)
    ctx.register_hook('post_llm_call', learning.finish)
