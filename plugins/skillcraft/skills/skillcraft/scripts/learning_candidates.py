"""Staging, verification, and guarded publication for learned skill candidates.

This module deliberately does not decide which observations should be learned.
Callers provide a skill directory and the scope in which it may be published.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import sys
import tempfile
import time
import uuid
import ctypes
from base64 import b64encode
from typing import Callable, Sequence

from scripts import learning_promotion


def _hash(path: Path) -> str:
    return learning_promotion._tree_hash(Path(path))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"candidate_record_invalid: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"candidate_record_invalid: {path}")
    return data


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _save_candidate_unlocked(state_dir: Path, candidate_id: str, record: dict,
                             expected_revision: int) -> dict:
    path = _candidate_dir(state_dir, candidate_id) / "candidate.json"
    current = _read_json(path)
    current_revision = current.get("record_revision", 0)
    if current_revision != expected_revision:
        raise ValueError("candidate_state_conflict: state changed during operation")
    record["record_revision"] = current_revision + 1
    _write_json(path, record)
    return record


def _save_candidate(state_dir: Path, candidate_id: str, record: dict,
                    expected_revision: int) -> dict:
    with _candidate_lock(Path(state_dir).absolute()):
        return _save_candidate_unlocked(state_dir, candidate_id, record, expected_revision)


@contextmanager
def _candidate_lock(state: Path):
    import fcntl
    state = Path(state).absolute()
    if state.is_symlink():
        raise ValueError("candidate_state_symlink: state")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = state / ".learning-candidates.lock"
    if lock.is_symlink():
        raise ValueError("candidate_state_symlink: lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("candidate_name_invalid: use letters, numbers, underscore, or hyphen")
    return name


def _real_directory(path: Path, label: str, *, exists: bool = True) -> Path:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError(f"{label}_symlink: {path}")
    if exists and not path.is_dir():
        raise ValueError(f"{label}_missing: {path}")
    return path.resolve(strict=True) if exists else path


def _approved_target_root(target_root: Path, project_root: Path, home_root: Path | None,
                          *, revision_source: Path | None = None) -> Path:
    root = _real_directory(target_root, "target_root", exists=False)
    project = _real_directory(project_root, "project_root")
    allowed = {project / ".agents" / "skills"}
    if home_root is not None:
        home = _real_directory(home_root, "home_root")
        allowed.add(home / ".agents" / "skills")
    if revision_source is not None:
        raw_source = Path(revision_source).expanduser().absolute()
        source = _real_directory(raw_source, "source")
        if root != source.parent:
            raise ValueError("candidate_target_mismatch: revision target must be the selected source parent")
        roots = [project / ".agents" / "skills", project / ".codex" / "skills", project / ".opencode" / "skills",
                 project / "opencode" / "skills", project / ".hermes" / "skills",
                 project / ".config" / "opencode" / "skills"]
        if home_root is not None:
            home = _real_directory(home_root, "home_root")
            roots.extend([home / ".agents" / "skills", home / ".codex" / "skills", home / ".opencode" / "skills",
                          home / "opencode" / "skills", home / ".hermes" / "skills",
                          home / ".config" / "opencode" / "skills"])
        source_is_lexical = False
        for base in roots:
            if raw_source == base or raw_source.is_relative_to(base):
                current = base
                source_is_lexical = True
                for part in raw_source.relative_to(base).parts:
                    current = current / part
                    if current.exists() and current.is_symlink():
                        raise ValueError(f"candidate_scope_symlink: {current}")
                break
        if not source_is_lexical or not any(root == base or root.is_relative_to(base) for base in roots):
            raise ValueError("candidate_scope_denied: revision source is outside approved local skill roots")
        allowed.add(root)
    if root not in allowed:
        raise ValueError("candidate_scope_denied: target must be approved .agents/skills or selected local skill root")
    scopes = [project] + ([_real_directory(home_root, "home_root")] if home_root is not None else [])
    scope = next((base for base in scopes if root.is_relative_to(base)), None)
    if scope is None:
        raise ValueError("candidate_scope_denied")
    # Reject symlinked existing ancestors below the approved project/home root.
    relative = root.relative_to(scope)
    ancestor = scope
    if ancestor.is_symlink():
        raise ValueError(f"candidate_scope_symlink: {ancestor}")
    for part in relative.parts:
        ancestor = ancestor / part
        if ancestor.exists() and ancestor.is_symlink():
            raise ValueError(f"candidate_scope_symlink: {ancestor}")
    return root


def _snapshot(source: Path, destination: Path) -> str:
    source = _real_directory(source, "content")
    for item in source.rglob("*"):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"candidate_source_symlink: {item}")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError(f"candidate_source_unsupported_entry: {item}")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"candidate_collision: {destination}")
    shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)
    return _hash(destination)


def _make_readonly(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)


def _mode_map(root: Path) -> dict[str, int]:
    return {path.relative_to(root).as_posix(): stat.S_IMODE(path.lstat().st_mode)
            for path in [root, *sorted(root.rglob("*"))]}


def _apply_mode_map(root: Path, modes: dict[str, int]) -> None:
    for relative, mode in modes.items():
        path = root if relative == "." else root / relative
        if not path.exists() or path.is_symlink():
            raise ValueError("candidate_mode_mismatch: mode map path differs")
        path.chmod(mode)


def _revision_snapshot(source: Path, proposal: Path, destination: Path) -> str:
    """Overlay a revision proposal onto source so omitted support files survive."""
    source, proposal = _real_directory(source, "source"), _real_directory(proposal, "content")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"candidate_collision: {destination}")
    for base in (source, proposal):
        for item in base.rglob("*"):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"candidate_source_symlink: {item}")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError(f"candidate_source_unsupported_entry: {item}")
    shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)
    for item in proposal.rglob("*"):
        relative = item.relative_to(proposal)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
    return _hash(destination)


def _evaluation_hash(path: Path) -> str:
    """Match utils.copy_tree(skill=True)'s projected hash, without optional YAML."""
    excluded = {".git", "node_modules", ".venv", "__pycache__", ".DS_Store"}
    root = _real_directory(path, "skill_snapshot")
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*")):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Symlink in snapshot: {item}")
        if stat.S_ISDIR(info.st_mode):
            continue
        relative = item.relative_to(root)
        parts = relative.parts
        if any(part in excluded for part in parts) or item.name.endswith(".pyc"):
            continue
        if len(parts) > 1 and parts[0] in {"evals", "tests"}:
            continue
        digest.update(relative.as_posix().encode() + b"\0" + item.read_bytes() + b"\0")
    return digest.hexdigest()


