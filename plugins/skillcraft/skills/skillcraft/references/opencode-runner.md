# OpenCode runner: operation and limitations

## Architecture

Python owns datasets, snapshots, repetitions, fixture copies, grading and reports.
Each `runtime.run_model()` starts the pinned TypeScript runner with a JSON request
file. The runner uses `@opencode/client@2.2.7` against the OpenCode server. It owns
one fresh archived session. Executor, judge and optimizer sessions are independent.
The judge and comparison prompts live under `references/prompts/`.

`opencode run --format json` is useful for diagnostics but has a different projected
event format. The embedded `@opencode/sdk` is not needed for this integration.

## Dependencies and installation

Set `SKILLCRAFT` to the installed skill directory containing `SKILL.md`. A
standalone installation can live under `<project>/.agents/skills/skillcraft/`.
Use Python 3.11+ and Node 22+. Set an absolute base path:

```sh
export SKILLCRAFT=/absolute/path/to/skillcraft
python3 -m venv "$SKILLCRAFT/.venv"
"$SKILLCRAFT/.venv/bin/pip" install -r "$SKILLCRAFT/requirements.txt"
npm --prefix "$SKILLCRAFT/scripts/opencode-runner" ci
```

Python pins PyYAML 6.0.3. Node dependencies are pinned and have a committed lockfile.
The globally installed OpenCode CLI is not required to be in PATH by the runner.
Keep this source directory and its runtime dependencies outside executor fixtures.

## Connection and auth

Default: `Service.discover()` locates the current local server;
`Service.headers(endpoint)` supplies its auth headers. No credential files are
parsed or copied. No server is restarted or auto-started.

For an explicit server set `SKILLCRAFT_SERVER` and, if required,
`SKILLCRAFT_HEADERS` to a JSON object of headers in the process environment.
The explicit context is used for both client requests and `/openapi.json`.
Never write auth headers into experiment JSON. The process environment is not
serialized. This filesystem runner currently requires the server and runner to
see the **same absolute paths**; an arbitrary remote host is unsupported without
shared storage. Server-local file paths are not file uploads.

Live checks used OpenCode server 2.2.7. The runner records the connected server's
version and verifies the required V2 endpoints in `/openapi.json` before prompting.

## Isolation profiles

### Agent Skills metadata and verified OpenCode interpretation

Release artifacts must separately pass strict Agent Skills validation and actual
OpenCode compatibility. Use a standard identifier matching the directory, required
description of at most 1024 characters, and string-valued metadata. Read
agent-skills-specification.md in full before frontmatter work.

Live probes on OpenCode 2.2.6 verified the following registry values and native
attachment behavior; the quoted forms are YAML strings:

| Source metadata.opencode/autoinvoke | Registry autoinvoke | Explicit native attachment | Agent Skills metadata type |
|---|---|---|---|
| Absent | Unset (default advertisement when description exists) | Loaded | Valid |
| true (boolean) | true | Loaded | Invalid |
| false (boolean) | false | Loaded | Invalid |
| "true" (string) | true | Loaded | Valid |
| "false" (string) | false | Loaded | Valid |

The documented false flag omits the skill from the model's available list; it does
not disable explicit attachment. Each probe returned its unique body-only proof
code. These checks establish registry interpretation and explicit loading, not
natural-trigger quality. Use the quoted strings for this verified version; do not
generalize boolean-like string interpretation to other clients without checking.
The five probes used the installed-extensions profile and remain controlled:false.
Detailed evidence is retained in the compliance workspace's metadata-probes-001.

### Profile behavior

The default `bounded-files` profile uses a temporary Location outside the source
project. `SKILLCRAFT_TEMP` can override the temp parent. On macOS the normal
location is `$TMPDIR/opencode/skill-eval-*`.

The generated `skillcraft-eval` agent has a fixed system prompt and 16 steps by
default; an explicit profile can set `steps` from 1 to 200.
Permissions deny by default and allow only:

- reads under `inputs/` and `outputs/`;
- edits under `outputs/`;
- the candidate's `skill` action and resource reads, when installed.

The baseline has no candidate and cannot read its source or the grader's data.
Input files are copied; symlinks and colliding destinations are rejected. Legacy
file paths retain basenames; explicit source/destination mappings preserve nested
trees through snapshot and runtime stages (see schemas.md, transport version 2).
Candidate copies omit evals/tests, caches, virtualenvs and node_modules. Scripts
remain present but shell execution requires an explicit custom profile.

