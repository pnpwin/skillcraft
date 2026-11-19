---
name: skillcraft
description: Create and improve Agent Skills for Codex (including local chats in the ChatGPT desktop app) and OpenCode. Use when the user wants to turn a recurring workflow into a skill, revise skill instructions, compare versions with A/B tests, review a skill benchmark, or improve when a skill is picked.
license: Apache-2.0
compatibility: Evaluations require Python 3.11+, a configured model, and the Codex App Server or OpenCode V2 APIs used by SkillCraft. The OpenCode runner also requires Node.js 22+ and shared absolute filesystem paths.
---

# SkillCraft

Build skills through measured iterations: intent → draft → evals → baseline/with-skill
→ grading → human feedback → revision → regression check → triggering optimization.

## Required methodological reading

Before interviewing or drafting, read **references/authoring-guide.md in full**.
Before creating, reviewing or changing frontmatter, also read
**references/agent-skills-specification.md in full**. It distinguishes mandatory
Agent Skills rules from runtime behavior and portability recommendations.
Before defining evaluations, grading, reviewing or revising, read
**references/evaluation-and-iteration.md in full**. These contain the complete
method, examples and reasoning, not optional background. Reuse them if already
loaded; do not substitute a short summary for unread guidance. Before triggering
optimization, consult that guide's natural-triggering and optimization sections.

There is no content-length target: preserve useful explanations and examples.
References organize the work; they do not justify economizing context at the
expense of authoring quality. `references/upstream-coverage.md` tracks the transfer.
Start at the user's current stage and honor their choice of formal tests versus
subjective review; an empty rubric has unknown automated quality, not a failed task.

## Setup and paths

Install the plugin from Git, or place this skill directory in
`<project>/.agents/skills/skillcraft/`. Use the directory containing this
`SKILL.md` as the base for resource paths and script commands. Keep experiments in
`<project>/.agents/skillcraft-workspaces/<skill-id>/iteration-NNN/`.

Use `SKILLCRAFT` for the absolute directory containing this `SKILL.md`. Keep
`SKILLCRAFT_VENV` at an absolute, writable path outside that directory so it
survives plugin updates. Install Python dependencies there and install Node
dependencies when using OpenCode.

```sh
export SKILLCRAFT_VENV="${SKILLCRAFT_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/skillcraft/venv}"
python3 -m venv "$SKILLCRAFT_VENV"
"$SKILLCRAFT_VENV/bin/python" -m pip install -r "$SKILLCRAFT/requirements.txt"
npm --prefix "$SKILLCRAFT/scripts/opencode-runner" ci  # OpenCode
```

Use the `SKILLCRAFT_VENV` interpreter for all Python commands. Scripts detect the
harness from the calling session, including local Codex chats in the ChatGPT
desktop app. In a terminal or CI, pass `--harness codex` or `--harness opencode`.
`--harness chatgpt` is an alias for the Codex runner. Read `references/codex-runner.md` or
`references/opencode-runner.md` before configuring models, authentication or
permissions.
Read `references/schemas.md` before generating eval inputs or consuming results.

## Frontmatter: mandatory Agent Skills contract

For release, verify Agent Skills conformance and compatibility with every
target harness. The checks report PASS, FAIL, NOT_RUN or ERROR. Task quality
and triggering have their own measurements.

| Field | Required | Constraint / purpose |
|---|---|---|
| `name` | Yes | Nonempty, 1–64 characters; lowercase Unicode alphanumeric characters and hyphens; no leading/trailing hyphen or `--`; matches its directory name |
| `description` | Yes | Nonempty string, at most 1024 characters; explains purpose and when to use the skill |
| `license` | When applicable | License name or reference to an included license file |
| `compatibility` | When applicable | String of 1–500 characters describing actual environment requirements |
| `metadata` | When applicable | Mapping from string keys to string values |
| `allowed-tools` | When applicable | Space-separated tool string; experimental and client-dependent |

Read the required specification reference for valid/invalid examples, YAML types,
Unicode details, allowed directory structure and the distinction between declared
tools and effective permissions. Do not invent optional values. Recommended main
file sizes are guidance, not permission to delete or shorten useful instructions.

## 1. Understand the task

