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
    hub = skills_root / ".hub"
    lock_path = hub / "lock.json"
    if any(path.is_symlink() for path in (manifest, hub, lock_path)):
        return set(), set(), False
    if any(path.exists() and not path.is_file() for path in (manifest, lock_path)):
        return set(), set(), False
    names: set[str] = set()
    manifest_valid = True
    try:
        with manifest.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                name, separator, digest = line.partition(":")
                if not separator or not name or not digest:
                    manifest_valid = False
                    break
                names.add(name.casefold())
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        manifest_valid = False
    managed_paths: set[Path] = set()
    lock_valid = manifest_valid
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
    *,
    linked: bool = False,
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
            or _crosses_managed_directory(skills_root, relative.parent if linked else relative))


def _skill_directories(skills_root: Path, *, hermes: bool, include_links: bool) -> list[Path]:
    """Yield direct skills; Hermes additionally groups skills by category."""
    try:
        entries = sorted(skills_root.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []
    candidates: list[Path] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            if include_links:
                candidates.append(entry)
            continue
        if not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            candidates.append(entry)
            continue
        if hermes:
            try:
                children = sorted(entry.iterdir(), key=lambda p: p.name.casefold())
            except OSError:
                continue
            for child in children:
                if child.name.startswith("."):
                    continue
                if child.is_symlink():
                    if include_links:
                        candidates.append(child)
                elif child.is_dir() and (child / "SKILL.md").is_file():
                    candidates.append(child)
    return candidates


def _has_managed_ancestry(path: Path) -> bool:
    """Reject known managed locations and package markers on their ancestors."""
    if any(part.casefold() in _EXCLUDED_PARTS for part in path.parts):
        return True
    for current in (path, *path.parents):
        if any((current / marker).is_file() for marker in _PACKAGE_MARKERS):
            return True
    return False


def _canonical_hermes_excluded(target: Path) -> bool:
    """Check bundle and Hub provenance roots along the canonical ancestry."""
    for ancestor in target.parents:
        manifest = ancestor / ".bundled_manifest"
        hub = ancestor / ".hub"
        lock_path = hub / "lock.json"
        if not (manifest.exists() or manifest.is_symlink() or hub.is_symlink()
                or lock_path.exists() or lock_path.is_symlink()):
            continue
        names, paths, lock_valid = _managed_hermes(ancestor)
        if _is_excluded(target, ancestor, names, paths, lock_valid):
            return True
    return False


def _has_repo_below(root: Path, target: Path) -> bool:
    """Reject nested repositories from the approved root through the target."""
    relative = target.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        marker = current / ".git"
        if marker.exists() or marker.is_symlink():
            return True
    return False


def _canonical_link_target(link: Path) -> Path | None:
    """Resolve a leaf link without reading skill contents."""
    try:
        if not link.is_symlink() or safe_id(link.name) != link.name:
            return None
        parent = link.parent.absolute()
        if parent.is_symlink():
            return None
        target = link.resolve(strict=True)
        skill_file = target / "SKILL.md"
        if (not target.is_dir() or target.name != link.name
                or skill_file.is_symlink() or not skill_file.is_file()
                or _has_managed_ancestry(target) or _canonical_hermes_excluded(target)):
            return None
        return target
    except (OSError, RuntimeError, ValueError):
        return None


def _inside_linked_roots(target: Path, linked_skill_roots: tuple[Path, ...]) -> bool:
    """Whether the target is already covered by a canonical approved source root."""
    for approved in linked_skill_roots:
        approved = Path(approved)
        if not approved.is_absolute():
            continue
        try:
            canonical_root = approved.resolve(strict=True)
            if canonical_root != approved or not canonical_root.is_dir():
                continue
            relative = target.relative_to(canonical_root)
        except (OSError, RuntimeError, ValueError):
            continue
        if relative.parts and not _has_repo_below(canonical_root, target):
            return True
    return False


def _under_linked_roots(target: Path, linked_skill_roots: tuple[Path, ...]) -> bool:
    """Whether a target is strictly below any canonical approved root."""
    for approved in linked_skill_roots:
        approved = Path(approved)
        if not approved.is_absolute():
            continue
        try:
            canonical_root = approved.resolve(strict=True)
            if canonical_root != approved or not canonical_root.is_dir():
                continue
            if target.relative_to(canonical_root).parts:
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def _preview_crosses_checkout(root: Path, target: Path) -> bool:
    if target.is_relative_to(root) and _has_repo_below(root, target):
        return True
    checkouts = [directory for directory in (target, *target.parents)
                 if (directory / ".git").exists() or (directory / ".git").is_symlink()]
    return target in checkouts or len(checkouts) > 1


def _scope_roots(root: Path, scope: str, hermes_home: Path | None) -> dict[Path, bool]:
    paths = _PROJECT_ROOTS if scope == "project" else _HOME_ROOTS if scope == "home" else ()
    default_hermes = Path(".hermes/skills")
    roots = {Path(path): Path(path) == default_hermes for path in paths}
    if scope == "home" and hermes_home is not None:
        roots.pop(default_hermes)
        skills = hermes_home / "skills"
        if skills.is_relative_to(root):
            relative = skills.relative_to(root)
            if not any(part.casefold() in _EXCLUDED_PARTS for part in relative.parts):
                roots[relative] = True
    return roots


def _scope_candidates(root: Path, scope: str, *, include_links: bool, hermes_home: Path | None):
    for relative, hermes in _scope_roots(root, scope, hermes_home).items():
        skills_root = root / relative
        if _crosses_managed_directory(root, relative) or not skills_root.is_dir():
            continue
        names, paths, valid = _managed_hermes(skills_root) if hermes else (set(), set(), True)
        for candidate in _skill_directories(skills_root, hermes=hermes, include_links=include_links):
            linked = candidate.is_symlink()
            if not _is_excluded(candidate, skills_root, names, paths, valid, linked=linked):
                yield candidate, linked


def linked_skill_target(link: Path, linked_skill_roots: tuple[Path, ...]) -> Path | None:
    """Return an approved canonical skill directory for a safe leaf symlink.

    Approved roots are checked again at use time. The target must be strictly
    below one of them, and all canonical path and metadata exclusions apply.
    """
    link = Path(link)
    if not linked_skill_roots:
        return None
    target = _canonical_link_target(link)
    if target is None or not _inside_linked_roots(target, linked_skill_roots):
        return None
    skill_file = target / "SKILL.md"
    if _skill_id(skill_file) != link.name:
        return None
    return target


def linked_root_candidates(
    root: Path,
    *,
    scope: str,
    linked_skill_roots: tuple[Path, ...] = (),
    hermes_home: Path | None = None,
    exclude=(),
    pins=None,
) -> dict[Path, tuple[tuple[str, Path], ...]]:
    """Propose canonical source roots for eligible linked local skill aliases.

    This preview checks paths and managed-install metadata only. It deliberately
    does not read ``SKILL.md`` contents; final discovery checks frontmatter after
    the user approves a root.
    """
    root = Path(root)
    if root.is_symlink():
        return {}
    root = root.resolve()
    excluded = set(exclude)
    pins = pins or {}
    grouped: dict[Path, set[tuple[str, Path]]] = {}
    for link, linked in _scope_candidates(root, scope, include_links=True, hermes_home=hermes_home):
        if not linked or link.name in excluded:
            continue
        alias = link.relative_to(root).as_posix()
        if link.name in pins and pins[link.name] != alias:
            continue
        target = _canonical_link_target(link)
        if target is None or _under_linked_roots(target, linked_skill_roots):
            continue
        candidate_root = target.parent
        if candidate_root == Path(candidate_root.anchor) or _preview_crosses_checkout(root, target):
            continue
        grouped.setdefault(candidate_root, set()).add((alias, target))
    return {
        candidate_root: tuple(sorted(aliases, key=lambda item: (item[0], item[1].as_posix())))
        for candidate_root, aliases in sorted(grouped.items(), key=lambda item: item[0].as_posix())
    }


def discover_local_skills(
    root: Path,
    *,
    scope: str,
    linked_skill_roots: tuple[Path, ...] = (),
    hermes_home: Path | None = None,
) -> dict[str, list[str]]:
    """Return exact skill IDs mapped to scope-relative source paths.

    ``root`` is the project root for ``scope="project"`` and the home root for
    ``scope="home"``. A selected Hermes profile replaces the default Hermes
    directory in home scope. Discovery is read-only and performed on every call.
    """
    root = Path(root)
    if root.is_symlink():
        return {}
    found: dict[str, set[str]] = {}
    for skill_dir, linked in _scope_candidates(
        root, scope, include_links=bool(linked_skill_roots), hermes_home=hermes_home,
    ):
        if linked:
            if linked_skill_target(skill_dir, linked_skill_roots) is None:
                continue
            skill_id = skill_dir.name
        else:
            skill_file = skill_dir / "SKILL.md"
            if skill_file.is_symlink():
                continue
            skill_id = _skill_id(skill_file)
        if skill_id:
            found.setdefault(skill_id, set()).add(skill_dir.relative_to(root).as_posix())
    return {key: sorted(paths) for key, paths in sorted(found.items())}
