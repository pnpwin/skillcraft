"""Versioned, explicit input destinations; legacy paths retain their basename."""
from pathlib import Path, PurePosixPath
import shutil
import unicodedata


def input_spec(item, base=None):
    base = Path(base or Path.cwd()).resolve()
    if isinstance(item, dict):
        if set(item) - {'source', 'destination', 'role'} or not {'source', 'destination'} <= set(item):
            raise ValueError('Input mapping requires source/destination and optional role')
        raw, destination, role = item['source'], item['destination'], item.get('role', 'fixture')
    else:
        raw, destination, role = item, Path(item).name, 'fixture'
    source = Path(raw)
    source = source if source.is_absolute() else base / source
    if not source.is_file() or source.is_symlink():
        raise ValueError(f'Invalid input file: {source}')
    # Base itself is a trusted, canonical caller-supplied root. Reject symlinks
    # below it, including directories, before resolving the final source.
    for parent in source.parents:
        if parent == base:
            break
        if parent.is_symlink():
            raise ValueError(f'Input source traverses symlink: {parent}')
    if not isinstance(destination, str) or not destination or '\\' in destination or '\0' in destination:
        raise ValueError('Input destination must be a nonempty POSIX relative path')
    dest = PurePosixPath(destination)
    if dest.is_absolute() or any(part in {'', '.', '..'} for part in destination.split('/')) or ':' in dest.parts[0]:
        raise ValueError(f'Unsafe input destination: {destination}')
    if role not in {'fixture', 'artifact', 'evidence'}:
        raise ValueError('Unknown input role')
    return {'source': str(source.resolve()), 'destination': dest.as_posix(), 'role': role}


def input_specs(items, base=None):
    specs = [input_spec(item, base) for item in items]
    names = set()
    for spec in specs:
        key = unicodedata.normalize('NFC', spec['destination']).casefold()
        if key in names or any(key.startswith(n + '/') or n.startswith(key + '/') for n in names):
            raise ValueError(f'Colliding input destination: {spec["destination"]}')
        names.add(key)
    return specs


def copy_inputs(items, target, *, base=None, readonly=False):
    from scripts.utils import digest
    specs = input_specs(items, base)
    target = Path(target).resolve()
    result = []
    for spec in specs:
        destination = target / spec['destination']
        if not destination.resolve().is_relative_to(target) or destination.is_symlink() or destination.exists():
            raise ValueError(f'Input destination exists or escapes root: {spec["destination"]}')
        for parent in destination.parents:
            if parent == target:
                break
            if parent.is_symlink():
                raise ValueError('Input destination traverses symlink')
    for spec in specs:
        destination = target / spec['destination']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(spec['source'], destination)
        if readonly:
            destination.chmod(0o444)
        result.append({**spec, 'copied': str(destination), 'sha256': digest(destination)})
    return result


def serial_inputs(items):
    return [dict(item) if isinstance(item, dict) else str(item) for item in items]
