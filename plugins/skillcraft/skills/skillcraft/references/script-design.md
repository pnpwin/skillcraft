# Designing scripts for reusable skill workflows

Read this guide in full before adding or materially changing a skill's scripts.
These patterns apply when they serve the task. They are not a requirement that
every skill contain scripts, an OS sandbox, or a universal approval ceremony.

## Choose a stable interface

Use a script for recurring deterministic work that benefits from consistent parsing,
calculation or file operations. Keep uncertain domain judgments in explicit agent
instructions or user review rather than hiding them inside an arbitrary numeric
score. Explain what the script receives and produces and when to run it.

Prefer noninteractive commands with explicit flags and useful `--help`. A batch
process should not wait indefinitely for a terminal prompt that the agent cannot
answer. Fail clearly for missing required inputs; distinguish a documented default
from a guess about the user's intent. Resolve bundled resources relative to the
script/skill location, and accept input/output paths independently of the cwd.

For example, a transaction normalizer might accept `--input ledger.csv --output
normalized.json --currency-column currency`. It should describe the expected
columns and encoding. It should not silently assume USD merely because the caller
omitted a required currency column unless the agreed domain contract supplies that
default.

## Dependencies and tools

Declare runtime versions and actual dependencies in the skill's setup guidance,
with reproducible installation commands. Pin versions where behavior depends on
them. A frontmatter compatibility string summarizes requirements; it does not
install them. Declared allowed-tools is neither an availability check nor a grant
of permissions. Verify the actual client/profile can perform the required work.

Do not install unrelated dependencies to make a simple workflow look complete.
Prefer existing supported project tools when they meet the task. If the environment
differs from the tested one, identify what must be checked before claiming support.

## Outputs and failure contracts

Keep machine-readable results on stdout when automation consumes them. Put progress
and diagnostics on stderr, or provide explicit quiet/JSON modes. Avoid printing a
large raw document when a concise result and a saved artifact suffice; retain enough
evidence to diagnose failures without treating truncated output as complete data.

Use exit codes consistently and document their meaning. A validation failure and
an infrastructure error can both be nonzero but should remain distinguishable in
structured output. Include the field, rule and actionable context in diagnostics.
Do not print authentication material or arbitrary source values solely to explain
a parser error. Do not convert missing telemetry or skipped checks into success.

An ordinary negative domain result is not automatically a broken command. For
example, a successfully computed benchmark can contain failed expectations; its
CLI should reserve infrastructure failure for an incomplete or invalid computation
unless the command is explicitly an acceptance gate.

## Repeated runs and file integrity

Decide whether repeated invocation is idempotent, creates a new version, or refuses
an existing destination. State that contract in help. For experiments, a new
workspace preserves historical evidence. For a regeneratable cache, replacement
may be appropriate. Use atomic replacement when partial output would be mistaken
for a completed artifact. Keep the original input when the workflow requires it.

Validate before expensive execution: parse input, check prerequisites, then run the
operation. Validate the actual output and fix/recheck meaningful failures. For a
fragile transformation, a useful sequence is plan the mapping → validate a small
representative transformation → execute → reconcile totals and schema. Do not turn
every reversible text edit into a mandatory dry-run or approval process.

## Verification by behavior

Test important boundaries and failure handling: malformed input, realistic edge
cases, missing dependencies, rerun behavior and exit codes used by callers. Invoke
the real CLI when testing its contract. Do not settle for tests that merely repeat
implementation details or assert that a file exists when its contents matter.

Keep a short task-specific checklist when a multistep workflow can otherwise lose
an essential step. For a CSV conversion: inspect schema → map fields → transform →
reconcile row counts and values → open the result → report unresolved rows. A
creative writing task needs different evidence, often human review instead.
