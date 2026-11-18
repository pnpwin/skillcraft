# Version 1 contracts

## Agent Skills and harness compliance result

New executor and request records carry `harness` (`codex` or `opencode`). Codex
requests encode the model as `{"id":"MODEL","effort":"EFFORT"}`, with optional
`effort`. OpenCode requests retain their existing model reference. Individual
compliance reports check one target harness. Shared SkillCraft packaging binds
one representative execution and a fresh probe per harness to the same skill
hash. Codex explicit attachment evidence is kept in `codex_attachment.json`.

Existing artifact schema_version 1 stays readable. The new compliance object has
its own schema_version 1 and is independent of task grading and critique:

```json
{
  "schema_version": 1,
  "skill_hash": "hash-of-the-checked-artifact",
  "contract": {
    "agent_skills_revision": "83021e2be457a9b6679148719c1f6074f0ad975e",
    "opencode_version": "2.2.7",
    "codex_version": "0.170.0"
  },
  "agent_skills": {
    "status": "FAIL",
    "issues": [{"field":"name","code":"name_format","message":"Name contains spaces"}],
    "evidence": []
  },
  "opencode": {
    "status": "NOT_RUN",
    "issues": [],
    "evidence": []
  },
  "recommendations": [],
  "accepted": false
}
```

Both status fields use PASS | FAIL | NOT_RUN | ERROR. PASS means all required checks
completed successfully; FAIL means concrete nonconformance; NOT_RUN means no check;
ERROR means a technical problem prevented completion. Preserve partial issues on
ERROR without representing incomplete evidence as full validation. `accepted` is
true only for two PASS values with matching, applicable artifact/environment evidence.

Runtime evidence must identify the server version, profile/environment identity,
discovery/load/execution observations and artifact hash. Static metadata acceptance
is not an OpenCode runtime PASS. Old artifacts without compliance data have no such
evidence: report NOT_RUN, not PASS. Recommendations do not change normative status.
Validation emits field/code/message and actual type/length where useful; do not copy
secrets from arbitrary metadata into diagnostics. Implementation is in quick_validate.py and
compliance.py; lifecycle callers preserve these checks independently of quality.

Standalone check CLI: zero only for that check's PASS. Final acceptance CLI: zero
only for two PASS values; document nonzero status codes with implementation tests.
This does not change benchmark aggregation's existing rule that a correctly graded
quality failure alone is not an infrastructure error.

### Hash-bound runtime evidence and acceptance

The runtime writes compliance.json beside each installed-skill execution. Both
checks use PASS/FAIL/NOT_RUN/ERROR. Trigger-only or preflight-only runs cannot certify
representative execution; missing legacy evidence remains NOT_RUN. A failed runtime
execution is ERROR, stale skill/runner/environment evidence is FAIL, and corrupt
evidence is ERROR. An artifact violating the standard can never be accepted even
when the separate runtime check passes.

skill_hash covers the execution projection (the same files copied by
copy_tree(skill=True)): all skill resources except root tests/evals and installed
dependencies/caches. It is not just SKILL.md. Root tests/evals are evaluation inputs,
not resources exposed to execution; package inventory is checked separately.
Runtime records include runner_hash (Python bridge/utilities/compliance code,
TypeScript runner/event code and dependency lockfile), effective profile, raw
profile source, model, server version, registry identity, native attachment and
hashes of request/profile/environment/messages/response evidence. Raw evidence and
installed copies must remain available; editing them invalidates acceptance.

assess() validates recorded compatibility for that environment. Final acceptance
and packaging call accept_current(), which performs a new authenticated preflight
and compares server version, background skills, plugin IDs, configuration fingerprint,
profile, model and runner. This is not an OS sandbox or a claim to fingerprint every
external dependency; custom profiles remain controlled:false. Recheck execution
when external tool/dependency behavior changes outside these measured conditions.

compliance.py exits 0 only for two PASS values; otherwise ERROR=3 takes precedence
over FAIL=1 and NOT_RUN=2. package_skill.py requires --run, refreshes the environment
and returns nonzero on any unmet gate. It compares the ZIP's execution projection
to the accepted hash before publishing the archive. Quality orchestration writes
per-configuration compliance into manifest/benchmark; run_loop records standard
eligibility before selection and combined checks before promotion. The selected
copy is still a review candidate, never an automatically installed or approved skill.
Quality failures retain ordinary benchmark semantics; compliance does not rewrite
expectation scores, critique results or historical artifacts.

### Implemented standalone standard validator

