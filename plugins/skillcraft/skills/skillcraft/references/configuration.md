# Configuration reference

Compact reference for user-facing SkillCraft settings. The [Learning guide](learning.md),
[Codex runner guide](codex-runner.md), [OpenCode runner guide](opencode-runner.md),
and [schema guide](schemas.md) explain workflows and contracts in more detail.

## Learning configuration

Put project configuration in `<project>/.skillcraft/learning.json`. Home-wide
configuration lives at `~/.skillcraft/learning.json`. Both use schema version 1.
The home file must set `scope` to `home`. Project configuration has precedence
over home configuration: the closest `.skillcraft/learning.json` walking up
from the current directory is used. An enabled project file is used, while a
disabled project file stops home fallback. Home fallback also stops at a
project marker (`.git`, `.hg`, `.svn`, or `AGENTS.md`) or a mount point.

This project configuration enables capture for one skill.

```json
{
  "schema_version": 1,
  "enabled": true,
  "sources": ["sessions", "evals"],
  "skills": {
    "example-skill": {"source": ".agents/skills/example-skill"}
  }
}
```

### Root fields

| Field | Values and default | Notes |
|---|---|---|
| `schema_version` | Required: `1` | Other versions are rejected. |
| `enabled` | Must be `true` to collect. Absent behaves as disabled | Set false to suspend collection. |
| `scope` | `project` (default) or `home` | `home` is valid only in the home configuration. |
| `sources` | List of `sessions` and `evals`. Default both. | `["evals"]` collects evaluation evidence only. An empty list collects neither. Hermes enrollment requires `sessions`. |
| `selection` | `explicit` (default) or `all` | `explicit` requires at least one `skills` entry. `all` discovers eligible local skills on each call. |
| `skills` | Object keyed by exact skill ID. Default `{}` | Pins/overrides settings for IDs. With `selection: explicit`, each entry needs `source`. With `all`, `source` pins a duplicate ID to one discovered path, including an approved symlink. |
| `exclude` | List of exact skill IDs. Default `[]` | Only valid with `selection: all`. |
| `defaults` | Skills-settings object. Default `{}` | Only valid with `selection: all`. Merged first, then a matching `skills` entry overrides it. Cannot contain `source` or `linked_target`. |
| `linked_skill_roots` | List of absolute source-directory paths. Default `[]`. | Only valid with `selection: all`. Eligible skill links must resolve under an approved root. SkillCraft uses snapshots for revisions and disables automatic promotion and rollback of linked sources. |

Automatic discovery checks `.agents/skills`, `.codex/skills`, `.hermes/skills`,
`.opencode/skills`, and `opencode/skills`. Home scope also checks
`~/.config/opencode/skills`. Hermes allows one category directory. Installed
plugins/marketplaces, nested Git checkouts and identified managed installs are
skipped. If an ID appears more than once, pin its `source` or exclude it.

Symlinked skill roots are reviewed before use. In an interactive terminal,
`enable PROJECT` asks about candidate roots on stderr and prints the result JSON
to stdout. Enter skips adding all candidates. Confirm selections with candidate
numbers separated by commas or spaces. EOF or Ctrl-C cancels. In a non-interactive
terminal with no flags, linked roots are not added. For a read-only preview,
`enable PROJECT --preview-linked-roots` prints `candidates` (`root` and
`links[].source/target`) and `preview_sha256`, for example.

```json
{"candidates": [{"root": "/absolute/source/skills", "links": [{"source": ".agents/skills/example", "target": "/absolute/source/skills/example"}]}], "preview_sha256": "DIGEST"}
```

Approve one or more displayed root paths by repeating
`--approve-linked-root ABSOLUTE_ROOT` and supply that digest with
`--linked-roots-preview-sha256 DIGEST`. Only approved roots are used. Preview
cannot be combined with approvals or `--auto-import`. Run a separate actual
Hermes enrollment with `--auto-import` if desired. The same flags are available
through `hermes skillcraft enable`. A Hermes refresh only carries prior shared
approval forward when that enrollment is already active. Other profiles must be
enabled again after a configuration change.

### Per-skill settings

Use these settings in a `skills` entry or in `defaults`, except `source`, which
belongs in `skills` only.

