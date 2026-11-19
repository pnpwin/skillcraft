#!/usr/bin/env python3
"""Generate and serve a review page for eval results.

Reads the workspace directory, discovers runs (directories with outputs/),
embeds all output data into a self-contained HTML page, and serves it via
a tiny HTTP server. Feedback auto-saves to feedback.json in the workspace.

Usage:
    python generate_review.py <workspace-path> [--port PORT] [--skill-name NAME]
    python generate_review.py <workspace-path> --previous-feedback /path/to/old/feedback.json

No dependencies beyond the Python stdlib are required.
"""

import argparse
import base64
import json
import mimetypes
import re
import sys
import webbrowser
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Files to exclude from output listings
METADATA_FILES = {"transcript.md", "user_notes.md", "metrics.json"}

# Extensions we render as inline text
TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".yaml", ".yml", ".xml", ".html", ".css", ".sh", ".rb", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sql", ".r", ".toml",
}

# Extensions we render as inline images
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# MIME type overrides for common types
MIME_OVERRIDES = {
    ".svg": "image/svg+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def get_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def find_runs(workspace: Path, selection=None) -> list[dict]:
    """Recursively find directories that contain an outputs/ subdirectory."""
    runs: list[dict] = []
    if selection is not None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts.grading_store import load_selection
        selection = load_selection(workspace, selection)
        for logical_id in selection['runs']:
            run = build_run(workspace, workspace / logical_id, selection)
            if run:
                runs.append(run)
    else:
        _find_runs_recursive(workspace, workspace, runs)
    runs.sort(key=lambda r: (str(r.get("eval_id", "")), r["logical_id"]))
    return runs


def _find_runs_recursive(root: Path, current: Path, runs: list[dict]) -> None:
    if not current.is_dir():
        return

    outputs_dir = current / "outputs"
    if outputs_dir.is_dir():
        run = build_run(root, current)
        if run:
            runs.append(run)
        return

    skip = {"node_modules", ".git", "__pycache__", "skill", "inputs", "snapshots", "judge", "optimizer", "candidates", "selected"}
    for child in sorted(current.iterdir()):
        if child.is_dir() and not child.is_symlink() and child.name not in skip:
            _find_runs_recursive(root, child, runs)


def build_run(root: Path, run_dir: Path, selection=None) -> dict | None:
    """Build a run dict with prompt, outputs, and grading data."""
    prompt = ""
    eval_id = None
    metadata = {}

    # Try eval_metadata.json
    ancestors = [run_dir, *run_dir.parents]
    ancestors = ancestors[:ancestors.index(root) + 1] if root in ancestors else [run_dir]
    for candidate in [p / "eval_metadata.json" for p in ancestors]:
        if candidate.exists():
            try:
                metadata = json.loads(candidate.read_text())
                prompt = metadata.get("prompt", "")
                eval_id = metadata.get("eval_id")
            except (json.JSONDecodeError, OSError):
                pass
            if prompt:
                break

    # Fall back to transcript.md
    if not prompt:
        for candidate in [run_dir / "transcript.md", run_dir / "outputs" / "transcript.md"]:
            if candidate.exists():
                try:
                    text = candidate.read_text()
                    match = re.search(r"## Eval Prompt\n\n([\s\S]*?)(?=\n##|$)", text)
                    if match:
                        prompt = match.group(1).strip()
                except OSError:
                    pass
                if prompt:
                    break

    if not prompt:
        prompt = "(No prompt found)"

    logical_id = run_dir.relative_to(root).as_posix()
    run_info = json.loads((run_dir / 'run.json').read_text()) if (run_dir / 'run.json').exists() else {}
    run_id = run_info.get('run_id', logical_id)

    # Collect output files
    outputs_dir = run_dir / "outputs"
    output_files: list[dict] = []
    if outputs_dir.is_dir():
        for f in sorted(outputs_dir.rglob('*')):
            if f.is_file() and not f.is_symlink() and f.name not in METADATA_FILES and f.resolve().is_relative_to(outputs_dir.resolve()):
                output_files.append({**embed_file(f), 'name': f.relative_to(outputs_dir).as_posix()})

    # Load grading if present
    grading = None
    for candidate in [run_dir / "grading.json", run_dir.parent / "grading.json"]:
        if candidate.exists():
            try:
                grading = json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError):
                pass
            if grading:
                break

    history = []
    attempt_id = None
    execution_run_id = run_id
    if selection is not None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts.grading_store import read_grading
        chosen = selection['runs'][logical_id]
        grading, _, provenance = read_grading(run_dir, chosen['selected'])
        attempt_id = provenance['attempt_id'] if provenance else 'legacy'
        run_id = str(run_id) + '@' + attempt_id
        for item in dict.fromkeys([chosen['selected'], *chosen.get('history', []), 'legacy']):
            if item == 'legacy' and not (run_dir / 'grading.json').exists():
                continue
            g, directory, m = read_grading(run_dir, item, allow_incomplete=True)
            history.append({'attempt_id': m['attempt_id'] if m else 'legacy', 'status': g.get('status'), 'path': str(directory)})

    return {
        "id": run_id,
        "logical_id": logical_id,
        "snapshot_hash": metadata.get('snapshot_hash'),
        "status": run_info.get('status', 'unknown'),
        "error": run_info.get('error'),
        "configuration": metadata.get('configuration', run_dir.parent.name),
        "prompt": prompt,
        "eval_id": eval_id,
        "outputs": output_files,
        "grading": grading,
        "execution_run_id": execution_run_id,
        "grading_attempt_id": attempt_id,
        "grading_history": history,
    }


