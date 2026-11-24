# Run SkillCraft evaluations

Read the sections for the current task. See the [methodology](evaluation-and-iteration.md) for experiment design and the [configuration reference](configuration.md) for setup.

## Define quality evals

Start with a small representative set, then cover edge cases and regressions. Write
`evals/evals.json` with unique IDs, prompts, optional skill-relative input files,
expected_output, and verifiable `expectations`. Add deterministic `checks` where
possible; use a judge model for substantive criteria. Do not reward trivial output
features when they do not demonstrate task success. Fix the rubric before comparison.

For an evaluation-design deliverable, specify executable policies, not placeholders:
which incomplete results are excluded from quality scores, which planned runs stay
in completion denominators, and the concrete score-independent rule selecting the
same rubric/grader/environment for every compared configuration. Include output
schema checks and meaningful negative controls. See the evaluation guide's design
review section for a worked policy and its limits.

## Run paired quality evaluations

Choose explicit executor and judge models available in the selected harness. For a
new skill compare `without_skill` against `with_skill`; for revisions also pass
`--previous` pointing to the saved prior skill directory.
Set `HARNESS` to `codex`, `opencode`, or `hermes` to run these commands from a
terminal. The same harness options are available for natural-trigger evaluation
and description optimization. Hermes uses the isolated macOS profile.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/run_quality_eval.py" \
  --skill "$CANDIDATE" --evals "$QUALITY_EVALS" --workspace "$ITERATION" \
  --harness "$HARNESS" --model "$EXECUTOR_MODEL" --judge-model "$JUDGE_MODEL" --repeats 3 --workers 2
```

For Hermes with the `openai-codex` provider, use `--model openai-codex:MODEL`
and a rubric covered entirely by deterministic file checks. Omit judge arguments.
This provider requires a signed-in Hermes profile; the supported loopback `custom`
provider does not. Both require the local Hermes Chat CLI and macOS
`sandbox-exec`. Use `SKILLCRAFT_HERMES_HOME` to select a source profile. The
baseline catalog and candidate preload are checked against native requests before
In a Hermes skill-quality run, the baseline catalog and candidate preload are
checked against native requests before grading. Hermes also supports instruction
prompt A/B with deterministic checks; see [prompt A/B evaluation](prompt-ab-evaluation.md)
for that distinct workflow.
Read the [Hermes runner guide](hermes-runner.md) before setting up Hermes or
interpreting its profile, model, isolation, or proof artifacts.

The runner creates fresh sessions and independent fixtures. `with_skill` uses a
native API attachment; this measures quality after loading, not natural selection.
Snapshots are fixed before workers run. Outputs, logs, grading and usage are saved.
Quality CLI stdout is a concise summary; use `--json` when parsing its full report.
An end-to-end pilot runs first for every configuration. Select representative cases
with `--pilot-case ID` (repeat for distinct capabilities; default first case).
Technical failures stop dispatch; valid negative quality verdicts do not. Pilot
cost and evidence are separate from main scores. See evaluation-and-iteration.md.
Do not edit an in-flight snapshot or reuse an experiment directory.

Both default profiles support input reads, skill resources and output writes.
OpenCode can opt in to additional capabilities with `--profile`. Those runs
are labelled uncontrolled. Codex uses its own sandbox settings. See the
corresponding runner guide for the supported operations and bounds.

## Grade and review

For Codex and OpenCode, the orchestrator applies deterministic checks and sends
remaining expectations to a separate judge session using
`references/prompts/grader.md`. A grading error is recorded separately from the
expectation's verdict. When `--judge-model` is supplied, the judge also critiques
the full rubric, including deterministic checks. With all expectations covered
deterministically this is a critique-only call. Without a judge, critique is
explicitly not run. Requested critique failures are reported separately and make
the experiment incomplete, without changing valid expectation verdicts. Apply
suggestions to a new rubric version and rerun comparable candidates rather than
revising scores retroactively. Hermes supports deterministic checks only: every
expectation must be covered by file checks, and judge-model grading and critique
are unsupported.

The files in `references/prompts/` configure the independent evaluation sessions.

Inspect actual artifacts, evidence, errors, repetitions, token usage and latency.
Show the human reviewer the viewer:

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/generate_review.py" \
  "$ITERATION" --benchmark "$ITERATION/benchmark.json" --no-browser
```

Open the printed loopback URL using the available browser. For static review use
`--static "$ITERATION/review.html"`; import the explicitly selected feedback file
with `--import-feedback`. Do not guess a download filename. Feedback is bound to
run IDs and snapshots. Wait for real human feedback; do not invent approval.

