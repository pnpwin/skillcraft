"""Owner-approved Codex executable identity and private launch snapshots."""
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import stat


CHATGPT_CODEX_WRAPPER = (
    b'#!/bin/sh\nset -eu\nentry="$0"\nwhile [ -L "$entry" ]; do\n'
    b'  parent=$(CDPATH= cd -P -- "$(dirname -- "$entry")" && pwd)\n'
    b'  entry=$(readlink "$entry")\n'
    b'  case "$entry" in /*) ;; *) entry="$parent/$entry" ;; esac\n'
    b'done\nbin_dir=$(CDPATH= cd -P -- "$(dirname -- "$entry")" && pwd)\n'
    b'exec "$bin_dir/../CodexCLI.app/Contents/MacOS/codex" "$@"\n'
)
MACHO_MAGICS = {
    b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
    b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
    b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca',
}


def _regular_info(path, capture=0):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise ValueError(f'Configured Codex binary must be an executable regular file: {path}')
        digest = hashlib.sha256()
        prefix = bytearray()
        captured = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            if len(prefix) < 4:
                prefix.extend(chunk[:4 - len(prefix)])
            if len(captured) < capture:
                captured.extend(chunk[:capture - len(captured)])
        if info.st_mode & 0o6000:
            raise ValueError(f'Set-id Codex executables are not supported: {path}')
        return digest.hexdigest(), stat.S_IMODE(info.st_mode), bytes(prefix), bytes(captured)
    finally:
        os.close(descriptor)


def _regular_prefix(path, size=4):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111 or info.st_mode & 0o6000:
            raise ValueError(f'Configured Codex binary must be an executable regular file: {path}')
        return stat.S_IMODE(info.st_mode), os.read(descriptor, size)
    finally:
        os.close(descriptor)


def _native(prefix, path):
    if prefix.startswith(b'\x7fELF') or prefix.startswith(b'MZ') or prefix[:4] in MACHO_MAGICS:
        return
    raise ValueError(f'Configured Codex binary is not a supported native executable: {path}')


def _bundle_root(path):
    return next((parent for parent in path.parents if parent.name.endswith('.app')), None)


def _package_root(path):
    path = Path(path)
    if path.parent.name == 'bin':
        candidate = path.parent.parent
    else:
        app = _bundle_root(path)
        if app is None or app.name != 'CodexCLI.app':
            return None
        candidate = app.parent
    manifest = candidate / 'codex-package.json'
    return candidate if manifest.is_file() and not manifest.is_symlink() else None


def _safe_package_path(value, field):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise ValueError(f'Invalid Codex package {field}')
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError(f'Invalid Codex package {field}')
    return relative.as_posix()


def _package_manifest(root):
    path = Path(root) / 'codex-package.json'
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError('Invalid Codex package manifest file')
        chunks = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        manifest = json.loads(b''.join(chunks))
    finally:
        os.close(descriptor)
    if (not isinstance(manifest, dict) or type(manifest.get('layoutVersion')) is not int
            or manifest.get('layoutVersion') != 1):
        raise ValueError('Unsupported Codex package layout')
    fields = {}
    for field in ('entrypoint', 'execServer', 'resourcesDir', 'pathDir'):
        relative = _safe_package_path(manifest.get(field), field)
        target = Path(root) / relative
        cursor = target
        while cursor != Path(root).parent:
            if cursor.is_symlink():
                raise ValueError(f'Codex package {field} traverses a symlink')
            cursor = cursor.parent
        if not target.exists() or target.is_symlink():
            raise ValueError(f'Codex package is missing its declared {field}')
        fields[field] = relative
    if not (Path(root) / fields['entrypoint']).is_file() or not (Path(root) / fields['execServer']).is_file():
        raise ValueError('Codex package entrypoint and exec server must be regular files')
    if not (Path(root) / fields['resourcesDir']).is_dir() or not (Path(root) / fields['pathDir']).is_dir():
        raise ValueError('Codex package resources and path directories must be directories')
    return fields


def _bundle_digest(root):
    root = Path(root).resolve(strict=True)
    digest = hashlib.sha256()
    entries = [root, *root.rglob('*')]
    for path in sorted(entries, key=lambda item: item.relative_to(root.parent).as_posix()):
        rel = path.relative_to(root.parent).as_posix()
        info = path.lstat()
        digest.update(rel.encode() + b'\0' + str(stat.S_IMODE(info.st_mode)).encode() + b'\0')
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            resolved = (path.parent / target).resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError(f'Codex app bundle contains an external symlink: {rel}')
            digest.update(b'L' + target.encode() + b'\0')
        elif stat.S_ISDIR(info.st_mode):
            digest.update(b'D\0')
        elif stat.S_ISREG(info.st_mode):
            content_hash = _regular_info(path)[0] if info.st_mode & 0o111 else _read_data(path)
            digest.update(b'F' + bytes.fromhex(content_hash) + b'\0')
        else:
            raise ValueError(f'Unsupported file in Codex app bundle: {rel}')
    return digest.hexdigest()


def _native_package_entry(root, manifest, requested):
    bundle_entry = 'CodexCLI.app/Contents/MacOS/codex'
    selected = Path(root) / requested
    if requested == manifest['entrypoint']:
        _, prefix = _regular_prefix(selected, len(CHATGPT_CODEX_WRAPPER) + 1)
        if requested != 'bin/codex' or prefix != CHATGPT_CODEX_WRAPPER:
            _native(prefix, selected)
            return requested
    elif requested != bundle_entry:
        raise ValueError('Configured Codex binary is not the package entrypoint or its native executable')
    native = Path(root) / bundle_entry
    _, prefix = _regular_prefix(native)
    _native(prefix, native)
    return bundle_entry


