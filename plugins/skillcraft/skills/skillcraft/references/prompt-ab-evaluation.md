# Comparing instruction prompts

Use prompt A/B when the question is whether two instruction files change task
quality or executor cost. It runs exactly two arms against the same frozen
evaluation rows, fixture snapshots, model, harness, profile, repeat count and
grader. It does not install a skill or measure natural skill triggering. Use the
normal skill evaluation separately for native `without_skill`/`with_skill`
comparisons and trigger diagnostics.

## Run a prompt comparison

From your project, provide two different, nonempty UTF-8 files and a new
workspace. Use the installed skill directory and evaluation environment from the
[configuration reference](configuration.md).

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/run_quality_eval.py" \
  --instruction-a evals/instructions/current.md \
  --instruction-b evals/instructions/revised.md \
  --evals evals/tasks.json \
  --workspace evals/results/prompt-ab-001 \
  --model provider/model \
  --harness opencode \
  --repeats 3
```

The two files are appended at the same point: after SkillCraft's shared system
instructions and tool-profile instructions, before the task prompt. This placement
is used by Codex developer instructions, the OpenCode agent system prompt and
Hermes instructions. Both arms use the same placement. The runner copies both
files into `snapshots/instructions/` and records their SHA-256 hashes and placement
in `manifest.json`; each run records the hash of its injected instruction text in
`request.json` (Codex and Hermes also capture the effective instruction text there).
Relative `files` paths in the eval rows resolve from the evals JSON file's directory;
input snapshots are then shared by both arms.

Prompt A/B mode does not accept `--skill` or `--previous`. Legacy invocation with
`--skill` continues to compare native no-skill and with-skill runs. It also remains
the path for previous-skill comparisons and natural-trigger evaluation.

## Read the result

The usual `pilot/` gate runs each arm once before repeated runs. Pilot measurements
are preparation costs and do not enter the comparison. In `benchmark.json`, inspect
`run_summary.prompt_a` and `run_summary.prompt_b` for per-arm quality, time and
token summaries, and `deltas.prompt_b_minus_a` for matched differences (B minus A).
Quality is paired only when both runs and grading pipelines completed and the
grading contracts are known and match. Executor task FAIL verdicts still count when
grading completed. Incomplete or missing sides appear under
`incomplete_pairs` with reasons and remain in the planned-run denominator; they do
not contribute to paired quality, time or token deltas. Unknown telemetry remains
null. Time is runner wall time, not pure model latency.

Use a frozen task set with an independently grounded rubric. Compare enough fresh
repeats to see variation, inspect individual run artifacts, and keep any holdout
cases outside prompt editing and selection. A prompt comparison measures these two
files on this task set and runtime; it does not establish broader skill quality or
natural loading behavior.
