# SkillCraft workflows

See the [configuration reference](configuration.md) for flags, defaults, and
supported combinations.

## Quality A/B

`run_quality_eval.py` creates a new workspace and freezes the inputs, rubric,
settings, and skill or instruction snapshots before running any arms.

```mermaid
flowchart TD
    A["Select the runtime and freeze the evaluation inputs"] --> B{"What is being evaluated?"}
    B -->|Skill| S["Plan a clean baseline and a candidate arm"]
    B -->|Prompt| P["Plan instruction A and B at the same placement"]
    S --> C{"Previous version supplied?"}
    C -->|Yes| D["Add the previous version as a third arm"]
    C -->|No| E["Run the end-to-end pilot"]
    D --> E
    P --> E
    E --> F{"Pilot pipeline complete?"}
    F -->|No| G["Record the failing stage and stop dispatch"]
    F -->|Yes| H["Run the planned repeats in fresh environments"]
    H --> I["Grade observed outputs with deterministic checks"]
    I --> J{"Judge model configured?"}
    J -->|Yes| L["Grade remaining expectations and critique the rubric"]
    J -->|No| K{"Deterministic checks cover every expectation?"}
    K -->|Yes| N["Report paired results, coverage, and errors"]
    K -->|No| M["Record incomplete grading"]
    L --> N
    M --> N
```

Codex and OpenCode can use an independent judge. Hermes file A/B requires
macOS and deterministic grading. A completed negative quality result can pass
the pilot pipeline. A missing artifact or technical failure is reported
separately from a scored task outcome.
See [prompt A/B](prompt-ab-evaluation.md) for instruction placement and
incomplete-pair accounting.

## Description optimization

Codex and OpenCode support the trigger-based `run_loop.py` workflow. Hermes can
propose descriptions from supplied training results with
`improve_description.py`. It has no trigger-evaluation runner.

```mermaid
flowchart TD
    A["Group and split trigger cases"] --> B["Use training results to propose descriptions"]
    B --> C["Select on validation and freeze the result"]
    C --> D{"Independent final test available?"}
    D -->|Yes| E["Compare original and selected descriptions once on the final test"]
    D -->|No| F["Report the missing independent test"]
    E --> G{"Quality evaluations supplied?"}
    F --> G
    G -->|Yes| H["Run paired quality regression"]
    G -->|No| I["Report the missing quality regression"]
```

## Learning

Use [project or home scope](learning.md) to choose the journal. Project
configuration takes precedence; home collection stops at project boundaries.
With `selection: explicit`, only enrolled skills are observed. With `selection:
all`, eligible skills are discovered on each call, but discovery alone grants no
model-work approval. Linked sources can contribute evidence and receive
candidate reports. Their source files are read-only to Learning promotion and
rollback.

```mermaid
flowchart TD
    A["Find the closest learning configuration"] --> PR{"Project configuration found?"}
    PR -->|Yes| PEN{"Enabled and enrolled?"}
    PEN -->|Yes| PJ["Use project journal and policy"]
    PEN -->|No| DISABLED["Skip Learning; disabled config blocks home fallback"]
    PR -->|No| HB{"Home fallback applies before a project boundary?"}
    HB -->|Yes| HJ["Use home journal and policy"]
    HB -->|No| B["Skip Learning"]
    PJ --> SEL{"Selection mode"}
    HJ --> SEL
    SEL -->|explicit| D["Use the enrolled skill list"]
    SEL -->|all| E["Discover eligible skills; apply exclusions and approved linked roots"]
    D --> P
    E --> P{"Home scope with an active or configured Hermes profile?"}
    P -->|Yes| Q["Use that profile's Hermes skills directory"]
    P -->|No| F
    Q --> F
    F -->|Completed Codex or OpenCode chat| G["Verify the skill version and record eligible feedback"]
    F -->|Completed Hermes chat| H["Record verified, version-bound feedback in its profile journal"]
    F -->|Completed A/B or reviewed feedback| I["Import verified evidence for the matching runs"]
    H --> J{"Automatic forwarding enabled or manual import requested?"}
    J -->|No| K["Keep evidence in the Hermes profile journal"]
    J -->|Yes, with shared enrollment| L["Import feedback only when scope, source, skill version, and completed turn match"]
    J -->|Yes, both scopes use automation| U["Import completed observations; some may remain unbound"]
    U --> UQ{"Owner-approved deterministic case or proof?"}
    UQ -->|No| UD["Keep diagnostic; no creation candidate"]
    UQ -->|Yes| OBS["Admit checked observation for creation"]
    G --> M["Queue a checkpoint every 15th observed message or completed model step"]
    I --> N["Queue a checkpoint when new qualifying evidence arrives"]
    L --> N["Queue a checkpoint when bound evidence is newly imported"]
    OBS --> N
    M --> AUTO{"Scheduled automation configured?"}
    N --> AUTO
    WORK["Explicit work command"] --> AUTO
    AUTO -->|No| REV["Ordinary revision worker; model calls need approved per-skill inputs and evidence"]
    AUTO -->|Yes| DUE{"Interval due or a batch is resuming?"}
    DUE -->|No| WAIT["Keep evidence; no scheduled batch model calls"]
    DUE -->|Yes| SCAN["Plan the due batch from currently eligible items"]
    SCAN --> ANY{"Any eligible batch items?"}
    ANY -->|No| EMPTY["Record an empty scan; no model calls"]
    ANY -->|Yes| READY{"Training evidence, approved inputs, models, and approval ready?"}
    READY -->|No| WAIT
    READY -->|Yes| BATCH["Run the due batch"]
```

