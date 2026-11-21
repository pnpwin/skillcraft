"""Hermes registration for the shared SkillCraft package."""

import json
from pathlib import Path
import sys

from .src.hermes.hermes_learning import Learning, run_learning


def cli_arguments(parser):
    commands = parser.add_subparsers(dest='learning_command', required=True)
    for name in ('enable', 'status'):
        command = commands.add_parser(name)
        command.add_argument('project', type=Path)
        if name == 'enable':
            command.add_argument('--auto-import', action='store_true',
                                 help='Forward completed experience to the project learning journal')
            command.add_argument('--preview-linked-roots', action='store_true',
                                 help='Preview linked project roots before enabling learning')
            command.add_argument('--approve-linked-root', action='append', default=[], metavar='ABS_PATH',
                                 help='Approve one absolute linked project root; may be repeated')
            command.add_argument('--linked-roots-preview-sha256',
                                 help='Digest of the linked-root preview being approved')
    recover = commands.add_parser('recover', help='Recover verified feedback from one local Hermes chat')
    recover.add_argument('project', type=Path)
    recover.add_argument('--session', required=True, help='Native Hermes session ID')


def journal_command(args):
    from hermes_constants import get_hermes_home

    try:
        home = get_hermes_home()
        project = args.project.resolve()
        if args.learning_command == 'recover':
            from .src.hermes.hermes_history import recover

            audit = recover(home, project, args.session)
            result = run_learning(home, 'recover-hermes', str(project), payload=audit)
            result['history'] = {key: audit[key] for key in ('audited', 'skipped', 'reason') if key in audit}
        else:
            options = []
            interactive = False
            if args.learning_command == 'enable':
                if args.approve_linked_root and not args.linked_roots_preview_sha256:
                    raise ValueError('--linked-roots-preview-sha256 is required with --approve-linked-root')
                if args.linked_roots_preview_sha256 and not args.approve_linked_root:
                    raise ValueError('--linked-roots-preview-sha256 requires --approve-linked-root')
                if args.auto_import:
                    options.append('--auto-import')
                if args.preview_linked_roots:
                    options.append('--preview-linked-roots')
                for root in args.approve_linked_root:
                    options.extend(('--approve-linked-root', root))
                if args.linked_roots_preview_sha256:
                    options.extend(('--linked-roots-preview-sha256', args.linked_roots_preview_sha256))
                interactive = (not args.preview_linked_roots and not args.approve_linked_root
                               and sys.stdin.isatty() and sys.stderr.isatty())
            result = run_learning(home, args.learning_command, str(project), *options,
                                  interactive=interactive)
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
    ctx.register_hook('post_api_request', learning.model_finished)
    ctx.register_hook('post_llm_call', learning.finish)
