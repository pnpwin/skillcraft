# Codex runner

SkillCraft uses Codex CLI and its local App Server over stdio. Install the
shared skill in `.agents/skills/skillcraft/` and keep experiment workspaces outside
the skill directory.

## Models and authentication

Pass a Codex model ID and optional reasoning effort as `--model 'MODEL#EFFORT'`.
The same syntax applies to `--judge-model` and `--optimizer-model`. Choose
available models for your Codex login. SkillCraft detects a nonempty
`CODEX_THREAD_ID` from a Codex shell. Pass `--harness codex` in a plain terminal,
in CI, or when both harnesses are detected.

Provide an authorized Codex profile with a file-backed login using
`SKILLCRAFT_CODEX_HOME`. Only its `auth.json` is copied to a mode-0700 temporary
directory for the App Server and removed at shutdown. Alternatively, pass
`CODEX_API_KEY` in the runner's environment. Keep credentials out of skill
files and experiment workspaces.

## Evaluation and evidence

The runner starts a fresh ephemeral thread for each case. It verifies the staged
`.agents/skills/<id>/SKILL.md` path through `skills/list`. A baseline preflight
rejects a leaked candidate. Quality runs attach the exact skill with a native
App Server input. Independent graders use structured output and read-only access.
The SkillCraft permission profile gives the executor read access to the staged
workspace and write access to `outputs/`. The model's tools run without network
access. OpenCode `--profile` files are specific to that adapter.

On macOS, a parent Codex sandbox can prevent the App Server from creating its
own sandbox. If the runner reports this, launch the evaluation from a regular
terminal or authorize the evaluation command through the parent harness. The
child App Server applies the SkillCraft permission profile either way.

Trigger runs send the original task prompt and collect
`codex.skill_invocation` on a loopback OTLP endpoint. A matching implicit event
is bound to the skill and turn. A completed turn with healthy telemetry and no
match is a negative. When observation is unavailable, `triggered` is null and the
optimizer stops. Invocation and task
quality are measured separately.

The run directory records final response, transcript, timing, usage and
discovery/attachment evidence. Token and cost fields stay null when Codex does
not supply them. See [App Server](https://learn.chatgpt.com/docs/app-server)
for the protocol and [the configuration guide](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
for OTLP settings.