`quick_validate.py SKILL [--json] [--lint]` checks Agent Skills only. Its result
contains schema_version, check=`agent_skills`, contract_revision, status, issues,
recommendations, evidence and skill_md_sha256 (the entire SKILL.md file hash).
It does not set a live OpenCode PASS. The tuple helper `validate_skill()` now
enforces this strict standard, while `validate_opencode_static()` is explicitly
separate and leaves opencode_compatibility=NOT_RUN.

Exit codes: 0=PASS, 1=FAIL, 2=NOT_RUN, 3=ERROR. This local check normally runs to
PASS/FAIL/ERROR; 2 is reserved for the shared status contract, not a way to skip a
required check. Invalid format/UTF-8 is FAIL; unreadable input is ERROR. Text output
identifies field/code and actual type/length; JSON does not reproduce source values
from YAML parser exceptions. Missing SKILL.md is a concrete FAIL, not a false PASS.

Limits count parsed Unicode code points without silently trimming values. Names
must exactly match the directory spelling; reference-library NFKC conveniences are
not applied to repair the source. Blank/whitespace-only compatibility is rejected
because it states no environment requirement. Empty license/allowed-tools strings
receive recommendations rather than invented minimum-length constraints.

JSON artifacts use `schema_version: 1` unless a compatibility format is explicitly
noted. UTF-8 paths passed to the runner are absolute. NaN/Infinity are invalid.

## Quality evals

```json
{
  "evals": [{
    "id": "receipt",
    "prompt": "Normalize the attached receipt.",
    "expected_output": "A valid JSON record with correct amounts.",
    "files": ["evals/files/receipt.txt"],
    "expectations": ["The amount is 12.5", "The summary explains any ambiguity"],
    "checks": [{
      "text": "The amount is 12.5",
      "type": "json_equals",
      "file": "receipt.json",
      "path": ["total"],
      "value": 12.5
    }]
  }]
}
```

- IDs are unique filesystem-safe strings/integers, normalized to strings.
- Legacy string files are skill-relative (canonical absolute paths also accepted)
  and retain their basename under executor `inputs/`. For a project tree use
  `{"source":"evals/files/root/package.json","destination":"root/package.json"}`.
  The optional `role` is `fixture` (default), `artifact`, or `evidence`. Explicit
  destinations survive snapshots, optimizer loops and runtime copying. Unsafe paths,
  symlinks, duplicate/prefix destinations and case/Unicode-fold collisions are rejected.
  `input-manifest.json` records transport_version 2, source, destination, role and hash.
  Judges receive `inputs/artifacts/<relative>` and separate `inputs/evidence/` files.
- `expectations` is canonical; legacy `assertions` is accepted on input.
- A deterministic check maps to exactly one expectation by identical `text`.
- Checks: `exists`, `contains`, `equals` (trimmed text), `json_equals` (optional
  array of object keys/array indices in `path`). Paths are confined to `outputs/`.
- `_response.txt` contains the final assistant message text. Requested artifacts are copied
  from the execution Location's `outputs/`.
- Uncovered expectations require `--judge-model`; otherwise grading is an error.

## Trigger evals

Either an `evals` object or a legacy top-level list:

```json
{"evals": [{"id":"positive-1","prompt":"Extract the total from my receipt.","should_trigger":true,"group":"receipt-family-1"}]}
```

Legacy `query` is accepted as `prompt`. IDs distinguish identical query texts.
`group` binds paraphrases for splitting; exact and close normalized duplicates are
also grouped. Conflicting labels within a group are rejected. At least two groups
per class are needed for train/validation, three for a two-class final test.

## Runner request (`request.json`)

```json
{
  "schema_version": 1,
  "operation": "run",
  "directory": "/absolute/execution-location",
  "output": "/absolute/result-directory",
  "prompt": "User task",
  "model": {"providerID":"openai","id":"model-id","variant":"low"},
  "agent": "skillcraft-eval",
  "timeout_seconds": 180,
  "mode": "quality",
  "skill_id": "candidate",
  "skill_path": "/absolute/execution-location/.agents/skills/candidate/SKILL.md",
  "skill_sha256": "sha256-of-SKILL.md",
  "attach_skill": true,
  "permissions": [{"action":"*","resource":"*","effect":"deny"}],
  "controlled": true,
  "run_id": "unique-run-id"
}
```

Modes: `quality`, `trigger`, `judge`, `optimizer`. Operation `probe` performs
preflight without a model call. Baseline omits `skill_path`/hash/attachment but
retains `skill_id` so global candidate leakage can be detected. Authentication is
environment-only. Python captures exit status and preserves runner errors.