def embed_file(path: Path) -> dict:
    """Read a file and return an embedded representation."""
    ext = path.suffix.lower()
    mime = get_mime_type(path)

    if ext in TEXT_EXTENSIONS:
        try:
            content = path.read_text(errors="replace")
        except OSError:
            content = "(Error reading file)"
        return {
            "name": path.name,
            "type": "text",
            "content": content,
        }
    elif ext in IMAGE_EXTENSIONS:
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {
            "name": path.name,
            "type": "image",
            "mime": mime,
            "data_uri": f"data:{mime};base64,{b64}",
        }
    elif ext == ".pdf":
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {
            "name": path.name,
            "type": "pdf",
            "data_uri": f"data:{mime};base64,{b64}",
        }
    elif ext == ".xlsx":
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {
            "name": path.name,
            "type": "xlsx",
            "data_b64": b64,
        }
    else:
        # Binary / unknown — base64 download link
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {
            "name": path.name,
            "type": "binary",
            "mime": mime,
            "data_uri": f"data:{mime};base64,{b64}",
        }


def load_previous_iteration(workspace: Path) -> dict[str, dict]:
    """Load previous iteration's feedback and outputs.

    Returns a map of run_id -> {"feedback": str, "outputs": list[dict]}.
    """
    result: dict[str, dict] = {}

    # Load feedback
    feedback_map: dict[str, str] = {}
    feedback_path = workspace / "feedback.json"
    if feedback_path.exists():
        try:
            data = json.loads(feedback_path.read_text())
            feedback_map = {
                r["run_id"]: r["feedback"]
                for r in data.get("reviews", [])
                if r.get("feedback", "").strip()
            }
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Load runs (to get outputs)
    prev_runs = find_runs(workspace)
    for run in prev_runs:
        result[run["logical_id"]] = {
            "feedback": feedback_map.get(run["id"], ""),
            "outputs": run.get("outputs", []),
        }

    # Also add feedback for run_ids that had feedback but no matching run
    for run_id, fb in feedback_map.items():
        if run_id not in result:
            result[run_id] = {"feedback": fb, "outputs": []}

    return result


