---
name: skillcraft
description: Create or revise agent skills and reusable prompts, compare versions with A/B tests, diagnose skill selection, and review evaluation evidence for Codex, OpenCode v2, and Hermes Agent. Use when turning a recurring workflow into a skill, improving instructions, or checking a candidate against a baseline.
license: Apache-2.0
compatibility: Evaluations require Python 3.11+, a configured model, and the Codex App Server or OpenCode V2 APIs used by SkillCraft. The OpenCode runner also requires Node.js 22+ and shared absolute filesystem paths. Hermes file A/B requires the local Hermes Chat CLI on macOS and sandbox-exec.
---

# SkillCraft

Start where the user is in the workflow. Identify the intended result, representative
inputs and how to recognize success. Reuse what the conversation or repository
already establishes. Ask only about gaps that affect the result.

| User goal | Start here |
| --- | --- |
| Create a skill, including from this conversation | Read the sections of the [authoring guide](references/authoring-guide.md) that apply to the request. Draft and validate the skill. |
| Create a reusable prompt | Follow the prompt workflow below. |
| Evaluate an artifact, check a candidate now, or review a report | Read [evaluation and iteration](references/evaluation-and-iteration.md) for the selected evaluation, then follow the [evaluation commands](references/run-evaluations.md). |
| Improve an existing skill | Reproduce the problem before proposing a small change. Compare the candidate with the current version and protect successful cases. |
| Review completed turns for durable corrections or rules | Use opt-in scheduled reflection for the enrolled project or home scope, or review selected turns in the current chat on request. Route evidence to a proven skill, a matching skill in scope, or a new skill when its name is available. |

## Create

When starting from a conversation, use the relevant turns to identify the recurring
task, durable rules, and confirmed examples. Show which evidence supports each
rule and note where an expected result still needs verification. Keep transient
errors, private details, and credentials out of the skill. For a revision, use the
current package and preserve behavior supported by earlier checks.

For a skill, match the folder and frontmatter name and write a description that
helps the harness select it for realistic requests. Keep workflow and essential
constraints in `SKILL.md`. Put substantial procedures or examples in references
and link them where used. Read the [Agent Skills specification](references/agent-skills-specification.md)
before changing frontmatter and the [script guide](references/script-design.md)
before adding executable resources. Keep licensing and attribution intact.

For a prompt, define the intended output and how to handle incomplete or ambiguous
inputs. Deliver an editable instruction file, representative inputs and a rubric
with observable expectations. Use the user's variable names and record where the
prompt is placed in a model request. For an example-based check, compare the
prompt's requested result with the rubric on at least one normal case and one
relevant edge case. Use the same role, fixture and tool settings when comparing
versions.

Validate a skill with `scripts/quick_validate.py CANDIDATE --lint` using the
SkillCraft interpreter. If the user asks for a draft, deliver a draft and identify
the checks still pending.

## Evaluate

Freeze the rubric, model settings, inputs and snapshots before running cases.
Measure skill quality after explicit loading and measure natural selection
separately on positive and close negative requests. Include a clean no-skill
baseline and, for a revision, the previous version. Inspect artifacts, grading
evidence, incomplete pairs and model usage before interpreting scores.

For two versions of an instruction file, use [prompt A/B](references/prompt-ab-evaluation.md).

Choose the harness and profile using the [configuration reference](references/configuration.md).
The scripts detect unambiguous native sessions. Pass `--harness codex`,
`--harness opencode` or `--harness hermes` from a plain terminal. The reference
lists capabilities by harness, including deterministic file checks for Hermes on
macOS. Install runner dependencies for evaluations and open a new workspace for
each experiment. Resolve `scripts/`, `references/` and `assets/` from this
file's directory. See the [workflow diagrams](references/workflows.md) and
[data formats](references/schemas.md) for report and fixture details.

## Improve

Use failed checks and user feedback to identify the reusable correction. Preserve
the preceding version, useful resources and behavior that passed. When selection
is the issue, revise the description and retest triggering and quality separately.
When the output is the issue, revise the instructions or resources. Consider
splitting only when the workflows have distinct requests and deliverables. Check
the new routing on positives and near misses and evaluate the cost of the split.

Show the diff, representative results and remaining limitations. To save a draft,
check it now, apply it, inspect scheduled work, or roll it back, follow
[session candidates](references/learning.md#session-candidates). Run A/B on request
in the manual workflow. A verification request ends with the report. Use the
separate apply action when the user requests publication. Read
[scheduled Learning](references/learning.md#scheduled-learning) before enabling
automatic creation, updates, or session reflection.
