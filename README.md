# SkillCraft for Codex and OpenCode

Create, evaluate, and refine agent skills through measured iterations.

SkillCraft combines an Agent Skills-compatible authoring workflow with a Python
evaluation toolkit with native Codex and OpenCode runners. It supports paired quality
evaluations, natural-trigger testing, training-only description optimization,
blind comparisons, and hash-bound acceptance and packaging.

The standalone skill ID is `skillcraft`. Both harnesses discover the same project
installation.

## Requirements

- Python **3.11+** and the dependencies in `requirements.txt`.
- For Codex, a CLI with the required App Server APIs, Git and an authorized model connection. See the
  [Codex runner guide](references/codex-runner.md).
- For OpenCode, a server with the required V2 APIs, a configured model, Node.js **22+** and the
  locked dependencies under `runner/`. The server and runner share absolute paths.
  See the [OpenCode runner guide](references/opencode-runner.md).

Live checks have been run with Codex 0.170.0 and OpenCode 2.2.7. SkillCraft records
the versions it observes and checks the runtime interfaces it uses.

## Install

From the project where you use Codex or OpenCode:

```sh
git clone https://github.com/pnpwin/skillcraft.git .agents/skills/skillcraft
export SKILLCRAFT="$PWD/.agents/skills/skillcraft"
python3 -m venv "$SKILLCRAFT/.venv"
"$SKILLCRAFT/.venv/bin/python" -m pip install -r "$SKILLCRAFT/requirements.txt"
npm --prefix "$SKILLCRAFT/runner" ci  # required for OpenCode
```

The installation directory must be named **`skillcraft`**, matching `name` in
`SKILL.md`. To install from the optional `.skill` ZIP, extract its
`skillcraft/` directory into `<project>/.agents/skills/` and install the dependencies.

Ask the agent to use **SkillCraft** for a recurring workflow:

> Use SkillCraft to turn our recurring meeting-note cleanup into a reusable skill.
> Preserve missing owners and deadlines rather than inventing them, and test the result.

The agent can also select SkillCraft naturally for relevant authoring, evaluation,
revision, or triggering requests. Instructions and full required guides start at
[SKILL.md](SKILL.md).

## Validate and evaluate

```sh
# Validate the Agent Skills format.
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/scripts/quick_validate.py" "$SKILLCRAFT" --json

# Development checks.
"$SKILLCRAFT/.venv/bin/python" -B -m unittest discover -s "$SKILLCRAFT/tests"
npm --prefix "$SKILLCRAFT/runner" run check
npm --prefix "$SKILLCRAFT/runner" test
```

Each experiment needs a new workspace outside the skill, for example
`.agents/skillcraft-workspaces/`. Commands detect the calling harness from the
shell session. Use `--harness codex` or `--harness opencode` in a plain terminal,
in CI, or when both session signals are present. Each experiment uses the selected
harness for execution, grading and optimization.

Run `compliance.py` on representative native-attachment evidence for each target.
To package SkillCraft, pass the Codex and OpenCode run directories with two
`--run` arguments to `package_skill.py`. Both runs must certify the same hashes.
The packager refreshes both environments before it writes the archive.

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

- `SKILL.md`, `references/`: authoring and evaluation methodology.
- `scripts/`: validation, evaluation, optimization, reporting, packaging and the Codex adapter.
- `runner/`: OpenCode V2 client integration.
- `agents/`: independent judge prompt resources.
- `eval-viewer/`, `assets/`: review interfaces and templates.
- `tests/`: regression tests and representative fixtures.

Custom OpenCode permissions profiles are recorded as **controlled:false**.
Small evaluation datasets give limited statistical evidence. Human approvals are
recorded separately from automated grades.

## License and provenance

See [LICENSE.txt](LICENSE.txt) for Apache-2.0. The authoring method incorporates
Anthropic's upstream skill-creator; its original identity, pinned source and
attribution are preserved in [UPSTREAM.md](UPSTREAM.md).