Preflight waits for Location plugin activation, verifies native skill registration,
path, hash and advertising, inspects configuration sources, ancestor instructions,
plugin origins and failures. Unexpected global instructions or extensions fail a
controlled run. Background catalogs and plugin IDs are compared across quality runs.
Built-in/background skills are denied to the executor unless deliberately enabled.

For tasks needing shell, web or MCP, provide `--profile /absolute/profile.json`:

```json
{
  "permissions": [
    {"action": "*", "resource": "*", "effect": "deny"},
    {"action": "read", "resource": "*", "effect": "allow"},
    {"action": "edit", "resource": "outputs/*", "effect": "allow"},
    {"action": "skill", "resource": "my-skill", "effect": "allow"},
    {"action": "shell", "resource": "python3 *", "effect": "allow"}
  ]
}
```

Custom profiles are labelled `controlled: false`. Shell permissions are not an OS
sandbox. For strong isolation of arbitrary commands, run the server and worker in
an independently isolated environment. Image generation remains unsupported.
Delegation is disabled by default; `allow_delegation: true` explicitly enables only
the owned `skillcraft-worker` agent, which receives the same profile and cannot
delegate again. Child permission/questions, completion, cancellation and projected
usage are tracked. Shell background jobs are tracked and cancelled. The
default profile is intended for bounded file/text tasks, not full tool parity
with the interactive build agent. Record the profile when interpreting results.

### Profile options

`name` labels the profile; `inherit_bounded_files: true` starts with the default
file/skill rules, then appends explicit `permissions`. This gives A/B equal tools
while granting candidate-resource access only when the candidate is installed.
Without inheritance, the permissions list replaces file defaults. `steps` controls
the agent step budget; `allow_delegation` controls owned-worker opt-in. Unknown
options and invalid rule shapes fail explicitly. Network/MCP access depends on
the server's installed tools plus explicit permissions; permission is not proof
that a tool is actually configured. Tests/fixtures includes shell and delegation
profiles. Each run records effective `profile.json` and its source hash is captured
by experiment manifests. Observed tool calls are preserved in messages/transcripts.

With delegation, usage.executor includes root and owned descendant executor steps;
root_executor and child_executors provide provenance. A missing component makes the
family total unknown. Per-child messages/usage are saved under children/. Maintenance
telemetry remains limited by replay availability and is not included in that sum.

Executor permissions are not inherited by judges or optimizer. Use `--judge-profile`
on quality/loop commands and `--optimizer-profile` on the loop when those roles need
an explicit environment; standalone grade/improve/compare accept `--profile` for
their own role. Profile snapshots are frozen separately. A custom bounded-tools
profile can acknowledge installed global extensions without adding shell permission,
but remains uncontrolled because plugin effects are not an isolation guarantee.
Strict default audit still rejects unexpected extensions. Never disable or rewrite
the user's global plugins just to obtain a controlled label.

## Lifecycle and durable recovery

### Optional CLI state adapters

A custom profile may include `cli_tools`, a list of objects with `id`, an absolute
`executable`, and an `environment` string mapping of **non-secret** settings.
`{state}` expands to a separate per-execution, per-tool directory; `{project}` to
the execution Location. HOME/PATH replacement and credential settings are rejected.
The runtime advertises an absolute launcher in the agent system instructions. The
launcher applies environment settings in the actual server-side tool process. Merely
setting variables on the Python/TypeScript client does not configure that process.

Use the launcher for every relevant command. `--skillcraft-state` prints its scoped
settings; inspect the real CLI's cache/database path as a separate end-to-end probe.
`cli-state.json` records configured settings and observed invocations; no invocation
means `unverified`. Observed does not mean an OS sandbox or that arbitrary commands
cannot bypass the launcher. Custom profiles remain `controlled:false`.

Declare external state before live runs: caches/config/data, GC, background workers
and network prerequisites. Prefer the CLI's supported state variables, not a global
HOME change. Keep tool-specific settings in the experiment profile, outside the
generic runner. Preserve execution state for diagnosis; clean only owned paths after
saving evidence and verifying no owned jobs remain. Do not restart the shared server.

1. Preflight server/version, Location and catalog.
2. Create session with explicit agent, model, permissions and run metadata.
3. Archive it and submit one prompt; save session and prompt IDs immediately.
4. Read durable log batches with exclusive `after` and `follow: false`. A
   `log.synced` watermark advances the cursor even when no records are emitted.
5. Reconcile durable paginated messages. Tool calls are scoped by
   session/message/call ID; successful `skill` loading determines natural triggering.
6. Require the submitted user message, a terminal outcome, empty inbox and no
   running owned jobs. An idle wait response alone is insufficient.
