"""Fail-closed verification and atomic promotion for learned skill snapshots."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

SAFE_SUFFIXES = frozenset({
    ".md", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".csv", ".tsv",
})
EXECUTABLE_MANIFESTS = frozenset({
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "pyproject.toml",
    "setup.py", "setup.cfg", "requirements.txt", "tox.ini", "pytest.ini",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Makefile", "Dockerfile", "plugin.json",
})


def _tree_hash(path: Path) -> str:
    """Match scripts.utils.tree_hash without importing its optional YAML stack."""
    root = Path(path)
    digest = hashlib.sha256()
    if root.is_symlink():
        raise ValueError(f"Symlink in snapshot: {root}")
    if not root.is_dir():
        raise ValueError(f"Snapshot must be a directory: {root}")
    for item in sorted(root.rglob("*")):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Symlink in snapshot: {item}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Unsupported filesystem entry: {item}")
        if stat.S_ISREG(info.st_mode):
            digest.update(item.relative_to(root).as_posix().encode() + b"\0" + item.read_bytes() + b"\0")
    return digest.hexdigest()


def tree_modes(root: Path) -> str:
    """Fingerprint paths and permissions separately from the package content hash."""
    digest = hashlib.sha256()
    for path in [root, *sorted(root.rglob("*"))]:
        mode = stat.S_IMODE(path.lstat().st_mode)
        digest.update(f"{path.relative_to(root).as_posix()}\0{mode:o}\0".encode())
    return digest.hexdigest()


def _matches_tree(path: Path, expected_hash: str, expected_modes: str | None = None) -> bool:
    return _tree_hash(path) == expected_hash and (expected_modes is None or tree_modes(path) == expected_modes)


def _changed_files(original: Path, candidate: Path) -> list[str]:
    """Return changed relative paths, rejecting non-regular filesystem entries."""
    original, candidate = Path(original), Path(candidate)
    before, after = {}, {}
    for root, result in ((original, before), (candidate, after)):
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"Snapshot must be a real directory: {root}")
        for path in root.rglob("*"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Symlinks are not permitted in snapshots: {path}")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Unsupported filesystem entry: {path}")
            relative = path.relative_to(root).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def _container_runtime() -> str:
    for name in ("docker", "podman"):
        executable = shutil.which(name)
        if executable:
            return executable
    raise RuntimeError("verification_unavailable: no Docker or Podman runtime")


def _local_image_id(runtime: str) -> str:
    image = os.environ.get("SKILLCRAFT_TEST_IMAGE", "")
    if not image:
        raise RuntimeError("verification_unavailable: SKILLCRAFT_TEST_IMAGE is not configured")
    result = subprocess.run(
        [runtime, "image", "inspect", "--format", "{{.Id}}", image],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False,
    )
    image_id = result.stdout.strip()
    if result.returncode or not image_id.startswith("sha256:"):
        raise RuntimeError("verification_unavailable: configured test image is not present locally")
    return image_id


def _run_isolated_tests(candidate: Path, frozen_source: Path, allowed_tests: list[list[str]]) -> None:
    """Run configured command vectors in a no-network, read-only local container."""
    if not allowed_tests:
        raise ValueError("verification_required: executable changes need configured tests")
    runtime = _container_runtime()
    image_id = _local_image_id(runtime)  # Pin the local immutable ID; never pull a tag.
    for command in allowed_tests:
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
            raise ValueError("verification_required: tests must be nonempty command vectors")
    args = [runtime, "run", "--rm", "--pull=never", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
            "--memory=1g", "--cpus=1", "--user=65534:65534",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount", f"type=bind,src={candidate},dst=/candidate,readonly",
            "--mount", f"type=bind,src={frozen_source},dst=/verification,readonly",
            "--workdir", "/candidate", image_id]
    for command in allowed_tests:
        vector = [arg.replace("{candidate}", "/candidate").replace("{verification}", "/verification")
                  for arg in command]
        result = subprocess.run(args + vector, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=600, check=False)
        if result.returncode:
            raise RuntimeError(f"verification_failed: isolated test exited {result.returncode}")


def verify_code(original: Path, candidate: Path, allowed_tests: list[list[str]], out: Path) -> None:
    """Verify executable changes in isolation; preserve both input snapshots."""
    changed = _changed_files(Path(original), Path(candidate))
    requires_gate = any(
        Path(name).suffix.lower() not in SAFE_SUFFIXES
        or "scripts" in Path(name).parts
        or Path(name).name in EXECUTABLE_MANIFESTS
        for name in changed
    )
    if not requires_gate:
        return
    if not allowed_tests:
        raise ValueError("verification_required: executable changes need configured tests")
    output = Path(out)
    output.mkdir(parents=True, exist_ok=True)
    # The baseline is copied before tests, then mounted read-only as frozen verification input.
    with tempfile.TemporaryDirectory(prefix="skillcraft-verify-") as temp:
        frozen = Path(temp) / "verification"
        _snapshot(Path(original), frozen)
        staged_candidate = Path(temp) / "candidate"
        _snapshot(Path(candidate), staged_candidate)
        _run_isolated_tests(staged_candidate.resolve(), frozen.resolve(), allowed_tests)


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")


def _atomic_exchange(first: Path, second: Path) -> bool:
    """Exchange directory names and report whether the rename was synced."""
    first, second = Path(first), Path(second)
    if os.stat(first).st_dev != os.stat(second).st_dev:
        raise RuntimeError("atomic_exchange_unavailable: paths are on different filesystems")
    first_parent, second_parent = os.open(first.parent, os.O_RDONLY), os.open(second.parent, os.O_RDONLY)
    try:
        for parent_fd, path in ((first_parent, first), (second_parent, second)):
            entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode):
                raise ValueError(f"atomic_exchange_refused: path is not a real directory: {path}")
        if sys_platform() == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            function = getattr(libc, "renameatx_np", None)
            if function is None:
                raise RuntimeError("atomic_exchange_unavailable: renameatx_np is unavailable")
            function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            function.restype = ctypes.c_int
            result = function(first_parent, os.fsencode(first.name), second_parent, os.fsencode(second.name), 0x00000002)
        elif sys_platform() == "linux":
            libc = ctypes.CDLL(None, use_errno=True)
            function = getattr(libc, "renameat2", None)
            if function is None:
                raise RuntimeError("atomic_exchange_unavailable: renameat2 is unavailable")
            function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            function.restype = ctypes.c_int
            result = function(first_parent, os.fsencode(first.name), second_parent, os.fsencode(second.name), 2)
        else:
            raise RuntimeError("atomic_exchange_unavailable: unsupported operating system")
        if result != 0:
            code = ctypes.get_errno()
            raise RuntimeError(f"atomic_exchange_failed: {os.strerror(code)}")
        durable = True
        for parent_fd in {first_parent, second_parent}:
            try:
                os.fsync(parent_fd)
            except OSError:
                # The exchange already succeeded. The caller must verify both
                # trees and retain a rollback route even if durability is unknown.
                durable = False
        return durable
    finally:
        os.close(first_parent)
        os.close(second_parent)


def sys_platform() -> str:
    # Kept tiny and injectable in tests without changing the public API.
    import sys
    return sys.platform


def _lock(state: Path):
    import fcntl
    _reject_symlink(state, "state")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    lock_path = state / ".learning-promotion.lock"
    _reject_symlink(lock_path, "lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def _snapshot(source: Path, destination: Path) -> str:
    source = Path(source)
    _tree_hash(source)  # Validate the complete tree before copying it.
    shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)
    for root, dirs, files in os.walk(destination, topdown=False):
        for name in files:
            path = Path(root) / name
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        directory = Path(root)
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return _tree_hash(destination)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_recovery_record(record_path: Path | None, record: dict) -> None:
    """Atomically write and sync a learning recovery record."""
    if record_path is None:
        return
    destination = Path(record_path).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(destination, "record")
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _verify_exchange(source: Path, stage: Path, record: dict) -> None:
    try:
        displaced_matches = _matches_tree(stage, record["before_hash"], record.get("before_modes"))
    except (OSError, ValueError):
        displaced_matches = False
    if not displaced_matches:
        # Preserve either tree if an editor also touched the newly active copy.
        if not _matches_tree(source, record["after_hash"], record.get("after_modes")):
            raise RuntimeError(f"promotion_conflict: both trees changed; displaced tree is at {stage}")
        durable = _atomic_exchange(source, stage)
        detail = " (directory sync failed)" if durable is False else ""
        raise ValueError("source_hash_mismatch: a concurrent edit was restored after exchange" + detail)
    if not _matches_tree(source, record["after_hash"], record.get("after_modes")):
        raise RuntimeError("promotion_integrity_error: promoted content hash differs")


def recover_exchange(source: Path, record: dict, state: Path) -> bool:
    """Reconcile a recorded operation if its candidate is currently active."""
    source = Path(source).resolve(strict=True)
    stage = Path(record["exchange_stage"])
    if (record["source"] != str(source) or stage.parent != source.parent
            or not stage.name.startswith(f".{source.name}.promotion-")):
        raise ValueError("promotion_record_mismatch: record does not belong to this source")
    descriptor = _lock(state)
    try:
        if not _matches_tree(source, record["after_hash"], record.get("after_modes")):
            return False
        _verify_exchange(source, stage, record)
        try:
            _fsync_directory(source.parent)
        except OSError:
            record["durability"] = "unknown"
        return True
    finally:
        os.close(descriptor)


def _promote_locked(source: Path, candidate: Path, expected_hash: str, state: Path,
                    expected_candidate_hash: str | None = None,
                    record_path: Path | None = None, *,
                    expected_source_modes: str | None = None,
                    expected_candidate_modes: str | None = None) -> dict:
    source, candidate, state = Path(source), Path(candidate), Path(state)
    for path, name in ((source, "source"), (candidate, "candidate")):
        _reject_symlink(path, name)
        if not path.is_dir():
            raise ValueError(f"{name} must be a directory: {path}")
    source, candidate = source.resolve(strict=True), candidate.resolve(strict=True)
    if source.resolve() == candidate.resolve():
        raise ValueError("source and candidate must be distinct directories")
    candidate_hash = _tree_hash(candidate)
    if expected_candidate_hash is not None and candidate_hash != expected_candidate_hash:
        raise ValueError("candidate_hash_mismatch: candidate differs from its verified fingerprint")
    before_hash = _tree_hash(source)
    if before_hash != expected_hash:
        raise ValueError("source_hash_mismatch: source changed since it was inspected")
    before_modes = tree_modes(source)
    candidate_modes = tree_modes(candidate)
    if expected_source_modes is not None and before_modes != expected_source_modes:
        raise ValueError("source_mode_mismatch: permissions changed since verification")
    if expected_candidate_modes is not None and candidate_modes != expected_candidate_modes:
        raise ValueError("candidate_mode_mismatch: permissions changed since verification")
    snapshots = state / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    suffix = f"{int(time.time())}-{uuid.uuid4().hex}"
    backup = snapshots / f"before-{before_hash}-{suffix}"
    candidate_snapshot = snapshots / f"candidate-{candidate_hash}-{suffix}"
    backup_hash = _snapshot(source, backup)
    if backup_hash != before_hash or tree_modes(backup) != before_modes:
        raise ValueError("source_hash_mismatch: backup differs from the verified source")
    if _snapshot(candidate, candidate_snapshot) != candidate_hash or tree_modes(candidate_snapshot) != candidate_modes:
        raise ValueError("candidate_hash_mismatch: candidate changed while snapshotting")
    _fsync_directory(snapshots)
    stage = source.parent / f".{source.name}.promotion-{uuid.uuid4().hex}"
    if _snapshot(candidate, stage) != candidate_hash or not _matches_tree(candidate, candidate_hash, candidate_modes):
        raise ValueError("candidate_hash_mismatch: candidate changed while staging")
    record = {"source": str(source), "before_hash": before_hash,
              "after_hash": candidate_hash, "before_modes": before_modes, "after_modes": candidate_modes,
              "backup": str(backup.resolve()),
              "backup_hash": backup_hash, "candidate_snapshot": str(candidate_snapshot.resolve()),
              "candidate_hash": candidate_hash, "exchange_stage": str(stage),
              "timestamp": time.time()}
    write_recovery_record(record_path, record)
    if not _matches_tree(stage, candidate_hash, candidate_modes):
        raise ValueError("candidate_hash_mismatch: staged copy changed before exchange")
    if not _matches_tree(source, before_hash, before_modes):
        raise ValueError("source_hash_mismatch: source changed before exchange")
    durable = _atomic_exchange(source, stage)
    _verify_exchange(source, stage, record)
    if durable is False:
        record["durability"] = "unknown"
    return record


def promote(source: Path, candidate: Path, expected_hash: str, state: Path, *,
            expected_candidate_hash: str | None = None,
            record_path: Path | None = None,
            expected_source_modes: str | None = None,
            expected_candidate_modes: str | None = None) -> dict:
    """Atomically exchange snapshots if source still matches its expected hash."""
    source, candidate, state = Path(source).absolute(), Path(candidate).absolute(), Path(state).absolute()
    descriptor = _lock(state)
    try:
        return _promote_locked(source, candidate, expected_hash, state, expected_candidate_hash, record_path,
                               expected_source_modes=expected_source_modes,
                               expected_candidate_modes=expected_candidate_modes)
    finally:
        os.close(descriptor)


def rollback(source: Path, record: dict, state: Path, *, record_path: Path | None = None) -> dict:
    """Restore a promoted snapshot only if the live source still matches the record."""
    source, state = Path(source).absolute(), Path(state).absolute()
    descriptor = _lock(state)
    try:
        if str(source.resolve()) != record.get("source"):
            raise ValueError("rollback_source_mismatch: record belongs to another source")
        _reject_symlink(source, "source")
        if not _matches_tree(source, record.get("after_hash"), record.get("after_modes")):
            raise ValueError("rollback_hash_mismatch: source changed after promotion")
        backup = Path(record.get("backup", ""))
        _reject_symlink(backup, "backup")
        backup_hash = _tree_hash(backup)
        if backup_hash != record.get("backup_hash") or (
                record.get("before_modes") is not None and tree_modes(backup) != record["before_modes"]):
            raise ValueError("rollback_backup_mismatch: backup snapshot changed")
        temporary = source.parent / f".{source.name}.rollback-{uuid.uuid4().hex}"
        shutil.copytree(backup, temporary, symlinks=False, copy_function=shutil.copy2)
        restored_hash = _tree_hash(temporary)
        if restored_hash != backup_hash:
            raise ValueError("rollback_backup_mismatch: copied snapshot changed")
        return _promote_locked(source, temporary, record.get("after_hash", ""), state,
                               restored_hash, record_path,
                               expected_source_modes=record.get("after_modes"),
                               expected_candidate_modes=record.get("before_modes"))
    finally:
        os.close(descriptor)