For subjective comparisons, `scripts/compare.py` produces a randomized blind A/B
comparison and a separate unblinded analysis using the comparator/analyzer resources.

## Revise and repeat

Inspect failing expectations, artifacts, judge evidence and human feedback together.
Find root causes and weak evals. Revise general instructions or supporting scripts;
avoid overfitting to prompts. Save the old candidate, create a new iteration directory,
and repeat all affected evals against baseline and the previous version. Report
paired deltas, variance, completion rates and any environment drift.


For opted-in projects, SkillCraft can accumulate verified successes,
failures, and corrections, draft a small revision, and run A/B before
locally applying it. See [automatic learning](learning.md)
before configuring collection or automatic updates.

## Evaluate natural triggering

Build realistic positive, negative and near-miss requests. Include negatives sharing
keywords with the skill. Each row has a stable ID, prompt, boolean `should_trigger`,
and a `group` shared by paraphrases/near-duplicates.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/run_eval.py" \
  --skill "$CANDIDATE" --evals "$TRIGGER_EVALS" --workspace "$TRIGGER_RUN" \
  --harness "$HARNESS" --model "$EXECUTOR_MODEL" --repeats 3 --workers 2
```

The prompt must not instruct the model to load the candidate or disclose its ID.
The runner stages the skill under `.agents/skills/` and observes native invocation.
OpenCode records successful `skill` tool calls. Codex records implicit-invocation
events scoped to the turn. Errors, timeouts and unavailable observations have
their own counts. Use quality evals to measure whether the skill was followed.

Include real action cases with prepared projects and close negatives. Separate
ready, missing-but-creatable and read-only-missing capabilities. Path-bound indexes
are prepared on the final root via explicit `preparation` steps (see schemas.md),
not copied from another project. `--judge-model` can independently grade task
outcomes; trigger diagnostics preserve observed loads through later failures and
distinguish preparation from model CLI use. Pilot new capability slices before
repeats; keep description and model/profile fixed within each slice.

## Optimize the description

If the parsed description exceeds 1024 characters, read
**references/description-reformulation.md in full** and use its dedicated
shrink_description.py workflow first. It preserves the oversized source as an
immutable, non-promotable historical baseline, reformulates within the limit and
runs blind semantic review, held-out triggering A/B and quality regression.
At 1024 characters or fewer, do not rewrite solely because of length. Show the
generated diff and results before changing the installed skill; readiness is not
human approval. The normal optimizer below accepts only standard-valid candidates.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/run_loop.py" \
  --skill "$CANDIDATE" --evals "$TRIGGER_EVALS" --workspace "$OPTIMIZATION" \
  --harness "$HARNESS" --model "$EXECUTOR_MODEL" --optimizer-model "$OPTIMIZER_MODEL" \
  --quality-evals "$QUALITY_EVALS" --judge-model "$JUDGE_MODEL" --iterations 3
```

The loop groups duplicates before splitting train/validation/final test. It improves
on train, selects on validation and evaluates final test only after selection. Small
datasets may have no independent test; report this explicitly. The selected copy
is exported, never silently applied to the installed candidate. Run quality regression
checks after description selection, and review their results before promotion.
Previous descriptions, training results and rationale are supplied to the optimizer
in generation order. The interactive `report.html` shows dataset matrices,
per-repeat results, errors, and the final-test comparison with the original
description. Use `scripts/generate_report.py HISTORY_JSON OUTPUT_HTML`
to regenerate a report, including legacy histories with adjacent result files.

## Completion

Deliver the skill directory, concise usage instructions, benchmark/trigger reports,
human feedback status and remaining limitations. `scripts/package_skill.py` can
create a distribution ZIP. Install by extracting the skill directory into
`.agents/skills/`. Include the license and keep experiment artifacts separate.

For each target harness, verify the candidate against a completed representative
quality run using native attachment. The packager requires the completed Codex and OpenCode runs shown below.
For Hermes also retain its native file-run evidence.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/compliance.py" "$CANDIDATE" \
  --run "$QUALITY_RUN" --workspace "$NEW_ACCEPTANCE_DIR"
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/package_skill.py" "$CANDIDATE" "$DIST" \
  --run "$CODEX_QUALITY_RUN" --run "$OPENCODE_QUALITY_RUN" --verification-dir "$NEW_PACKAGE_CHECK_DIR"
```

Each command checks evidence hashes and refreshes its target environment.
Packaging also verifies the archived bytes. Refresh evidence after changes to
the skill, runner or environment. Report quality and human feedback separately.
