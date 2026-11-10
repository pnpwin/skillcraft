---
name: skillcraft
description: Create, improve, and evaluate OpenCode skills. Use this skill whenever the user wants to turn a repeated workflow into a skill, draft or revise skill instructions, compare versions against a baseline, review skill benchmarks, or diagnose and improve natural triggering—even when they do not name SkillCraft or use evaluation terminology. Also use it for requests such as “capture this process as a skill” or “make my skill get picked for the right tasks.” Ordinary prose editing, unrelated coding, and explanations of a term without a skill-authoring or evaluation task are outside its scope.
metadata:
  opencode/autoinvoke: "true"
license: Apache-2.0
compatibility: Requires OpenCode 2.2.6, Python 3.11+ with requirements.txt installed, Node.js 22+ with runner dependencies installed, and an accessible configured model. Runner and server must share the same absolute filesystem paths.
---

# SkillCraft for OpenCode V2

Build skills through measured iterations: intent → draft → evals → baseline/with-skill
→ grading → human feedback → revision → regression check → triggering optimization.

## Required methodological reading

Before interviewing or drafting, read **references/authoring-guide.md in full**.
Before creating, reviewing or changing frontmatter, also read
**references/agent-skills-specification.md in full**. It distinguishes mandatory
Agent Skills rules from OpenCode runtime behavior and portability recommendations.
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

Install this directory at `<project>/.opencode/skills/skillcraft/`. All paths below
are relative to this SKILL.md, not the shell's current directory. Resolve this base
directory before running scripts. Keep experiments in
`<project>/.opencode/skillcraft-workspaces/<skill-id>/iteration-NNN/`.

Requirements: Python 3.11+, Node 22+, OpenCode **2.2.6** running locally or at an
explicitly configured endpoint. Install once:

```sh
python3 -m venv "$SKILLCRAFT/.venv"
"$SKILLCRAFT/.venv/bin/pip" install -r "$SKILLCRAFT/requirements.txt"
npm --prefix "$SKILLCRAFT/runner" ci
```

Here `SKILLCRAFT` is the absolute directory containing this SKILL.md. Use its
virtualenv Python for all commands. Read `references/opencode-runner.md` before
configuring models, isolation, server authentication or a custom permissions profile.
Read `references/schemas.md` before generating eval inputs or consuming results.

## Frontmatter: mandatory Agent Skills contract

Every releasable skill must pass **both** strict Agent Skills validation and
verified OpenCode compatibility. OpenCode accepting a file does not waive the
format rules. Both results use PASS / FAIL / NOT_RUN / ERROR; only two PASS values
establish acceptance. Task quality and triggering are additional evidence.

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

Create `.opencode/skills/<skill-id>/SKILL.md` with a clear description and concise
instructions. The directory determines the ID; `name` is a display label.
For Agent Skills conformance, that frontmatter label must also meet the standard's
identifier rules and match the directory; use a human-readable Markdown heading
for a separate title. This requirement is stricter than OpenCode's loader.
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
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/scripts/quick_validate.py" "$CANDIDATE" --lint
```

OpenCode has no runtime 1024-character description limit, but Agent Skills does:
a release description must be at most 1024 characters. Check format conformance
separately from actual runtime compatibility; recommendations are not format errors.
Preserve upstream licenses. The detailed examples and exceptions of the reference
implementation are explained in the required specification guide.

## 3. Define quality evals

Start with a small representative set, then cover edge cases and regressions. Write
`evals/evals.json` with unique IDs, prompts, optional skill-relative input files,
expected_output, and verifiable `expectations`. Add deterministic `checks` where
possible; use a judge model for substantive criteria. Do not reward trivial output
features when they do not demonstrate task success. Fix the rubric before comparison.

## 4. Run paired quality evaluations

Choose explicit executor and judge models available on this OpenCode server. For a
new skill compare `without_skill` against `with_skill`; for revisions also pass
`--previous` pointing to the saved prior skill directory.

```sh
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/scripts/run_quality_eval.py" \
  --skill "$CANDIDATE" --evals "$QUALITY_EVALS" --workspace "$ITERATION" \
  --model "$EXECUTOR_MODEL" --judge-model "$JUDGE_MODEL" --repeats 3 --workers 2
