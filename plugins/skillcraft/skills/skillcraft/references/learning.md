# Learn from verified skill use

For a compact field/default and precedence table, see the
[configuration reference](configuration.md#learning-configuration).

Enable learning for a project or for eligible local skill sources under home.
Install the SkillCraft plugin in Codex or OpenCode to collect conversation
events. Standalone SkillCraft can collect evaluation and review evidence.
Review and trust SkillCraft's bundled `UserPromptSubmit` and `Stop` hooks in
Codex before using conversation collection. Trust `PostModelRequest` for
model-step counting where the installed Codex exposes it. Scheduled Learning also
uses `PostToolUse` for tool outcomes. Review and trust that hook when enabling
scheduled collection. The marketplace package uses the Codex compatibility
manifest for hook discovery.
[Codex hook trust](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)
Completed quality evaluations and validated HTML feedback feed the same journal.
For a run with the skill loaded, set an explicit success, failure, or correction
verdict to use feedback as evidence. For a correction, include the changed
behavior in the comment. A completed review saves nonempty remarks on
`without_skill` runs as baseline feedback tied to the original run and case.
Baseline remarks carry no skill verdict and do not schedule revision work.
For live feedback, open the full URL printed by the review server. Its
fragment carries the session capability for reading and submitting feedback.

## Project configuration

Create `.skillcraft/learning.json` at the project root. For example:

```json
{
  "schema_version": 1,
  "enabled": true,
  "sources": ["sessions", "evals"],
  "skills": {
    "meeting-actions": {
      "source": ".agents/skills/meeting-actions",
      "auto_apply": true,
      "train": "evals/meeting-actions/train.json",
      "holdout": "evals/meeting-actions/test.json",
      "harnesses": {
        "codex": {"model": "gpt-6.2-sol#low", "optimizer_model": "gpt-6.2-sol#low"}
      },
      "editable": ["SKILL.md", "references/*", "assets/*", "scripts/*", "agents/*"],
      "tests": [["python", "-m", "unittest", "discover", "-s", "/verification/tests"]],
      "repeats": 3
    }
  }
}
```

Use project-relative source and dataset paths. Give training and holdout
cases distinct IDs, prompts, and paraphrase groups. Datasets use the same
format as quality evaluations. Keep holdout files outside deployable skill
resources. Set `harnesses` to each runtime required for
activation. Configure its model and authenticated connection as in the
runner guides. The first configured runtime supplies the optimizer by default.
To use Hermes for revision with any supported evaluator, add
`"optimizer": {"harness": "hermes", "model": "provider:model"}` to the skill's
settings, using the configured Hermes provider and model. Set an absolute
`home` in that object to select a Hermes profile.

For a Hermes-only loop, set `harnesses.hermes` with a `model` and optionally an
absolute `home`. Hermes uses the isolated file-quality runner on macOS and
requires deterministic checks for every scored expectation. Add an independent
`judge_model` in a supported Codex or OpenCode evaluation setup when the rubric
requires model grading.

Schema version 1 also supports automatic selection. For example, a
capture-only project configuration is:

```json
{
  "schema_version": 1,
  "enabled": true,
  "sources": ["sessions", "evals"],
  "selection": "all",
  "exclude": ["private-skill"]
}
```

The existing `skills` map selects sources explicitly when `selection` is absent
or set to `"explicit"`. Use `"all"` to discover local skills on each call. To opt
out of specific skills, add their exact IDs to `exclude`. Automatic selection
works in an empty project and adds eligible new skills without re-enrollment.

Project discovery uses `.agents/skills`, `.codex/skills`, `.hermes/skills`,
`.opencode/skills`, and `opencode/skills`. Hermes also supports one level of
categories. Automatic discovery skips nested Git checkouts, known plugin and
marketplace locations or manifests, and installs identified
by Hermes bundle and Hub metadata. A standalone copy with no installation
metadata can be excluded by ID. If the same skill ID exists in several
eligible directories, pin its `source` by adding it to the `skills` map.

Symlinked skills require an explicit `linked_skill_roots` list of absolute
source directories. For example, add
`"linked_skill_roots": ["/path/to/checkout/skills/team"]` to a configuration
with `"selection": "all"` and run `enable` again. A skill directory link is
eligible if its final target is inside an approved directory and passes the
managed-source exclusions. SkillCraft checks the target again when binding
evidence or reading a revision source. Multiple links to the same source
count as one skill. Learning can prepare revisions from a snapshot of a
linked skill. Automatic promotion and rollback through a linked skill are
disabled.

For capture only, omit evaluation settings. To set up model-based revision in
bulk, set `defaults` with shared `harnesses` and dataset paths such as
`"train": "evals/{skill}/train.json"` and
`"holdout": "evals/{skill}/holdout.json"`. The optional `skills` map overrides
settings by ID. Run `enable` after preparing a new skill's evaluation data to
approve its inputs before model-based revision. Automatic promotion requires
`auto_apply: true`.

For background runs, set Codex's optional `codex_home` and `binary` paths
or OpenCode's optional `server` URL in the corresponding runtime settings.
If `codex.binary` is configured, enable records its executable identity and
content. The worker snapshots those approved bytes into a private temporary
directory and launches that copy. Run `enable` again after updating the
configured executable or app bundle. SkillCraft supports native executables
and Codex packages with a `codex-package.json` manifest using layout version 1.
The snapshot keeps the packaged executable, helpers, and resources together.
For an unrecognized launcher, configure the path to the native executable.
Leave `binary` unset to use the owner process's `SKILLCRAFT_CODEX` or normal
`PATH` discovery. Keep credentials in the authorized runtime profile or
process environment.

Approve the exact local configuration to start collecting. Enabling also
records the train/holdout dataset bytes and each in-project fixture identity
and hash for model evaluation. Dataset or fixture changes pause model work
until the owner enables learning again. A configuration change pauses learning
until the owner enables it again. The journal stores
message identities, version bindings, and short excerpts with recognized
credentials removed outside the skill directory. This includes common API
tokens and URI userinfo; it is not general PII filtering. Identified skill turns
can retain a short redacted answer excerpt so a later confirmation preserves
the concrete successful behavior.

```sh
"${SKILLCRAFT_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/skillcraft/venv}/bin/python" \
  "$SKILLCRAFT/scripts/learning.py" enable "$PWD"
```

Set `sources` to `["evals"]` for an evaluation-only comparison. To suspend
collection, set `enabled` to false. Existing journal and revision files stay
available for inspection.

## Home configuration

For home-wide collection, create `~/.skillcraft/learning.json` with
`"scope": "home"` and an automatic selection configuration, then approve it:

```sh
"${SKILLCRAFT_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/skillcraft/venv}/bin/python" \
  "$SKILLCRAFT/scripts/learning.py" enable "$HOME"
```

Home collection applies under `HOME`. An intervening mount point, project
configuration, or project marker (`.git`, `.hg`, `.svn`, or `AGENTS.md`) stops
home fallback. A session
at exactly `HOME` opts in even when `HOME` has a repository marker. An approved
project configuration takes precedence; a disabled project configuration blocks
home fallback. Home collection never uses a project's separate journal.
Home automatic selection includes `~/.codex/skills`, `~/.hermes/skills`, and
`~/.config/opencode/skills`. The native Hermes plugin uses only the active
profile's Hermes skill root, including one category level. Explicit selection
works at home as well. See the [configuration reference](configuration.md#root-fields)
for the full list and the rules for linked sources.

To send experience from a named profile's own skills to the shared home
journal, set `"hermes_skill_home": ".hermes/profiles/work"` alongside
`"scope": "home"` and `"selection": "all"`. Re-enable shared Learning and
enroll the named profile. This scopes shared discovery to the chosen Hermes
skill root and keeps each profile journal separate.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" enable "$HOME"
hermes -p work skillcraft enable "$HOME" --auto-import
hermes -p work skillcraft status "$HOME"
```

## Local Hermes chats

Install and enable the SkillCraft Git plugin in the Hermes profile that hosts
the chat. The plugin registers the shared skill as `skillcraft:skillcraft`.
To journal the use of a project skill, enroll its local source with the project
configuration above and include `sessions` in `sources`. Use the same Hermes
profile for enrollment, the chat, and status.

```sh
hermes skillcraft enable "$PWD"
hermes skillcraft status "$PWD"
```

Open a local chat in the project and ask Hermes to use `skill_view` to read the
enrolled skill. After checking the answer, reply `I verified it works.` or
provide a correction in the next message. Hermes records evidence when the successful tool result identifies the
enrolled `SKILL.md` with its complete contents and the version matches through
the final reply. Use a fresh chat to verify a read if Hermes has already cached
the tool result. A reference-page read or an unchanged-result stub is
insufficient on its own.

The Hermes journal lives under `$HERMES_HOME/skillcraft/learning/` in that
profile. To forward evidence automatically, first enable regular SkillCraft
Learning for the same project, then run this command from the project root in
the Hermes profile that owns the chats:

```sh
hermes skillcraft enable "$PWD" --auto-import
```

For the home configuration, enroll the same scope in Hermes with:

```sh
hermes skillcraft enable "$HOME" --auto-import
```

SkillCraft forwards verified feedback and adds the completed turn's messages
and model steps to the shared counts. Enrollment without `--auto-import` keeps collection
in the profile journal. To import that journal explicitly, set
`HERMES_PROFILE_HOME` to the source profile directory (`$HOME/.hermes` for the
default profile on a standard installation).

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" \
  import-hermes "$PWD" --from-profile "$HERMES_PROFILE_HOME"
```

Both enrollments must match the current project configuration. The command
reports imported, duplicate, and skipped entries. Import accepts feedback from
completed turns when both scopes select the same skill source and the recorded
version matches the current skill. New evidence queues a check under the existing evaluation
and promotion requirements.

To recover feedback from one older local chat, select its session ID in the
same Hermes profile and run:

```sh
hermes skillcraft recover "$PWD" --session SESSION_ID
```

Recovery checks the original final reply against a previously recorded
SkillCraft binding for the current skill version. The feedback must directly
follow that reply in the native transcript and belong to a completed turn.
The command reports skipped and duplicate records, including chats without a
verifiable version binding.

## Collection and promotion

The observer schedules a journal check after 15 user or final-answer
messages, after 15 completed model steps, or when an A/B result or feedback
import adds new evidence. A user turn can include multiple model steps.
Observers commit the step count and any feedback from a chat only after that
turn finishes. Step counts use runtime event identifiers, with no reasoning
text. Intermediate tool messages, subagent activity, interrupted turns and
learning-generated evaluations do not contribute model steps. A signal with an
unknown skill or version can support a new skill when a project-approved case
confirms the result. Revising an enrolled skill requires its verified version.
Repeats of the same case and input count once.

Codex uses `UserPromptSubmit` and `Stop` for turns and `PostModelRequest` for
completed model requests. A supported local rollout can bind
a successful command in that turn when it identifies the enrolled `SKILL.md`
and returns its exact bytes. This also covers command wrappers that preserve
the path and complete output. The package fingerprint must match the source
observed at both hooks. Missing rollouts, unsupported records, partial reads,
and changed files leave the turn unbound. OpenCode follows execution and inbox
events, then verifies the skill body in persisted attachments or successful native skill tool
results against the enrolled version observed at the start of the turn.

One reproducible defect can queue a revision. A/B imports use completed
deterministic and independent judge grades. Automatic publication also needs
independent holdout coverage and a fresh, controlled A/B result.
Ordinary English or Russian confirmations, failure reports and corrections
are recognized when they can be attributed to the preceding skill use. Add
`[skillcraft-case:case-id]` to link feedback to an existing case. A
correction to a deterministically graded failure can trigger an immediate
review at the next checkpoint. Confirmed successes linked to existing cases
must also pass when the candidate is tested. Test prompts and holdout evidence
stay with the evaluator.

Each proposal starts in an isolated copy. The worker preserves frontmatter,
limits edits to approved resources, and compares the candidate with the
original and clean baseline using the same frozen checks. A candidate needs
a reproducible gain and no per-check regression across the configured runtimes.
A review outcome or technical failure alone cannot activate a new version.

For executable changes, pre-load a trusted validation image in Docker or
Podman and set `SKILLCRAFT_TEST_IMAGE` in the worker launch environment. The
worker uses an already-present local image and runs the command vectors in
`tests` without network access or host credentials. The candidate is at
`/candidate`, and tests can refer to the frozen original at `/verification`.
The configured commands should exercise the candidate and come from the
owner, not the proposal. A missing runner, validation image or tests leaves
the candidate with a blocked report.

## Inspect and recover

Use `status "$PWD"` to see counts, evidence grouped by independent case and
skill version, a deterministic next action, and the latest revision outcomes.
The `preview "$PWD"` command gives the same eligibility explanation without
starting the worker. Both commands inspect the journal and can initialize its
schema. The evidence and history summaries contain metadata rather than feedback
text, transcripts or proposal contents. The existing `revisions` field also
provides local revision paths. Revision directories contain the proposed diff,
A/B reports, usage when available, and the promotion or rejection reason.
The source content and permissions must still match the tested version at
activation. The worker checks the displaced tree after exchange so a
concurrent edit can be restored. A supported same-filesystem atomic exchange
is required for auto-apply.

Use `rollback "$PWD" REVISION_ID` to restore a recorded backup. Rollback
checks the currently installed hash before switching directories.

After upgrading, if Hermes discovers copies named `.promotion-…` or
`.rollback-…` next to a skill, run `repair-exchanges "$PWD"` in the enrolled
project. It archives copies that match the hashes and permissions in completed
revision records and lists other matches under `unverified`. It preserves the
active skill, journal history, and rollback records.

To import an existing run by hand use `ingest WORKSPACE --project "$PWD"`, with
`--feedback FILE` for an already generated review file. If the review was
generated with a grading selection, also pass the same `--selection FILE`.
Repeat imports are idempotent. Feedback imports report `new_evidence` and
`new_baseline_feedback`. The `status` command shows the number of baseline
remarks in the journal. A worker picks up pending checkpoints the next time
a hook or `work "$PWD"` starts it. Failed and interrupted optimizer attempts
keep their evidence. A later checkpoint can retry when new evidence or changed
optimizer settings produce a different request.

When a proposal is saved but code or A/B verification is blocked, fixing the
owner-controlled tests, evaluator settings, or validation runtime and running
`work "$PWD"` queues a validation retry that reuses the verified proposal.
Use `work "$PWD" --retry-blocked` to request one durable validation-only retry
without changing those settings. These validation checkpoints never call the
optimizer; they run only when a hash-verified proposal for the same source and
optimizer request is available. Failed or interrupted optimizer calls still
wait for new evidence or a changed optimizer request.


## Session candidates

Ask SkillCraft in the current chat to create a draft from a recurring workflow
or revise an existing skill from the relevant feedback. Keep the draft directory
outside the runtime's skill catalog and give it the same name as its frontmatter.
The draft and proposed checks can be reviewed before any model evaluation.

```sh
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" stage "$PWD" /path/to/drafts/meeting-actions
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" verify-now "$PWD" CANDIDATE_ID \
  --evals evals/meeting-actions.json --harness codex --model "$EXECUTOR_MODEL"
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" apply "$PWD" CANDIDATE_ID
"$SKILLCRAFT_VENV/bin/python" "$SKILLCRAFT/scripts/learning.py" rollback "$PWD" --candidate CANDIDATE_ID
```

For a revision, add `--source .agents/skills/meeting-actions` to the stage
command. The stage result contains the candidate ID, source and candidate hashes.
Verification writes the usual paired reports, including `report.html`, and
returns their location. Application is a separate explicit owner action.
The apply result records whether a trusted verification exists.

## Scheduled Learning

Add an `automation` block to the enrolled Learning policy. For example, this
project discovers eligible skills, drafts new skills or revisions, and prepares
reports for review. Apply the revised configuration with the same `enable`
command used for initial enrollment.

```json
{
  "schema_version": 1,
  "enabled": true,
  "selection": "all",
  "sources": ["sessions", "evals"],
  "automation": {
    "create": true,
    "improve": true,
    "auto_apply": false,
    "interval_days": 7,
    "max_candidates_per_batch": 3,
    "harnesses": {"codex": {"model": "<model available in your Codex profile>"}}
  }
}
```

Set `scope` to `home` in the policy under your home directory to use the same
actions there. Project boundaries and enrollment still apply. For Hermes,
enroll the local profile and enable its shared-journal auto-import. Trust the
project in Hermes, or configure the profile to discover `~/.agents/skills` for
home. Use the existing harness and project permission settings.

The next completed session turn after a batch becomes due wakes the worker.
`work` also checks the persisted due time, and `verify-now` evaluates a chosen
candidate immediately. The default of three candidates per project or home
batch is a starting setting. Raise `max_candidates_per_batch` when you can
run more (for example, to six). Each batch combines eligible lessons into at
most one candidate per skill. With one case, one harness, a pilot and three
repeats, creation runs the executor eight times and revision runs it twelve
times. Generation, judging and trigger checks add separate runs. The batch
summary and candidate reports include stage-by-stage usage, elapsed time and
provider-reported cost when available.

Confirmed feedback belongs to the previous completed turn. A failed tool result
can explain where a task went wrong, and a successful repeat can corroborate a
recovery. A tool result by itself does not qualify an improvement. The journal
records tool identifiers and categorical outcomes, with bounded redacted chat
excerpts for opted-in scopes. Use `[skillcraft-topic:meeting-actions]` in the
task or feedback to group related prompts. Untagged grouping is conservative.
When a turn used multiple proven skills and no single attribution is available,
the observation stays out of the creation queue.

One confirmed result with a deterministic check can queue creation. One
reproducible failure or correction can queue a revision. Cases can come from
one session. With only one case, the worker can prepare a draft and report
that an independent holdout case is needed for automatic publication.

For tool-generated artifacts, approve a deterministic train case and link a
task with `[skillcraft-case:case-id]`. On a completed turn, the observer
checks fresh files under `outputs/` (up to 8 MiB per file) and records the
result with input and artifact hashes. A tool status or a missing or unattributed file stays
diagnostic. To verify a recorded local artifact manually, run
`learning.py verify-artifact PROJECT HARNESS SESSION TURN --case CASE_ID`
with the journal's native session and turn IDs. Without hooks, run
`learning.py verify-artifact PROJECT --topic TOPIC --case CASE_ID` to record
a local proof without a session or skill-version attribution. A verified A/B
import can add version-bound evidence without session hooks.

When confirming a file result, optionally record a verifiable literal in a
separate feedback line. Use an output filename and the expected text. SkillCraft
compiles such literals to file checks on independent cases. Keep confidential
information out of examples sent to a configured model.

```text
I verified it works.
[skillcraft-check:{"file":"actions.csv","contains":"Owner,Due"}]
```

Alternatively, set `automation.cases.<topic>.train` and `holdout` to
scope-relative quality evaluation files for creating a skill. List each deterministic check's `text` in the case's `expectations`.
Existing per-skill approved datasets also work for revisions. Keep independent holdout
prompts, fixtures, and expected results out of training input. The worker
records insufficient checks and leaves activation pending when it cannot verify
a candidate. On a qualified candidate, `auto_apply: true` authorizes publication
or replacement only inside the enrolled scope. Check the batch items and
candidate reports with `status` and `preview`.