def _check_collisions(name: str, roots: Sequence[Path], *, allowed_source: Path | None = None) -> None:
    allowed = Path(allowed_source).resolve() if allowed_source is not None else None
    for root in roots:
        root = Path(root).expanduser().absolute()
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"collision_root_invalid: {root}")
        for current, directories, _files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for directory in directories:
                entry = current_path / directory
                if directory.casefold() == name.casefold() and entry.is_symlink():
                    raise ValueError(f"candidate_collision: same-name symlink exists in {root}")
            directories[:] = [directory for directory in directories
                              if not (current_path / directory).is_symlink()]
            for directory in directories:
                candidate = current_path / directory
                if directory.casefold() == name.casefold() and candidate.resolve() != allowed:
                    raise ValueError(f"candidate_collision: same-name skill exists in {root}")


def _candidate_dir(state_dir: Path, candidate_id: str) -> Path:
    if not candidate_id or Path(candidate_id).name != candidate_id or candidate_id in {".", ".."}:
        raise ValueError("candidate_id_invalid")
    return Path(state_dir).absolute() / "candidates" / candidate_id


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a directory while refusing even an empty collision."""
    if os.stat(source).st_dev != os.stat(destination.parent).st_dev:
        raise RuntimeError("candidate_publish_failed: staging and target are on different filesystems")
    source_fd = os.open(source.parent, os.O_RDONLY)
    target_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            function = getattr(libc, "renameatx_np", None)
            flag = 0x00000004  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            function = getattr(libc, "renameat2", None)
            flag = 0x00000001  # RENAME_NOREPLACE
        else:
            function, flag = None, 0
        if function is None:
            raise RuntimeError("candidate_publish_unavailable: exclusive atomic rename is unsupported")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(source_fd, os.fsencode(source.name), target_fd,
                          os.fsencode(destination.name), flag)
        if result != 0:
            code = ctypes.get_errno()
            raise FileExistsError(code, os.strerror(code), str(destination))
        for fd in {source_fd, target_fd}:
            try:
                os.fsync(fd)
            except OSError:
                # The rename is complete; report durability separately.
                pass
    finally:
        os.close(source_fd)
        os.close(target_fd)


def stage_candidate(*, content_path: Path, state_dir: Path, project_root: Path,
                    name: str, kind: str = "new", source_path: Path | None = None,
                    candidate_id: str | None = None, collision_roots: Sequence[Path] = (),
                    home_root: Path | None = None, target_root: Path | None = None) -> dict:
    """Copy a manually requested skill into a hash-bound candidate record."""
    name = _safe_name(name)
    if kind not in {"new", "revision"}:
        raise ValueError("candidate_kind_invalid: expected new or revision")
    if (kind == "revision") != (source_path is not None):
        raise ValueError("candidate_source_required: revisions need an existing source skill")
    target_roots = [Path(p).expanduser().absolute() for p in collision_roots]
    _check_collisions(name, target_roots,
                      allowed_source=source_path if kind == "revision" else None)
    cid = candidate_id or uuid.uuid4().hex
    destination = _candidate_dir(state_dir, cid)
    with _candidate_lock(Path(state_dir).absolute()):
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"candidate_collision: {destination}")
        destination.mkdir(parents=True, mode=0o700)
        try:
            candidate_path = destination / name
            source_hash = None
            resolved_source = None
            if source_path is not None:
                resolved_source = _real_directory(source_path, "source")
                source_hash = _hash(resolved_source)
                candidate_hash = _revision_snapshot(resolved_source, Path(content_path), candidate_path)
            else:
                candidate_hash = _snapshot(Path(content_path), candidate_path)
            if not (candidate_path / "SKILL.md").is_file() or (candidate_path / "SKILL.md").is_symlink():
                raise ValueError("candidate_skill_file_missing: SKILL.md is required")
            candidate_modes_map = _mode_map(candidate_path)
            candidate_modes = learning_promotion.tree_modes(candidate_path)
            _make_readonly(candidate_path)
            staged_modes = learning_promotion.tree_modes(candidate_path)
            canonical_project = _real_directory(project_root, "project_root")
            target_root = _approved_target_root(
                Path(target_root) if target_root else (resolved_source.parent if kind == "revision"
                                                       else canonical_project / ".agents" / "skills"),
                project_root, home_root, revision_source=resolved_source if kind == "revision" else None)
            record = {
                "schema_version": 1, "id": cid, "name": name, "kind": kind,
                "status": "staged", "candidate_path": str(candidate_path.resolve()),
                "candidate_hash": candidate_hash, "candidate_modes": candidate_modes,
                "candidate_modes_map": candidate_modes_map, "staged_modes": staged_modes,
                "source_path": str(resolved_source) if resolved_source else None,
                "source_hash": source_hash, "target_root": str(target_root),
                "project_root": str(canonical_project),
                "home_root": str(_real_directory(home_root, "home_root")) if home_root is not None else None,
                "candidate_eval_hash": _evaluation_hash(candidate_path),
                "source_eval_hash": _evaluation_hash(resolved_source) if resolved_source else None,
                "target_path": str(target_root / name), "created_at": time.time(),
                "record_revision": 0, "verification": None, "promotion": None, "catalog": None,
            }
            _write_json(destination / "candidate.json", record)
            return record
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise


def load_candidate(*, candidate_id: str, state_dir: Path) -> tuple[dict, Path]:
    directory = _candidate_dir(state_dir, candidate_id)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("candidate_not_found")
    record = _read_json(directory / "candidate.json")
    name = _safe_name(record.get("name"))
    skill = directory / name
    if (record.get("id") != candidate_id or _hash(skill) != record.get("candidate_hash") or
            learning_promotion.tree_modes(skill) != record.get("staged_modes")):
        raise ValueError("candidate_hash_mismatch: staged candidate changed")
    return record, skill


def _check_matrix(report: dict, workspace: Path, baseline: str) -> tuple[dict, str | None]:
    """Read sealed grading outputs and compare each candidate check to its baseline."""
    metadata = report.get("metadata") or {}
    rows = metadata.get("evals") or []
    repeats = metadata.get("repeats")
    if not rows or type(repeats) is not int or repeats < 1:
        return {}, "evaluation_check_matrix_missing"
    matrix = {}
    try:
        for row in rows:
            case_id = row["id"]
            for arm in (baseline, "with_skill"):
                for repeat in range(1, repeats + 1):
                    run_dir = workspace / f"eval-{case_id}" / arm / f"run-{repeat:03d}"
                    manifest = _read_json(run_dir / "grading-manifest.json")
                    grade = _read_json(run_dir / "grading.json")
                    rubric = _read_json(run_dir / "rubric.json")
                    if manifest.get("status") != "completed":
                        return {}, "evaluation_check_matrix_incomplete"
                    if hashlib.sha256((run_dir / "grading.json").read_bytes()).hexdigest() != manifest.get("result_sha256"):
                        return {}, "evaluation_check_matrix_tampered"
                    rubric_value = {key: rubric.get(key) for key in
                                    ["expectations", "assertions", "checks", "expected_output", "required_evidence"]}
                    rubric_digest = hashlib.sha256(json.dumps(
                        rubric_value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                    if rubric_digest != manifest.get("rubric_hash"):
                        return {}, "evaluation_check_matrix_tampered"
                    checks = grade.get("expectations") or []
                    if (not grade.get("evaluation_complete") or not checks or
                            any(type(check.get("passed")) is not bool for check in checks)):
                        return {}, "evaluation_check_matrix_incomplete"
                    for check in checks:
                        key = (case_id, check.get("text"))
                        if not key[1]:
                            return {}, "evaluation_check_matrix_invalid"
                        record = matrix.setdefault(key, {baseline: [], "with_skill": [], "deterministic": []})
                        record[arm].append(check["passed"])
                        record["deterministic"].append(check.get("source") == "deterministic")
    except (OSError, ValueError, KeyError, TypeError):
        return {}, "evaluation_check_matrix_unavailable"
    if not matrix or any(len(result[baseline]) != repeats or len(result["with_skill"]) != repeats
                         for result in matrix.values()):
        return {}, "evaluation_check_matrix_unpaired"
    return matrix, None


def _trusted_pass(report: dict, candidate_eval_hash: str, source_hash: str | None,
                  source_eval_hash: str | None, workspace: Path) -> tuple[bool, str, dict]:
    metadata = report.get("metadata") or {}
    if report.get("errors") or report.get("validation_errors"):
        return False, "evaluation_incomplete_or_invalid", {}
    if metadata.get("controlled_requested") is not True or metadata.get("environment_consistent") is not True:
        return False, "evaluation_not_controlled_or_environment_unstable", {}
    if metadata.get("candidate_hash") != candidate_eval_hash:
        return False, "evaluation_candidate_hash_mismatch", {}
    configs = metadata.get("configurations") or {}
    if "with_skill" not in configs or configs["with_skill"].get("snapshot_hash") != candidate_eval_hash:
        return False, "evaluation_candidate_binding_missing", {}
    baseline = "previous_skill" if source_hash is not None else "without_skill"
    baseline_config = configs.get(baseline)
    if not baseline_config or (source_hash is not None and baseline_config.get("snapshot_hash") != source_eval_hash):
        return False, "evaluation_source_binding_missing", {}
    rows = [row for row in report.get("runs", []) if row.get("included", True)]
    if not rows or any(row.get("status") != "completed" or not row.get("quality_complete") for row in rows):
        return False, "evaluation_runs_incomplete", {}
    arms = {row.get("configuration") for row in rows}
    if not {baseline, "with_skill"} <= arms:
        return False, "evaluation_has_no_complete_baseline_arm", {}
    matrix, error = _check_matrix(report, workspace, baseline)
    if error:
        return False, error, {}
    regressed, improved = [], []
    for (case_id, check), outcomes in sorted(matrix.items()):
        before, after = outcomes[baseline], outcomes["with_skill"]
        protected = {item.get("id") for item in metadata.get("evals", [])
                     if item.get("protected") is True or item.get("role") == "protected"}
        if sum(after) < sum(before) or (case_id in protected and not all(after)):
            regressed.append({"case_id": case_id, "check": check})
        if (not any(before) and all(after) and all(outcomes["deterministic"])):
            improved.append({"case_id": case_id, "check": check})
    if regressed:
        return False, "candidate_regressed_check_results", {"regressed": regressed, "improved": improved}
    if not improved:
        return False, "candidate_has_no_reproducible_deterministic_gain", {"regressed": [], "improved": []}
    return True, "passed", {"regressed": [], "improved": improved, "baseline": baseline}


def render_quality_review(workspace: Path) -> Path:
    """Create the full standalone review HTML for an existing quality workspace."""
    import subprocess
    workspace = Path(workspace).resolve(strict=True)
    benchmark = workspace / "benchmark.json"
    if not benchmark.is_file():
        raise ValueError("quality_review_input_missing: benchmark.json")
    output = workspace / "review.html"
    command = [sys.executable, str(Path(__file__).with_name("generate_review.py")),
               str(workspace), "--static", str(output), "--benchmark", str(benchmark)]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=120, check=False)
    if result.returncode or not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("quality_review_generation_failed")
    return output


def verify_candidate(*, candidate_id: str, state_dir: Path, evals: Path,
                     workspace: Path | None = None, model: str,
                     run_quality: Callable | None = None,
                     render_review: Callable | None = None, **quality_options) -> dict:
    """Run a fresh paired evaluation and bind its result to both snapshot hashes."""
    record, candidate = load_candidate(candidate_id=candidate_id, state_dir=state_dir)
    base_revision = record.get("record_revision", 0)
    if run_quality is None:
        from scripts.run_quality_eval import run_quality as runner
        run_quality = runner
    if workspace is None:
        workspace = Path(state_dir).absolute() / "candidates" / candidate_id / f"verification-{uuid.uuid4().hex}"
    workspace = Path(workspace).absolute()
    if workspace.exists():
        raise ValueError(f"verification_workspace_collision: {workspace}")
    source = Path(record["source_path"]) if record.get("source_path") else None
    if source is not None and _hash(source) != record.get("source_hash"):
        raise ValueError("candidate_source_hash_mismatch: source changed since staging")
    report = run_quality(candidate, evals, workspace, model, previous=source, **quality_options)
    if not isinstance(report, dict):
        raise ValueError("evaluation_report_invalid")
    renderer = render_review or render_quality_review
    try:
        review_path = Path(renderer(workspace)).resolve(strict=True)
    except Exception:
        review_path = None
    benchmark_path = workspace / "benchmark.json"
    report_file_hash = (hashlib.sha256(benchmark_path.read_bytes()).hexdigest()
                        if benchmark_path.is_file() else None)
    passed, reason, quality_evidence = _trusted_pass(
        report, record["candidate_eval_hash"], record.get("source_hash"),
        record.get("source_eval_hash"), workspace)
    if report_file_hash is None:
        passed, reason = False, "evaluation_report_artifact_missing"
    if review_path is None:
        passed, reason = False, "quality_review_generation_failed"
    record["verification"] = {
        "timestamp": time.time(), "workspace": str(workspace), "candidate_hash": record["candidate_hash"],
        "source_hash": record.get("source_hash"), "trusted_pass": passed, "reason": reason,
        "report": str(workspace / "benchmark.json"),
        "review": str(review_path) if review_path else None,
        "report_hash": report_file_hash,
        "quality_evidence": quality_evidence,
    }
    record["status"] = "verified" if passed else "verification_failed"
    _save_candidate(state_dir, candidate_id, record, base_revision)
    return {"candidate": record, "report": report, "trusted_pass": passed, "reason": reason}


def verify_catalog(target_path: Path, *, harness: str, catalog_probe: Callable) -> dict:
    """Record native catalog evidence; the probe must use the named harness API."""
    target = _real_directory(target_path, "published_skill")
    expected = _hash(target)
    evidence = catalog_probe(target, harness=harness)
    if not isinstance(evidence, dict) or evidence.get("harness") != harness:
        raise ValueError("catalog_probe_invalid: expected evidence for requested harness")
    loaded_path = evidence.get("path")
    loaded_hash = evidence.get("hash")
    matched = False
    if loaded_path and loaded_hash and evidence.get("enabled") is not False:
        try:
            matched = Path(loaded_path).resolve(strict=True) == target.resolve(strict=True) and loaded_hash == expected
        except OSError:
            matched = False
    return {**evidence, "target_path": str(target), "target_hash": expected,
            "loaded_path": str(loaded_path) if loaded_path else None,
            "loaded_hash": loaded_hash, "verified": matched}


def native_catalog_probe(root: Path, harness: str, models: dict | None = None) -> Callable:
    """Build a real native catalog probe for a supported harness.

    The returned callback is suitable for ``apply_candidate(catalog_probe=...)``.
    It never invokes a model. Codex and OpenCode use their native list endpoints;
    Hermes calls its active native catalog API in the current Hermes runtime.
    """
    root = _real_directory(root, "catalog_root")
    if harness not in {"codex", "chatgpt", "opencode", "hermes"}:
        raise ValueError("catalog_harness_unsupported")
    models = models or {}

    def probe(target_path: Path, *, harness: str) -> dict:
        if harness not in {"codex", "chatgpt", "opencode", "hermes"}:
            raise ValueError("catalog_harness_unsupported")
        target = _real_directory(target_path, "published_skill")
        skill_file = target / "SKILL.md"
        if not skill_file.is_file() or skill_file.is_symlink():
            raise ValueError("catalog_skill_file_missing")
        if harness in {"codex", "chatgpt"}:
            model_config = models if "codex" in models else {**models, "codex": models.get("chatgpt")}
            evidence = _probe_codex(root, target, model_config)
        elif harness == "opencode":
            evidence = _probe_opencode(root, target, models)
        else:
            evidence = _probe_hermes(root, target)
        return {"harness": harness,
                "path": str(target), "hash": _hash(target), **evidence}

    return probe


def _configured_model(models: dict, harness: str) -> dict:
    value = models.get(harness) if isinstance(models, dict) else None
    if value is None:
        raise ValueError(f"catalog_probe_model_required: configure a {harness} model reference")
    from scripts.harness import model_ref
    return model_ref(value, harness)


def _probe_codex(root: Path, target: Path, models: dict) -> dict:
    import subprocess
    script_root = Path(__file__).resolve().parent
    skill_file = target / "SKILL.md"
    launcher = shutil.which(os.environ.get("SKILLCRAFT_CODEX", "codex"))
    if not launcher:
        raise RuntimeError("codex_native_catalog_probe_unavailable: installed Codex CLI missing")
    try:
        binary = _trusted_catalog_executable(launcher, root, home_installs=(".local", ".cargo", ".codex"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"codex_native_catalog_probe_unavailable: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="skillcraft-catalog-codex-") as tmp:
        base = Path(tmp)
        output = base / "out"
        request_path = base / "request.json"
        request = {"schema_version": 1, "harness": "codex", "operation": "probe",
                   "mode": "quality", "directory": str(root), "output": str(output),
                   "prompt": "", "model": _configured_model(models, "codex"),
                   "skill_id": target.name, "skill_path": str(skill_file),
                   "skill_sha256": hashlib.sha256(skill_file.read_bytes()).hexdigest(),
                   "attach_skill": True, "controlled": False, "timeout_seconds": 90,
                   "run_id": uuid.uuid4().hex}
        request_path.write_text(json.dumps(request), encoding="utf-8")
        env = {"PATH": os.defpath, "TMPDIR": str(base), "SKILLCRAFT_CODEX": str(binary)}
        result = subprocess.run([sys.executable, "-I", "-B", str(script_root / "codex_runner.py"), str(request_path)],
                               cwd=script_root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, timeout=120, check=False)
        env_file = output / "environment.json"
        if result.returncode or not env_file.is_file():
            raise RuntimeError("codex_native_catalog_probe_failed")
        environment = _read_json(env_file)
        candidate = environment.get("candidate") or {}
        if Path(candidate.get("path", "")).resolve() != skill_file.resolve():
            raise ValueError("codex_native_catalog_candidate_mismatch")
        return {"catalog": "skills/list", "server_version": environment.get("server_version"),
                "enabled": candidate.get("autoinvoke")}


def _probe_opencode(root: Path, target: Path, models: dict) -> dict:
    import subprocess
    script_root = Path(__file__).resolve().parent
    runner = script_root / "opencode-runner"
    executable = runner / "node_modules" / ".bin" / "tsx"
    skill_file = target / "SKILL.md"
    try:
        relative_target = target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError("opencode_native_catalog_probe_unsupported_target: target is outside the project root") from exc
    source_prefix = relative_target.parts[:2]
    if source_prefix not in {(".agents", "skills"), (".opencode", "skills")}:
        raise RuntimeError("opencode_native_catalog_probe_unsupported_target: expected project .agents/skills or .opencode/skills")
    if not executable.is_file():
        raise RuntimeError("opencode_native_catalog_probe_unavailable: runner dependencies missing")
    cli = (os.environ.get("SKILLCRAFT_OPENCODE_BINARY")
           or shutil.which("opencode")
           or "/Applications/OpenCode.app/Contents/Resources/opencode-cli")
    try:
        cli_path = _trusted_catalog_executable(cli, root, home_installs=(".opencode", ".local"))
        git = "/usr/bin/git" if Path("/usr/bin/git").is_file() else shutil.which("git")
        node = shutil.which("node")
        if not git or not node:
            raise ValueError("Git and Node.js are required for the private OpenCode catalog")
        git = _trusted_catalog_executable(git, root, home_installs=(".local",))
        node = _trusted_catalog_executable(node, root, home_installs=(".local", ".nvm", ".volta", ".asdf"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"opencode_native_catalog_probe_unavailable: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="skillcraft-catalog-opencode-") as tmp:
        base = Path(tmp)
        profile = base / "profile"
        home = profile / "home"
        project = base / "project"
        project.mkdir()
        home.mkdir(parents=True)
        allowed_environment = {"USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG",
                               "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR"}
        isolated = {key: value for key, value in os.environ.items() if key in allowed_environment}
        isolated["HOME"] = str(home)
        native_path = [str(git.parent), str(node.parent), *os.defpath.split(os.pathsep)]
        isolated["PATH"] = os.pathsep.join(dict.fromkeys(native_path))
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            directory = profile / key.lower()
            directory.mkdir(parents=True)
            isolated[key] = str(directory)
        # The disposable location has no project extension directories; only
        # OpenCode's native catalog/configuration plugins are allowlisted.
        (project / "opencode.json").write_text(json.dumps(
            {"plugins": ["-*", "opencode.skill", "opencode.config.skill",
                         "opencode.config.compatibility", "opencode.vcs.git"],
             "update": "disable"}), encoding="utf-8")
        initialized = subprocess.run([str(git), "init", "--quiet"], cwd=project, env=isolated,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, timeout=15, check=False)
        if initialized.returncode:
            raise RuntimeError("opencode_native_catalog_probe_unavailable: isolated project init failed")
        native_target = project / relative_target
        native_target.parent.mkdir(parents=True)
        native_target.symlink_to(target.resolve(), target_is_directory=True)
        version = subprocess.run([str(cli_path), "--version"], cwd=Path("/"), env=isolated,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, timeout=15, check=False)
        cli_version = version.stdout.strip()
        if version.returncode or not cli_version:
            raise RuntimeError("opencode_native_catalog_probe_unavailable: installed CLI version could not be verified")

        service = subprocess.Popen([str(cli_path), "serve", "--hostname", "127.0.0.1", "--port", "0"],
                                   cwd=project, env=isolated, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            # Standalone serve prints a generated password; keep it in memory only.
            with selectors.DefaultSelector() as selector:
                selector.register(service.stdout, selectors.EVENT_READ)
                startup_deadline = time.monotonic() + 15
                output = b""
                password = None
                server_url = None
                while time.monotonic() < startup_deadline and service.poll() is None and not (password and server_url):
                    if selector.select(timeout=min(1, max(0, startup_deadline - time.monotonic()))):
                        output += os.read(service.stdout.fileno(), 4096)
                        password_match = re.search(rb"server password ([^\r\n]+)\r?\n", output)
                        url_match = re.search(rb"on\s+(http://127\.0\.0\.1:\d+)\r?\n", output)
                        if password_match:
                            password = password_match.group(1).decode("utf-8")
                        if url_match:
                            server_url = url_match.group(1).decode("ascii")
                if not password or not server_url:
                    raise RuntimeError("opencode_native_catalog_probe_unavailable: isolated server did not start")
            output = base / "out"
            request_path = base / "request.json"
            request = {"schema_version": 1, "operation": "catalog_probe", "directory": str(project),
                       "output": str(output), "prompt": "", "model": _configured_model(models, "opencode"),
                       "agent": "build", "timeout_seconds": 90, "mode": "quality",
                       "skill_id": target.name, "skill_path": str(skill_file),
                       "skill_sha256": hashlib.sha256(skill_file.read_bytes()).hexdigest(),
                       "attach_skill": True, "permissions": [], "controlled": False,
                       "run_id": uuid.uuid4().hex}
            request_path.write_text(json.dumps(request), encoding="utf-8")
            runner_env = {**isolated, "SKILLCRAFT_SERVER": server_url,
                          "SKILLCRAFT_HEADERS": json.dumps({"authorization": "Basic " +
                                                            b64encode(f"opencode:{password}".encode()).decode()})}
            result = subprocess.run([str(node), str(executable), str(runner / "src" / "main.ts"), str(request_path)],
                                    cwd=Path("/"), env=runner_env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, check=False)
            env_file = output / "environment.json"
            if result.returncode or not env_file.is_file():
                raise RuntimeError("opencode_native_catalog_probe_failed")
            environment = _read_json(env_file)
            candidate = environment.get("candidate") or {}
            server_version = environment.get("server_version")
            cli_release = re.fullmatch(r"(?:opencode v)?(\d+\.\d+\.\d+(?:-[\w.-]+)?)", cli_version)
            if not cli_release or cli_release.group(1) != server_version:
                raise RuntimeError("opencode_native_catalog_probe_failed: CLI and private server versions differ")
            if Path(candidate.get("path", "")).resolve() != skill_file.resolve():
                raise ValueError("opencode_native_catalog_candidate_mismatch")
            return {"catalog": "skill.list", "server_version": server_version,
                    "enabled": candidate.get("autoinvoke"), "cli_path": str(cli_path),
                    "cli_version": cli_version, "isolated_profile": True,
                    "native_catalog_path": candidate.get("path"),
                    "native_catalog_realpath": candidate.get("realpath"),
                    "plugins": environment.get("plugins"),
                    "catalog_source": "/".join(source_prefix),
                    "catalog_scope": "native project discovery at the target's original relative path; disposable symlink must resolve to installed target",
                    "project_plugins_absent": True, "builtin_plugins_allowlisted": True}
        finally:
            if service.poll() is None:
                service.terminate()
                try:
                    service.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=5)
            if service.stdout:
                service.stdout.close()


def _probe_hermes(root: Path, target: Path) -> dict:
    import subprocess
    import shlex
    try:
        project_root = Path(root).resolve()
        interpreter = os.environ.get("SKILLCRAFT_HERMES_PYTHON")
        if not interpreter:
            launcher_path = shutil.which(os.environ.get("SKILLCRAFT_HERMES_BIN", "hermes"))
            if not launcher_path:
                raise ValueError("Hermes CLI is unavailable")
            launcher = _trusted_catalog_executable(launcher_path, project_root, home_installs=(".hermes",))
            try:
                with launcher.open("rb") as handle:
                    first_line = handle.readline(4096).decode("utf-8").strip()
            except (OSError, UnicodeError):
                first_line = ""
            if first_line.startswith("#!"):
                command = shlex.split(first_line[2:].strip())
                interpreter_candidate = (command[1] if len(command) > 1 and
                                         Path(command[0]).name == "env" else
                                         command[0] if command else "")
                if Path(interpreter_candidate).name.startswith("python"):
                    interpreter = (shutil.which(interpreter_candidate) if not Path(interpreter_candidate).is_absolute()
                                   else interpreter_candidate)
            interpreter = interpreter or sys.executable
        interpreter = str(_trusted_catalog_executable(interpreter, project_root, home_installs=(".hermes",)))
        program = (
            "import hashlib, json, sys; "
            "from tools.skills_tool import _skill_catalog, skill_view, skills_list; "
            "name = sys.argv[1]; "
            "matches = [str(s.get('path')) for s in _skill_catalog() if s.get('load_name') == name]; "
            "listing = json.loads(skills_list()); "
            "listed = sum(s.get('name') == name for s in listing.get('skills', [])); "
            "view = json.loads(skill_view(name, preprocess=False)) if matches and listed == 1 else {}; "
            "content = view.get('content'); "
            "print(json.dumps({'matches': matches, 'listed': listed, 'success': view.get('success'), "
            "'directory': view.get('skill_dir'), 'content_hash': "
            "hashlib.sha256(content.encode()).hexdigest() if isinstance(content, str) else None}))"
        )
        profile = os.environ.get("SKILLCRAFT_HERMES_HOME") or os.environ.get("HERMES_HOME")
        profile = str(Path(profile).expanduser().resolve()) if profile else str(Path.home() / ".hermes")
        # Catalog inspection needs no provider credentials. Keep dynamic-loader,
        # Python startup, and credential variables from the caller out of scope.
        environment = {"PATH": os.defpath,
                       "HOME": str(Path.home()), "HERMES_HOME": profile,
                       "TERMINAL_CWD": str(project_root)}
        result = subprocess.run([interpreter, "-I", "-B", "-c", program, target.name], cwd=Path("/"), env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=45, check=False)
        if result.returncode:
            raise ValueError("native Hermes catalog process failed")
        response = json.loads(result.stdout.strip().splitlines()[-1])
        expected = hashlib.sha256((target / "SKILL.md").read_text(encoding="utf-8-sig").encode()).hexdigest()
        matches = [Path(path).resolve() for path in response["matches"]]
        if (response["listed"] != 1 or response["success"] is not True or len(matches) != 1
                or matches[0] not in {target.resolve(), (target / "SKILL.md").resolve()}
                or Path(response["directory"] or "/").resolve() != target.resolve()
                or response["content_hash"] != expected):
            raise ValueError("Native catalog did not return the exact enabled skill")
        return {"catalog": "Hermes skills_list/skill_view", "enabled": True, "content_hash": expected}
    except (ImportError, OSError, ValueError, TypeError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("hermes_native_catalog_probe_unavailable") from exc


def _trusted_catalog_executable(value: str | Path, project_root: Path, *, home_installs=()) -> Path:
    """Accept only executable paths protected from project and other-user writes.

    Validate configured paths and all symlink hops. Preserve the launcher's
    lexical location so a linked Python executable can still find its venv.
    """
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        raise ValueError("Catalog runtime path must be absolute")
    project_root = Path(project_root).resolve()
    owners = {0, os.geteuid()}
    lexical = Path(os.path.abspath(configured))
    home = Path.home().resolve()
    admin_gid = None
    if sys.platform == "darwin":
        import grp
        try:
            admin_gid = grp.getgrnam("admin").gr_gid
        except KeyError:
            pass

    def enrolled_path(path: Path) -> bool:
        return path.is_relative_to(project_root) and not (
            project_root == home and any(path.is_relative_to(home / install) for install in home_installs))

    def trusted_writes(info):
        # macOS administrators already control desktop and Homebrew installs.
        return (not info.st_mode & stat.S_IWOTH
                and (not info.st_mode & stat.S_IWGRP or info.st_gid == admin_gid))

    if enrolled_path(lexical):
        raise ValueError("Catalog runtime cannot be inside the enrolled project")

    # Walk every symlink hop. Looking only at the final realpath would miss
    # intermediary directories that can replace a link before execution.
    current = Path(lexical.anchor)
    pending = deque(lexical.parts[1:])
    links = 0
    while pending:
        part = pending.popleft()
        if part == "..":
            current = current.parent
            continue
        entry = current / part
        if project_root != home and enrolled_path(entry):
            raise ValueError("Catalog runtime cannot be inside the enrolled project")
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in owners or links >= 40:
                raise ValueError("Catalog runtime path has an untrusted symlink")
            links += 1
            link = entry.readlink()
            if link.is_absolute():
                current = Path(link.anchor)
            pending.extendleft(reversed(link.parts[1:] if link.is_absolute() else link.parts))
        else:
            if pending:
                sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners
                        or (not trusted_writes(info) and not sticky_root)):
                    raise ValueError("Catalog runtime has an untrusted parent directory")
            current = entry

    if enrolled_path(current):
        raise ValueError("Catalog runtime cannot be inside the enrolled project")
    info = current.stat()
    if (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111
            or info.st_uid not in owners or not trusted_writes(info)):
        raise ValueError("Catalog runtime executable is not trusted")
    # Keep the lexical venv path as argv[0]. Python uses it to locate the venv's
    # site-packages even when the executable itself is a symlink to base Python.
    return lexical


def apply_candidate(*, candidate_id: str, state_dir: Path, project_root: Path,
                    manual: bool = False, catalog_probe: Callable | None = None,
                    harness: str | None = None, home_root: Path | None = None,
                    collision_roots: Sequence[Path] = ()) -> dict:
    """Publish a staged candidate after trusted A/B or an explicit manual action."""
    record, candidate = load_candidate(candidate_id=candidate_id, state_dir=state_dir)
    _approved_target_root(Path(record["target_root"]), project_root, home_root,
                          revision_source=Path(record["source_path"]) if record["kind"] == "revision" else None)
    verification = record.get("verification") or {}
    report_path = Path(verification.get("report", "")) if verification.get("report") else None
    report_hash_matches = bool(report_path and report_path.is_file() and
                               hashlib.sha256(report_path.read_bytes()).hexdigest() == verification.get("report_hash"))
    verified = (verification.get("trusted_pass") is True and
                verification.get("candidate_hash") == record.get("candidate_hash") and
                verification.get("source_hash") == record.get("source_hash") and report_hash_matches)
    if not verified and not manual:
        raise ValueError("candidate_verification_required: use verify-now or explicit manual apply")
    if not manual and (catalog_probe is None or not harness):
        raise ValueError("catalog_verification_required: automatic apply needs a native catalog probe")
    target = Path(record["target_path"])
    if target.name != record["name"] or target.parent.resolve() != Path(record["target_root"]).resolve():
        raise ValueError("candidate_target_mismatch")
    state = Path(state_dir).absolute() / "promotion" / candidate_id
    state.mkdir(parents=True, exist_ok=True)
    candidate_file = _candidate_dir(state_dir, candidate_id) / "candidate.json"
    record_revision = record.get("record_revision", 0)
    with _candidate_lock(Path(state_dir).absolute()):
        current = _read_json(candidate_file)
        if current.get("record_revision", 0) != record_revision:
            raise ValueError("candidate_state_conflict: state changed before apply")
        record = current
        pending = record.get("promotion") if record.get("status") == "applying" else None
        allowed_source = record.get("source_path") if record["kind"] == "revision" else None
        if pending and record["kind"] == "new":
            staged = Path(pending.get("stage_path", ""))
            if (pending.get("created") is not True or pending.get("source") != str(target.absolute())
                    or pending.get("after_hash") != record["candidate_hash"]
                    or not learning_promotion._valid_exchange_path(target, staged, state, "candidate")):
                raise ValueError("candidate_promotion_intent_mismatch")
            if target.exists() or target.is_symlink():
                if (target.is_symlink() or not target.is_dir() or staged.is_symlink() or staged.exists()
                        or not learning_promotion._matches_tree(target, record["candidate_hash"],
                                                                record["candidate_modes"])):
                    raise ValueError("candidate_collision: pending target differs from the published candidate")
                allowed_source = target
        _check_collisions(record["name"], collision_roots, allowed_source=allowed_source)
        if pending and not pending.get("pending"):
            if _hash(target) != record["candidate_hash"]:
                raise ValueError("candidate_collision: pending published tree changed")
            promotion = pending
        elif record["kind"] == "new":
            if pending:
                if allowed_source is None:
                    if (staged.is_symlink() or (staged.exists() and not learning_promotion._matches_tree(
                            staged, record["candidate_hash"], record["candidate_modes"]))):
                        raise ValueError("candidate_promotion_stage_mismatch")
                    if not staged.exists():
                        staged = learning_promotion._exchange_parent(target, state) / f".{target.name}.candidate-{uuid.uuid4().hex}"
                        shutil.copytree(candidate, staged, symlinks=False, copy_function=shutil.copy2)
                        _apply_mode_map(staged, record["candidate_modes_map"])
                        if _hash(staged) != record["candidate_hash"]:
                            shutil.rmtree(staged, ignore_errors=True)
                            raise ValueError("candidate_hash_mismatch: copy changed")
                        if learning_promotion.tree_modes(staged) != record["candidate_modes"]:
                            shutil.rmtree(staged, ignore_errors=True)
                            raise ValueError("candidate_mode_mismatch: copied permissions differ")
                        pending["stage_path"] = str(staged)
                        record["promotion"] = pending
                        record = _save_candidate_unlocked(state_dir, candidate_id, record, record_revision)
                        record_revision = record["record_revision"]
                    _rename_noreplace(staged, target)
                promotion = {**pending, "pending": False, "after_modes": learning_promotion.tree_modes(target)}
            else:
                if target.exists() or target.is_symlink():
                    raise ValueError(f"candidate_collision: target already exists: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = learning_promotion._exchange_parent(target, state) / f".{target.name}.candidate-{uuid.uuid4().hex}"
                if _hash(candidate) != record["candidate_hash"]:
                    raise ValueError("candidate_hash_mismatch")
                shutil.copytree(candidate, staged, symlinks=False, copy_function=shutil.copy2)
                _apply_mode_map(staged, record["candidate_modes_map"])
                if _hash(staged) != record["candidate_hash"]:
                    shutil.rmtree(staged, ignore_errors=True)
                    raise ValueError("candidate_hash_mismatch: copy changed")
                if learning_promotion.tree_modes(staged) != record["candidate_modes"]:
                    shutil.rmtree(staged, ignore_errors=True)
                    raise ValueError("candidate_mode_mismatch: copied permissions differ")
                intent = {"source": str(target.absolute()), "before_hash": None,
                          "after_hash": record["candidate_hash"], "created": True,
                          "pending": True, "stage_path": str(staged),
                          "pending_catalog": {"harness": harness, "status": "pending"}}
                record["promotion"] = intent
                record["status"] = "applying"
                record = _save_candidate_unlocked(state_dir, candidate_id, record, record_revision)
                record_revision = record["record_revision"]
                try:
                    _rename_noreplace(staged, target)
                except Exception:
                    if staged.exists() or not target.exists():
                        record["promotion"] = None
                        record["status"] = "verified" if verified else "staged"
                        _save_candidate_unlocked(state_dir, candidate_id, record, record_revision)
                        shutil.rmtree(staged, ignore_errors=True)
                    raise
                promotion = {**intent, "pending": False,
                             "after_modes": learning_promotion.tree_modes(target)}
        else:
            source = Path(record["source_path"])
            if source.resolve() != target.resolve():
                raise ValueError("candidate_source_hash_mismatch: source differs from target")
            recovery_path = state / "promotion-record.json"
            if recovery_path.is_symlink():
                raise ValueError("candidate_recovery_record_symlink")
            recovery = _read_json(recovery_path) if recovery_path.is_file() else None
            if pending:
                if pending.get("source") != str(source.resolve()) or pending.get("after_hash") != record["candidate_hash"]:
                    raise ValueError("candidate_promotion_intent_mismatch")
                if _hash(source) == record["candidate_hash"]:
                    if (not recovery or recovery.get("after_hash") != record["candidate_hash"] or
                            not learning_promotion.recover_exchange(source, recovery, state)):
                        raise ValueError("candidate_recovery_record_mismatch")
                    promotion = {**recovery, "pending": False}
                else:
                    if _hash(source) != record["source_hash"]:
                        raise ValueError("candidate_source_hash_mismatch: source changed during pending promotion")
                    deployment = state / "candidate-deployment"
                    if deployment.exists():
                        shutil.rmtree(deployment)
                    shutil.copytree(candidate, deployment, symlinks=False, copy_function=shutil.copy2)
                    _apply_mode_map(deployment, record["candidate_modes_map"])
                    promotion = learning_promotion.promote(
                        source, deployment, record["source_hash"], state,
                        expected_candidate_hash=record["candidate_hash"],
                        expected_candidate_modes=record["candidate_modes"],
                        record_path=recovery_path,
                        expected_source_modes=learning_promotion.tree_modes(source))
                    shutil.rmtree(deployment, ignore_errors=True)
                    promotion["pending"] = False
            else:
                if _hash(source) != record["source_hash"]:
                    raise ValueError("candidate_source_hash_mismatch: revision source changed")
                intent = {"source": str(source.resolve()), "before_hash": record["source_hash"],
                          "after_hash": record["candidate_hash"], "created": False, "pending": True,
                          "pending_catalog": {"harness": harness, "status": "pending"}}
                record["promotion"] = intent
                record["status"] = "applying"
                record = _save_candidate_unlocked(state_dir, candidate_id, record, record_revision)
                record_revision = record["record_revision"]
                deployment = state / "candidate-deployment"
                if deployment.exists():
                    shutil.rmtree(deployment)
                shutil.copytree(candidate, deployment, symlinks=False, copy_function=shutil.copy2)
                _apply_mode_map(deployment, record["candidate_modes_map"])
                promotion = learning_promotion.promote(
                    source, deployment, record["source_hash"], state,
                    expected_candidate_hash=record["candidate_hash"],
                    expected_candidate_modes=record["candidate_modes"], record_path=recovery_path,
                    expected_source_modes=learning_promotion.tree_modes(source))
                shutil.rmtree(deployment, ignore_errors=True)
                promotion["pending"] = False
        record["promotion"] = promotion
        record["catalog"] = {"verified": False, "reason": "pending_native_probe",
                             "harness": harness}
        record["status"] = "applying"
        record = _save_candidate_unlocked(state_dir, candidate_id, record, record_revision)
        record_revision = record["record_revision"]
    if catalog_probe is not None and harness:
        try:
            catalog = verify_catalog(target, harness=harness, catalog_probe=catalog_probe)
        except Exception as exc:
            record["catalog"] = {"verified": False, "reason": f"catalog_probe_error:{type(exc).__name__}",
                                 "harness": harness}
            record = _save_candidate(state_dir, candidate_id, record, record_revision)
            record_revision = record["record_revision"]
            rollback_candidate(candidate_id=candidate_id, state_dir=state_dir)
            raise ValueError("catalog_verification_failed: promotion rolled back") from exc
        record["catalog"] = catalog
        record = _save_candidate(state_dir, candidate_id, record, record_revision)
        record_revision = record["record_revision"]
        if not catalog["verified"]:
            rollback_candidate(candidate_id=candidate_id, state_dir=state_dir)
            raise ValueError("catalog_verification_failed: promotion rolled back")
    else:
        record["catalog"] = {"verified": False, "reason": "native_catalog_probe_unavailable"}
    record["status"] = ("applied_verified" if verified else
                        "applied_manual" if record["catalog"].get("verified") else
                        "applied_manual_unverified")
    try:
        record = _save_candidate(state_dir, candidate_id, record, record_revision)
    except ValueError:
        # A concurrent verify/retry may have updated state while the native
        # catalog was being queried. Restore the live tree instead of clobbering it.
        rollback_candidate(candidate_id=candidate_id, state_dir=state_dir)
        raise
    return record


def rollback_candidate(*, candidate_id: str, state_dir: Path) -> dict:
    """Restore the prior revision or remove a newly created skill if its hash is intact."""
    record, _ = load_candidate(candidate_id=candidate_id, state_dir=state_dir)
    revision = record.get("record_revision", 0)
    with _candidate_lock(Path(state_dir).absolute()):
        current = _read_json(_candidate_dir(state_dir, candidate_id) / "candidate.json")
        if current.get("record_revision", 0) != revision:
            raise ValueError("candidate_state_conflict: state changed before rollback")
        record = current
        promotion = record.get("promotion")
        if not promotion or promotion.get("pending"):
            raise ValueError("candidate_not_applied")
        target = Path(promotion["source"])
        if target.resolve() != Path(record.get("target_path", "")).resolve():
            raise ValueError("rollback_source_mismatch: promotion target differs from candidate target")
        _approved_target_root(Path(record["target_root"]),
                              Path(record["project_root"]),
                              Path(record["home_root"]) if record.get("home_root") else None,
                              revision_source=Path(record["source_path"]) if record["kind"] == "revision" else None)
        if record.get("status") == "rolled_back":
            return record
        if promotion.get("created"):
            state = Path(state_dir).absolute() / "promotion" / candidate_id
            state.mkdir(parents=True, exist_ok=True)
            tombstone = record.get("rollback_stage")
            archive = (Path(tombstone) if tombstone else learning_promotion._exchange_parent(target, state)
                       / f".{target.name}.rollback-{uuid.uuid4().hex}")
            if not learning_promotion._valid_exchange_path(target, archive, state, "rollback"):
                raise ValueError("rollback_stage_mismatch")
            if not archive.exists():
                if (_hash(target) != promotion.get("after_hash") or
                        learning_promotion.tree_modes(target) != promotion.get("after_modes")):
                    raise ValueError("rollback_hash_mismatch: published skill changed after apply")
                record["rollback_stage"] = str(archive)
                record["status"] = "rolling_back"
                record = _save_candidate_unlocked(state_dir, candidate_id, record, revision)
                revision = record["record_revision"]
                _rename_noreplace(target, archive)
            if (_hash(archive) != promotion.get("after_hash") or
                    learning_promotion.tree_modes(archive) != promotion.get("after_modes")):
                if not target.exists() and not target.is_symlink():
                    _rename_noreplace(archive, target)
                raise ValueError("rollback_hash_mismatch: published skill changed during removal")
            result = {"rolled_back": True, "removed": str(target), "archive": str(archive)}
        else:
            result = learning_promotion.rollback(target, promotion,
                                                 Path(state_dir).absolute() / "promotion" / candidate_id)
        record["rollback"] = result
        record["status"] = "rolled_back"
        record = _save_candidate_unlocked(state_dir, candidate_id, record, revision)
        if promotion.get("created"):
            shutil.rmtree(archive, ignore_errors=True)
        return record