```

The runner creates fresh sessions and independent fixtures. `with_skill` uses a
native API attachment; this measures quality after loading, not natural selection.
Snapshots are fixed before workers run. Outputs, logs, grading and usage are saved.
Do not edit an in-flight snapshot or reuse an experiment directory.

The default bounded-files profile permits input reads, skill resources and output
writes. Skills requiring shell/network need an explicit `--profile`; such runs are
labelled uncontrolled unless an external sandbox is independently established.
Report that limitation with the benchmark. Never call a new session a sandbox.
Profiles can inherit bounded file rules, set a step budget and opt into the owned
worker for delegation. The effective profile and child evidence are recorded; see
`references/opencode-runner.md` before changing capabilities.

## 5. Grade and review

The orchestrator applies deterministic checks and sends remaining expectations to a
separate judge session using `agents/grader.md`. A grading error is not a failed
expectation. These `agents/*.md` files are prompt resources, not registered agents.
When `--judge-model` is supplied, the judge also critiques the full rubric, including
deterministic checks. With all expectations covered deterministically this is a
critique-only call. Without a judge, critique is explicitly not run. Requested
critique failures are reported separately and make the experiment incomplete,
without changing valid expectation verdicts. Apply suggestions to a new rubric
version and rerun comparable candidates rather than revising scores retroactively.

Inspect actual artifacts, evidence, errors, repetitions, token usage and latency.
Show the human reviewer the viewer:

```sh
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/eval-viewer/generate_review.py" \
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

## 7. Evaluate natural triggering

Build realistic positive, negative and near-miss requests. Include negatives sharing
keywords with the skill. Each row has a stable ID, prompt, boolean `should_trigger`,
and a `group` shared by paraphrases/near-duplicates.

```sh
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/scripts/run_eval.py" \
  --skill "$CANDIDATE" --evals "$TRIGGER_EVALS" --workspace "$TRIGGER_RUN" \
  --model "$EXECUTOR_MODEL" --repeats 3 --workers 2
```

The prompt must not instruct the model to load the candidate or disclose its ID.
The runner installs and advertises a real native skill, then records successful
`skill` tool calls. Other tools do not end the observation. Technical errors and
timeouts remain errors, not false negatives. Loading is not proof of following
the skill: use quality evals for that.

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
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/scripts/run_loop.py" \
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
in generation order. The interactive `report.html` shows separate dataset matrices,
per-repeat errors/results and the validation-selected candidate; final test is shown
only for that candidate. Use `scripts/generate_report.py HISTORY_JSON OUTPUT_HTML`
to regenerate a report, including legacy histories with adjacent result files.

## Completion

Deliver the skill directory, concise usage instructions, benchmark/trigger reports,
human feedback status and remaining limitations. OpenCode installs directories;
`scripts/package_skill.py` can create an optional distribution ZIP. A `.skill` ZIP
does not install itself. Keep artifacts out of the distribution and include LICENSE.

Before acceptance or distribution, verify the current candidate against a completed
representative quality run using native attachment. Natural triggering alone is
not compatibility proof. Both Agent Skills and OpenCode must report PASS:

```sh
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/scripts/compliance.py" "$CANDIDATE" \
  --run "$QUALITY_RUN" --workspace "$NEW_ACCEPTANCE_DIR"
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/scripts/package_skill.py" "$CANDIDATE" "$DIST" \
  --run "$QUALITY_RUN" --verification-dir "$NEW_PACKAGE_CHECK_DIR"
```

Each command checks evidence hashes and refreshes the current OpenCode environment;
packaging also checks the archive's execution files against the verified candidate.
A changed skill, runner or environment requires new applicable evidence. Old runs
without the new evidence contract remain NOT_RUN. Static validation can be used
offline, but cannot certify OpenCode compatibility. Quality and real human feedback
remain separately reported; a two-contract PASS does not fabricate either.
