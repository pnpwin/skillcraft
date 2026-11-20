# Learn from verified skill use

Enable learning for a specific project and its checked-out skill sources.
Install the SkillCraft plugin in Codex or OpenCode to collect conversation
events. Standalone SkillCraft can collect evaluation and review evidence.
Review and trust SkillCraft's two bundled hooks in Codex before using
conversation collection. The marketplace package uses the Codex compatibility
manifest for hook discovery. [Codex hook trust](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)
Completed quality evaluations and validated HTML feedback feed the same journal.
A review mark alone records that a run was seen. Set an explicit success,
failure, or correction verdict to use human feedback as evidence. For a
correction, include the changed behavior in the comment.
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
runner guides. The first configured runtime supplies the optimizer. Add an
independent `judge_model` where deterministic checks do not cover the rubric.
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
message identities, version bindings, and short redacted evidence outside the
skill directory. Identified skill turns can retain a short redacted answer
excerpt so a later confirmation preserves the concrete successful behavior.

```sh
"${SKILLCRAFT_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/skillcraft/venv}/bin/python" \
  "$SKILLCRAFT/scripts/learning.py" enable "$PWD"
```

Set `sources` to `["evals"]` for an evaluation-only comparison. To suspend
collection, set `enabled` to false. Existing journal and revision files stay
available for inspection.

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
profile. Hermes collects evidence only. Run evaluation and automatic revisions
from a configured Codex or OpenCode project.

## Collection and promotion

The observer schedules background work after 15 new user or final-answer
messages, or after an A/B result or feedback import adds new evidence. It
ignores intermediate tool messages and learning-generated evaluations. A
signal with an unknown skill or version stays diagnostic. Repeated A/B
samples from one case or one explicit paraphrase group count as one source
of evidence.

Codex uses `UserPromptSubmit` and `Stop`. A supported local rollout can bind
a successful command in that turn when it identifies the enrolled `SKILL.md`
and returns its exact bytes. This also covers command wrappers that preserve
the path and complete output. The package fingerprint must match the source
observed at both hooks. Missing rollouts, unsupported records, partial reads,
and changed files leave the turn unbound. OpenCode follows execution and inbox
events, then verifies the skill body in persisted attachments or successful native skill tool
results against the enrolled version observed at the start of the turn.

The worker uses three independent observations or one correction backed by
a reproducible failure. A/B imports use completed deterministic and independent
judge grades. A correction needs a deterministic reproduction to qualify on its own.
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

Use the same interpreter and script with `status "$PWD"` to list checkpoint
counts and revision IDs. Revision directories contain the proposed diff,
A/B reports, usage when available, and the promotion or rejection reason.
The source content and permissions must still match the tested version at
activation. The worker checks the displaced tree after exchange so a
concurrent edit can be restored. A supported same-filesystem atomic exchange
is required for auto-apply.

Use `rollback "$PWD" REVISION_ID` to restore a recorded backup. Rollback
checks the currently installed hash before switching directories. To import
an existing run by hand use `ingest WORKSPACE --project "$PWD"`, with
`--feedback FILE` for an already generated review file. If the review was
generated with a grading selection, also pass the same `--selection FILE`.
Repeat imports are idempotent. A worker picks up pending checkpoints the next
time a hook or `work "$PWD"` starts it. Failed and interrupted optimizer
attempts keep their evidence. A later checkpoint can retry when new evidence
or changed optimizer settings produce a different request.

When a proposal is saved but code or A/B verification is blocked, fixing the
owner-controlled tests, evaluator settings, or validation runtime and running
`work "$PWD"` queues a validation retry that reuses the verified proposal.
Use `work "$PWD" --retry-blocked` to request one durable validation-only retry
without changing those settings. These validation checkpoints never call the
optimizer; they run only when a hash-verified proposal for the same source and
optimizer request is available. Failed or interrupted optimizer calls still
wait for new evidence or a changed optimizer request.
