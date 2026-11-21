"""Discover user-authored local skills without traversing managed installs."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.utils import parse_frontmatter, safe_id


_PROJECT_ROOTS = (
    ".agents/skills",
    ".codex/skills",
    ".hermes/skills",
    ".opencode/skills",
    "opencode/skills",
)
_HOME_ROOTS = (
    ".agents/skills",
    ".codex/skills",
    ".hermes/skills",
    ".opencode/skills",
    ".config/opencode/skills",
    "opencode/skills",
)
_EXCLUDED_PARTS = {
    ".git", ".system", "cache", "caches", "marketplace", "marketplaces",
    "node_modules", "plugins", "plugin", "bundled", "vendor",
}
_PACKAGE_MARKERS = (
    "plugin.json",
    ".codex-plugin/plugin.json",
    ".claude-plugin/plugin.json",
    "marketplace.json",
    "openclaw.plugin.json",
    ".codex-marketplace-install.json",
)


def _skill_id(skill_file: Path) -> str | None:
    try:
        name = safe_id(skill_file.parent.name)
        metadata, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8-sig"))
        return name if metadata.get("name", name) == name else None
    except (OSError, ValueError):
        return None


def _managed_hermes(skills_root: Path) -> tuple[set[str], set[Path], bool]:
    """Return bundled names and Hub-managed paths from Hermes native metadata."""
    manifest = skills_root / ".bundled_manifest"
    names: set[str] = set()
    try:
        with manifest.open("r", encoding="utf-8") as stream:
            for line in stream:
                name, separator, _digest = line.strip().partition(":")
                if separator and name:
                    names.add(name.casefold())
    except (OSError, UnicodeError):
        pass
    lock_path = skills_root / ".hub" / "lock.json"
    managed_paths: set[Path] = set()
    lock_valid = True
    if (skills_root / ".hub").is_symlink() or lock_path.is_symlink():
        lock_valid = False
    if lock_path.exists():
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            installed = payload["installed"]
            if not isinstance(installed, dict):
                raise ValueError("invalid Hermes Hub installed map")
            for entry in installed.values():
                if not isinstance(entry, dict):
                    continue
                install_path = entry.get("install_path")
                if not isinstance(install_path, str) or not install_path:
                    continue
                relative = Path(install_path)
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                managed_paths.add(relative)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            # The native runtime treats an unreadable lock as managed provenance.
            lock_valid = False
    return names, managed_paths, lock_valid


def _crosses_managed_directory(root: Path, relative: Path) -> bool:
    """Check each descendant on the path, leaving the scope root eligible."""
    current = root
    for part in relative.parts:
        current = current / part
        git_marker = current / ".git"
        if current.is_symlink() or git_marker.is_symlink() or git_marker.exists():
            return True
        if any((current / marker).is_file() for marker in _PACKAGE_MARKERS):
            return True
    return False


def _is_excluded(
    candidate: Path,
    skills_root: Path,
    managed_hermes_names: set[str],
    managed_hermes_paths: set[Path],
    hermes_lock_valid: bool,
) -> bool:
    try:
        relative = candidate.relative_to(skills_root)
    except ValueError:
        return True
    if any(part.casefold() in _EXCLUDED_PARTS for part in relative.parts):
        return True
    if not hermes_lock_valid:
        return True
    if any(relative == managed or managed in relative.parents for managed in managed_hermes_paths):
        return True
    return (candidate.name.casefold() in managed_hermes_names
            or _crosses_managed_directory(skills_root, relative))


def _skill_directories(skills_root: Path, *, hermes: bool) -> list[Path]:
    """Yield direct skills; Hermes additionally groups skills by category."""
    try:
        entries = sorted(skills_root.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []
    candidates: list[Path] = []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith(".") or entry.is_symlink():
            continue
        if (entry / "SKILL.md").is_file():
            candidates.append(entry)
            continue
        if hermes:
            try:
                children = sorted(entry.iterdir(), key=lambda p: p.name.casefold())
            except OSError:
                continue
            candidates.extend(
                child for child in children
                if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
                and (child / "SKILL.md").is_file()
            )
    return candidates


def discover_local_skills(root: Path, *, scope: str) -> dict[str, list[str]]:
    """Return exact skill IDs mapped to canonical relative source paths.

    ``root`` is the project root for ``scope="project"`` and the home root for
    ``scope="home"``. Discovery is read-only and performed on every call.
    """
    roots = _PROJECT_ROOTS if scope == "project" else _HOME_ROOTS if scope == "home" else ()
    root = Path(root)
    if root.is_symlink():
        return {}
    found: dict[str, set[str]] = {}
    for rel_root in roots:
        relative = Path(rel_root)
        skills_root = root / relative
        if _crosses_managed_directory(root, relative) or not skills_root.is_dir():
            continue
        hermes = rel_root.endswith(".hermes/skills")
        if hermes:
            managed_names, managed_paths, lock_valid = _managed_hermes(skills_root)
        else:
            managed_names, managed_paths, lock_valid = set(), set(), True
        for skill_dir in _skill_directories(skills_root, hermes=hermes):
            skill_file = skill_dir / "SKILL.md"
            if skill_file.is_symlink() or _is_excluded(
                skill_dir, skills_root, managed_names, managed_paths, lock_valid
            ):
                continue
            skill_id = _skill_id(skill_file)
            if skill_id:
                found.setdefault(skill_id, set()).add(skill_dir.relative_to(root).as_posix())
    return {key: sorted(paths) for key, paths in sorted(found.items())}