There is no background timer. In the normal hook flow, the completed assistant
turn checks the interval; explicit `work` checks it directly, and completed
evaluation or feedback imports can wake the worker. The batch checks the
persisted due time again before it starts.

Unverified chat use stays diagnostic. Feedback on `without_skill` is attached
to the A/B run without rating the skill or triggering a revision by itself.
Learning's own A/B runs do not feed back into the journal.

```mermaid
flowchart TD
    A["Process a queued check"] --> B{"Reproducible defect and approved training inputs?"}
    B -->|No| C["Retain the evidence and skip generation"]
    B -->|Yes| D["Generate a candidate in an isolated snapshot"]
    D --> E{"Approved checks and controlled A/B pass?"}
    E -->|No| F["Record the outcome without applying"]
    E -->|Yes| G{"Auto-apply enabled for a writable local source?"}
    G -->|No| H["Keep the qualified candidate for review"]
    G -->|Yes| I["Recheck source, approval, and snapshot hashes"]
    I -->|Match| K{"Scheduled publication?"}
    K -->|No| J["Apply atomically and retain rollback data"]
    K -->|Yes| N["Apply and probe the native catalog"]
    N -->|Match| Q["Record active hash and rollback history"]
    N -->|Mismatch| R["Restore the previous version"]
    I -->|Mismatch| F
```

One reproducible defect can queue a revision. In `selection: all`, discovered
skills are observational for ordinary per-skill revisions until approved
datasets, fixtures, complete harness settings, and current approval are present.
Scheduled creation can use approved deterministic cases and save a draft before
an independent holdout exists. The optimizer sees approved training evidence
only. A/B validation withholds the independent holdout and protects
previously confirmed behavior across configured runtimes. Executable changes
also require owner-provided tests in the configured offline validation
environment. Scheduled auto-publication additionally requires `auto_apply:
true`, a writable local source, unchanged approval and hashes, and a successful
native catalog check; a failed check rolls back the change. The ordinary
per-skill revision worker promotes atomically with recovery data but does not
run this scheduled catalog probe.

### Scheduled candidates

```mermaid
flowchart LR
    A["Explicit work or hook wake"] --> DUE{"Interval due or batch resuming?"}
    DUE -->|No| WAIT["Keep collecting; no scheduled batch calls"]
    DUE -->|Yes| B["Scan scoped evidence and artifact checks"]
    B --> ANY{"Any eligible batch items?"}
    ANY -->|No| EMPTY["Record empty scan; no model calls"]
    ANY -->|Yes| R{"Approved inputs, models, and qualifying evidence?"}
    R -->|No| WAIT
    R -->|Yes| S{"Confirmed case or reproducible defect?"}
    S -->|No| WAIT
    S -->|Yes| GEN["Generate a candidate in isolation"]
    GEN --> H{"Independent holdout available?"}
    H -->|No| W["Keep draft/proposal; await independent checks"]
    H -->|Yes| E["Paired train/holdout evaluation; holdout withheld from optimizer"]
    E --> RPT["Keep candidate and reports for review"]
    M["Manual draft"] --> ST["stage: save a separate candidate"]
    ST --> VNOW["verify-now: run fresh A/B"]
    VNOW --> ME["Evaluate supplied evals; do not apply"]
    ST --> MREV["Review staged files and hashes"]
    ME --> MREV
    MREV -->|Explicit manual apply| MA["Publish by owner action; verification may be bypassed"]
    MA --> MRES["Manual result; no native catalog probe"]
    MRES --> RB
    RPT -->|Manual revision publication| RST["Stage report candidate with --source"]
    RST --> MREV
    RPT -->|Explicit manual apply for staged ID| MA
    RPT -->|Qualified auto-apply enabled| AC["Recheck approval and source hashes"]
    AC --> CAT{"Native catalog matches?"}
    CAT -->|Yes| Q["Record active hash and rollback history"]
    CAT -->|No| V["Restore previous version"]
    Q --> RB["Audit status and reports; rollback checks current hash"]
```

Manual `apply` is an owner-controlled publication and can bypass verification;
its result reports `applied_manual_unverified`. Run `verify-now` first for a
fresh A/B. Scheduled auto-apply rechecks approval and hashes, then verifies the
native catalog and restores the prior tree if that check fails. `status` exposes
candidate state and revision history; revision reports retain recovery records
for interrupted promotion and rollback. A scheduled revision's report candidate
path is not a staged ID: stage it with `--source` before manual verification or
apply. Scheduled new-skill reports already contain a staged candidate ID.