## Run result (`run.json`)

Contains run ID, session/prompt IDs, mode, model, agent, execution directory,
fixture hashes, last durable sequence, environment audit, error and cancellation.

Statuses:

- `completed`: terminal success, admitted prompt confirmed, owned work finished.
- `preflight_error`: version, catalog or isolation problem before execution.
- `execution_error`: provider/model/agent execution failed.
- `permission_blocked`, `question_blocked`: interactive execution cannot proceed.
- `timeout`, `cancelled`, `interrupted`: incomplete execution, cancellation recorded.
- `transport_error`, `attachment_error`: protocol/collection/attachment failure.
- `snapshot_error`: the executor modified a frozen skill or input fixture.
- `artifact_error`: output collection failed (for example, an output symlink).
- `runner_error`, `runner_lost`: bridge failed or did not acknowledge termination.

`triggered` is true after a confirmed successful candidate tool load; false only
after completed observation without a load; otherwise null. Even when true, a
failed run is excluded from trigger classification metrics and counted as an error.
`attached` records forced attachment confirmation and is not natural triggering.

## Usage and timing

`usage.json` separates `executor` and `maintenance` (each null if unknown). Known
usage contains `tokens: {input,output,reasoning,cache:{read,write}}`, `total_tokens`,
`cost`, and `records`. `projection_fallback` marks message-based recovery;
`maintenance_complete` indicates whether that fallback limited maintenance telemetry.
The `role` identifies quality/trigger/judge/optimizer execution. Judge usage lives
in `judge/usage.json`, optimizer usage in its own run folder.

`timing.json.total_duration_seconds` is wall time including preflight and collection.
`output_chars` in benchmarks is independent of token usage.

Opt-in delegation adds root_executor and child_executors provenance to usage.json;
executor is their sum when every component is known. Child messages/usage live in
children/<session-id>.json. Effective permissions, steps and delegation choices are
saved in profile.json and run.profile; experiment manifests retain the profile hash.

Profiles optionally accept `cli_tools: [{id, executable, environment}]`. IDs are
unique safe identifiers; executable is an installed absolute executable path;
environment contains only non-secret string settings. `{state}` and `{project}`
expand per execution. `cli-state.json` records launcher paths and observations
(`observed` or `unverified`); this is scoped-launcher evidence, not sandbox evidence.
Use actual tool cache/database probes before relying on a new tool's state settings.

## Grading

`json_multiset_equals` accepts the same file/path fields as `json_equals` and an
expected JSON list. It compares elements without ordering, preserving multiplicity
and JSON types (true differs from 1). Missing/invalid output fails the criterion;
a non-list expected value is a rubric configuration error. Use it for exhaustive
sets where duplicates must not silently disappear. Process/truncation claims still
require actual execution evidence, not only deterministic self-report fields.

### Evidence projection v1

The Python bridge derives `evidence/` from immutable `messages.json`; legacy
`transcript.md` remains untouched. `evidence/transcript.md` leads to bounded index
pages and lossless full-message parts (2400 Unicode characters each, at most 9600
UTF-8 bytes). The manifest records message IDs/order, character ranges, part hashes,
byte counts and source hash. Reassembling parts reproduces each canonical message
including inputs, outputs, errors, user and final assistant text. No model summary
replaces evidence. The grader verifies source/parts before calling a judge, then
copies all referenced pages/parts under `inputs/evidence/`; legacy runs still use
their raw transcript. Grading records the projection version and manifest hash.
Compatibility hashes cover the renderer and derived evidence. The size bound is a
tested reading convenience, not a guarantee for every client or arbitrary artifact.

Grader contract 2 supplies task/expected_output, all_expectations,
grade_expectations, deterministic_results, artifacts, transcript_path and compliance.
The judge returns only expectations and eval_feedback; the orchestrator computes
summary and persists results. `grader_contract` records version and prompt hash.
Judge mode uses read-oriented system instructions and denies edit tools. Listed
artifact notes are evidence, not authoritative timing/usage. A valid response never
overrides a non-completed judge execution status.

```json
{
  "schema_version": 1,
  "status": "completed",
  "expectations": [{"text":"The amount is 12.5","passed":true,"evidence":"receipt.json total: 12.5","source":"deterministic"}],
  "summary": {"passed":1,"failed":0,"total":1,"pass_rate":1.0}
}
```

Statuses are `completed`, `error`, `not_run`. On error, summary is null and error
explains why; a judge failure is never a failed expectation. An empty rubric has
unknown pass rate. The judge returns exact expectation strings, boolean verdicts
and nonempty evidence, validated before aggregation.

