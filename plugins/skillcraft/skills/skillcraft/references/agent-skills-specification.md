# Agent Skills conformance and OpenCode compatibility

## Authority and scope

Normative source: <https://agentskills.io/specification>, pinned to repository
revision `83021e2be457a9b6679148719c1f6074f0ad975e`,
`docs/specification.mdx`. Do not silently change this contract during an experiment.
The source SHA-256 is
`b9079c0c10b7930e8c6a20ff2bc10cda2a3343c55185120e3f1116a1a529b220`.

Two independent requirements apply to one skill directory:

1. Strict Agent Skills conformance of the artifact.
2. Verified compatibility of that artifact with the stated OpenCode environment.

OpenCode 2.2.6 accepts some artifacts that do not conform to Agent Skills. Its
permissive loader is not an exception to the format contract. Conversely, correct
frontmatter does not establish discovery, tool availability, or successful execution.
Quality and natural triggering provide additional evidence; they do not replace
either requirement. A baseline can be measured without becoming eligible for release.

## Required format

A skill is a directory containing `SKILL.md`, with a YAML frontmatter mapping and
a Markdown body. `scripts/`, `references/`, and `assets/` are optional. Additional
additional directories may be included as needed.
Do not delete useful resources merely because they are not one of the example
directories. References should be directly discoverable from `SKILL.md`.

| Field | Required | Contract |
|---|---|---|
| `name` | Yes | Nonempty string, 1–64 characters; lowercase Unicode alphanumeric characters and hyphens; no leading/trailing hyphen or consecutive `--`; matches the parent directory name |
| `description` | Yes | Nonempty string, at most 1024 characters; explains purpose and when to use the skill |
| `license` | No | String naming the license or referring to an included license file |
| `compatibility` | No | String of 1–500 characters when present; actual environment requirements |
| `metadata` | No | Mapping from string keys to string values |
| `allowed-tools` | No | Space-separated tool string; experimental, interpreted by supporting clients |

Use optional fields only when applicable. Do not invent licensing terms, runtime
support or tool capabilities. Custom properties belong in namespaced metadata;
the supported top-level field set is the six fields above. A declared tool list
does not grant permissions or prove that the client provides those tools.

Length applies to the parsed Unicode string, not UTF-8 bytes or YAML quoting.
Whitespace-only mandatory fields are not meaningful descriptions or names.
Name spelling must match the directory; do not silently repair or normalize the
file while validating it. Prefer ASCII lowercase kebab-case for portable new names,
without treating that recommendation as a ban on valid Unicode names.

## Frontmatter examples and field selection

### Minimal portable skill

The directory in this example is `report-writer/`:

```yaml
---
name: report-writer
description: Draft evidence-based reports from supplied findings. Use when the user needs a structured analysis report with conclusions and recommendations.
---
```

Follow the frontmatter with the actual operating instructions. Do not add metadata
merely to fill a template. The name is an identifier; a human-readable heading such
as `# Report Writer` belongs in the Markdown body and does not relax the name rule.

### Extended example with applicable optional fields

For a directory `receipt-normalizer/` that genuinely bundles an Apache-2.0 licensed
Python helper and requires Python 3.11 or later:

```yaml
---
name: receipt-normalizer
description: Normalize supplied receipts into structured records. Use for extracting merchant, payment total and currency from receipt text; not for drafting receipt-request emails or converting currencies.
license: Apache-2.0
compatibility: Requires Python 3.11 or later to run the bundled normalization helper.
metadata:
  example.org/author: Example Team
  example.org/version: "1.0"
  example.org/reviewed: "true"
---
```

This example states its assumptions; do not copy its license or Python requirement
into unrelated skills. A license reference such as `license: See LICENSE.txt` is
also useful when the file is actually bundled. Preserve existing attribution and
license files. If authorship/licensing is unknown, ask rather than inventing it.

`compatibility` describes a real product, runtime, dependency or network requirement
in 1–500 characters. It does not prove compatibility or configure the environment.
Keep detailed setup commands in the body or references; retain all necessary detail
there instead of overloading this field.

