# Hermes runner: file A/B on macOS

This guide covers SkillCraft's **local Hermes Chat CLI** runner for paired
file-quality evaluation with deterministic grading. SkillCraft explicitly
preloads the corresponding snapshot in each skill-enabled arm. For shared
settings and supported commands, see the
[configuration reference](configuration.md#runner-settings).

## Install and sign in

Follow the [Hermes quickstart](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart)
and [SkillCraft installation instructions](https://github.com/pnpwin/skillcraft#install).
Check that the installed Hermes CLI provides the native `chat` subcommand and
stream JSON interface:

```sh
hermes --version
hermes chat --help
hermes profile list
hermes auth list openai-codex
```

The runner requires `hermes` on `PATH`, or an executable selected with
`SKILLCRAFT_HERMES_BIN`. `hermes --version` must report an identifiable Hermes
Agent version. The runner invokes `hermes chat --query-file ... --format
stream-json`; it also uses `--in`, `--ignore-rules`, `--provider`, `--model`,
`--toolsets`, and, for the candidate arm, `--skills`. These flags and the JSONL
event stream are documented in the official [CLI reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md#hermes-chat).

Sign in to the profile that will supply the model:

```sh
hermes auth add openai-codex
hermes auth list openai-codex
hermes model
```

For a named profile, first get its directory with `hermes profile show NAME`,
then set both `HERMES_HOME` and `SKILLCRAFT_HERMES_HOME` to that directory in
the shell where you sign in and run SkillCraft. The former selects the profile
for Hermes commands; the latter selects the source profile read by SkillCraft.

Hermes stores its own credentials in the selected profile. A Codex CLI login is
not automatically the same thing as a Hermes sign-in; use Hermes's documented
OAuth flow or select a profile that already has its own usable sign-in. See the
official [provider setup](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/integrations/providers.md)
and [profiles FAQ](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/faq.md#profiles).

## Profile and model resolution

The source profile is selected in this order:

1. `SKILLCRAFT_HERMES_HOME`, if set;
2. `HERMES_HOME`, if set;
3. `~/.hermes`.

Set `SKILLCRAFT_HERMES_HOME` to the actual profile directory when a named Hermes
profile is active but its path is not available through `HERMES_HOME`. Hermes
named profiles normally live below `~/.hermes/profiles/`; use `hermes profile
show NAME` to inspect a profile. Do not pass the source profile to the worker as
its writable home: each run gets a new temporary profile.

For Learning's active-profile skill discovery and the separate
`hermes_skill_home` setting, see the [Learning guide](learning.md#home-configuration).

For ChatGPT/Codex subscription traffic, select a Hermes model exposed by the
`openai-codex` provider and pass `openai-codex:MODEL_ID`. The separator for
Hermes model references is a colon (`provider:model`), unlike Codex's
`model[#effort]` or OpenCode's `provider/model[#variant]`. SkillCraft reads the
source profile's `config.yaml` for `agent.reasoning_effort` and
`agent.service_tier`, and reads only the `openai-codex` entry from its
`auth.json` provider and credential-pool maps. It copies those credentials into
the disposable run profile, where Hermes can refresh them. The source auth file
is not used as the CLI's writable home.

The only other supported provider is `custom` configured with a loopback
OpenAI-compatible endpoint at `http://127.0.0.1:PORT/v1` (optional trailing
slash). The runner supplies a placeholder key for that loopback endpoint. It
does not support arbitrary remote custom endpoints or other Hermes providers.
Provider and model availability can change; confirm them in the selected
profile before starting the experiment.

## What each arm runs

Each case, arm and repeat gets a new mode-0700 temporary execution directory
and a new mode-0700 Hermes profile. The runner creates a clean input/output
workspace, copies input fixtures, and installs a read-only snapshot of the
candidate under `.agents/skills/<skill-id>` only for `with_skill`.

The baseline runs without a candidate in Hermes's skill catalog. The candidate
arm uses Hermes's native `--skills <skill-id>` attachment. Before the model
request, a disposable plugin checks the exact native catalog and resolved skill
path, then hashes the complete preloaded instruction block observed in the
provider request. It also records the provider/model, native request and
response IDs, and available tool definitions. A missing or unexpected preload,
changed snapshot, or mismatched request identity fails the run; a success in
the tool output alone is not proof that the skill was attached.

The runner disables automatic skill loading, memory and user-profile memory,
lazy installs, and tool search. It applies its own run budget, 16 Hermes agent
turns, and a terminal limit of at most 60 seconds per command. This is a
controlled evaluation profile rather than an emulation of the user's
interactive Hermes configuration.

## macOS file and terminal boundary

Hermes file A/B is supported only on macOS with `/usr/bin/sandbox-exec`. Before
each run, the runner exercises the same Seatbelt policy it will use for model
tool calls. It requires successful fixture reads and output writes, and requires
denials for input writes, a host-file canary read, and a loopback network
connection. If these checks fail, execution stops before the evaluated turn.

The model receives Hermes's file and terminal tools. File access is bounded to
the run inputs, outputs, scratch area and the candidate skill resources.
Terminal commands execute inside the macOS Seatbelt profile: commands can read
inputs and skill resources and write outputs/scratch; they cannot write inputs,
read the tested outside canary, or use the network. Background terminal modes
and Hermes process management are blocked. Command output is capped at 256 KiB;
the shell's file-size limit is 32 KiB and command timeouts are bounded. The
model provider request itself is made by Hermes outside the terminal sandbox,
so model traffic still needs the configured provider connection.

This boundary depends on the host macOS Seatbelt implementation and installed
Hermes CLI. The per-run checks are evidence for the tested capabilities, not a
claim that arbitrary macOS system behavior is simulated. The runner refuses
non-macOS systems and does not accept a custom permission profile. Hermes rules
are ignored for the evaluation command so the purpose-built OS profile, rather
than a user's path rules, defines command access.

## Deterministic paired evaluation

Every Hermes expectation must be covered by deterministic file checks; Hermes
does not support judge-model grading, standalone grading, blind comparison, or
custom permission profiles. Use checks that validate
actual files under `outputs/`, not just the response prose. Keep prompts and
checks identical for both arms. SkillCraft stages one immutable candidate and
fixture snapshot before dispatch.

Prompt A/B is also available through `run_quality_eval.py` with
`--instruction-a` and `--instruction-b`. It compares shared-system instruction
files without attaching a skill; it still requires deterministic file checks and
does not support judge arguments. See [prompt A/B evaluation](prompt-ab-evaluation.md).

```sh
fixture="$PWD/tests/fixtures/hermes-file-ab/meeting-actions"
workspace="$PWD/.agents/skillcraft-workspaces/hermes-file-ab-001"

"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/run_quality_eval.py" \
  --harness hermes --model openai-codex:MODEL_ID --skill "$fixture" \
  --evals "$fixture/evals/evals.json" --workspace "$workspace" \
  --repeats 3 --workers 1

"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/generate_review.py" \
  "$workspace" --static "$workspace/review.html"
```

Choose a new workspace path for every experiment; the command refuses to reuse
an existing one. For a revision comparison, add `--previous
/absolute/path/to/previous-skill`. Use `--pilot-case CASE_ID` to select a
representative case for the pilot when the first eval is not suitable. Do not
pass `--judge-model` or a profile file: Hermes rejects both for paired quality
runs.

Inspect the review, outputs, deterministic checks, and per-run evidence. A
completed CLI turn is not by itself a passing quality result. Each run's
`hermes-proof.json` records the observed catalog, native preload hash, request
and response IDs, sandbox check results, and output hashes; `environment.json`
records the observed Hermes version and model settings. The run also preserves
the native JSONL events and request receipts for diagnosis. The runner keeps
execution directories to support review and recovery; remove only experiment
directories you own after retaining required evidence.

## Natural skill selection

Use `run_eval.py --harness hermes` with positive and close negative requests.
The same isolated macOS profile advertises the candidate in the native skill
catalog. A verified native `skill_view` load counts as a trigger. Compare these
results separately from the file-quality benchmark.

## Failure interpretation

- **CLI unavailable or install lock denied access:** verify the executable,
  install ownership/permissions and `SKILLCRAFT_HERMES_BIN`. A CLI that cannot
  start or report its version is not a model-quality failure.
- **Missing ChatGPT sign-in:** confirm the selected source profile and run
  `hermes auth list openai-codex` for that profile. Re-authenticate through
  Hermes if needed; do not copy credentials manually.
- **Model rejected or rerouted:** check the exact provider/model ID in the
  selected profile. The runner verifies the requested provider/model identity
  against native request receipts, and records response model IDs when Hermes
  reports them.
- **Sandbox preflight failed:** treat the run as unavailable. Do not weaken or
  skip the capability checks to obtain a result.
- **Missing skill proof, incomplete stream, or output hash mismatch:** classify
  the run as technically incomplete and inspect preserved events and receipts.
  Do not include it as a quality verdict.
- **Timeout or provider error:** distinguish runner/CLI failure from a completed
  turn that failed a deterministic expectation. Only completed, verified runs
  have usable quality verdicts.

The runner does not change the user's Hermes profile, sign-in, configuration,
or installed plugins. It reads the source profile and builds a temporary
Hermes home for each isolated run. This also means changes made to the source
profile after a run do not retroactively change that run's recorded evidence.
