# SkillCraft for Codex, OpenCode v2 and Hermes Agent

Create, evaluate, and refine agent skills and prompts through measured iterations.

SkillCraft combines an Agent Skills-compatible authoring workflow with a Python
evaluation toolkit for Codex and OpenCode and file-based A/B for Hermes on
macOS. Local Codex chats in the ChatGPT desktop app use the same skill and
evaluation scripts as Codex CLI. See the
[configuration reference](plugins/skillcraft/skills/skillcraft/references/configuration.md#harness-capabilities)
for supported combinations of paired evaluations, trigger testing, description optimization,
blind comparisons, and hash-bound packaging.

The standalone skill ID is `skillcraft`. See the
[Codex runner guide](plugins/skillcraft/skills/skillcraft/references/codex-runner.md#local-codex-in-the-chatgpt-desktop-app)
for setup and verification in a local ChatGPT desktop chat.

## Requirements

- Python **3.11+** and the dependencies in the skill's `requirements.txt`.
- For Codex, a CLI with the required App Server APIs, Git and an authorized model connection. See the
  [Codex runner guide](plugins/skillcraft/skills/skillcraft/references/codex-runner.md).
- For OpenCode, a server with the required V2 APIs, a configured model, Node.js **22+** and the
  locked dependencies under `scripts/opencode-runner/`. The server and runner share absolute paths.
  See the [OpenCode runner guide](plugins/skillcraft/skills/skillcraft/references/opencode-runner.md).
- For Hermes file A/B, a local signed-in Hermes Chat CLI, macOS, and
  `/usr/bin/sandbox-exec`. See the
  [Hermes runner guide](plugins/skillcraft/skills/skillcraft/references/hermes-runner.md).

Live checks have been run with Codex 0.170.0 and OpenCode 2.2.7. SkillCraft records
the versions it observes and checks the runtime interfaces it uses.

## Install and configure

The Git plugin contains the same `skillcraft` skill for all three harnesses.
Choose the installer for your harness. Authoring is available once the skill is
discovered. Set up a runner when you want to run an evaluation, and enroll your
project separately when you want Learning to collect feedback.

**Codex CLI or local Codex in ChatGPT desktop**

```sh
codex plugin marketplace add pnpwin/skillcraft --ref main
codex plugin add skillcraft@pnpwin
```

**OpenCode V2**

```sh
opencode plugin add 'github:pnpwin/skillcraft#main::path:plugins/skillcraft'
```

Use a release tag or commit for a reproducible installation. The OpenCode
package registers the same skill directory used by the Codex plugin.
The shared package declares Codex hooks in `.codex-plugin/plugin.json` and
the OpenCode entry point in `package.json`.

**Hermes Agent**

```sh
hermes plugins install 'pnpwin/skillcraft#plugins/skillcraft'
hermes plugins enable skillcraft
```

Open the shared skill with `skill_view("skillcraft:skillcraft")`. Hermes supports
skill authoring and an [opt-in learning journal](plugins/skillcraft/skills/skillcraft/references/learning.md#local-hermes-chats).
For file-based quality A/B on macOS, install and sign in to the native Hermes
Chat CLI, then follow the [Hermes runner guide](plugins/skillcraft/skills/skillcraft/references/hermes-runner.md).

**Verify discovery**

Open a fresh local session. In Codex, check that `skillcraft@pnpwin` is enabled
and select `skillcraft:skillcraft` from the skill picker. In OpenCode, ask the agent to
load `skillcraft` with the `skill` tool. In Hermes, use the registered name
`skillcraft:skillcraft`. A successful check resolves the installed `SKILL.md`
and can open its linked `references/authoring-guide.md`. This check needs no
evaluation workspace or Learning enrollment.

**Standalone installation**

Copy the `plugins/skillcraft/skills/skillcraft/` directory from a checkout into
`<project>/.agents/skills/skillcraft/`. An exported `.skill` archive contains
the same standalone directory.

**Evaluation dependencies**

For a standalone installation, run `export SKILLCRAFT="$PWD/.agents/skills/skillcraft"`
from the project root. For a Git plugin installation, set `SKILLCRAFT` to the
absolute directory containing `SKILL.md` in the runtime's skill registry.
Keep `SKILLCRAFT_VENV` at an absolute, writable path outside the skill directory
so plugin updates can replace the package without removing Python dependencies.
The setup below can also recreate a previous package-local virtualenv at the new path.

```sh
export SKILLCRAFT_VENV="${SKILLCRAFT_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/skillcraft/venv}"
python3 -m venv "$SKILLCRAFT_VENV"
"$SKILLCRAFT_VENV/bin/python" -m pip install -r "$SKILLCRAFT/requirements.txt"
npm --prefix "$SKILLCRAFT/scripts/opencode-runner" ci  # required for OpenCode
```

The skill directory is named `skillcraft`, matching its frontmatter name.

Ask the agent to use **SkillCraft** for a recurring workflow:

> Use SkillCraft to turn our recurring meeting-note cleanup into a reusable skill.
> Preserve missing owners and deadlines rather than inventing them, and test the result.

The agent can also select SkillCraft naturally for relevant authoring, evaluation,
revision, or triggering requests. Instructions and guides for each workflow start at
[SKILL.md](plugins/skillcraft/skills/skillcraft/SKILL.md).

For a compact index of learning configuration, runner settings, environment
variables and evaluation/optimization CLI options, see the
[configuration reference](plugins/skillcraft/skills/skillcraft/references/configuration.md).
The [workflow diagrams](plugins/skillcraft/skills/skillcraft/references/workflows.md)
show how these options affect quality A/B, description optimization, and Learning.

**Choose your setup**

| Use | What to configure |
| --- | --- |
| Author a skill or prompt | Invoke `skillcraft` with the intended outcome and examples. |
| Run A/B on demand | Install evaluation dependencies, sign in to the selected runner, and choose the model, cases, and repeat count. See [runner settings](plugins/skillcraft/skills/skillcraft/references/configuration.md#runner-settings). |
| Collect feedback | Create `.skillcraft/learning.json` using the example below and enroll the project. For Codex sessions, review and trust the installed [Learning hooks](plugins/skillcraft/skills/skillcraft/references/learning.md). |
| Create or improve skills in batches | Add the optional [`automation` settings](plugins/skillcraft/skills/skillcraft/references/configuration.md#scheduled-automation) and re-enroll the project or home scope. |
| Revise a skill at Learning checkpoints | Without an `automation` block, configure a model and separate training and holdout cases for that skill. See [per-skill settings](plugins/skillcraft/skills/skillcraft/references/configuration.md#per-skill-settings). |

This example enrolls a local skill for feedback collection. Replace `example-skill`
with the directory name of a skill you want to track.

```json
{
  "schema_version": 1,
  "enabled": true,
  "sources": ["sessions", "evals"],
  "skills": {
    "example-skill": {
      "source": ".agents/skills/example-skill",
      "auto_apply": false
    }
  }
}
```

From the project root, approve the configuration with the SkillCraft virtualenv.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" enable "$PWD"
```

For Hermes capture, also enroll the selected Hermes profile with
`hermes skillcraft enable "$PWD"`. See the [Learning guide](plugins/skillcraft/skills/skillcraft/references/learning.md)
for approval, status, and optional Hermes feedback forwarding.

## Workflow

In a chat, ask SkillCraft to create a skill from the relevant turns or improve an
existing skill from a correction. It prepares a candidate with examples and
checks. Ask to **check now** to run A/B for that candidate and get a report.
Use **apply** as a separate publishing action. See
[session candidates](plugins/skillcraft/skills/skillcraft/references/learning.md#session-candidates).

Learning collects feedback as turns finish in the enrolled project or home
scope. Use `selection: "all"` to discover eligible local skills without
listing each one. With scheduled Learning enabled, the first completed project
or home turn on or after the stored due time can wake the worker. The default
interval is seven days, with up to three candidates across that scope per
batch. An empty batch requires zero model calls.

One independently verifiable case can qualify a draft. Automatic publication
requires the relevant `auto_apply` setting, a passing A/B with independent
holdout coverage, and the required native checks. `learning.py status` shows
the schedule, queue and batch resource report.
[Configuration and examples](plugins/skillcraft/skills/skillcraft/references/learning.md#scheduled-learning)

```text
Install from Git → select a profile → open skillcraft
Manual: task → candidate → optional A/B → report → owner decision
Scheduled: completed turns and evaluated evidence → journal → due batch
           → eligible candidate → A/B and report → publication gates
```

## Validate and evaluate

For two instruction files, see [prompt A/B](plugins/skillcraft/skills/skillcraft/references/prompt-ab-evaluation.md).
For a native skill comparison, follow the [evaluation commands](plugins/skillcraft/skills/skillcraft/references/run-evaluations.md).

```sh
# Validate the Agent Skills format.
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/quick_validate.py" "$SKILLCRAFT" --json

# Development checks from the repository root, with SKILLCRAFT set to the checkout's skill directory.
"$SKILLCRAFT_VENV/bin/python" -B -m unittest discover -s tests -t .
node --test tests/viewer.test.mjs
node --test tests/opencode/*.test.mjs
npm --prefix "$SKILLCRAFT/scripts/opencode-runner" run check
"$SKILLCRAFT/scripts/opencode-runner/node_modules/.bin/tsx" --test tests/opencode/*.test.mts
```

Each experiment needs a new workspace outside the skill, for example
`.agents/skillcraft-workspaces/`. Commands detect Codex, OpenCode, or Hermes
from unambiguous native session signals. Use `--harness codex`,
`--harness opencode`, or `--harness hermes` in a plain terminal, in CI, or when
signals conflict. `--harness chatgpt` explicitly selects the same Codex runner. Each
experiment uses the selected harness for execution, grading and optimization.

For controlled OpenCode evaluations, point `SKILLCRAFT_SERVER` at a separate
server with a clean config and a configured model. The interactive Git plugin
registers SkillCraft in its locations, including locations that would otherwise
be a clean baseline. Codex evaluations start their own isolated App Server.

### File A/B in Hermes on macOS

The Hermes runner uses a private local gateway, a fresh profile for each arm,
and macOS `/usr/bin/sandbox-exec` for file and terminal tools. Use a local Hermes
installation with the native chat JSON stream and terminal-provider APIs. Set
`SKILLCRAFT_HERMES_HOME` to the signed-in Hermes profile if it differs from
`HERMES_HOME` or `~/.hermes`. `SKILLCRAFT_HERMES_BIN` can select another local CLI.

Run from a local terminal with a selected ChatGPT model and deterministic file
checks. Replace `MODEL` with a model available to the Hermes profile.

```sh
fixture="$PWD/tests/fixtures/hermes-file-ab/meeting-actions"
workspace=/path/to/new/hermes-ab-workspace
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/run_quality_eval.py" \
  --harness hermes --model openai-codex:MODEL --skill "$fixture" \
  --evals "$fixture/evals/evals.json" --workspace "$workspace"
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/generate_review.py" \
  "$workspace" --static "$workspace/review.html"
```

Each completed run records the observed catalog and a hash of the native
preload in `hermes-proof.json`. Commands can read the fixtures and candidate
snapshot and write inside `outputs/` and the tool scratch directory. The
gateway uses the selected profile's ChatGPT sign-in for model traffic. Check
the proof, artifacts and report before using the quality results.

### Package a skill

Run `compliance.py` on representative native-attachment evidence for each target.
To package SkillCraft, pass the Codex and OpenCode run directories with two
`--run` arguments to `package_skill.py`. Both runs must certify the same hashes.
The packager refreshes both environments before it writes the archive.
Use `--openai-plugin-version VERSION` to also wrap the same skill in an OpenAI
plugin ZIP for Codex.


For opt-in automatic revisions from local sessions and evaluated experience,
see the [learning workflow](plugins/skillcraft/skills/skillcraft/references/learning.md).

## Connection and migration

The OpenCode adapter discovers and authenticates with the local service by default.
For an explicit endpoint, use `SKILLCRAFT_SERVER`; if needed, provide a JSON object
of headers through `SKILLCRAFT_HEADERS` in the process environment. Never commit
credentials. `SKILLCRAFT_TEMP` overrides the temporary execution parent.

For installations migrating from `.opencode/skills/` or the former `skill-creator` ID:

1. Preserve old experiments and hashes as historical evidence.
2. Install under `.agents/skills/skillcraft/` and update active instructions and
   scripts to the new ID. Use `SKILLCRAFT*` variables in place of `SKILL_CREATOR*`.
3. Recreate the Python virtualenv at its new path; activation scripts and executable
   shebangs can contain the old absolute directory. Install Node dependencies too.
4. Move the old copy outside all discovered skill directories. OpenCode can prioritize
   `.opencode/skills/` over `.agents/skills/`. Review the resolved path in both registries.
5. Re-run validation and native execution under the new ID. Old compatibility
   evidence does not certify a renamed or modified artifact.

## Layout

See [AGENTS.md](AGENTS.md) for development, safety, and merge rules.

```text
.
├── AGENTS.md
├── README.md
├── .agents/plugins/marketplace.json     # Codex marketplace
├── .github/workflows/ci.yml
├── plugins/skillcraft/
│   ├── .codex-plugin/plugin.json        # Codex manifest
│   ├── hooks/
│   │   ├── hooks.json                   # Codex hook registration
│   │   └── learning_capture.py          # Codex hook entry point
│   ├── package.json                     # OpenCode package and exports
│   ├── opencode.js                      # OpenCode entry point
│   ├── plugin.yaml                      # Hermes manifest
│   ├── __init__.py                      # Hermes register(ctx)
│   ├── src/                             # Plugin-only helper modules
│   │   ├── __init__.py
│   │   ├── codex/
│   │   │   └── learning_capture.py
│   │   ├── hermes/
│   │   │   ├── __init__.py
│   │   │   ├── hermes_history.py
│   │   │   └── hermes_learning.py
│   │   └── opencode/
│   │       └── learning.js
│   └── skills/skillcraft/               # Standalone skill and resources
│       ├── SKILL.md
│       ├── assets/
│       ├── references/
│       └── scripts/                     # Shared Learning, A/B, and run adapters
└── tests/                                # Regression tests and fixtures
```

Custom OpenCode permissions profiles are recorded as **controlled:false**.
Small evaluation datasets give limited statistical evidence. Human approvals are
recorded separately from automated grades.

## License and provenance

See [LICENSE.txt](plugins/skillcraft/skills/skillcraft/LICENSE.txt) for Apache-2.0. The authoring method incorporates
Anthropic's upstream skill-creator; its original identity, pinned source and
attribution are preserved in [UPSTREAM.md](plugins/skillcraft/skills/skillcraft/UPSTREAM.md).