7. Save outputs, transcripts, projected messages, raw available events and usage.

**Observed v2.2.6 behavior:** in the tested installation, replay returned only
`log.synced` although messages and outcomes were persisted. The runner therefore
recovers tool state, terminal outcomes and executor tokens from durable message
projections. `usage.projection_fallback` reports this. Assistant message IDs dedupe
late step events. Maintenance usage remains unknown when absent from the log;
it is not inferred from output size or double-counted session totals.

On timeout/SIGINT/SIGTERM the runner interrupts its own session tree with
`resume: false`, cancels owned shell jobs and checks for remaining jobs. It never
cancels unrelated sessions. Pending permissions are rejected, questions cancelled,
and each gets its own run status. Transport reads have bounded timeouts and log
reads retry from the saved cursor. If the Python watchdog must kill an unresponsive
runner, `runner_lost` and `session.json` identify work needing investigation.

Verified V2 lifecycle details: a session retains both busy and idle timestamps;
current activity requires busy > idle (or no idle), not merely the presence of busy.
Shell operations are Location-scoped: cancellation must use the owning session's
Location, otherwise remove can silently target a different registry. Stop requests
are followed by a bounded check of durable job state before reporting cancellation
complete. These details were confirmed with live owned-worker and background-shell
tests; they apply to this version, not an assumed cross-version API contract.

Execution directories are retained for reproducibility. Delete only directories
listed in the relevant run manifests after verifying cancellation and retaining
the required artifacts. The tool does not stop the shared OpenCode service.

## Usage and cost

`input`, `output`, `reasoning`, `cache.read`, `cache.write` are disjoint normalized
fields in this version. Their sum is `total_tokens`. Executor steps and maintenance
records are separate. Judge and optimizer usage remain under their own run paths.

OpenCode's `cost` may be provider-reported or catalog-derived. Zero from a provider
with no pricing information is **not proof of free execution**. Treat it as the
server's reported value, not a billing statement. Unknown tokens remain null.
One repetition has unknown sample standard deviation, not zero variance.

## Commands and checks

Run the development checks below from the repository root. Skill commands can be invoked by absolute path from any cwd.
All experiment destinations must be new; this prevents accidental overwrite.
Quality/aggregate commands default to a short human summary and save full JSON plus
`summary.md`. Automation that consumes stdout must pass `--json`. API callers still
receive the full Python report object. Explicit grading selection and report outputs
preserve historical source workspaces; see schemas.md before composing summaries.

```sh
npm --prefix "$SKILLCRAFT/scripts/opencode-runner" run check
"$SKILLCRAFT/scripts/opencode-runner/node_modules/.bin/tsx" --test tests/opencode/*.test.mts
"$SKILLCRAFT/.venv/bin/python" -m unittest discover -s tests -t . -v
"$SKILLCRAFT/.venv/bin/python" tests/smoke_test.py \
  --model 'provider/model#variant' --workspace "$PWD/.agents/skillcraft-workspaces/smoke-001"
```

Live checks make model requests and exercise paired repetitions, trigger detection,
judge grading, provider error, timeout, permission rejection and external-read denial.
`run_loop.py` separately exercises train/validation selection and quality regression.
Human review remains human: automation verifies feedback plumbing, not user approval.

The restored skill's full live acceptance is reproducible with:

```sh
"$SKILLCRAFT/.venv/bin/python" tests/verify_restoration.py \
  --workspace "$PWD/.agents/skillcraft-workspaces/restoration/new-acceptance" \
  --model 'openai/gpt-6-astra#low' \
  --judges 'openai/gpt-6.1-terra#low' 'openai/gpt-6.1-sol#low'
```

Choose available models on your server; catalog presence alone does not guarantee
provider-account access. This suite freezes its thresholds before execution, makes
the actual SkillCraft deliver two different skills, runs a generated skill,
checks natural triggering, exercises three optimization candidates and evaluates
the delivered artifacts with two different LLM judges. It explicitly uses custom
profiles (including a bounded judge/optimizer profile) and reports uncontrolled
global-extension effects. Model scores on these fixtures are not a guarantee of
general quality or a substitute for the user's subjective review.

For a live connection-recovery/auth-context check (also makes a model request), run
`"$SKILLCRAFT/scripts/opencode-runner/node_modules/.bin/tsx" tests/opencode/live-reconnect.mts /absolute/new-result-dir provider/model#variant`.
The test starts an owned loopback proxy, interrupts one log connection, checks every
request's explicit auth context, verifies successful recovery, and stops its proxy.
