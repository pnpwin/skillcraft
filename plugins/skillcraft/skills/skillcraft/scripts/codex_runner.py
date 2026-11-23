"""Isolated Codex App Server adapter used by SkillCraft.

The runner deliberately has no dependency on the Codex Python SDK. It speaks
the newline-delimited JSON-RPC protocol exposed by `codex app-server` and keeps
the request/response transcript in the same root-level artifact contract as
the OpenCode runner.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid


SERVER_TIMEOUT = 30
MAX_COMMAND_OUTPUT_CHARS = 16_000
_OUTPUT_TRUNCATION = "\n[... command output truncated ...]\n"
_TERMINAL_CONTROLS = re.compile(
    r"\x1b\][^\x1b\x07]*(?:\x07|\x1b\\)|"
    r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)


def _json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_event(event: dict) -> dict:
    """Keep protocol shape and IDs, omit all free text and arbitrary payloads."""
    method = event.get("method", "")
    params = event.get("params") or {}
    result = {"method": method}
    for key in ("threadId", "turnId", "itemId"):
        value = params.get(key)
        if isinstance(value, str):
            result[key] = value
    item = params.get("item")
    if isinstance(item, dict):
        result["item"] = {k: item[k] for k in ("id", "type") if isinstance(item.get(k), (str, int))}
        # Skill item metadata is structured evidence, not prompt/response text.
        if item.get("type") == "userMessage":
            content = item.get("content") or []
            skills = [{k: el.get(k) for k in ("type", "name", "path")}
                      for el in content if isinstance(el, dict) and el.get("type") == "skill"]
            if skills:
                result["item"]["skills"] = skills
    turn = params.get("turn")
    if isinstance(turn, dict):
        result["turn"] = {k: turn[k] for k in ("id", "status") if isinstance(turn.get(k), str)}
    return result


def _safe_start_error(message: str) -> str:
    """Bound thread-start diagnostics without exposing paths or account data."""
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", message)
    value = re.sub(r"(?i)((?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|credential)(?:=|\s*:\s*)\s*)[^\s,;]+",
                   r"\1[redacted]", value)
    value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[account]", value)
    value = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "[id]", value)
    value = re.sub(r"(?:/Users/|/private/|/tmp/|/var/|/home/)[^\s,;]+", "[path]", value)
    return value[:320]


class _Rpc:
    def __init__(self, process, output: Path):
        self.process = process
        self.output = output
        self.inbox: queue.Queue = queue.Queue()
        self.pending: dict[str, queue.Queue] = {}
        self.lock = threading.Lock()
        self.stderr_chunks: list[str] = []
        self.notifications: list[dict] = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()
        self.next_id = 1

    def _read_stderr(self):
        for line in iter(self.process.stderr.readline, ""):
            # stderr is intentionally not written to artifacts: diagnostics may
            # include account metadata or user-provided text.
            self.stderr_chunks.append(line[:2000])

    def _read(self):
        for line in iter(self.process.stdout.readline, ""):
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if "id" in obj and ("result" in obj or "error" in obj):
                with self.lock:
                    target = self.pending.get(str(obj["id"]))
                if target:
                    target.put(obj)
            elif isinstance(obj.get("method"), str):
                self.notifications.append(obj)
                with self.lock:
                    for target in self.pending.values():
                        target.put({"notification": obj})
            else:
                self.inbox.put(obj)

    def request(self, method: str, params: dict | None = None, timeout=SERVER_TIMEOUT):
        reqid = str(self.next_id)
        self.next_id += 1
        response: queue.Queue = queue.Queue()
        with self.lock:
            self.pending[reqid] = response
        try:
            self.process.stdin.write(json.dumps({"id": reqid, "method": method, "params": params or {}}) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"Codex app-server request timed out: {method}")
                try:
                    obj = response.get(timeout=left)
                except queue.Empty:
                    raise TimeoutError(f"Codex app-server request timed out: {method}")
                if "notification" in obj:
                    self._handle_server_request(obj["notification"])
                    continue
                if obj.get("error"):
                    code = obj["error"].get("code")
                    suffix = f" (code {code})" if isinstance(code, int) else ""
                    message = str(obj["error"].get("message", ""))
                    if "sandbox_apply" in message and ("Operation not permitted" in message or "status exit status: 71" in message):
                        raise RuntimeError("Codex App Server could not start its macOS sandbox because this runner is already sandboxed; rerun outside the nested sandbox so App Server can enforce its own profile")
                    if method == "thread/start" and message:
                        raise RuntimeError(f"Codex app-server thread/start failed: {_safe_start_error(message)}")
                    raise RuntimeError(f"Codex app-server rejected {method}{suffix}")
                return obj.get("result")
        finally:
            with self.lock:
                self.pending.pop(reqid, None)

    def _handle_server_request(self, event):
        # Never authorize commands, edits, or other privileged operations.
        method = event.get("method", "")
        if not method.endswith("/requestApproval") and method not in {
            "item/tool/requestUserInput", "item/tool/request_user_input"
        }:
            return
        reqid = event.get("id")
        if reqid is None:
            return
        if "commandExecution" in method:
            decision = {"decision": "decline"}
        elif "fileChange" in method:
            decision = {"decision": "decline"}
        else:
            decision = {"answers": {}}
        self.process.stdin.write(json.dumps({"id": reqid, "result": decision}) + "\n")
        self.process.stdin.flush()


class _OtelCollector(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        self.records: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(min(size, 2_000_000))
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
                for event in _find_skill_events(payload):
                    outer.records.append(event)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                return

        super().__init__(("127.0.0.1", 0), Handler)


def _find_skill_events(value):
    found = []
    if isinstance(value, dict):
        attrs = value.get("attributes")
        flat = {}
        if isinstance(attrs, list):
            for item in attrs:
                if isinstance(item, dict) and isinstance(item.get("key"), str):
                    val = item.get("value")
                    if isinstance(val, dict):
                        val = next((v for k, v in val.items() if k.endswith("Value")), None)
                    flat[item["key"]] = val
        elif isinstance(attrs, dict):
            flat = attrs
        event_name = flat.get("event.name") or value.get("eventName") or value.get("name")
        invocation_type = flat.get("skill.invocation_type") or flat.get("invocation_type")
        if event_name == "codex.skill_invocation" or invocation_type in {"implicit", "explicit"}:
            skill = flat.get("skill.name") or flat.get("skill_name") or flat.get("skillName") or flat.get("name")
            turn_id = flat.get("turn.id") or flat.get("turn_id") or flat.get("turnId")
            implicit = invocation_type == "implicit" if invocation_type in {"implicit", "explicit"} else flat.get("implicit")
            if isinstance(skill, str) and isinstance(turn_id, str):
                found.append({"kind": "skill_invocation", "event_name": event_name, "turn_id": turn_id,
                              "skill_name": skill, "implicit": implicit if isinstance(implicit, bool) else None})
        event_kind = flat.get("event.kind")
        event_label = str(event_kind or event_name or "").lower().replace("_", ".")
        if event_label in {"response.completed", "response.complete"} or event_label.startswith("api.response"):
            turn_id = flat.get("turn.id") or flat.get("turn_id") or flat.get("turnId")
            found.append({"kind": "transport_health", "event_name": str(event_name), "event_kind": str(event_kind or ""),
                          "turn_id": turn_id if isinstance(turn_id, str) else None,
                          "conversation_scoped": not isinstance(turn_id, str)})
        for child in value.values():
            found.extend(_find_skill_events(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_skill_events(child))
    return found


def _read_cli_version(binary, cwd, env):
    result = subprocess.run([str(binary), "--version"], cwd=cwd, env=env,
                            text=True, capture_output=True, timeout=15)
    fields = result.stdout.strip().split(maxsplit=2)
    if result.returncode != 0 or len(fields) < 2 or fields[0] != "codex-cli":
        raise RuntimeError("Could not read the Codex CLI version from codex --version")
    return fields[1]


def _environment(candidate, skills, controlled, version, mode="quality"):
    background = []
    for skill in skills:
        if skill.get("name") == (candidate or {}).get("id"):
            continue
        content_hash = None
        try:
            content_hash = _sha(Path(skill["path"]))
        except (KeyError, OSError, TypeError):
            pass
        identity = {"name": skill.get("name"), "description": skill.get("description"),
                    "enabled": skill.get("enabled"), "content_sha256": content_hash}
        body = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
        background.append({"id": skill.get("name"), "hash": hashlib.sha256(body).hexdigest(),
                           "autoinvoke": skill.get("enabled")})
    background.sort(key=lambda item: str(item["id"]))
    profile = {"permissions": "skillcraft", "minimal": "read",
               "workspace_roots": "read", "outputs": "read" if mode in {"judge", "optimizer"} else "write",
               "isolated_codex_home": "deny", "network": False,
               "shell_environment_inherit": "none", "otel": "otlp-http-json-loopback"}
    fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    return {"kind": "codex-app-server", "harness": "codex", "server_version": version,
            "controlled": bool(controlled), "candidate": candidate,
            "background": background, "plugins": [], "configuration_fingerprint": fingerprint}


def _isolated_home(*, catalog_only=False):
    root = Path(tempfile.mkdtemp(prefix="skillcraft-codex-home-"))
    home = root / "codex"
    home.mkdir(mode=0o700)
    if catalog_only:
        return root, home
    source = os.environ.get("SKILLCRAFT_CODEX_HOME")
    if source:
        source_path = Path(source).expanduser().resolve()
        if source_path.is_dir() and (source_path / "auth.json").is_file():
            shutil.copy2(source_path / "auth.json", home / "auth.json")
            os.chmod(home / "auth.json", 0o600)
        else:
            raise ValueError("SKILLCRAFT_CODEX_HOME must point to an authorized Codex home with auth.json")
    elif not os.environ.get("CODEX_API_KEY"):
        raise ValueError("Provide an authorized SKILLCRAFT_CODEX_HOME or invocation-scoped CODEX_API_KEY")
    return root, home


def _configure_home(home: Path, workspace: Path, collector_port: int, mode: str, codex_binary: Path):
    """Create a Codex-only permission profile and a loopback OTel exporter."""
    outputs = workspace / "outputs"
    output_permission = "read" if mode in {"judge", "optimizer"} else "write"
    config = home / "config.toml"
    lines = [
        'default_permissions = "skillcraft"',
        "", "[analytics]", "enabled = false", "",
        "[shell_environment_policy]", 'inherit = "none"', "",
        "[permissions.skillcraft.filesystem]",
        '":minimal" = "read"',
        '":workspace_roots" = "read"',
        f"{json.dumps(str(codex_binary))} = \"read\"",
        f"{json.dumps(str(outputs))} = {json.dumps(output_permission)}",
        f"{json.dumps(str(home))} = \"deny\"",
        "", "[permissions.skillcraft.network]", "enabled = false", "",
        "[otel]",
        'exporter = { "otlp-http" = { endpoint = ' + json.dumps(f"http://127.0.0.1:{collector_port}") + ', protocol = "json" } }',
    ]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(config, 0o600)
    return config


def _validate_request(r):
    if r.get("schema_version") != 1 or r.get("harness", "codex") != "codex":
        raise ValueError("Invalid Codex runner request")
    if r.get("operation", "run") not in {"run", "probe"}:
        raise ValueError("operation must be run or probe")
    if r.get("mode") not in {"quality", "trigger", "judge", "optimizer"}:
        raise ValueError("mode must be quality, trigger, judge, or optimizer")
    if not Path(r.get("directory", "")).is_absolute() or not Path(r.get("output", "")).is_absolute():
        raise ValueError("directory and output must be absolute paths")
    model = r.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
        raise ValueError("model must be an object with an id")
    if model.get("effort") is not None and (not isinstance(model["effort"], str) or
                                              not model["effort"] or any(c.isspace() for c in model["effort"])):
        raise ValueError("model.effort must be a non-empty token")
    if float(r.get("timeout_seconds", 180)) <= 0:
        raise ValueError("timeout_seconds must be positive")
    skill_path = r.get("skill_path")
    if skill_path and not Path(skill_path).is_absolute():
        raise ValueError("skill_path must be absolute")
    if r.get("attach_skill") and not skill_path:
        raise ValueError("attach_skill requires skill_path")


def _messages(r, items, thread_id, turn_id):
    prompt = r.get("prompt", "")
    skill = None
    if r.get("attach_skill"):
        skill = {"type": "skill", "name": r.get("skill_id") or Path(r["skill_path"]).parent.name,
                 "path": r["skill_path"]}
    user_content = [{"type": "text", "text": prompt}]
    if skill:
        user_content.append(skill)
    messages = [{"type": "user", "id": f"{turn_id}:user", "content": user_content}]
    workspace = Path(r["directory"]).resolve()
    for item in items:
        if item.get("type") in {"agentMessage", "assistantMessage"}:
            text = item.get("text")
            if isinstance(text, str) and text:
                messages.append({"type": "assistant", "id": item.get("id", f"{turn_id}:assistant"),
                                 "phase": item.get("phase"), "content": [{"type": "text", "text": text}]})
        elif item.get("type") == "commandExecution":
            cwd = _relative_workspace_path(item.get("cwd"), workspace)
            command = _sanitize_command(item.get("command", ""))
            state = {"status": item.get("status"), "input": {"command": command, "cwd": cwd}}
            if isinstance(item.get("exitCode"), int):
                state["exitCode"] = item["exitCode"]
            if isinstance(output := item.get("aggregatedOutput"), str):
                output, truncated = _sanitize_command_output(output)
                state["content"] = [{"type": "text", "text": output}]
                state["output_truncated"] = truncated
            messages.append({"type": "assistant", "id": item.get("id"),
                             "content": [{"type": "tool", "id": item.get("id"),
                                          "name": "commandExecution", "state": state}]})
        elif item.get("type") == "fileChange":
            changes = [{"path": _relative_workspace_path(change.get("path"), workspace),
                        "kind": change.get("kind")}
                       for change in item.get("changes", []) if isinstance(change, dict)]
            messages.append({"type": "assistant", "id": item.get("id"),
                             "content": [{"type": "tool", "id": item.get("id"), "name": "fileChange",
                                          "state": {"status": item.get("status"), "input": {"changes": changes}}}]})
        elif item.get("type") == "functionCallOutput":
            messages.append({"type": "assistant", "id": item.get("id"),
                             "content": [{"type": "tool", "id": item.get("id"),
                                          "name": item.get("name", "tool"),
                                          "state": {"status": "completed", "input": {"name": item.get("name")}}}]})
        elif item.get("type") in {"mcpToolCall", "dynamicToolCall", "collabAgentToolCall", "webSearch", "imageView"}:
            kind = item["type"]
            name = item.get("tool") or kind
            if kind == "mcpToolCall" and item.get("server"):
                name = f"{item['server']}.{name}"
            if not isinstance(name, str):
                name = kind
            state = {"status": item.get("status", "completed"), "input": {"tool": name}}
            messages.append({"type": "assistant", "id": item.get("id"),
                             "content": [{"type": "tool", "id": item.get("id"), "name": name, "state": state}]})
    return messages


def _relative_workspace_path(value, workspace):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = workspace / path
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except (OSError, ValueError):
        return "outside-workspace"


def _sanitize_command(command):
    if not isinstance(command, str):
        return ""
    return _redact_credentials(command, positional=True)[:4000]


def _redact_credentials(text, *, positional=False):
    text = re.sub(r"(?i)(\bbearer[ \t]+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", text)
    text = re.sub(r"""(?i)(\bauthorization["']?[ \t]*[:=][ \t]*["']?basic[ \t]+)[A-Za-z0-9+/=]+""",
                  r"\1[redacted]", text)
    label = r"(?:api[_-]?key|(?:access|refresh)[_-]?token|password|secret|credential)"
    assignment = rf"""(?<![A-Za-z0-9]){label}["']?[ \t]*[:=][ \t]*"""
    flag_prefix = "" if positional else "--"
    argument = rf"{flag_prefix}{label}[ \t]+"
    value = r"""("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|[^\s,;}]+)"""

    def redact_value(match):
        credential = match[2]
        quote = credential[0] if credential[0] in {"'", '"'} and credential[-1] == credential[0] else ""
        return f"{match[1]}{quote}[redacted]{quote}"

    return re.sub(rf"(?i)({assignment}|{argument}){value}", redact_value, text)


def _sanitize_command_output(output):
    output = _TERMINAL_CONTROLS.sub("", output.replace("\r\n", "\n").replace("\r", "\n"))
    output = _redact_credentials(output)
    if len(output) <= MAX_COMMAND_OUTPUT_CHARS:
        return output, False
    available = MAX_COMMAND_OUTPUT_CHARS - len(_OUTPUT_TRUNCATION)
    head = available // 2
    tail = available - head
    return output[:head] + _OUTPUT_TRUNCATION + output[-tail:], True


def _final_response(items):
    assistant = [item for item in items if item.get("type") in {"agentMessage", "assistantMessage"}
                 and isinstance(item.get("text"), str)]
    final = [item for item in assistant if item.get("phase") in {"final_answer", "finalAnswer"}]
    selected = final or assistant
    return selected[-1].get("text", "") if selected else ""


def _trigger_result(telemetry, skill_id):
    hits = [x for x in telemetry.get("skill_invocations", [])
            if x.get("skill_name") == skill_id and x.get("implicit") is True]
    if hits:
        return True
    return False if telemetry.get("transport_health") else None


def run_request(r):
    _validate_request(r)
    output = Path(r["output"])
    output.mkdir(parents=True, exist_ok=True)
    operation = r.get("operation", "run")
    started = time.monotonic()
    run_id = r.get("run_id") or uuid.uuid4().hex
    status, error = "preflight_error", None
    thread_id = turn_id = None
    messages = []
    explicit_confirmed = None
    telemetry = {"status": "unavailable", "skill_invocations": [], "transport_health": []}
    collector = None
    collector_thread = None
    process = None
    isolated_root = isolated_home = None
    rpc = None
    env_result = None
    version = None
    items = []
    try:
        isolated_root, isolated_home = _isolated_home(catalog_only=operation == "probe")
        codex_command = os.environ.get("SKILLCRAFT_CODEX", "codex")
        codex_found = shutil.which(codex_command)
        if not codex_found:
            raise RuntimeError("Codex executable was not found")
        codex_binary = Path(codex_found).resolve()
        collector = _OtelCollector()
        collector_thread = threading.Thread(target=collector.serve_forever, daemon=True)
        collector_thread.start()
        _configure_home(isolated_home, Path(r["directory"]), collector.server_port, r["mode"], codex_binary)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(isolated_root),
               "CODEX_HOME": str(isolated_home), "TMPDIR": str(isolated_root)}
        if operation != "probe" and os.environ.get("CODEX_API_KEY"):
            env["CODEX_API_KEY"] = os.environ["CODEX_API_KEY"]
        version = _read_cli_version(codex_binary, r["directory"], env)
        command = [str(codex_binary), "app-server", "--listen", "stdio://"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
                                   start_new_session=False)
        rpc = _Rpc(process, output)
        rpc.request("initialize", {"clientInfo": {"name": "skillcraft", "title": "SkillCraft", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        process.stdin.flush()
        skills_reply = rpc.request("skills/list", {"cwds": [r["directory"]], "forceReload": True})
        if not isinstance(skills_reply, dict):
            raise RuntimeError("Codex skills/list returned an unsupported catalog format")
        skill_list = skills_reply.get("skills")
        if not isinstance(skill_list, list):
            groups = skills_reply.get("data")
            if not isinstance(groups, list) or not groups or any(
                    not isinstance(group, dict) or not isinstance(group.get("skills"), list) for group in groups):
                raise RuntimeError("Codex skills/list returned an unsupported catalog format")
            skill_list = [skill for group in groups for skill in group["skills"]]
        if any(not isinstance(skill, dict) for skill in skill_list):
            raise RuntimeError("Codex skills/list returned an unsupported catalog format")
        candidate = None
        staged_path = Path(r["skill_path"]).resolve() if r.get("skill_path") else None
        if staged_path:
            name = r.get("skill_id") or staged_path.parent.name
            matches = [s for s in skill_list if s.get("name") == name]
            # Codex reports the exact SKILL.md path, which is also the path
            # carried by the native SkillUserInput item.
            if len(matches) != 1:
                raise RuntimeError("Candidate name is duplicated in Codex skills/list")
            candidate = next((s for s in matches if Path(s.get("path", "")).resolve() == staged_path), None)
            if candidate is None:
                raise RuntimeError("Staged candidate missing or shadowed in Codex skills/list")
            if _sha(staged_path) != r.get("skill_sha256", _sha(staged_path)):
                raise RuntimeError("Candidate snapshot hash mismatch")
            if not candidate.get("enabled", True):
                raise RuntimeError("Candidate is disabled in Codex skills/list")
            if r["mode"] == "trigger" and not candidate.get("description"):
                raise RuntimeError("Candidate is not advertised for implicit invocation")
            candidate = {"id": candidate["name"], "path": candidate["path"],
                         "description": candidate.get("description"), "autoinvoke": candidate.get("enabled")}
        elif any(s.get("name") == r.get("skill_id") for s in skill_list):
            raise RuntimeError("Baseline candidate leaked through Codex skills/list")
        env_result = _environment(candidate, skill_list, r.get("controlled", False), version, r["mode"])
        _json(output / "environment.json", env_result)
        if operation == "probe":
            status = "completed"
        else:
            model = r["model"]
            start_params = {"cwd": r["directory"], "model": model["id"], "ephemeral": True,
                            "permissions": "skillcraft", "approvalPolicy": "never",
                            "runtimeWorkspaceRoots": [r["directory"]], "serviceName": "skillcraft",
                            "developerInstructions": r.get("instructions")}
            thread = rpc.request("thread/start", start_params)
            thread_obj = thread.get("thread", thread) if isinstance(thread, dict) else {}
            thread_id = thread_obj.get("id")
            if not thread_id:
                raise RuntimeError("Codex did not return a thread id")
            prompt = r.get("prompt", "")
            user_input = [{"type": "text", "text": prompt}]
            if r.get("attach_skill"):
                user_input.append({"type": "skill", "name": candidate["id"], "path": str(staged_path)})
            turn_params = {"threadId": thread_id, "input": user_input, "model": model["id"]}
            if model.get("effort"):
                turn_params["effort"] = model["effort"]
            if r.get("output_schema") is not None:
                turn_params["outputSchema"] = r["output_schema"]
            turn = rpc.request("turn/start", turn_params)
            turn_obj = turn.get("turn", turn) if isinstance(turn, dict) else {}
            turn_id = turn_obj.get("id")
            if not turn_id:
                raise RuntimeError("Codex did not return a turn id")
            status = "running"
            deadline = started + float(r.get("timeout_seconds", 180))
            done = None
            seen_items = set()
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Codex app-server exited during the turn")
                for event in list(rpc.notifications):
                    if event.get("method") == "item/completed" and (event.get("params") or {}).get("turnId") == turn_id:
                        item = event["params"].get("item")
                        if isinstance(item, dict) and item.get("id") not in seen_items:
                            seen_items.add(item.get("id"))
                            items.append(item)
                    if event.get("method") == "turn/completed" and (event.get("params") or {}).get("turn", {}).get("id") == turn_id:
                        done = event["params"]["turn"]
                if done:
                    break
                time.sleep(0.1)
            if not done:
                status = "timeout"
                try:
                    rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)
                except Exception:
                    pass
            elif done.get("status") == "completed":
                status = "completed"
            else:
                status = done.get("status") or "turn_error"
                error = "Codex turn did not complete successfully"
            messages = _messages(r, items, thread_id, turn_id)
            if r.get("attach_skill"):
                durable = []
                for event in rpc.notifications:
                    params = event.get("params") or {}
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    item = params.get("item") or {}
                    for sk in item.get("content", []) if item.get("type") == "userMessage" else []:
                        if isinstance(sk, dict) and sk.get("type") == "skill" and sk.get("name") == candidate["id"] and Path(sk.get("path", "")).resolve() == staged_path:
                            durable.append({"method": event.get("method"), "item_id": item.get("id")})
                explicit_confirmed = bool(durable)
                _json(output / "codex_attachment.json", {
                    "schema_version": 1, "confirmed": explicit_confirmed,
                    "evidence_kind": "explicit_input_and_durable_user_item" if durable else "explicit_input_only",
                    "run_id": run_id, "thread_id": thread_id, "turn_id": turn_id,
                    "user_message_id": durable[0]["item_id"] if durable else None,
                    "skill": {"name": candidate["id"], "path": str(staged_path), "sha256": _sha(staged_path)},
                    "source_item": durable[0] if durable else None})
                if status == "completed" and not explicit_confirmed:
                    status = "attachment_unverified"
                    error = "Codex durable evidence did not confirm the explicit skill input"
            # EOF lets the server flush its local OTLP exporter before its
            # collector is closed; no telemetry destination other than the
            # loopback collector is configured in this process environment.
            process.stdin.close()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=3)
            time.sleep(0.15)
            relevant = [x for x in collector.records
                        if x.get("turn_id") == turn_id or
                        (x.get("kind") == "transport_health" and x.get("conversation_scoped"))]
            telemetry = {"status": "captured" if relevant else "no_matching_event",
                         "skill_invocations": [x for x in relevant if x.get("kind") == "skill_invocation"],
                         "transport_health": [x for x in relevant if x.get("kind") == "transport_health"]}
            if r.get("mode") == "trigger":
                # Silence is not evidence of non-invocation: the exporter or
                # invocation event can be unavailable for this Codex build.
                triggered = _trigger_result(telemetry, r.get("skill_id") or "")
            else:
                triggered = None
        sanitized_events = [_safe_event(event) for event in (rpc.notifications if rpc else [])]
        (output / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in sanitized_events), encoding="utf-8")
        _json(output / "messages.json", messages)
        response_text = _final_response(items)
        (output / "response.txt").write_text(response_text, encoding="utf-8")
        (output / "transcript.md").write_text(
            "## Eval Prompt\n\n" + r.get("prompt", "") + "\n\n## Messages\n\n" +
            "\n\n".join(f"### {m['type']}\n\n```json\n{json.dumps(m, ensure_ascii=False, indent=2)}\n```" for m in messages),
            encoding="utf-8")
        if r.get("attach_skill") and not (output / "codex_attachment.json").exists():
            _json(output / "codex_attachment.json", {"schema_version": 1, "confirmed": None,
                  "evidence_kind": "unverified", "run_id": run_id, "thread_id": thread_id, "turn_id": turn_id,
                  "user_message_id": None, "skill": None, "source_item": None})
        status = status if status else "transport_error"
        token_event = next((e for e in reversed(rpc.notifications) if e.get("method") == "thread/tokenUsage/updated"
                            and (e.get("params") or {}).get("turnId") == turn_id), None) if rpc else None
        usage = {"schema_version": 1, "executor": None, "root_executor": None, "role": r["mode"],
                 "telemetry_status": telemetry["status"], "skill_invocations": telemetry["skill_invocations"]}
        if token_event:
            breakdown = (token_event.get("params", {}).get("tokenUsage") or {}).get("total") or {}
            usage["executor"] = {
                "tokens": {"input": breakdown.get("inputTokens"), "output": breakdown.get("outputTokens"),
                           "reasoning": breakdown.get("reasoningOutputTokens"),
                           "cache": {"read": breakdown.get("cachedInputTokens"), "write": breakdown.get("cacheWriteInputTokens", 0)}},
                "total_tokens": breakdown.get("totalTokens"), "records": 1, "cost": None}
            usage["root_executor"] = usage["executor"]
        _json(output / "usage.json", usage)
        _json(output / "telemetry.json", telemetry)
        _json(output / "timing.json", {"total_duration_seconds": time.monotonic() - started})
    except Exception as exc:
        error = str(exc)
        if status == "running":
            status = "transport_error"
        if not (output / "environment.json").exists():
            _json(output / "environment.json", {"kind": "codex-app-server", "harness": "codex", "server_version": version,
                  "controlled": False, "candidate": None, "background": [], "plugins": [], "configuration_fingerprint": None})
        if not (output / "messages.json").exists():
            _json(output / "messages.json", messages)
            (output / "response.txt").write_text("", encoding="utf-8")
        if r.get("attach_skill") and not (output / "codex_attachment.json").exists():
            _json(output / "codex_attachment.json", {"schema_version": 1, "confirmed": None,
                  "evidence_kind": "unverified", "run_id": run_id, "thread_id": thread_id, "turn_id": turn_id,
                  "user_message_id": None, "skill": None, "source_item": None})
        _json(output / "usage.json", {"schema_version": 1, "executor": None, "root_executor": None,
              "role": r.get("mode"), "telemetry_status": telemetry["status"], "skill_invocations": []})
        _json(output / "telemetry.json", telemetry)
        _json(output / "timing.json", {"total_duration_seconds": time.monotonic() - started})
        (output / "events.jsonl").touch(exist_ok=True)
    finally:
        if collector:
            collector.shutdown()
            collector.server_close()
        if process:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except OSError:
                    pass
                except subprocess.TimeoutExpired:
                    pass
        if isolated_root:
            shutil.rmtree(isolated_root, ignore_errors=True)
    model = r.get("model", {})
    result = {"schema_version": 1, "harness": "codex", "run_id": run_id,
              "status": status, "thread_id": thread_id, "turn_id": turn_id,
              "triggered": locals().get("triggered"),
              "attached": explicit_confirmed if r.get("attach_skill") else False,
              "error": error, "model": model, "mode": r.get("mode"), "environment": env_result,
              "evidence_hashes": {name: _sha(output / name) for name in
        ("messages.json", "response.txt", "environment.json", "events.jsonl", "telemetry.json", "usage.json", "timing.json", "transcript.md", "codex_attachment.json")
                  if (output / name).is_file()}}
    _json(output / "run.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a SkillCraft Codex App Server request")
    parser.add_argument("request", type=Path)
    args = parser.parse_args(argv)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    def handle_termination(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, handle_termination)
    signal.signal(signal.SIGINT, handle_termination)
    result = run_request(request)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
