import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import codex_runner


class CodexRunnerTests(unittest.TestCase):
    def test_request_requires_object_model_and_absolute_paths(self):
        request = {"schema_version": 1, "harness": "codex", "operation": "run", "mode": "quality",
                   "directory": "/tmp/eval", "output": "/tmp/out", "model": {"id": "gpt-6-codex"},
                   "timeout_seconds": 1}
        codex_runner._validate_request(request)
        with self.assertRaisesRegex(ValueError, "model must be an object"):
            codex_runner._validate_request({**request, "model": "gpt-6-codex"})
        with self.assertRaisesRegex(ValueError, "absolute"):
            codex_runner._validate_request({**request, "directory": "relative"})
        self.assertIsNone(codex_runner._validate_request({**request, "mode": "optimizer"}))

    def test_native_attachment_input_is_structured_and_bound_to_candidate(self):
        request = {"prompt": "Do the evaluation", "attach_skill": True, "skill_id": "demo",
                   "skill_path": "/tmp/skills/demo/SKILL.md", "directory": "/tmp/skills"}
        candidate = {"name": "demo"}
        turn_input = [{"type": "text", "text": request["prompt"]},
                      {"type": "skill", "name": candidate["name"], "path": request["skill_path"]}]
        self.assertEqual(turn_input[1], {"type": "skill", "name": "demo", "path": "/tmp/skills/demo/SKILL.md"})
        messages = codex_runner._messages(request, [{"type": "agentMessage", "text": "Done"}], "thread", "turn")
        self.assertEqual(messages[0]["content"][1], {"type": "skill", "name": "demo", "path": request["skill_path"]})
        self.assertEqual(messages[1]["content"], [{"type": "text", "text": "Done"}])

    def test_otlp_parser_extracts_only_sanitized_skill_invocation_fields(self):
        body = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{
            "body": {"stringValue": "private response must not be retained"},
            "attributes": [
                {"key": "event.name", "value": {"stringValue": "codex.skill_invocation"}},
                {"key": "turn.id", "value": {"stringValue": "turn-1"}},
                {"key": "skill.name", "value": {"stringValue": "demo"}},
                {"key": "skill.invocation_type", "value": {"stringValue": "implicit"}},
                {"key": "account_id", "value": {"stringValue": "must-not-leak"}},
            ]}]}]}]}
        self.assertEqual(codex_runner._find_skill_events(body), [{
            "kind": "skill_invocation", "event_name": "codex.skill_invocation", "turn_id": "turn-1",
            "skill_name": "demo", "implicit": True}])
        safe = codex_runner._safe_event({"method": "item/completed", "params": {
            "threadId": "t", "turnId": "u", "item": {"id": "i", "type": "userMessage",
            "content": [{"type": "text", "text": "secret prompt"},
                       {"type": "skill", "name": "demo", "path": "/skills/demo"}]}}})
        serialized = json.dumps(safe)
        self.assertNotIn("secret prompt", serialized)
        self.assertNotIn("account_id", serialized)
        self.assertIn("/skills/demo", serialized)

    def test_otlp_parser_uses_sse_event_kind_for_healthy_turn(self):
        event = {"attributes": [
            {"key": "event.name", "value": {"stringValue": "codex.sse_event"}},
            {"key": "event.kind", "value": {"stringValue": "response.completed"}},
            {"key": "turn.id", "value": {"stringValue": "turn-2"}},
            {"key": "error.message", "value": {"stringValue": "must not be retained"}},
        ]}
        self.assertEqual(codex_runner._find_skill_events(event), [{
            "kind": "transport_health", "event_name": "codex.sse_event", "event_kind": "response.completed",
            "turn_id": "turn-2", "conversation_scoped": False}])

    def test_response_uses_final_answer_and_keeps_commentary_in_messages(self):
        items = [{"id": "c", "type": "agentMessage", "phase": "commentary", "text": "Thinking"},
                 {"id": "f", "type": "agentMessage", "phase": "final_answer", "text": "{\"score\":1}"}]
        self.assertEqual(codex_runner._final_response(items), '{"score":1}')
        messages = codex_runner._messages({"prompt": "Review", "directory": "/tmp/work"}, items, "thread", "turn")
        self.assertEqual([m["content"][0]["text"] for m in messages[1:]], ["Thinking", '{"score":1}'])

    def test_tool_evidence_is_minimal_and_workspace_relative(self):
        messages = codex_runner._messages({"prompt": "Run it", "directory": "/tmp/work"}, [
            {"id": "cmd-1", "type": "commandExecution", "command": "curl --api-key=secret123 https://example.test",
             "cwd": "/tmp/work/inputs", "status": "completed", "exitCode": 0,
             "aggregatedOutput": "do not keep"},
            {"id": "change-1", "type": "fileChange", "status": "completed",
             "changes": [{"path": "/tmp/work/outputs/report.json", "kind": "add", "diff": "secret diff"}]},
        ], "thread", "turn")
        command = messages[1]["content"][0]
        self.assertEqual(command["state"]["input"]["cwd"], "inputs")
        self.assertEqual(command["state"]["exitCode"], 0)
        self.assertNotIn("secret123", json.dumps(messages))
        self.assertNotIn("do not keep", json.dumps(messages))
        self.assertEqual(messages[2]["content"][0]["state"]["input"]["changes"],
                         [{"path": "outputs/report.json", "kind": "add"}])

    def test_trigger_outcome_distinguishes_healthy_negative_from_missing_telemetry(self):
        self.assertTrue(codex_runner._trigger_result({
            "skill_invocations": [{"skill_name": "demo", "implicit": True}], "transport_health": []}, "demo"))
        self.assertFalse(codex_runner._trigger_result({"skill_invocations": [], "transport_health": [
            {"event_name": "response.completed", "turn_id": "turn"}]}, "demo"))
        self.assertIsNone(codex_runner._trigger_result({"skill_invocations": [], "transport_health": []}, "demo"))

    def test_permission_profile_denies_codex_home_and_scopes_writes(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            home = root / "codex"
            workspace = root / "workspace"
            (workspace / "outputs").mkdir(parents=True)
            home.mkdir()
            binary = Path("/opt/homebrew/bin/codex")
            config_path = codex_runner._configure_home(home, workspace, 43121, "quality", binary)
            config = config_path.read_text()
            self.assertIn('default_permissions = "skillcraft"', config)
            self.assertIn('":workspace_roots" = "read"', config)
            self.assertIn(f'"{binary}" = "read"', config)
            self.assertIn(f'"{workspace / "outputs"}" = "write"', config)
            self.assertIn(f'"{home}" = "deny"', config)
            self.assertIn('enabled = false', config)
            self.assertIn('protocol = "json"', config)

    def test_background_identity_and_configuration_fingerprint_ignore_temp_paths(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            skill_a, skill_b = root / "a/SKILL.md", root / "b/SKILL.md"
            skill_a.parent.mkdir(); skill_b.parent.mkdir()
            skill_a.write_text("---\nname: bg\ndescription: Same\n---\n")
            skill_b.write_text(skill_a.read_text())
            one = codex_runner._environment(None, [{"name": "bg", "description": "Same", "enabled": True,
                                                   "path": str(skill_a)}], True, "0.171.0")
            two = codex_runner._environment(None, [{"name": "bg", "description": "Same", "enabled": True,
                                                   "path": str(skill_b)}], True, "0.171.0")
        self.assertEqual(one["background"], two["background"])
        self.assertEqual(one["configuration_fingerprint"], two["configuration_fingerprint"])

    def test_probe_environment_contract_is_codex_scoped(self):
        with patch.object(codex_runner.subprocess, "run") as command:
            command.return_value.returncode = 0
            command.return_value.stdout = "codex-cli 0.171.0-beta.1\n"
            version = codex_runner._read_cli_version("/tmp/codex", "/tmp", {})
        env = codex_runner._environment(None, [{"name": "background", "enabled": True}], True, version)
        self.assertEqual(env["server_version"], "0.171.0-beta.1")
        self.assertTrue(env["controlled"])
        self.assertIsNone(env["candidate"])
        self.assertEqual(env["background"][0]["id"], "background")
        self.assertIn("plugins", env)
        self.assertIn("configuration_fingerprint", env)


if __name__ == "__main__":
    unittest.main()