### Experimental tool declaration

```yaml
allowed-tools: Bash(git:*) Bash(jq:*) Read
```

This illustrates the specification's space-separated string form, not portable
tool names across every client. Verify the target client's interpretation before
using a declaration. OpenCode permissions are configured and verified separately.
Do not put a YAML list in this field, assume that a declared tool is installed, or
grant tool permissions just because the file lists them. Omit the field when it
does not have a useful, supported meaning for the target skill.

### Invalid examples and corrections to propose

| Invalid input | Why it fails | Appropriate proposal |
|---|---|---|
| Missing `name` or `description` | Both fields are required | Derive a truthful value from the agreed task; do not treat loader defaults as standard compliance |
| `name: Skill Craft` | Spaces and uppercase; differs from `skillcraft/` | Propose `name: skillcraft`; retain readable title in the body |
| `name: -reports`, `reports-`, `two--words` | Invalid hyphen positions/repetition | Propose a valid identifier and consistent directory |
| `name: receipts` in `invoice-reader/` | Name-directory mismatch | Agree the identity; do not rename paths without considering users and references |
| `description: "   "` | No meaningful description | Write purpose and intended activation contexts |
| A parsed description of 1025 characters | Exceeds the 1024-character limit | Preserve original; reformulate and run the agreed blind A/B workflow |
| `compatibility: ""` or a 501-character value | Outside 1–500 characters | Omit when inapplicable, or propose an accurate concise requirement with full setup retained elsewhere |
| `metadata: [a, b]` | Not a mapping | Use applicable string key/value pairs |
| `metadata: {version: 1.0}` | YAML parses the value as numeric under typical typed loaders | Quote `"1.0"`; don't claim a coercing parser proves correct source types |
| `metadata: {enabled: true}` | Value is a boolean under the project's YAML parser | Propose `"true"` only after checking any consuming client's semantics |
| `metadata: {owner: {name: team}}` | Nested object is not a string value | Choose an appropriate textual property rather than assuming nested schema support |
| `allowed-tools: [Read, Bash]` | List instead of string | Use the supported client's space-separated declaration, if applicable |
| A top-level `author:` property | Not one of the supported top-level fields | Use an appropriately namespaced metadata property |

These are proposed repairs, not permission for the validator to edit input files.
The current compliance-migration project requires review of the exact existing-
frontmatter/instruction diff under its agreed plan. This project-specific approval
rule is not an extra approval ceremony for every future skill-authoring task.

## YAML types, lengths and identity

Quote values whose intended type is a string but whose YAML spelling looks like a
boolean, number, date or null. For example, `"true"`, `"1.0"`, `"2026-11-10"` and
`"null"` remain text. Apply the same care to metadata keys. Do not use implicit
coercion to manufacture conformance after parsing. Reject unsupported YAML tags and
ambiguous duplicate keys with useful diagnostics rather than selecting a value
silently. Normal YAML strings and block scalars remain valid representations.

```yaml
description: >-
  Extract totals from supplied receipt text.
  Use for purchase-record normalization, including indirect expense-entry requests.
metadata:
  example.org/version: "1.0"
  example.org/enabled: "false"
```

The folded description is one parsed string. Count its characters after YAML
parsing, including preserved whitespace/newlines, not indentation or quote syntax.
`>-` strips the final newline; `>` generally retains it. Do not silently strip
characters merely to fit the limit. Use reproducible Unicode code-point counting
and record the measured value. Name limits are also character limits; avoid using
UTF-8 byte length as an accidental stricter rule for non-ASCII names.

The specification permits lowercase Unicode alphanumeric names. The reference
implementation applies NFKC and uses Unicode `isalnum()` plus lowercase checks.
Those details can make visually similar spellings compare alike in that library.
Prefer exact identifier/directory spelling, generate ASCII lowercase kebab-case
unless the task requires otherwise, and do not silently rename Unicode files.
Document differential behavior instead of asserting that all clients normalize
identically. Any unresolved normative ambiguity must be identified before enforcing
a new exclusion; portability advice is not an invented mandatory restriction.

