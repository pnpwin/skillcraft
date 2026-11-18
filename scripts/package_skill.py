#!/usr/bin/env python3
"""
Skill Packager - Creates a distributable .skill file of a skill folder

Usage:
    python scripts/package_skill.py <path/to/skill-folder> [output-directory] --run opencode=<run-dir> --run codex=<run-dir>

Example:
    python scripts/package_skill.py .opencode/skills/my-skill ./dist --run ./experiment/eval-task/with_skill/run-001

Install by extracting the skill directory into <project>/.agents/skills/.
The ZIP is a distribution format, not a native harness installer.
"""

import fnmatch
import argparse
import sys
import zipfile
import tempfile
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.quick_validate import validate_skill
from scripts.compliance import accept_current, assess, skill_hash

# Patterns to exclude when packaging skills.
EXCLUDE_DIRS = {"__pycache__", "node_modules", ".venv", ".git", "skillcraft-workspaces"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
# Directories excluded only at the skill root (not when nested deeper).
ROOT_EXCLUDE_DIRS = {"evals", "tests"}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging."""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    # rel_path is relative to skill_path.parent, so parts[0] is the skill
    # folder name and parts[1] (if present) is the first subdir.
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def _normalize_runs(run_dir=None, run_dirs=None):
    if run_dir is not None and run_dirs is not None:
        raise ValueError('Use run_dir or run_dirs, not both')
    if run_dirs is not None:
        runs = dict(run_dirs)
    elif run_dir is not None:
        # Historical API used one OpenCode run directory.
        runs = {'opencode': run_dir}
    else:
        runs = {}
    if set(runs) - {'opencode', 'codex'}:
        raise ValueError('Run mapping keys must be opencode or codex')
    if len(set(map(str, runs.values()))) != len(runs):
        raise ValueError('Each harness must have a distinct run directory')
    return runs


def package_skill(skill_path, output_dir=None, *, run_dir=None, run_dirs=None, verification_dir=None):
    """
    Package a skill folder into a .skill file.

    Args:
        skill_path: Path to the skill folder
        output_dir: Optional output directory for the .skill file (defaults to current directory)

    Returns:
        Path to the created .skill file, or None if error
    """
    skill_path = Path(skill_path).resolve()

    # Validate skill folder exists
    if not skill_path.exists():
        print(f"❌ Error: Skill folder not found: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"❌ Error: Path is not a directory: {skill_path}")
        return None

    # Validate SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"❌ Error: SKILL.md not found in {skill_path}")
        return None

    # Run validation before packaging
    print("🔍 Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"❌ Validation failed: {message}")
        print("   Please fix the validation errors before packaging.")
        return None
    print(f"✅ {message}\n")

    runs = _normalize_runs(run_dir, run_dirs)
    is_skillcraft = skill_path.name.casefold() == 'skillcraft'
    if is_skillcraft and set(runs) != {'opencode', 'codex'}:
        print('SkillCraft release requires one hash-bound run from each harness.')
        return None
    if not runs:
        print('Packaging requires hash-bound representative execution evidence (--run).')
        return None
    run_hashes = set()
    for selected, path in runs.items():
        preliminary = assess(skill_path, path, harness=selected)
        run_hashes.add(preliminary['skill_hash'])
        status = preliminary[selected]['status']
        if not preliminary['accepted']:
            print(f'{selected} compatibility: {status}')
            print('Packaging requires hash-bound representative execution evidence (--run).')
            return None
    if len(run_hashes) != 1 or None in run_hashes:
        print('Harness evidence does not bind the same skill hash.')
        return None

    # Determine output location
    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path.cwd()

    skill_filename = output_path / f"{skill_name}.skill"
    if output_path == skill_path or skill_path in output_path.parents:
        raise ValueError('Archive output must be outside the skill directory')

    if not isinstance(verification_dir, dict):
        verification_dir = Path(verification_dir) if verification_dir else output_path / ('acceptance-' + uuid.uuid4().hex)
    for selected, path in runs.items():
        if isinstance(verification_dir, dict):
            verify_dir = verification_dir.get(selected)
        elif verification_dir and len(runs) == 1:
            verify_dir = verification_dir
        else:
            base = Path(verification_dir) if verification_dir else output_path
            verify_dir = base / ('acceptance-' + selected + '-' + uuid.uuid4().hex)
        if not verify_dir:
            raise ValueError(f'No verification directory for {selected}')
        check = accept_current(skill_path, path, verify_dir, harness=selected)
        if not check['accepted']:
            print(f'{selected} compatibility:', check[selected]['status'])
            return None

    # Create the .skill file (zip format)
    try:
        temporary_archive = skill_filename.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        with zipfile.ZipFile(temporary_archive, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Walk through the skill directory, excluding build artifacts
            for file_path in sorted(skill_path.rglob('*')):
                arcname = file_path.relative_to(skill_path.parent)
                if should_exclude(arcname):
                    continue
                if file_path.is_symlink():
                    raise ValueError(f'Symlinks are not distributable: {file_path}')
                if not file_path.is_file():
                    continue
                zipf.write(file_path, arcname)
                print(f"  Added: {arcname}")

        # Validate the bytes actually distributed, not merely the source before ZIP.
        with tempfile.TemporaryDirectory() as temporary:
            with zipfile.ZipFile(temporary_archive) as archive:
                archive.extractall(temporary)
            if skill_hash(Path(temporary) / skill_name) != check['skill_hash'] or skill_hash(skill_path) != check['skill_hash']:
                raise ValueError('Skill changed between acceptance and packaging')
        temporary_archive.replace(skill_filename)

        print(f"\n✅ Successfully packaged skill to: {skill_filename}")
        return skill_filename

    except Exception as e:
        if 'temporary_archive' in locals():
            temporary_archive.unlink(missing_ok=True)
        print(f"❌ Error creating .skill file: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description='Package an Agent Skill for distribution. Extract into <project>/.agents/skills/.')
    parser.add_argument('skill', type=Path)
    parser.add_argument('output', type=Path, nargs='?')
    parser.add_argument('--run', action='append', default=[], metavar='[HARNESS=]PATH',
                        help='Completed hash-bound run; repeat once per harness (Codex and OpenCode for SkillCraft)')
    parser.add_argument('--verification-dir', type=Path, help='New directory for current environment verification')
    args = parser.parse_args()
    skill_path = args.skill
    output_dir = args.output

    print(f"📦 Packaging skill: {skill_path}")
    if output_dir:
        print(f"   Output directory: {output_dir}")
    print()

    runs = {}
    for value in args.run:
        if '=' in value:
            selected, path = value.split('=', 1)
        else:
            path = value
            try:
                from scripts.utils import read_json
                persisted = read_json(Path(path) / 'run.json').get('harness', 'opencode')
                selected = persisted
            except (OSError, ValueError, TypeError):
                selected = 'opencode' if len(args.run) == 1 else ''
        if selected in runs:
            parser.error(f'Duplicate --run harness: {selected}')
        if selected not in {'opencode', 'codex'}:
            parser.error(f'Cannot determine harness for --run {value!r}; use harness=path')
        runs[selected] = Path(path)

    result = package_skill(skill_path, output_dir, run_dirs=runs or None, verification_dir=args.verification_dir)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
