# Evaluation, human review and iterative improvement

Read this guide before defining tests, running comparisons or revising a skill.
It preserves the methodological guidance of upstream skill-creator while using
OpenCode API execution. Commands and JSON formats are in SKILL.md and schemas.md.

## Choose evaluation for the task

Discuss what success means, then choose a suitable combination:

| Task | Useful evidence | Avoid |
|---|---|---|
| File conversion / extraction | Schema, values, row counts, preserved data, actual file opening | Merely checking that a file exists |
| Code / exact workflow | Meaningful tests, tool results, behavior and failure handling | Scoring keyword presence as correctness |
| Research / analysis | Source grounding, coverage, uncertainty, decision usefulness | Rewarding confident wording without evidence |
| Writing / art / tone | Human preference, blind comparison, concrete audience/style constraints | Inventing a universal objective score for taste |

Automated assertions are helpful when outputs can be checked reliably. Subjective
work may need only human review, or a small set of factual constraints plus review.
Do not force every task into the same rubric. With an empty rubric, automated pass
rate is unknown, not zero or a perfect score. Record what was and was not assessed.
If the user declines automated tests, continue with the agreed manual process and
make the limitation explicit instead of fabricating a benchmark.

## Define realistic cases and a rubric

Start with two or three realistic tasks that capture the user's actual work, then
expand to important edge cases and regressions. Show the proposed cases to the user
when their interpretation matters. Include example inputs and expected properties;
do not bake the answer into the executor prompt. Use meaningful variations rather
than changing only names in one template.

```json
{"evals":[{"id":"invoice","prompt":"Normalize the attached purchase record into JSON.",
"files":["evals/files/invoice.txt"],"expected_output":"A record with correct amount and currency",
"expectations":["The total matches the source","The output is valid JSON"]}]}
```

Use stable IDs, even for identical texts. Expectations should be discriminating:
“contains a heading” is weak if the task is to explain a financial discrepancy.
For subjective criteria, say what evidence a judge should use. Prefer deterministic
checks for exact properties and a separate judge for substantive interpretation.
Freeze the rubric before comparison. If early exploration reveals a weak test,
revise the dataset version and rerun both sides; do not rescore only the favored side.

## Paired runs and fair comparison

For a new skill compare with-skill against without-skill. For a revision save an
immutable previous version and compare the new version against it, optionally also
against no skill. Do not compare against a moving source directory.

Keep prompt, copied fixtures, executor model/variant, agent, limits and tool profile
equivalent. The only intentional difference is the candidate and its loading access.
Use fresh sessions, independent locations and fixed snapshots. Launch independent
runs with bounded parallelism to reduce elapsed time, without sharing mutable files.
The Python quality runner implements this; available subagents are not a prerequisite.

Choose a profile matching required capabilities. A denied shell command is an
environment limitation, not evidence that a coding skill cannot solve the task.
Default bounded-files is appropriate for file/text work; custom profiles must be
recorded and are not OS sandboxes. Preserve successful runs and failures alike.

## Measure and grade actual execution

Inspect actual outputs, not the executor's claim that it succeeded. Save real
timing and usage immediately through the runner. Unknown telemetry remains null;
characters are not tokens. Wall time includes orchestration and must not be labeled
pure model latency. Separate executor, maintenance, judge and optimizer expenses.

Use grader.md to evaluate expectations and critique the tests. Weak assertions can
create false confidence even at 100% pass rate. Check whether a weak unrelated
answer would pass and whether a good alternative answer would be rejected. Ground
verdicts in files, values and transcript evidence. A judge failure is not a failed
expectation. Critique is a proposal for the next rubric, not a retroactive verdict.

Give judges the verified platform/version contract when validity depends on it.
Model memory can confuse platform recommendations with runtime requirements. In
the OpenCode V2 restoration test, a judge incorrectly rejected a display name with
spaces/capitals using older naming rules, despite V2 deriving the ID from the path.
Check such claims against primary documentation and actual loading evidence.
Preserve the original verdict; correct the judge context and rerun comparable
cases as a new experiment. Do not silently rewrite scores or lower thresholds.

That historical restoration judgment concerned the permissive OpenCode runtime
contract only. The current release workflow additionally requires strict Agent
Skills conformance: a standard name matching the directory, description at most
1024 characters, and string-to-string metadata. Identify which claim an expectation
assesses. A runtime-valid artifact may fail the standard; quality scores establish
neither contract. Keep historical verdicts unchanged and record both current
statuses with their own evidence (PASS / FAIL / NOT_RUN / ERROR).

Inspect both aggregate results and per-run evidence. Look for assertions every
configuration passes, noisy criteria, unusually expensive runs, and apparent wins
that coincide with more failures or changed environments. Show variance and sample
counts; a small toy test establishes plumbing, not general effectiveness.

## Human review

Generate the main viewer to show prompts, actual artifacts, grading, critique and
benchmark. Use the available browser to open the printed loopback URL; otherwise
produce a static HTML report. Explain how to move between runs and save/download
feedback. Import the file the user explicitly selects, not a guessed download path.