def generate_html(
    runs: list[dict],
    skill_name: str,
    previous: dict[str, dict] | None = None,
    benchmark: dict | None = None,
    server_available: bool = False,
) -> str:
    """Generate the complete standalone HTML page with embedded data."""
    template_path = Path(__file__).resolve().parents[1] / "assets" / "viewer.html"
    template = template_path.read_text()

    # Build previous_feedback and previous_outputs maps for the template
    previous_feedback: dict[str, str] = {}
    previous_outputs: dict[str, list[dict]] = {}
    if previous:
        for run_id, data in previous.items():
            if data.get("feedback"):
                previous_feedback[run_id] = data["feedback"]
            if data.get("outputs"):
                previous_outputs[run_id] = data["outputs"]

    embedded = {
        "skill_name": skill_name,
        "server_available": server_available,
        "runs": runs,
        "previous_feedback": previous_feedback,
        "previous_outputs": previous_outputs,
    }
    if benchmark:
        embedded["benchmark"] = benchmark

    data_json = json.dumps(embedded).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')

    return template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {data_json};")


# ---------------------------------------------------------------------------
# HTTP server (stdlib only, zero dependencies)
# ---------------------------------------------------------------------------

def validate_feedback_entries(data):
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('reviews'), list):
        raise ValueError('Expected versioned feedback with reviews')
    seen = set()
    for review in data['reviews']:
        if not isinstance(review, dict):
            raise ValueError('Expected a feedback object for each run')
        rid = review.get('run_id')
        if not isinstance(rid, str) or not rid or rid in seen:
            raise ValueError('Unknown or duplicate feedback run_id')
        seen.add(rid)
        if not isinstance(review.get('feedback'), str) or type(review.get('reviewed')) is not bool:
            raise ValueError('Invalid feedback text/reviewed flag')
        verdict = review.get('verdict')
        if verdict is not None and verdict not in ('success', 'failure', 'correction'):
            raise ValueError('Invalid feedback verdict')
        if verdict == 'correction' and (review.get('reviewed') is not True or not review['feedback'].strip()):
            raise ValueError('Correction verdict requires a reviewed run and nonempty feedback')
    if data.get('status') == 'complete' and not all(r['reviewed'] for r in data['reviews']):
        raise ValueError('Complete feedback must review every run')
    return seen


def validate_feedback(data, runs):
    seen = validate_feedback_entries(data)
    lookup = {r['id']: r for r in runs}
    if not seen <= lookup.keys():
        raise ValueError('Unknown or duplicate feedback run_id')
    if data.get('status') == 'complete' and seen != lookup.keys():
        raise ValueError('Complete feedback must review every run')
    for review in data['reviews']:
        if review.get('snapshot_hash') != lookup[review['run_id']]['snapshot_hash']:
            raise ValueError('Feedback snapshot mismatch')
    return data


def save_feedback(path, data, runs):
    validate_feedback(data, runs)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def notify_feedback_safely(workspace: Path, feedback_path: Path, selection=None) -> None:
    """Offer completed feedback to the learning pipeline when it is available."""
    package_root = str(Path(__file__).resolve().parents[1])
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    try:
        from scripts.learning import notify_feedback
        notify_feedback(workspace, feedback_path, selection=selection)
    except Exception:
        # Learning ingestion is optional; saving feedback must remain successful.
        pass