Additive partial-grading fields: `pending_expectations`, `coverage` (graded/total/
pending), `partial_summary` (known passed/failed/graded), `failure_stage`, and
`evaluation_complete`. Completed deterministic verdicts are checkpointed even when
a later check or judge fails. Partial grading retains `summary:null`; ungraded
criteria are not false verdicts. Complete verdicts with required critique failure
keep their summary but `evaluation_complete:false`; aggregate quality rates exclude
these incomplete evaluations. Executor completion and evaluation completion are
reported separately. Legacy errors lacking partial evidence remain unknown.

## Benchmark and manifest

Quality manifests include `pilot_policy` (case IDs, configurations, max_in_flight,
stop_threshold), and persisted `queue.jobs` states. `pilot/` is a separate phase
with its own manifest, benchmark and static review. Main `pilot_summary` records
status, resources and failure/repair context. `scheduling` distinguishes planned,
started, pipeline_completed, not_started_after_stop and missing_unexpected. Missing
or stopped work keeps the source experiment incomplete; task FAIL alone is not a
technical stop. Queue failure fingerprints normalize invocation-specific paths/IDs.

### Immutable grading attempts and explicit selection

The first grade retains `run/grading.json` and `run/judge/` for legacy consumers,
with a new `grading-manifest.json` and frozen `rubric.json`. A second invocation
cannot overwrite it. `grade.py RUN --output NEW_ATTEMPT --rubric ROW_JSON` reserves
a new directory **outside** the source execution, records execution/task/rubric,
model/profile/renderer identities, and seals the result hash. A changed task,
fixtures or expected output contract requires a new execution. `required_evidence`
can name source-relative files necessary for a revised rubric; missing files reject
reuse. Incomplete attempts are retained, never selected automatically. Regrading
does not refresh OpenCode runtime acceptance of an older execution.

`aggregate_benchmark.py SOURCE --selection SELECTION --output NEW_REPORT` writes
outside SOURCE. Selection version 1 contains exact `source_workspace`, a nonempty
predeclared `rule`, and `runs: {relative_run: {selected: absolute_attempt_or_legacy,
history: [absolute_attempt_or_legacy]}}`. Every source run must be explicit. The
default without selection reads the first legacy grade, never the newest/best one.
An explicit selection may set `included:false` with a nonempty `reason`. Such rows
remain in the report and source error/resource totals, but not selected summary
statistics. Fix the inclusion rule before inspecting scores. Empty selections are
invalid; unmatched pairs are listed. Source-planned and selected-planned counts
are separate. An excluded source failure is still disclosed as an incomplete source
experiment, never silently converted to success.
Selected pairs need matching known rubric hashes and grading conditions (judge,
profile, prompt, projection version and runner). Missing/incompatible rubrics
or judge environment identities (server, control status, background skills, plugins,
configuration fingerprint; excluding incidental location diagnostics)
block a comparable quality delta. Source evidence/result/rubric hashes are checked.
Selected judge tokens and all listed attempt tokens are separate; executor is
counted once. Unknown usage stays null; attempt path aliases are deduplicated.

The viewer accepts `--selection`, shows selected and listed historical attempts,
and binds feedback IDs to execution plus attempt. Use `--static` outside SOURCE
or a separate `--feedback-output` for a live/imported review. Selection support
uses SkillCraft's virtualenv. Old feedback cannot approve a newly selected grade.

### Additive critique contract (schema version 1)

`grading.eval_feedback` contains `status: completed|error|not_run`, `required`,
`issues`, and optional `error`/`reason`. Each issue has `category` (weak_check,
ambiguity, coverage_gap, false_confidence), `expectation` (exact rubric text or
null), nonempty `explanation` and `suggestion`, and optional string/null `evidence`.
Passing a judge model requests critique even for fully deterministic grading.
Critique failure does not overwrite valid grading; a required incomplete critique
is an experiment error. Legacy grading without this field means critique was not
run, not that a judge found no issues. Rubric suggestions apply to future versions.

`manifest.json` fixes evals, models, runtime versions, repetition count, configuration
roles, snapshot hashes and requested isolation. Final environment checks add
`controlled` and `environment_consistent`.

`benchmark.json` contains `metadata`, `run_summary`, `deltas`, `runs`, `errors`.
Roles are explicit: `without_skill`, `with_skill`, `previous_skill`. Statistics
contain `n,mean,stddev,min,max`; missing values remain null. Deltas pair the same
eval and repetition, rather than comparing arbitrary directory order. Execution
and grading status counts are shown alongside pass rate and completion rate.

