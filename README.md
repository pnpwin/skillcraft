# SkillCraft for OpenCode V2

Create, evaluate, and refine agent skills through measured iterations.

SkillCraft combines an Agent Skills-compatible authoring workflow with a Python
evaluation toolkit and an OpenCode TypeScript runner. It supports paired quality
evaluations, natural-trigger testing, training-only description optimization,
blind comparisons, and hash-bound acceptance and packaging.

**Repository:** [pnpwin/opencode-skillcraft](https://github.com/pnpwin/opencode-skillcraft)
**Native skill ID:** `skillcraft`. This repository is a standalone skill, not an
OpenCode plugin or a published npm package.

## Requirements

- OpenCode server **2.2.6**, with an accessible configured model.
- Python **3.11+** and the dependencies in `requirements.txt`.
- Node.js **22+** and the locked dependencies under `runner/`.
- The runner and OpenCode server must share the same absolute filesystem paths.

## Install

From the project where you use OpenCode:

```sh
git clone https://github.com/pnpwin/opencode-skillcraft.git .opencode/skills/skillcraft
export SKILLCRAFT="$PWD/.opencode/skills/skillcraft"
python3 -m venv "$SKILLCRAFT/.venv"
"$SKILLCRAFT/.venv/bin/python" -m pip install -r "$SKILLCRAFT/requirements.txt"
npm --prefix "$SKILLCRAFT/runner" ci
```

The installation directory must be named **`skillcraft`**, matching `name` in
`SKILL.md`. A default clone into `opencode-skillcraft/` is a repository checkout,
not a correctly named skill installation. If you use the optional `.skill` ZIP,
extract its `skillcraft/` directory into `<project>/.opencode/skills/` and install
the same dependencies. OpenCode discovers the directory; the ZIP is not an installer.

OpenCode watches local skill files. Ask it to use **SkillCraft**, for example:

> Use SkillCraft to turn our recurring meeting-note cleanup into a reusable skill.
> Preserve missing owners and deadlines rather than inventing them, and test the result.

The agent can also select SkillCraft naturally for relevant authoring, evaluation,
revision, or triggering requests. Instructions and full required guides start at
[SKILL.md](SKILL.md).

## Validate and evaluate

```sh
# Standard-only validation; this does not certify runtime compatibility.
"$SKILLCRAFT/.venv/bin/python" "$SKILLCRAFT/scripts/quick_validate.py" "$SKILLCRAFT" --json

# Development checks.
"$SKILLCRAFT/.venv/bin/python" -B -m unittest discover -s "$SKILLCRAFT/tests"
npm --prefix "$SKILLCRAFT/runner" run check
npm --prefix "$SKILLCRAFT/runner" test
```

See [the runner guide](references/opencode-runner.md) for models, explicit profiles,
smoke tests and the full authoring-to-execution suite. Each experiment needs a new
workspace outside the skill, normally `.opencode/skillcraft-workspaces/`.
Use `--help` on the Python scripts for their CLI interfaces.

Final acceptance requires **Agent Skills PASS** and **OpenCode compatibility PASS**
for the same artifact. `compliance.py` checks representative native execution and
refreshes the environment; `package_skill.py --run ...` requires that evidence.
Quality, natural triggering and actual human feedback are separate results.

## Connection and migration

The runner discovers and authenticates with the local OpenCode service by default.
For an explicit endpoint, use `SKILLCRAFT_SERVER`; if needed, provide a JSON object
of headers through `SKILLCRAFT_HEADERS` in the process environment. Never commit
credentials. `SKILLCRAFT_TEMP` overrides the temporary execution parent.

For installations migrating from this project's former `skill-creator` identity:

1. Preserve old experiments and hashes as historical evidence.
2. Install under `.opencode/skills/skillcraft/` and update active instructions and
   scripts to the new ID. Use `SKILLCRAFT*` variables in place of `SKILL_CREATOR*`.
3. Recreate the Python virtualenv at its new path; activation scripts and executable
   shebangs can contain the old absolute directory. Install Node dependencies too.
4. Avoid advertising both old and new copies in the same project. Keep any baseline
   outside `.opencode/skills/`.
5. Re-run validation and native execution under the new ID. Old compatibility
   evidence does not certify a renamed or modified artifact.

## Layout

- `SKILL.md`, `references/`: authoring and evaluation methodology.
- `scripts/`: validation, evaluation, optimization, reporting and packaging.
- `runner/`: OpenCode 2.2.6 client integration.
- `agents/`: independent judge prompt resources.
- `eval-viewer/`, `assets/`: review interfaces and templates.
- `tests/`: regression tests and representative fixtures.

Custom permissions profiles are recorded as **controlled:false**; they are not OS
sandboxes. Small evaluation datasets do not establish general statistical
equivalence. Automated judges never substitute for actual human approval.

## License and provenance

Apache-2.0; see [LICENSE.txt](LICENSE.txt). The authoring method incorporates
Anthropic's upstream skill-creator; its original identity, pinned source and
attribution are preserved in [UPSTREAM.md](UPSTREAM.md). SkillCraft is this
OpenCode adaptation's name, not a rename of the upstream project.