## Structure and resource navigation

```text
report-writer/
├── SKILL.md
├── scripts/                 # verified recurring executable work, if useful
├── references/              # complete instructions or domain knowledge
├── assets/                  # output templates and other supporting resources
└── tests/                   # optional development resources, also permitted
```

Use relative resource references resolved from the skill directory. Link important
references directly from `SKILL.md` and state when to read them. The recommendation
to keep reference navigation one level deep is about avoiding hidden prerequisite
chains, not a prohibition on legitimate nested folders. A reference can contain
the full detailed method without forcing every unrelated task to load it.

SkillCraft itself intentionally requires its complete guides. For skills
it creates, choose task-specific routing: for example, require the full selected
cloud-provider guide before deployment, or the full error-handling section after
the specified error condition. Do not replace required instructions with a short
summary or remove examples to meet an arbitrary length target.

## Recommendations are not format errors

The documentation recommends a main file under 500 lines and 5000 tokens, direct
reference navigation, and loading supporting material when needed. These are
authoring recommendations, not permission to remove useful content automatically.
The format imposes no fixed body template or universal rubric for task quality.

SkillCraft deliberately requires full reading of its authoring and evaluation
guides. Preserve this choice. Skills it creates should use explicit conditions for
loading resources and full reading of the relevant required sections. Organizing
material and deleting material are different operations.

## Reference implementation: comparison, not authority

The pinned `skills-ref/README.md` explicitly labels the library demonstration-only,
not for production. Use it for differential checks while enforcing the written
contract independently. Relevant known differences in this revision:

- `validate_metadata()` checks name, description, compatibility and top-level keys,
  but does not enforce all types for license, allowed-tools or nested metadata.
- It accepts empty compatibility, despite the specification's 1–500 requirement.
- Its StrictYAML parser produces/coerces string metadata values. A direct call with
  already-parsed boolean metadata can pass without proving string→string conformance.
- Name validation applies trimming and NFKC normalization before comparison; those
  implementation conveniences must not silently mutate or repair the source artifact.
- Its parser also accepts lowercase `skill.md`; the portable artifact specified
  here must contain the documented `SKILL.md` filename.

Document differential-test discrepancies with their reason. Do not weaken a rule
merely to make two implementations return the same result, or claim full reference
CLI validation when only an internal function was exercised.

## Independent, symmetric results

Both checks use exactly `PASS`, `FAIL`, `NOT_RUN`, and `ERROR`:

| Status | Meaning |
|---|---|
| PASS | The required checks completed and all requirements passed |
| FAIL | Completed checks established a concrete nonconformance |
| NOT_RUN | The check has not been performed; no conclusion is available |
| ERROR | A technical problem prevented completion; no complete conclusion is available |

Missing frontmatter is a format FAIL. An unreadable artifact is a validation ERROR.
A source accepted by the loader but rejected by the standard retains both results.
If required checks abort after partial findings, preserve those findings while
reporting the incomplete check as ERROR. Never convert missing/legacy evidence to PASS.

Acceptance requires both results to be PASS for the same artifact and applicable
environment. Static OpenCode checks are supporting observations, not a live
compatibility verdict. Record skill hash, contract revision, runtime version,
profile/environment identity and evidence paths. An old PASS is not automatically
valid for changed instructions, resources or execution conditions.

## OpenCode-specific interpretation

The primary runtime source is <https://opencode.ai/docs/skills/>. Live checks
used OpenCode 2.2.7. Each release requires fresh runtime evidence for the observed
version. The probes below describe behavior observed with OpenCode 2.2.6.

OpenCode derives the ID from the path and treats `name` as a display label; runtime
frontmatter may be optional and descriptions may exceed 1024 characters. A release
under this workflow must nevertheless obey Agent Skills. Inform the judge which
requirement is being assessed rather than letting one contract replace the other.