## Trigger report and optimizer history

Trigger rows may carry `category`, `capability` and `preparation`. Preparation is a
list of `{tool, args, cwd}` steps using configured CLI launcher IDs and existing
relative directories under inputs/. It runs at the final per-execution root before
model observation, with a 60-second bound per command. Full stdout/stderr, exit code
and elapsed setup time are saved in `preparation.json`; failure means
`preparation_error`, not a false negative. Preparation must preserve input hashes.
Do not distribute an index tied to another absolute project root.

Launcher records distinguish `phase:preparation` from `phase:model` and timestamp
actual invocation starts. `trigger-diagnostics.json` separately records successful
skill events, model CLI use, use after load (null if timing is unavailable), terminal
status/reason and independent task correctness. The `--judge-model` option grades
rows with expectations; quality does not change the trigger confusion matrix.
Non-completed executions are excluded from that matrix even when an observed load
is retained separately. Reports count unique cases/groups, repeats, errored loads,
practical uses and unknown quality. Preparation costs/time are outside model usage.

Quality and aggregate CLIs print a bounded human summary by default. Pass `--json`
for full machine-readable stdout; Python return values and persisted benchmark JSON
retain the complete report. `summary.md` points to full evidence and states unique
case/repetition counts, errors, selection and scope. `provenance` binds the source
manifest/candidate/profiles/fixtures and rows identify execution and grading attempt.
`resources` separates source and selected runs, executor, selected/all-listed judge
attempts and maintenance, with known sums/counts and null totals when incomplete.
Optional `pilot_summary` in the manifest is copied to `benchmark.pilot` separately;
pilot runs are not counted as comparative executions. Unknown telemetry is not zero.

### Paired resource comparisons (additive version 1)

Each deltas baseline contains paired_pass_rate, paired_time_seconds and paired_tokens
statistics, each with its own n. Values are with_skill minus baseline. Positive
time/tokens means more consumption, not better quality. Time is wall seconds including
preflight/collection; tokens are executor-only. Only completed pairs with matching
eval/repeat, model, agent and fixture metadata and no known environment drift are
compared. Missing individual metrics exclude that pair only for that metric; excluded
executions remain in per-run summaries. Legacy missing metadata is unknown, not a
new isolation guarantee. No percentage division by a zero baseline is performed.
completion_rate measures found runs; planned_completion_rate uses planned_runs and
includes missing_runs. An unknown planned denominator remains null.

`trigger-results.json` has per-run records and a summary with `tp,fp,tn,fn,total,
valid,errors,error_rate,precision,recall,accuracy`. Undefined ratios are null.

`split.json` fixes groups, seed and independent-test availability. `history.json`
stores each candidate/hash, training and validation metrics, balanced-accuracy
selection score, selected path, final test and quality regression. Validation
errors disqualify a candidate. Ties retain the earlier candidate. The optimizer
only sees training evidence. Final test never selects or revises the candidate.
The selected directory is exported for review; the installed source is unchanged.

## Feedback

### Training-only optimizer history

The general history adds `description`, `rationale`, `train_results` and
`validation_results` per iteration, plus `final_test_results` only for `best`.
This report-facing history is never the optimizer input. The visual report exposes
per-repeat expected/actual triggering, FP/FN vs errors, missing records, independent
datasets and the validation-selected candidate. Its CLI can hydrate older summary
histories from adjacent owned iteration directories; absent records remain unknown.

`training-history.json` is a list in generation order. Entries contain candidate_id,
snapshot_hash, description, rationale, training_results (eval_id, repeat, prompt,
should_trigger, triggered, status). The optimizer receives an allowlisted projection
of this list plus current training evidence; unknown fields and non-training IDs
are discarded. It never receives the general history's validation ranking or final
test. Missing legacy fields remain null and appear in missing_fields. Each optimizer
run saves optimizer-input.json and its full request prompt. History is not silently
truncated for context savings; provider/context failures remain explicit errors.

```json
{
  "schema_version": 1,
  "status": "in_progress",
  "reviews": [{"run_id":"unique-run-id","snapshot_hash":"candidate-hash-or-null","reviewed":true,"feedback":"Human comment","timestamp":"ISO timestamp"}]
}
```

Run IDs and snapshot hashes must match the current experiment. Complete feedback
requires every run to be explicitly reviewed. Static feedback is downloaded and
imported by an explicit path. Previous-iteration display uses logical run paths;
current feedback uses unique run IDs to prevent accidental cross-iteration reuse.
