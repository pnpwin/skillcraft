# Critical assumptions ledger

Experiment / candidate / frozen rubric: …

Use only for claims that change correctness, scope, data/state, exit-status meaning
or required capabilities. Ordinary background prose does not require a probe.

| Claim and consequence if false | Installed version / executable hash | Documentation URL and revision | Source revision inspected | Independent fixture/oracle | Command, cwd, scoped state and raw evidence path | Observation | Evidence status | Limits / unresolved question |
|---|---|---|---|---|---|---|---|---|
| … | … | … | … | … | … | … | unresolved | … |

Statuses describe evidence, not confidence: `documented`, `source-inspected`,
`behavior-verified`, `unresolved`. Keep separate rows or columns when multiple
evidence types exist. Do not mark a source revision inspected merely because a
version string matches it. A successful exit is not necessarily a successful task.

Before freezing a rubric:
1. Derive expected behavior from an independently constructed fixture and task
   requirements, not the draft skill's explanation.
2. Try the smallest counterexample that would disprove the consequential claim.
3. Record actual commands/cwd/version/output and the state isolation used.
4. Resolve contradictions or retain them as explicit limits; preserve the old draft
   and rubric before a correction. Repeat the relevant probe before the pilot.

Reviewer challenge: could the same wrong sentence copied into skill and rubric
pass this procedure? If yes, the oracle is still circular. A behavioral walkthrough
validates the procedure's applicability, not a measured improvement in author behavior.
