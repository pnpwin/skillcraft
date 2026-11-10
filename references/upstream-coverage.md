# Upstream coverage ledger

Source: Anthropic skill-creator revision `b9e19e6f44773509fbdd7001d77ff41a49a486c1`.
Line ranges refer to its SKILL.md (485 lines). Code-fence headings inside examples
are part of Writing Patterns, not independent omitted chapters.

This ledger records semantic coverage, not a token budget. Full explanations and
examples must survive adaptation. Read the relevant guides before performing work.

Restoration status: methodology and executable losses below have been implemented.
Sequential acceptance evidence is kept in the workspace's `restoration/REVIEW.md`;
the final integration gate separately records actual model and UI checks.

| Source section | Local destination | Disposition / completion gate |
|---|---|---|
| Overview and flexible entry into workflow (6–31) | SKILL.md, authoring-guide | Preserve iteration, description-only work and user's preferred starting point |
| Communicating with the user (32–44) | authoring-guide: Communication | Restore vocabulary adaptation and short explanations |
| Capture Intent (47–55) | authoring-guide: Capture intent | Restore history extraction, four core questions and choice of evaluation |
| Interview and Research (56–61) | authoring-guide: Interview | Restore formats, edge cases, dependencies, research and minimal user burden |
| Write SKILL.md (62–70) | authoring-guide: Description | Restore concrete activation guidance; OpenCode directory ID semantics |
| Anatomy (73–85) | authoring-guide: Anatomy | Preserve directory example, scripts/references/assets and their purposes |
| Progressive Disclosure (86–110) | authoring-guide: Resource organization | Preserve domain-specific routing example and navigation; remove arbitrary pressure to shorten useful guidance |
| Principle of Lack of Surprise (111–114) | authoring-guide: Predictable behavior | Preserve user-intent alignment and instruction integrity |
| Writing Patterns (115–136) | authoring-guide: Patterns | Restore report template and input/output commit-message example |
| Writing Style (137–140) | authoring-guide: Style | Preserve imperative wording, rationale, and useful examples |
| Test Cases (141–162) | evaluation-and-iteration: Choose evaluation | Restore realistic small initial set, user agreement, concrete eval example |
| Parallel with-skill/baseline runs (163–198) | evaluation-and-iteration: Paired runs; run_quality_eval.py | Adapt subagents to fresh API sessions; preserve previous snapshot and same-input comparison |
| Draft assertions (199–206) | evaluation-and-iteration: Rubric | Preserve useful, discriminating criteria; freeze before comparison instead of adapting to observed outputs |
| Timing capture (207–220) | runner, schemas, evaluation-and-iteration | Preserve real measurements; API telemetry replaces notifications; unknown is null |
| Grade/aggregate/viewer (221–252) | grade.py, aggregator, viewer, evaluation guide | Restore critique as executable contract, preserve evidence and anomaly analysis |
| Reviewer experience (253–266) | viewer, evaluation guide | Preserve outputs, benchmark and free-text feedback; include errors and critique |
| Read feedback (267–291) | evaluation guide, version-bound feedback | Preserve interpretation/iteration; empty comment alone is not approval; stop only own viewer process |
| Improving / how to think (292–307) | evaluation-and-iteration: Improvement | Restore generalization, transcript analysis, why, repeated-helper scripts and deliberate revision |
| Iteration loop (308–324) | evaluation guide, quality runner | Preserve repeated comparison and stopping discussion; no fabricated human approval |
| Blind comparison (325–332) | compare.py, evaluation guide | Preserve randomized A/B and post-hoc analysis with source identities revealed afterwards |
| Trigger queries (333–359) | evaluation guide: Natural triggering | Restore realistic positive/negative/near-miss examples and diverse wording |
| User review of queries (360–374) | review_evals.py, eval_review.html, evaluation guide | Preserve review/edit/export before optimization; IDs and groups added |
| Optimization loop (375–395) | run_loop.py, improve_description.py, report | Restore previous attempts and visual matrix; holdout renamed validation; add untouched final test |
| Trigger mechanism (396–401) | evaluation guide, runner reference | Explain native catalog/load; model-specific undertriggering is an empirical question |
| Apply result (402–407) | evaluation guide, selected snapshot | Preserve review of best description; validate quality before installation |
| Package and Present (408–419) | package_skill.py, SKILL.md | Preserve deliverable and usage instructions; native OpenCode installation is directory-based |
| Claude.ai adaptation (420–444) | evaluation guide: Capability-aware workflow | Preserve fallback to manual review when automation unavailable; do not claim Claude runtime support |
| Cowork adaptation (445–458) | evaluation guide: Capability-aware workflow | Preserve artifact-first review, capability checks and non-interactive/static review |
| Reference files and final checklist (459–485) | SKILL.md, references, completion checklist | Preserve grader/comparator/analyzer navigation and complete handoff |

## Executable losses tracked separately

| Loss | Restoration stage | Acceptance |
|---|---|---|
| Grader critique ignored by orchestration | 5 | Validated eval_feedback saved and rendered |
| Optimizer previous attempts not supplied | 6 | Training-only payload contains earlier candidates/results/rationale |
| Visual optimization report replaced by raw JSON | 8 | Query × candidate matrix, errors, repeats, train/validation/test |
| Time/token comparison missing | 7 | Paired deltas with per-metric sample counts |
| Aggregator CLI ignores report errors | 1 | Nonzero for infrastructure/validation errors, zero for valid quality failures |

## Deliberate platform corrections

Do not restore Claude CLI calls, temporary command-based trigger proxies, output
characters as tokens, test-set-driven optimization, or killing another process
on a viewer port. Keep native API loading, honest unknowns, independent final test,
owned-process cleanup and the retained upstream LICENSE. Tool availability is a
profile decision, not an excuse to omit the authoring methodology.