def _read_data(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'Codex app bundle contains a non-regular file: {path}')
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def describe(path):
    """Resolve and fingerprint an explicit executable and any declared Codex package."""
    configured = str(Path(path).expanduser().absolute())
    resolved = Path(configured).resolve(strict=True)
    package = _package_root(resolved)
    if package is not None:
        package = package.resolve(strict=True)
        mode, _ = _regular_prefix(resolved)
        manifest = _package_manifest(package)
        relative = resolved.relative_to(package).as_posix()
        native_entry = _native_package_entry(package, manifest, relative)
        return {
            'configured_path': configured, 'path': str(resolved), 'mode': mode,
            'kind': 'package', 'package_root': str(package), 'package_sha256': _bundle_digest(package),
            'manifest': manifest, 'relative_path': relative, 'launch_path': native_entry,
        }
    file_hash, mode, prefix, contents = _regular_info(resolved, capture=4096)
    bundle = _bundle_root(resolved)
    kind = 'standalone'
    record = {'configured_path': configured, 'path': str(resolved), 'sha256': file_hash, 'mode': mode}
    if (resolved.parent.name == 'bin' and resolved.parent.parent.name == 'codex-cli'
            and contents == CHATGPT_CODEX_WRAPPER):
        bundle = resolved.parent.parent / 'CodexCLI.app'
        native = bundle / 'Contents/MacOS/codex'
        _, _, native_prefix, _ = _regular_info(native)
        _native(native_prefix, native)
        record.update(kind='chatgpt_launcher', bundle=str(bundle.resolve(strict=True)),
                      bundle_sha256=_bundle_digest(bundle))
    elif bundle is not None:
        _native(prefix, resolved)
        record.update(kind='app_binary', bundle=str(bundle), bundle_sha256=_bundle_digest(bundle),
                      relative_path=resolved.relative_to(bundle).as_posix())
    else:
        _native(prefix, resolved)
        record['kind'] = kind
    return record


def _copy_regular(source, target):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(source, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o6000:
            raise ValueError(f'Unsupported Codex executable input: {source}')
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256()
        with target.open('xb') as output:
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        mode = stat.S_IMODE(info.st_mode)
        target.chmod(mode & 0o777)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _copy_bundle(source, target):
    def copy_file(src, dst):
        _copy_regular(src, dst)
        return dst
    shutil.copytree(source, target, symlinks=True, copy_function=copy_file)


def stage(approved, destination):
    """Copy exactly approved bytes into private state and return the launch path."""
    resolved = Path(approved['configured_path']).resolve(strict=True)
    if str(resolved) != approved.get('path'):
        raise ValueError('Approved Codex binary changed; re-enable learning')
    if approved['kind'] != 'package' and _package_root(resolved) is not None:
        raise ValueError('Codex runtime now has a package manifest; re-enable learning')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    if approved['kind'] == 'package':
        source_root = Path(approved['package_root'])
        target_root = destination / source_root.name
        _copy_bundle(source_root, target_root)
        if _bundle_digest(target_root) != approved['package_sha256']:
            raise ValueError('Codex package snapshot differs from owner approval')
        staged_manifest = _package_manifest(target_root)
        staged_launch = _native_package_entry(target_root, staged_manifest, approved['relative_path'])
        if staged_manifest != approved['manifest'] or staged_launch != approved['launch_path']:
            raise ValueError('Codex package launch layout differs from owner approval')
        return target_root / staged_launch
    if approved['kind'] == 'standalone':
        target = destination / Path(approved['path']).name
        if _copy_regular(approved['path'], target) != approved['sha256']:
            raise ValueError('Codex binary snapshot differs from owner approval')
        return target

    source_bundle = Path(approved['bundle'])
    if approved['kind'] == 'chatgpt_launcher':
        codex_cli = destination / 'codex-cli'
        codex_cli.mkdir(mode=0o700)
        _copy_bundle(source_bundle, codex_cli / source_bundle.name)
        launcher_source = Path(approved['path'])
        launcher = codex_cli / 'bin' / launcher_source.name
        if _copy_regular(launcher_source, launcher) != approved['sha256']:
            raise ValueError('Codex launcher snapshot differs from owner approval')
        if _bundle_digest(codex_cli / source_bundle.name) != approved['bundle_sha256']:
            raise ValueError('Codex app bundle snapshot differs from owner approval')
        if _regular_info(launcher)[0] != approved['sha256']:
            raise ValueError('Codex launcher snapshot differs from owner approval')
        return codex_cli / source_bundle.name / 'Contents/MacOS/codex'

    target_bundle = destination / source_bundle.name
    _copy_bundle(source_bundle, target_bundle)
    if _bundle_digest(target_bundle) != approved['bundle_sha256']:
        raise ValueError('Codex app bundle snapshot differs from owner approval')
    return target_bundle / approved['relative_path']


def read_roots(binary):
    """Paths Codex App Server needs read access to when launching a bundle."""
    binary = Path(binary).resolve()
    roots = {binary}
    app = _bundle_root(binary)
    if app:
        roots.add(app.resolve())
    return sorted(roots)
