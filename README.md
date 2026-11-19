# SkillCraft for Codex and OpenCode

Create, evaluate, and refine agent skills through measured iterations.

SkillCraft combines an Agent Skills-compatible authoring workflow with a Python
evaluation toolkit for Codex and OpenCode. Local Codex chats in the ChatGPT
desktop app use the same skill and evaluation scripts as Codex CLI. The toolkit
supports paired quality evaluations, natural-trigger testing, training-only
description optimization, blind comparisons, and hash-bound packaging.

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

Live checks have been run with Codex 0.170.0 and OpenCode 2.2.7. SkillCraft records
the versions it observes and checks the runtime interfaces it uses.

## Install

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
revision, or triggering requests. Instructions and full required guides start at
[SKILL.md](plugins/skillcraft/skills/skillcraft/SKILL.md).

## Validate and evaluate

```sh
# Validate the Agent Skills format.
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/quick_validate.py" "$SKILLCRAFT" --json

# Development checks from the repository root, with SKILLCRAFT set to the checkout's skill directory.
"$SKILLCRAFT_VENV/bin/python" -B -m unittest discover -s tests -t .
node --test tests/viewer.test.mjs
npm --prefix "$SKILLCRAFT/scripts/opencode-runner" run check
"$SKILLCRAFT/scripts/opencode-runner/node_modules/.bin/tsx" --test tests/opencode/*.test.mts
```

Each experiment needs a new workspace outside the skill, for example
`.agents/skillcraft-workspaces/`. Commands detect the calling harness from the
shell session, including local Codex chats in ChatGPT desktop. Use `--harness codex`
or `--harness opencode` in a plain terminal, in CI, or when both session signals
are present. `--harness chatgpt` explicitly selects the same Codex runner. Each
experiment uses the selected harness for execution, grading and optimization.

For controlled OpenCode evaluations, point `SKILLCRAFT_SERVER` at a separate
server with a clean config and a configured model. The interactive Git plugin
registers SkillCraft in its locations, including locations that would otherwise
be a clean baseline. Codex evaluations start their own isolated App Server.

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

- `.agents/plugins/marketplace.json` registers the Codex marketplace.
- `plugins/skillcraft/` contains the Codex manifest and the OpenCode plugin entry point.
- `plugins/skillcraft/skills/skillcraft/` contains the shared skill, A/B scripts,
  runners, judge prompts and review templates.
- `tests/`: regression tests and representative fixtures.

Custom OpenCode permissions profiles are recorded as **controlled:false**.
Small evaluation datasets give limited statistical evidence. Human approvals are
recorded separately from automated grades.

## License and provenance

See [LICENSE.txt](plugins/skillcraft/skills/skillcraft/LICENSE.txt) for Apache-2.0. The authoring method incorporates
Anthropic's upstream skill-creator; its original identity, pinned source and
attribution are preserved in [UPSTREAM.md](plugins/skillcraft/skills/skillcraft/UPSTREAM.md).