class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the review HTML and handles feedback saves.

    Regenerates the HTML on each page load so that refreshing the browser
    picks up new eval outputs without restarting the server.
    """

    def __init__(
        self,
        workspace: Path,
        skill_name: str,
        feedback_path: Path,
        previous: dict[str, dict],
        benchmark_path: Path | None,
        *args,
        selection=None,
        **kwargs,
    ):
        self.workspace = workspace
        self.skill_name = skill_name
        self.feedback_path = feedback_path
        self.previous = previous
        self.benchmark_path = benchmark_path
        self.selection = selection
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            # Regenerate HTML on each request (re-scans workspace for new outputs)
            runs = find_runs(self.workspace, self.selection)
            benchmark = None
            if self.benchmark_path and self.benchmark_path.exists():
                try:
                    benchmark = json.loads(self.benchmark_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            html = generate_html(runs, self.skill_name, self.previous, benchmark, server_available=True)
            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/feedback":
            data = b"{}"
            if self.feedback_path.exists():
                data = self.feedback_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/feedback":
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://{self.headers.get("Host")}', f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}:
                self.send_error(403)
                return
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 2_000_000:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                save_feedback(self.feedback_path, data, find_runs(self.workspace, self.selection))
                if data.get('status') == 'complete':
                    notify_feedback_safely(self.workspace, self.feedback_path, self.selection)
                resp = b'{"ok":true}'
                self.send_response(200)
            except (json.JSONDecodeError, OSError, ValueError) as e:
                resp = json.dumps({"error": str(e)}).encode()
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress request logging to keep terminal clean
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and serve eval review")
    parser.add_argument("workspace", type=Path, help="Path to workspace directory")
    parser.add_argument("--port", "-p", type=int, default=0, help="Server port (default: choose a free port)")
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--import-feedback', type=Path)
    parser.add_argument('--selection', type=Path, help='Explicit grading selection; use SkillCraft virtualenv')
    parser.add_argument('--feedback-output', type=Path, help='Separate feedback path for selected historical evidence')
    parser.add_argument("--skill-name", "-n", type=str, default=None, help="Skill name for header")
    parser.add_argument(
        "--previous-workspace", type=Path, default=None,
        help="Path to previous iteration's workspace (shows old outputs and feedback as context)",
    )
    parser.add_argument(
        "--benchmark", type=Path, default=None,
        help="Path to benchmark.json to show in the Benchmark tab",
    )
    parser.add_argument(
        "--static", "-s", type=Path, default=None,
        help="Write standalone HTML to this path instead of starting a server",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"Error: {workspace} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.selection and not args.static and not args.feedback_output:
        parser.error('--selection requires --static or a separate --feedback-output')
    for path in [args.static, args.feedback_output]:
        if args.selection and path and path.resolve().is_relative_to(workspace):
            parser.error('Selected historical reviews must write outside the source workspace')
    runs = find_runs(workspace, args.selection)
    if not runs:
        print(f"No runs found in {workspace}", file=sys.stderr)
        sys.exit(1)

    skill_name = args.skill_name or workspace.name.replace("-workspace", "")
    feedback_path = args.feedback_output.resolve() if args.feedback_output else workspace / "feedback.json"
    if args.import_feedback:
        if args.selection and not args.feedback_output:
            parser.error('Selected review imports require a separate --feedback-output')
        feedback_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(args.import_feedback.read_text())
        save_feedback(feedback_path, data, runs)
        if data.get('status') == 'complete':
            notify_feedback_safely(workspace, feedback_path, args.selection)
        print(f'Imported feedback: {feedback_path}')
        return

    previous: dict[str, dict] = {}
    if args.previous_workspace:
        previous = load_previous_iteration(args.previous_workspace.resolve())

    benchmark_path = args.benchmark.resolve() if args.benchmark else workspace / 'benchmark.json'
    benchmark = None
    if benchmark_path and benchmark_path.exists():
        try:
            benchmark = json.loads(benchmark_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if args.static:
        html = generate_html(runs, skill_name, previous, benchmark)
        args.static.parent.mkdir(parents=True, exist_ok=True)
        args.static.write_text(html)
        print(f"\n  Static viewer written to: {args.static}\n")
        sys.exit(0)

    port = args.port
    handler = partial(ReviewHandler, workspace, skill_name, feedback_path, previous, benchmark_path, selection=args.selection)
    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError:
        # An occupied port belongs to someone else; bind a free one.
        server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]

    url = f"http://localhost:{port}"
    print(f"\n  Eval Viewer")
    print(f"  ─────────────────────────────────")
    print(f"  URL:       {url}", flush=True)
    print(f"  Workspace: {workspace}")
    print(f"  Feedback:  {feedback_path}")
    if previous:
        print(f"  Previous:  {args.previous_workspace} ({len(previous)} runs)")
    if benchmark_path:
        print(f"  Benchmark: {benchmark_path}")
    print(f"\n  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