| Field | Values and default | Notes |
|---|---|---|
| `source` | Required for explicit selection. Project-relative directory | Must contain that ID's `SKILL.md`. Explicit sources cannot use symlinks or paths outside the configured root. With `selection: all`, a pin may name a discovered symlink only when its target is under an approved `linked_skill_roots` directory. |
| `train`, `holdout` | Both omitted for capture-only. Both required for model-based revision | Project-relative JSON datasets. They must be distinct in case IDs, prompts and paraphrase groups. Holdout files must stay outside deployable skill resources. With `defaults` and `selection: all`, `{skill}` is replaced by the discovered ID. |
| `harnesses` | Object with one or more of `codex`, `opencode`, `hermes` | Each configured runtime requires `model`. See model syntax below. |
| `harnesses.<runtime>.model` | Required when that runtime is configured | Codex `MODEL[#EFFORT]`. OpenCode `provider/model[#variant]`. Hermes `provider:model`. |
| `harnesses.<runtime>.optimizer_model` | Optional | Runtime-specific optimizer model, same syntax as `model`. |
| `harnesses.<runtime>.judge_model` | Optional | Independent grader. Needed when quality expectations are not fully covered by deterministic checks. |
| `harnesses.codex.codex_home` | Optional absolute path | Selects a Codex profile for background runs. |
| `harnesses.codex.binary` | Optional absolute path | Pins a Codex executable/package. Re-run `enable` after changing it. |
| `harnesses.opencode.server` | Optional endpoint URL | Selects an OpenCode server for background runs. |
| `harnesses.hermes.home` | Optional absolute path | Selects a Hermes profile. |
| `optimizer` | Optional object: `{"harness":"hermes","model":"provider:model","home":"/absolute/profile"}` | Explicit cross-runtime optimizer. Only `hermes` is accepted here. `home` is optional. |
| `auto_apply` | Boolean. Default false | Automatic promotion requires true. Linked sources force false. |
| `editable` | List of skill-relative globs. Default `SKILL.md`, `references/*`, `assets/*`, `scripts/*`, `agents/*` | Limits files an optimizer may change. |
| `tests` | List of argv arrays. Default `[]` | Commands run as deterministic checks, for example `["python3", "-m", "unittest", "discover", "-s", "tests"]`. |
| `repeats` | Positive integer. Default `3` | Number of repeated quality runs per candidate. |
| `timeout` | Positive number of seconds. Default `180` | Per-run limit. |

Model-based revision requires both `train` and `holdout` plus at least one
configured harness and model. If `optimizer` is omitted, the first configured
harness supplies the optimizer by default.

Executable changes require owner-provided `tests`. Those commands run in an
already-present local Docker or Podman image named by `SKILLCRAFT_TEST_IMAGE`.
SkillCraft does not download an image. Without a configured image, runner or
tests, executable-change verification is unavailable.

This configuration supplies model and dataset defaults for discovered skills.

```json
{
  "schema_version": 1,
  "enabled": true,
  "selection": "all",
  "defaults": {
    "train": "evals/{skill}/train.json",
    "holdout": "evals/{skill}/holdout.json",
    "harnesses": {"codex": {"model": "MODEL#EFFORT"}},
    "repeats": 3
  },
  "skills": {"example-skill": {"source": ".agents/skills/example-skill"}},
  "exclude": ["another-skill"]
}
```

