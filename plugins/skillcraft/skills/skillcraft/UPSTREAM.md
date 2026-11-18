# Provenance

**SkillCraft** is published as `pnpwin/skillcraft`. This file records the
Anthropic source and the original OpenCode adaptation.

- Source: https://github.com/anthropics/skills/tree/b9e19e6f44773509fbdd7001d77ff41a49a486c1/skills/skill-creator
- Upstream revision: `b9e19e6f44773509fbdd7001d77ff41a49a486c1`.
- Original license: Apache-2.0; see the preserved `LICENSE.txt`.
- Adaptation: OpenCode V2 API runner, Python orchestration, corrected measurement
  contracts, isolated file-task profile, and version-bound review feedback.
- `references/prompts/`, `scripts/generate_review.py`, the HTML under `assets/`, and packaging code are
  derived from upstream and modified for OpenCode. Other runtime scripts and the
  TypeScript bridge implement the OpenCode-specific adaptation.
- OpenCode compatibility target: tag `v2.2.6`, commit
  `dc2ffcf8bf26f9e669b6aa22b95f50e12be15e06`.
- The actual server's `/openapi.json`, the installed `@opencode/client@2.2.6`
  types, and live smoke checks are the implementation contract.

This adaptation does not claim that the toy smoke benchmark proves effectiveness
on arbitrary skills. It verifies the evaluation workflow and records its limits.