Establish the intended outcome, inputs, artifacts, tool requirements, and situations
where the skill should or should not trigger. Reuse evidence from the conversation.
Ask only for genuinely missing requirements. Record representative requests and
what success means. Distinguish quality problems from selection problems.
Before drafting, state the agreed task and expected deliverable, using requirements
already established. Confirm substantive unresolved interpretations with the user;
ask only about real gaps. Complete requirements need acknowledgment, not another
approval round. Follow the task flow: extract known requirements → clarify gaps →
confirm the task/result → draft → verify both contracts and the agreed quality
criteria → present artifacts, evidence and remaining limitations.

## 2. Write a draft

Create `.agents/skills/<skill-id>/SKILL.md` with a clear description and concise
instructions. Match the frontmatter `name` to the directory identifier. Use a
human-readable Markdown heading for the title.
Put reusable code in `scripts/`, detailed knowledge in `references/`, and templates
in `assets/`.
Explain the purpose of important instructions. Prefer generalizable rules over
instructions tied to a single test. Supporting resources load only when read.
Before adding or materially changing scripts, read
**references/script-design.md in full** for applicable interface, dependency,
output, failure and repeatability patterns. Scripts are optional; use them when
verified recurring work benefits from a stable implementation.

Validate with:

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/quick_validate.py" "$CANDIDATE" --lint
```

Agent Skills requires a description of at most 1024 characters. Check format
conformance separately from actual runtime compatibility.
Preserve upstream licenses. The detailed examples and exceptions of the reference
implementation are explained in the required specification guide.

## 3. Define quality evals

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

## 4. Run paired quality evaluations

Choose explicit executor and judge models available in the selected harness. For a
new skill compare `without_skill` against `with_skill`; for revisions also pass
`--previous` pointing to the saved prior skill directory.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/run_quality_eval.py" \
  --skill "$CANDIDATE" --evals "$QUALITY_EVALS" --workspace "$ITERATION" \
  --model "$EXECUTOR_MODEL" --judge-model "$JUDGE_MODEL" --repeats 3 --workers 2
```

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

## 5. Grade and review

The orchestrator applies deterministic checks and sends remaining expectations to a
separate judge session using `references/prompts/grader.md`. A grading error is
recorded separately from the expectation's verdict. The files in
`references/prompts/` configure the independent evaluation sessions.
When `--judge-model` is supplied, the judge also critiques the full rubric, including
deterministic checks. With all expectations covered deterministically this is a
critique-only call. Without a judge, critique is explicitly not run. Requested
critique failures are reported separately and make the experiment incomplete,
without changing valid expectation verdicts. Apply suggestions to a new rubric
version and rerun comparable candidates rather than revising scores retroactively.

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

## 6. Revise and repeat

Inspect failing expectations, artifacts, judge evidence and human feedback together.
Find root causes and weak evals. Revise general instructions or supporting scripts;
avoid overfitting to prompts. Save the old candidate, create a new iteration directory,
and repeat all affected evals against baseline and the previous version. Report
paired deltas, variance, completion rates and any environment drift.


For opted-in projects, SkillCraft can accumulate verified successes,
failures, and corrections, draft a small revision, and run A/B before
locally applying it. See [automatic learning](references/learning.md)
before configuring collection or automatic updates.

## 7. Evaluate natural triggering

Build realistic positive, negative and near-miss requests. Include negatives sharing
keywords with the skill. Each row has a stable ID, prompt, boolean `should_trigger`,
and a `group` shared by paraphrases/near-duplicates.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/run_eval.py" \
  --skill "$CANDIDATE" --evals "$TRIGGER_EVALS" --workspace "$TRIGGER_RUN" \
  --model "$EXECUTOR_MODEL" --repeats 3 --workers 2
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

## 8. Optimize the description

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
  --model "$EXECUTOR_MODEL" --optimizer-model "$OPTIMIZER_MODEL" \
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
quality run using native attachment. Release this shared SkillCraft package with
evidence from both harnesses.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/compliance.py" "$CANDIDATE" \
  --run "$QUALITY_RUN" --workspace "$NEW_ACCEPTANCE_DIR"
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/package_skill.py" "$CANDIDATE" "$DIST" \
  --run "$CODEX_QUALITY_RUN" --run "$OPENCODE_QUALITY_RUN" --verification-dir "$NEW_PACKAGE_CHECK_DIR"
```

Each command checks evidence hashes and refreshes its target environment.
Packaging also verifies the archived bytes. Refresh evidence after changes to
the skill, runner or environment. Report quality and human feedback separately.