`enable` approves the current configuration and, for model-based work, dataset,
fixture and configured Codex executable identities. Re-enable after changing
these inputs. Run `"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" --help` for the
public learning commands. Common setup.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" enable "$PWD"
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" status "$PWD"
```

For Hermes profile enrollment, use `hermes skillcraft enable PROJECT` and
`hermes skillcraft status PROJECT`. `--auto-import` opts that profile into
forwarding completed feedback to the shared journal. See [Local Hermes chats](learning.md#local-hermes-chats).
`learning.py enable` also accepts `--preview-linked-roots` or repeated
`--approve-linked-root ABSOLUTE_ROOT` together with
`--linked-roots-preview-sha256 DIGEST`. Preview mode and approval mode are
mutually exclusive.

Per-harness profile values in `harnesses` take precedence over their matching
process environment settings for background runs. The Codex `binary` option
selects the executable. `codex_home`, OpenCode `server`, and Hermes `home`
select the respective runtime profile or endpoint.

### Hermes Learning cycle

The Hermes profile journals verified feedback from completed local chat turns.
Use `--auto-import` or `learning.py import-hermes` to forward that evidence to
the shared Learning worker after enabling shared Learning for the same scope.
When enough evidence qualifies for revision, the worker uses the approved
`train` cases to prepare a candidate and evaluates it on `holdout` cases. To
include Hermes in the quality runs, set `harnesses.hermes.model` and supply
deterministic file checks for all expectations. To select Hermes as the
proposal generator, use the `optimizer` setting above. Applying a passing
candidate follows the normal `auto_apply` rules. See
[Collection and promotion](learning.md#collection-and-promotion).

## Runner settings

| Setting | Applies to | Default / accepted values |
|---|---|---|
| `--harness` | Evaluation, optimization and comparison commands | Autodetected from the active Codex/OpenCode session when unambiguous. Explicit choices: `codex`, `chatgpt` (Codex alias), `opencode`, `hermes`. |
| `--model` | Runtime commands | Required. Syntax depends on harness: Codex `MODEL[#EFFORT]`, OpenCode `provider/model[#variant]`, Hermes `provider:model`. |
| `--profile FILE` | OpenCode evaluation, grading, comparison, optimization. Not Codex or Hermes | Optional JSON permissions profile. Custom profiles are recorded as uncontrolled. Codex and Hermes use their built-in bounded profiles. |
| `--judge-profile FILE` | Quality / loop | Optional separate grading profile. |
| `--optimizer-profile FILE` | Optimization loop | Optional separate optimizer profile. |

OpenCode profile keys are `name` (string, default `custom`),
`inherit_bounded_files` (boolean, default false for a supplied profile),
`permissions` (array of `{action, resource, effect}` where effect is `allow`,
`deny`, or `ask`. Default `[]`), `steps` (integer 1–200, default 16),
`allow_delegation` (boolean, default false), and `cli_tools` (default `[]`).
Each `cli_tools` item has `id`, `executable` (existing absolute executable
path), and `environment` (string-to-string map of non-secret settings). The
environment may use `{state}` and `{project}`. It cannot override `HOME` or
`PATH` or contain credentials. With no custom profile SkillCraft uses bounded
file rules. When inheritance is true,
those defaults are followed by the supplied permission rules. Otherwise the
supplied list replaces them. Profile permissions do not provide OS isolation.
See [OpenCode profile behavior](opencode-runner.md#profile-options) for rule
details. Custom `--profile`, `--judge-profile`, or `--optimizer-profile` settings
are supported only with OpenCode. Codex and Hermes reject them.

Hermes supports paired file-quality runs with deterministic grading and
schema-checked description proposals through `improve_description.py`. Trigger
evaluations, including the trigger-based `run_loop.py` workflow, require Codex
or OpenCode. Model-judged grading, standalone grading, and blind comparison also
require Codex or OpenCode.

## Environment

| Variable | Purpose |
|---|---|
| `SKILLCRAFT` | Absolute SkillCraft skill directory used by examples and commands. |
| `SKILLCRAFT_VENV` | Python virtual environment path. Keep writable and outside the skill directory. Defaults under `${XDG_DATA_HOME:-$HOME/.local/share}/skillcraft/venv`. |
| `SKILLCRAFT_SERVER` | Explicit OpenCode server endpoint. Otherwise discover the local server. |
| `SKILLCRAFT_HEADERS` | Optional JSON object of HTTP headers for an explicit OpenCode server. Treat as secret. Do not put it in config or experiment files. |
| `SKILLCRAFT_TEMP` | Temporary execution parent. Otherwise the system temporary directory is used. |
| `SKILLCRAFT_CODEX_HOME` | Codex profile home for file-backed authentication. |
| `SKILLCRAFT_CODEX` | Optional Codex executable override. Normally leave unset when using `PATH` discovery. |
| `CODEX_API_KEY` | Alternative Codex runner authentication via the process environment. |
| `SKILLCRAFT_HERMES_HOME` | Hermes profile home. Takes precedence over `HERMES_HOME`, then `~/.hermes`. |
| `SKILLCRAFT_HERMES_BIN` | Optional local Hermes CLI executable. Defaults to `hermes` on `PATH`. |
| `SKILLCRAFT_TEST_IMAGE` | Optional image name for isolated checks of executable skill changes. Must already exist in local Docker or Podman. No default. |

Keep credentials in the authorized runtime profile or process environment.
OpenCode defaults to local service discovery and authentication. SkillCraft does
not restart or start the server. Hermes file A/B runs require macOS.

## Evaluation and optimization CLI

Run commands with the SkillCraft virtualenv's Python. Use `--help` on a script
for the full current option list.

| Script | Required inputs | Main options |
|---|---|---|
| `learning.py enable` | Project path positional argument | `--auto-import` for an enrolled Hermes profile, or linked-root preview/approval flags described above. |
| `learning.py import-hermes` | Project path and `--from-profile` | Imports verified feedback from the specified Hermes profile into shared Learning. |
| `learning.py ingest` | Evaluation workspace | `--project` selects the Learning scope. `--feedback` imports reviewed feedback. `--selection` selects the grading attempt and requires `--feedback`. |
| `learning.py work` | Project path | `--retry-blocked` queues validation of a saved candidate. |
| `run_quality_eval.py` | `--skill`, `--evals`, `--workspace`, `--model` | Paired quality runs. `--repeats` (3), `--workers` (1), `--judge-model`, `--judge-profile`, repeated `--pilot-case` (defaults to first case), `--previous`, `--stop-threshold` (2), `--json`, `--timeout` (180), `--profile`, `--harness`. |
| `run_eval.py` | `--skill`, `--evals`, `--workspace`, `--model` | Trigger runs. `--repeats` (3), `--workers` (1), optional independent `--judge-model` and `--judge-profile`, `--timeout` (180), `--profile`, `--harness`. |
| `run_loop.py` | `--skill`, `--evals` (trigger set), `--quality-evals`, `--workspace`, `--model`, `--optimizer-model` | Trigger-based description optimization plus quality regression with Codex or OpenCode. Optional `--judge-model`, `--profile`, `--judge-profile`, `--optimizer-profile`, `--iterations` (3), `--repeats` (3), `--workers` (1), `--timeout` (180), `--seed` (42), `--harness`. |
| `improve_description.py` | `--skill`, `--training-results`, `--output`, `--model` | Training-only description proposal with Codex, OpenCode, or Hermes. Optional `--history`, `--profile`, `--timeout` (180), `--harness`. |
| `compare.py` | `--left`, `--right`, `--output`, `--model` | Blind comparison. Optional `--analyzer-model`, `--profile`, `--seed` (42), `--timeout` (180), `--harness`. |
| `grade.py` | Run directory positional argument | Optional `--judge-model`, `--profile`, `--output`, `--rubric`, `--timeout` (180), `--harness`. |
| `generate_review.py` | Evaluation workspace | `--port` (0 chooses a free port), `--no-browser`, `--static` (HTML output), `--import-feedback`, `--selection`, `--feedback-output`, `--skill-name`, `--previous-workspace`, `--benchmark`. |
| `package_skill.py` | Skill directory positional argument | Packaging requires repeated `--run [HARNESS=]PATH` for accepted runs. Optional output directory, `--verification-dir`, `--openai-plugin-version`. |
| `quick_validate.py` | Skill directory positional argument | `--json`, `--lint`. Checks Agent Skills format only. |

Review feedback and benchmark data default to `feedback.json` and
`benchmark.json` in the workspace. A review with `--selection` requires
`--static` or `--feedback-output` outside that workspace. Importing feedback
with a selection requires `--feedback-output`.

Quality cases use an ID and prompt, with expected output, expectations and
optional deterministic checks.

```json
{"evals": [{"id": "case-1", "prompt": "Normalize this example.", "expected_output": "A concise result.", "expectations": ["The result preserves the amount."], "checks": [{"text": "The result preserves the amount.", "type": "contains", "file": "summary.txt", "value": "amount"}]}]}
```

See [Quality evals](schemas.md#quality-evals) for accepted fields and check
types. Trigger cases use `id`, `prompt`,
`should_trigger`, and optional `group` for paraphrases.

```json
{"evals": [{"id": "positive-1", "prompt": "Process this example.", "should_trigger": true, "group": "example-family"}]}
```

See [Trigger evals](schemas.md#trigger-evals) for accepted legacy forms, split
requirements and duplicate handling. Optimization requires enough independent
paraphrase groups to keep train, validation and (where possible) final test
separate. Quality regression uses its own dataset and grader settings.