Ask the user to judge usefulness, correctness and fit to their workflow, including
things automated checks missed. Free-text feedback is important; do not reduce it
to a score prematurely. Bind comments to the exact run and snapshot. An empty
comment is only meaningful when the user actually reviewed the output; missing
feedback is not approval. Do not invent a user's sign-off or treat automated UI
test comments as human review. Stop only the viewer process you own when finished.

## Improvement: generalize, inspect, explain and reuse

1. **Generalize from feedback.** A skill should work across many future requests,
   not merely the few examples the user has reviewed repeatedly. Identify the
   underlying need. If a report omitted caveats, teach how to determine relevant
   limitations rather than inserting a sentence tied to that one report.
2. **Read transcripts, not just final outputs.** Find wasted steps, repeated tool
   failures and misunderstood instructions. Remove an unproductive action when
   evidence supports doing so. Do not delete useful guidance to meet a token quota.
3. **Explain why.** Convey the user's purpose. Prefer a reasoned instruction that
   transfers to new inputs over layers of uppercase prohibitions. If a recurring
   failure persists, try another representation, metaphor or workflow and test it.
4. **Find repeated work across cases.** If every run writes a similar
   `create_docx.py` or `build_chart.py`, implement and verify the reusable helper once
   in `scripts/`. Tell the skill when and how to use it, including inputs, outputs
   and dependencies. Do not bundle scripts merely because code looks sophisticated.

Take time to understand terse feedback; do not confuse the user's frustration with
a request for brittle rules. Draft a revision, reconsider it with a fresh reading,
check the whole workflow for contradictions, and explain the intended improvement.

## Iteration loop and stopping

Save the prior candidate, change the general instructions/resources, then run a new
iteration with the same relevant cases and new cases for the newly understood risk.
Keep comparable rubrics fixed; if the rubric changes, rerun comparable candidates.
Open the new viewer with prior feedback for context, collect real comments, and
repeat. Preserve the history of changed hypotheses, not just the final winning file.

Stop when the agreed outcomes are met, the user is satisfied, or further iterations
are no longer productive under the agreed budget. Report unresolved problems. Do
not silently stop after one example or continue indefinitely just to chase noise.

## Blind comparison

For subjective changes, compare randomized A/B outputs with identities hidden.
The comparator judges actual artifacts against the task and explains its verdict.
After fixing that verdict, reveal which version produced which output to an
analyzer, along with instructions and transcripts, to understand why. Blind labels
reduce one bias; they do not make the judge infallible or replace human preference.

## Natural triggering and realistic queries

Measure natural selection separately from forced loading. A quality run's native
attachment says nothing about whether the agent would choose the skill unaided.
Build an initial diverse set (around twenty realistic cases is often useful), with
both should-trigger and should-not-trigger requests and genuinely tricky near misses.

Prompts should sound like real users: context, filenames, informal wording, typos,
implicit goals and a range of detail. For a spreadsheet-analysis skill, a positive
could ask “The quarterly sales workbook is attached; compare regions and explain
the decline.” A negative might ask “Draft an email asking my colleague for the sales
spreadsheet” or “Explain what a pivot table is.” Shared keywords alone should not
force selection. An unrelated Fibonacci request is too easy as the only negative.

Use clear labels and group paraphrases together before splitting. Review proposed
queries with the user in the trigger-set editor, allow edits, and import the actual
export. Bad cases optimize a description for the wrong problem. Do not discard a
real simple user request solely because a particular model did not load the skill;
decide whether the skill would genuinely help and record the intended boundary.

The model sees the advertised ID/name/description and decides whether to load the
skill. The body is consulted after loading. Some models underinvoke skills on tasks
they believe they can do directly, but that is a hypothesis to measure for the actual
model, not a universal law inherited from Claude. Successful loading does not prove
the model followed the instructions; that is what quality evaluation checks.

## Description optimization and promotion

Keep train, validation and final test separate. Generate improvements using training
results and previous attempts; select candidates on validation. Evaluate the chosen
candidate once on the reserved final test, after selection. Never feed final-test
failures back into the same optimization and still call it an untouched final test.
If another tuning round is needed, reserve a new independent test set.

Inspect per-query results, repetitions, false positives/negatives and technical
errors in the optimization report. Do not reward errors as correct non-triggering.
Review the chosen description for invented capabilities and overspecific examples,
then run quality regression against the prior skill. Install the selected snapshot
only after review; a validation tie is not evidence of improvement.

Small datasets may not support an independent final test; say so. The current split
uses one group per class for validation/test and may be too small for strong claims.
More repetitions do not replace diversity of independent cases.

## Capability-aware workflow and delivery

When an environment lacks subagents, a browser, file execution or model access,
adapt the mechanics without pretending unavailable checks occurred. Separate model
sessions can replace subagents; static outputs can replace a live viewer. A manual
draft → task → review → revision loop remains useful when automation is unavailable,
but should not be labeled a controlled benchmark. For file-oriented environments,
show the actual deliverables, not only prose about them.

Deliver the skill directory, required resources/dependencies, usage examples,
benchmark and triggering evidence, human review status, and limitations. Preserve
LICENSE and provenance. An optional `.skill` ZIP is a distribution archive: install
its directory under `.opencode/skills/`, not through a nonexistent native importer.
Keep experiment outputs out of the package. Validate the package from a clean
directory, and make sure all referenced files needed by future users are included.
