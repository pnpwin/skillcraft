# SkillCraft workflows

See the [configuration reference](configuration.md) for flags, defaults, and
supported combinations.

## Quality A/B

`run_quality_eval.py` creates a new workspace and freezes the inputs, rubric,
settings, and skill snapshots before running any arms.

```mermaid
flowchart TD
    A["Select the runtime and freeze the evaluation inputs"] --> B["Plan a clean baseline and a candidate arm"]
    B --> C{"Previous version supplied?"}
    C -->|Yes| D["Add the previous version as a third arm"]
    C -->|No| E["Run the end-to-end pilot"]
    D --> E
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

Use [project or home scope](learning.md) to choose the journal. Linked sources
can contribute evidence and receive candidate reports. Their source files are
read-only to Learning promotion and rollback.

```mermaid
flowchart TD
    A{"Scope enabled for this configuration?"} -->|No| B["Skip Learning"]
    A -->|Yes| C{"Selection mode"}
    C -->|explicit| D["Use the enrolled skill list"]
    C -->|all| E["Discover eligible project or home skills and apply exclusions and approved linked roots"]
    D --> F{"Enabled source"}
    E --> P{"Home scope with an active or configured Hermes profile?"}
    P -->|Yes| Q["Use that profile's Hermes skills directory"]
    P -->|No| F
    Q --> F
    F -->|Completed Codex or OpenCode chat| G["Verify the skill version and record eligible feedback"]
    F -->|Completed Hermes chat| H["Record verified feedback in its profile journal"]
    F -->|Completed A/B or reviewed feedback| I["Import verified evidence for the matching runs"]
    H --> J{"Automatic forwarding enabled or manual import requested?"}
    J -->|No| K["Keep evidence in the Hermes profile journal"]
    J -->|Yes, with shared enrollment| L["Verify the same selected source and version, then deduplicate into the shared journal"]
    G --> M["Queue a check at a message or model-step threshold"]
    I --> N["Queue a check when new qualifying evidence arrives"]
    L --> O["Check shared thresholds and newly imported evidence"]
```

Unverified chat use stays diagnostic. Feedback on `without_skill` is attached
to the A/B run without rating the skill or triggering a revision by itself.
Learning's own A/B runs do not feed back into the journal.

```mermaid
flowchart TD
    A["Process a queued check"] --> B{"Enough independent evidence and approved datasets and models?"}
    B -->|No| C["Retain the evidence and skip generation"]
    B -->|Yes| D["Generate a candidate in an isolated snapshot"]
    D --> E{"Approved checks and controlled A/B pass?"}
    E -->|No| F["Record the outcome without applying"]
    E -->|Yes| G{"Auto-apply enabled for a writable local source?"}
    G -->|No| H["Keep the qualified candidate for review"]
    G -->|Yes| I["Recheck source, approval, and snapshot hashes"]
    I -->|Match| J["Apply atomically and retain rollback data"]
    I -->|Mismatch| F
```

Generation requires three independent observations or a correction backed by a
reproducible failure. The optimizer sees approved training evidence. The A/B
worker protects holdout and previously confirmed behavior across configured
runtimes. Executable changes also require owner-provided tests in the configured
offline validation environment.
