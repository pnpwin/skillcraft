#!/usr/bin/env python3
"""
Skill Packager - Creates a distributable .skill file of a skill folder

Usage:
    python scripts/package_skill.py <path/to/skill-folder> [output-directory] --run <quality-run-directory>

Example:
    python scripts/package_skill.py .opencode/skills/my-skill ./dist --run ./experiment/eval-task/with_skill/run-001

Install by extracting the skill directory into <project>/.opencode/skills/.
The ZIP is a distribution format, not a native OpenCode installer.
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
ROOT_EXCLUDE_DIRS = {"evals"}


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


def package_skill(skill_path, output_dir=None, *, run_dir=None, verification_dir=None):
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

    preliminary = assess(skill_path, run_dir)
    if not preliminary['accepted']:
        print('OpenCode compatibility:', preliminary['opencode']['status'])
        print('Packaging requires hash-bound representative execution evidence (--run).')
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

    verification_dir = Path(verification_dir) if verification_dir else output_path / ('acceptance-' + uuid.uuid4().hex)
    check = accept_current(skill_path, run_dir, verification_dir)
    if not check['accepted']:
        print('OpenCode compatibility:', check['opencode']['status'])
        return None

    # Create the .skill file (zip format)
    try:
        temporary_archive = skill_filename.with_suffix('.skill.tmp')
        with zipfile.ZipFile(temporary_archive, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Walk through the skill directory, excluding build artifacts
            for file_path in skill_path.rglob('*'):
                if should_exclude(file_path.relative_to(skill_path.parent)):
                    continue
                if file_path.is_symlink():
                    raise ValueError(f'Symlinks are not distributable: {file_path}')
                if not file_path.is_file():
                    continue
                arcname = file_path.relative_to(skill_path.parent)
                if should_exclude(arcname):
                    print(f"  Skipped: {arcname}")
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
    parser = argparse.ArgumentParser(description='Package an OpenCode skill directory for distribution. Extract into <project>/.opencode/skills/.')
    parser.add_argument('skill', type=Path)
    parser.add_argument('output', type=Path, nargs='?')
    parser.add_argument('--run', type=Path, help='Completed representative native-attachment run with hash-bound evidence')
    parser.add_argument('--verification-dir', type=Path, help='New directory for current environment verification')
    args = parser.parse_args()
    skill_path = args.skill
    output_dir = args.output

    print(f"📦 Packaging skill: {skill_path}")
    if output_dir:
        print(f"   Output directory: {output_dir}")
    print()

    result = package_skill(skill_path, output_dir, run_dir=args.run, verification_dir=args.verification_dir)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