For platform metadata, verify strings, legacy boolean values, absence and both
enabled/disabled behavior before changing existing types. Record advertised and
explicit loading separately. Do not change global plugins or silently broaden
permissions to obtain a passing label. Custom profiles remain uncontrolled unless
independent isolation is actually established.

## Implementation coverage ledger

This is a requirements-to-evidence map, not a claim that planned work already passes.
Stages refer to the historical `skill-creator-agent-skills-compliance-plan.md`,
written before the SkillCraft rename; its original title and evidence are retained.

| Requirement | Documentation | Implementation target | Verification/evidence target | Stage |
|---|---|---|---|---|
| Required frontmatter and SKILL.md | This reference; main field table | quick_validate / parser | missing/malformed/mapping cases | 2–3 |
| Name format, length, directory | This reference; field examples | strict validator | 1/64/65, hyphens, case, Unicode, mismatch | 2–3 |
| Description nonempty and ≤1024 | Main table; optimization guide | validator / optimizer | 1024/1025 and scalar cases | 2–3, 6 |
| License string / actual licensing | Field examples | validator / authoring | optional/type cases; migration diff | 2–4 |
| Compatibility 1–500 / actual dependencies | Field examples | validator / authoring | empty, 1/500/501, types | 2–4 |
| String→string metadata | YAML examples; runtime guide | validator / runtime probes | nested/nonstring negatives; live interpretation | 2–4 |
| allowed-tools / real permissions | Field examples; runtime guide | validator / runtime probes | type checks; capability evidence | 2–4 |
| Unsupported top-level fields | Field examples | validator | unknown key versus custom metadata | 2–3 |
| Symmetric status/provenance | schemas.md | validation / orchestration | status matrix; stale/absent evidence | 1, 3–5 |
| Discovery, ID, native load | Runtime guide | existing API runner | same-snapshot live probes | 4, 8 |
| Tool availability and execution | Runtime guide | explicit profile / runner | representative live task | 4, 8 |
| Candidate selection and package gate | schemas.md; SKILL.md | run_loop / package_skill | runtime-valid but format-invalid rejection | 5 |
| Judge contract separation | grader resource | grade / acceptance | preserve separate verdicts and evidence | 5 |
| Oversized baseline cannot be promoted | Evaluation guide | A/B orchestration | immutable baseline, release rejection | 6 |
| Blind A/B and holdout isolation | Evaluation guide | optimizer / comparator / runner | recorded requests, labels, repeats, verdicts | 6 |
| Whole-guide reading by creator | SKILL.md | authoring instructions | before/after document review | 2, 7 |
| Conditional resource reading in generated skills | Authoring guide | authoring instructions | representative output review | 7–8 |
| Intent and expected-result confirmation | SKILL.md; authoring guide | authoring instructions | incomplete versus already-complete requirements | 7–8 |
| Scripts / defaults / Gotchas / validation loops | Authoring guide | reusable guidance | examples and applicable workflow checks | 7 |
| Standard plus live acceptance from clean package | Verification report | tests / live suite / packaging | E2E, clean install, hashes | 8 |

Live evidence, approval records, reviewer findings and stage completion are saved in
the compliance workspace. Historical restoration experiments keep their original
contracts and verdicts; they are not retroactively relabeled as standard-conformance
tests.

### Implemented coverage and verification entry points

The Agent Skills ledger supplements the Anthropic upstream-coverage.md ledger;
it does not replace or remove that methodology. Stages 1–6 provide the strict
validator (test_standard_validation.py), lifecycle/hash acceptance
(test_compliance.py), live metadata interpretation, actual packaging and oversized
A/B (test_description_reformulation.py and retained live evidence). Stage 7 adds
explicit intent confirmation, applicable defaults/Gotchas/checklists and the full
script-design.md guide without shortening the existing authoring method.

Use quick_validate.py for standard-only checks, compliance.py for two-contract
acceptance, and package_skill.py with a representative --run for distribution.
shrink_description.py owns the narrow oversized-baseline comparison. Stage 8's
final E2E and clean-install report must bind those results to the actual release;
the ledger's presence alone does not certify a future archive.
