# Oversized descriptions: preserved source and blind comparison

Use this workflow only when the parsed description exceeds 1024 Unicode characters.
At exactly 1024, no length-driven rewrite is needed. Resolve any other standard
violation first: the observation-only historical baseline exception is deliberately
limited to an otherwise valid skill whose sole violation is oversized description.

## Inputs and preconditions

- A complete skill directory whose identity matches its directory.
- Realistic, labeled trigger cases with independent positive/negative groups for
  train, validation and final test. Include near misses, not only easy negatives.
- Quality evals with meaningful expectations/checks. A dedicated judge supplies
  uncovered grades and critiques the rubric separately.
- Available executor, optimizer and judge models; profiles matching the task.
- A new workspace outside the skill. The source remains installed and unchanged.

The executor receives only its own variant's advertised description; it cannot be
blind to that description and still perform a natural-selection test. The separate
comparison judge receives randomized A/B descriptions and unchanged operating
instructions, with no historical/candidate labels or filesystem identities. The
private map stays outside the judge's permitted input files. Its verdict is saved
before unblinding. This reduces identity bias; length differences remain visible.

## Execute a fixed-budget comparison

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/shrink_description.py" \
  --skill "$CANDIDATE" --workspace "$NEW_WORKSPACE" \
  --evals "$TRIGGER_EVALS" --quality-evals "$QUALITY_EVALS" \
  --model "$EXECUTOR_MODEL" --optimizer-model "$OPTIMIZER_MODEL" \
  --judge-model "$JUDGE_MODEL" --repeats 2 --workers 2
```

Use `--profile`, `--optimizer-profile`, and `--judge-profile` when needed. Each custom
profile remains uncontrolled. The workflow freezes datasets, fixture bytes, profiles,
models, repeats, seed, timeout, thresholds and a one-generation budget before any
model request. The source text and hashes are preserved; baseline SKILL.md remains
byte-identical, read-only, standard FAIL and ineligible for promotion/package.

Only historical training evidence reaches the optimizer. It reformulates the text;
mechanical truncation is not an acceptable substitute. The proposal must pass the
strict validator; the Markdown body, other parsed frontmatter values/types and
supporting resources must remain unchanged. Invalid proposals are not candidates.

Both variants receive the same validation cases in fresh sessions with the same
model/profile/budget. Selection requires no validation errors, no loss of balanced
accuracy, and the blind judge finding no substantive lost scope or invented capability
in the candidate. Only after selection are the reserved final tests evaluated.
Any subsequent optimization requires a new independent final test.

Quality regression then measures the selected skill, the historical baseline and
no skill on identical tasks. Native attachment establishes execution after loading,
not natural selection. Candidate expectations must pass, execution/critique errors
must be absent, and compatibility requires two independently verified PASS results
plus a current-environment refresh. Baseline standard FAIL remains visible even
when its runtime execution succeeds; the ordinary release/package gate has no
baseline exception.

## Review and interpretation

Inspect `preregistered.json`, `original-description.json`, `selection.json`,
`semantic-review/verdict.json`, the later `semantic-review/unblinded.json`, all
trigger-results.json files, quality/benchmark.json, acceptance/compliance.json,
description.diff and result.json. Per-run directories retain prompts, transcripts,
responses, tool evidence, usage and timing. Completion failures remain errors,
not correct negative triggers. Report TP/TN/FP/FN, completion, actual available
resource metrics and environment limitations for both variants.

`ready_for_review` is a measured readiness signal, never human approval or permission
to overwrite the installed skill. Show the diff and results before applying the
candidate, and wait for the applicable human decision. A small dataset can verify
the workflow and reveal regressions; it cannot prove general statistical equivalence.
If the comparison is rejected, inconclusive or technically incomplete, report that
result faithfully and preserve its evidence rather than declaring quality preserved.
