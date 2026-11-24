import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/skillcraft/skills/skillcraft"
sys.path.insert(0, str(SCRIPTS))

from scripts import learning_reflection
from scripts.learning import SCHEMA as JOURNAL_SCHEMA
from scripts.learning_candidates import load_candidate
from scripts.learning_observations import SCHEMA as OBSERVATION_SCHEMA
from scripts.utils import tree_hash


class LearningReflectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.root = base / "project"
        self.root.mkdir()
        self.state = base / "state"
        self.state.mkdir()
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.journal = self.state / "journal.sqlite"
        self.db = sqlite3.connect(self.journal)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(JOURNAL_SCHEMA)
        self.db.executescript(OBSERVATION_SCHEMA)
        self.db.executescript(learning_reflection.SCHEMA)
        self.addCleanup(self.db.close)
        self.config = {
            "root": self.root,
            "state": self.state,
            "scope": "project",
            "skills": {},
            "automation": {
                "create": True,
                "improve": True,
                "auto_apply": False,
                "reflect": True,
                "interval_days": 7,
                "max_candidates_per_batch": 3,
                "harnesses": {"codex": {"model": "offline-test-model"}},
            },
        }

    def add_turn(self, *, index=0, kind="correction", skill=None, version=None,
                 topic="meeting-actions", ambiguous=False, reviewed=True, prompt=None, excerpt=None):
        prompt = prompt or f"Create the meeting action list for scenario {index}."
        excerpt = excerpt or f"How to do it: preserve the owner label for scenario {index}."
        details = {"prompt": prompt, "answer": "The action list is ready."}
        if reviewed:
            details["review_turn"] = f"review-{index}"
        if ambiguous:
            details["ambiguous_use"] = True
        cursor = self.db.execute(
            "INSERT INTO observations(witness,harness,session,turn,kind,topic,skill,version,independent,excerpt,details) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"witness-{index}", "codex", f"session-{index}", f"turn-{index}", kind, topic,
             skill, version, f"family-{index}", excerpt, json.dumps(details)),
        )
        self.db.commit()
        return self.db.execute("SELECT * FROM observations WHERE id=?", (cursor.lastrowid,)).fetchone()

    def proposal(self, row, *, name="meeting-actions", matches=None, old_text="", new_text="",
                 decision="draft"):
        skill_md = (
            "---\nname: meeting-actions\ndescription: Create an action list from meeting notes.\n---\n"
            "# Steps\n\n1. Preserve each owner label.\n"
        ) if not matches else ""
        return {
            "decision": decision,
            "reason": "The correction states a reusable rule.",
            "applies_when": "When preparing a meeting action list.",
            "rule": "Preserve the owner label for each action.",
            "matches": matches or [],
            "source_ids": [row["id"]],
            "name": name if not matches else "",
            "skill_md": skill_md,
            "old_text": old_text,
            "new_text": new_text,
        }

    def fake_model(self, proposal, calls):
        def run_model(prompt, output, model, **kwargs):
            calls.append((prompt, output, model, kwargs))
            output.mkdir(parents=True, exist_ok=True)
            (output / "response.txt").write_text(json.dumps(proposal), encoding="utf-8")
            return {"status": "completed"}
        return run_model

    def reflect_with_model(self, row, proposal, calls, *, batch_id=17, slot="item-1"):
        with (patch("scripts.runtime.require_model_consent"),
              patch("scripts.runtime.run_model", side_effect=self.fake_model(proposal, calls)),
              patch("scripts.learning_worker.approved_runner", return_value=nullcontext())):
            return learning_reflection.reflect(self.config, self.db, row["topic"], [row], batch_id, slot)

    def test_observation_batch_is_bounded_to_six_turns_per_topic(self):
        for index in range(9):
            self.add_turn(index=index)
        planned = learning_reflection.plan(self.db, self.config, existing=[])
        self.assertEqual(1, len(planned))
        self.assertEqual("reflect", planned[0]["operation"])
        self.assertEqual(learning_reflection.MAX_TURNS, len(planned[0]["rows"]))
        self.assertEqual(list(range(1, 7)), [row["id"] for row in planned[0]["rows"]])

    def approve_holdout(self, filename, cases):
        dataset = self.root / filename
        dataset.write_text(json.dumps({"evals": cases}), encoding="utf-8")
        return {"path": filename, "sha256": sha256(dataset.read_bytes()).hexdigest()}

    def test_approved_holdout_is_removed_before_model_and_does_not_use_slots(self):
        private = "Format the independent blue meeting notes."
        other = "Summarize the approved automation-only notes."
        self.config["approval"] = {"meeting-actions": {"datasets": {"holdout": self.approve_holdout(
            "per-skill.json", [{"id": "protected-id", "prompt": private, "group": "family-3"}],
        )}}}
        self.config["automation_inputs"] = {"cases": {"another-topic": {"holdout": self.approve_holdout(
            "scheduled.json", [{"id": "second-id", "prompt": other}],
        )}}}
        by_prompt = self.add_turn(index=1, prompt=f"Extra instructions. {private}", excerpt="Private prompt rule.")
        by_id = self.add_turn(index=2, excerpt="Private ID rule. [skillcraft-case:protected-id]")
        by_group = self.add_turn(index=3, excerpt="Private group rule.")
        by_batch = self.add_turn(index=4, prompt=other, excerpt="Private automation rule.")
        allowed = self.add_turn(index=5)
        planned = learning_reflection.plan(self.db, self.config, existing=[])
        self.assertEqual([[allowed["id"]]], [[row["id"] for row in item["rows"]] for item in planned])

        calls = []
        with (patch("scripts.runtime.require_model_consent"),
              patch("scripts.runtime.run_model", side_effect=self.fake_model(self.proposal(allowed), calls)),
              patch("scripts.learning_worker.approved_runner", return_value=nullcontext())):
            result = learning_reflection.reflect(self.config, self.db, allowed["topic"],
                                                 [by_prompt, by_id, by_group, by_batch, allowed], 17, "item-1")
        self.assertEqual("awaiting_checks", result["status"])
        self.assertEqual(1, len(calls))
        self.assertIn(allowed["excerpt"], calls[0][0])
        for phrase in (private, other, by_id["excerpt"], by_group["excerpt"]):
            self.assertNotIn(phrase, calls[0][0])
        sources = self.db.execute("SELECT source_rows FROM learning_lessons").fetchone()[0]
        self.assertEqual([allowed["id"]], json.loads(sources))

    def test_stale_approved_holdout_blocks_model_before_evidence_is_read(self):
        approval = self.approve_holdout("holdout.json", [{"id": "heldout", "prompt": "Previous version."}])
        self.config["approval"] = {"meeting-actions": {"datasets": {"holdout": approval}}}
        self.approve_holdout("holdout.json", [{"id": "heldout", "prompt": "Changed after approval."}])
        row = self.add_turn()
        calls = []
        result = self.reflect_with_model(row, self.proposal(row), calls)
        self.assertEqual("reflection_preflight_error", result["status"])
        self.assertEqual([], calls)

    def test_reapproved_holdout_does_not_reenter_reflection_via_a_prior_lesson(self):
        earlier = self.add_turn(index=1, prompt="Build the proprietary fixture with independent blue notes.")
        proposal = self.proposal(earlier, decision="skip")
        proposal["rule"] = "Unique old fixture rule."
        self.reflect_with_model(earlier, proposal, [])
        self.config["approval"] = {"meeting-actions": {"datasets": {"holdout": self.approve_holdout(
            "holdout.json", [{"id": "new-holdout", "prompt": json.loads(earlier["details"])["prompt"]}],
        )}}}
        allowed = self.add_turn(index=2)
        calls = []
        result = self.reflect_with_model(allowed, self.proposal(allowed), calls, batch_id=18)
        self.assertEqual("awaiting_checks", result["status"])
        self.assertNotIn(proposal["rule"], calls[0][0])

    def test_one_correction_without_case_stages_text_draft_and_duplicate_reuses_it(self):
        row = self.add_turn()
        calls = []
        result = self.reflect_with_model(row, self.proposal(row), calls)

        self.assertEqual("awaiting_checks", result["status"])
        self.assertTrue(result["candidate_id"].startswith("lesson-"))
        self.assertEqual(1, len(calls))
        candidate, path = load_candidate(candidate_id=result["candidate_id"], state_dir=self.state)
        self.assertEqual("new", candidate["kind"])
        self.assertEqual("staged", candidate["status"])
        self.assertEqual("meeting-actions", candidate["name"])
        self.assertIsNone(candidate["verification"])
        self.assertIn("Preserve each owner label", (path / "SKILL.md").read_text())
        self.assertIsNone(candidate.get("case_id"))

        self.db.commit()
        self.db.close()
        self.db = sqlite3.connect(self.journal)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        duplicate = learning_reflection.reflect(self.config, self.db, row["topic"], [row], 18, "item-1")
        self.assertEqual("already_reflected", duplicate["status"])
        self.assertEqual(result["candidate_id"], duplicate["candidate_id"])
        self.assertEqual(1, len(calls), "duplicate reflection must not spend another model run")
        self.assertEqual([], learning_reflection.plan(self.db, self.config, existing=[]))

    def test_existing_bound_skill_is_patched_and_later_ab_uses_same_staged_id(self):
        source = self.root / ".agents/skills/meeting-actions"
        source.mkdir(parents=True)
        original = (
            "---\nname: meeting-actions\ndescription: Create a meeting action list.\n---\n"
            "# Rules\n\nKeep the owner label.\nKeep the due date.\n"
        )
        (source / "SKILL.md").write_text(original, encoding="utf-8")
        version = tree_hash(source)
        self.config["skills"] = {"meeting-actions": {"source": ".agents/skills/meeting-actions"}}
        row = self.add_turn(skill="meeting-actions", version=version)
        proposal = self.proposal(row, matches=["meeting-actions"], old_text="Keep the owner label.",
                                 new_text="Keep the owner label on every action.")
        calls = []
        result = self.reflect_with_model(row, proposal, calls)
        self.assertEqual("awaiting_checks", result["status"])
        self.assertEqual("meeting-actions", result["name"])
        candidate, path = load_candidate(candidate_id=result["candidate_id"], state_dir=self.state)
        self.assertEqual("revision", candidate["kind"])
        self.assertEqual(str(source), candidate["source_path"])
        self.assertEqual(version, candidate["source_hash"])
        staged_text = (path / "SKILL.md").read_text()
        self.assertIn("Keep the owner label on every action.", staged_text)
        self.assertIn("Keep the due date.", staged_text)

        from scripts.learning_reflection import evaluate
        captured = []
        def create_candidate(config, db, name, settings, rows, batch_id, *, staged_id=None):
            captured.append((name, staged_id))
            return {"status": "awaiting_checks", "staged_id": staged_id}
        lesson_id = result["lesson_id"]
        with patch("scripts.learning_batch.create_candidate", side_effect=create_candidate):
            evaluated = evaluate(self.config, self.db, lesson_id, {}, [row], 19)
        self.assertEqual("awaiting_checks", evaluated["status"])
        self.assertEqual([("meeting-actions", result["candidate_id"])], captured)
        lesson = json.loads(self.db.execute(
            "SELECT lesson FROM learning_lessons WHERE id=?", (lesson_id,)
        ).fetchone()["lesson"])
        self.assertEqual("awaiting_checks", lesson["verification_status"])

    def test_opencode_empty_catalog_decides_matches_without_the_model(self):
        self.config["automation"]["harnesses"] = {"opencode": {"model": "openai/test-model"}}
        row = self.add_turn()
        proposal = self.proposal(row)
        proposal["matches"] = ["Preserve the exact owner label in action lists."]
        calls = []

        result = self.reflect_with_model(row, proposal, calls)

        self.assertEqual("awaiting_checks", result["status"])
        self.assertEqual([], result["targets"])
        schema = calls[0][3]["output_schema"]
        self.assertNotIn("matches", schema["required"])
        self.assertNotIn("matches", schema["properties"])
        candidate, _ = load_candidate(candidate_id=result["candidate_id"], state_dir=self.state)
        self.assertEqual("new", candidate["kind"])

    def test_opencode_unknown_match_is_rejected_when_catalog_has_a_skill(self):
        self.config["automation"]["harnesses"] = {"opencode": {"model": "openai/test-model"}}
        source = self.root / ".agents/skills/existing"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: existing\ndescription: Create an action list from meeting notes.\n---\n",
            encoding="utf-8",
        )
        row = self.add_turn()
        proposal = self.proposal(row)
        proposal["matches"] = ["Preserve the exact owner label in action lists."]
        calls = []

        result = self.reflect_with_model(row, proposal, calls)

        self.assertEqual("blocked", result["status"])
        self.assertIn("matches", calls[0][3]["output_schema"]["required"])
        self.assertIsNone(self.db.execute("SELECT candidate_id FROM learning_lessons").fetchone()[0])

    def test_ambiguous_attribution_and_missing_signal_do_not_call_model(self):
        ambiguous = self.add_turn(ambiguous=True)
        calls = []
        result = self.reflect_with_model(ambiguous, self.proposal(ambiguous), calls)
        self.assertEqual("needs_review", result["status"])
        self.assertEqual([], calls)

        missing = self.add_turn(index=1, reviewed=False, excerpt="Thanks.", prompt="One-off instruction.")
        no_signal = learning_reflection.reflect(self.config, self.db, missing["topic"], [missing], 20, "item-2")
        self.assertEqual("no_evidence", no_signal["status"])
        self.assertEqual([], calls)

    def test_new_skill_catalog_stays_inside_project_and_home_scope(self):
        personal = self.home / ".agents/skills/private-guide"
        personal.mkdir(parents=True)
        (personal / "SKILL.md").write_text(
            "---\nname: private-guide\ndescription: A home skill.\n---\nHome content.\n", encoding="utf-8")
        sibling = self.root.parent / "other-project"
        other = sibling / ".agents/skills/other-guide"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text(
            "---\nname: other-guide\ndescription: A sibling skill.\n---\nOther content.\n", encoding="utf-8")
        row = self.add_turn()
        proposal = self.proposal(row)
        calls = []
        result = self.reflect_with_model(row, proposal, calls)
        self.assertEqual("awaiting_checks", result["status"])
        prompt = calls[0][0]
        self.assertIn('"private-guide"', prompt)
        self.assertNotIn('"other-guide"', prompt)

    def test_scheduled_batch_reuses_draft_and_withholds_holdout_from_model(self):
        from scripts.learning_batch import run_batch
        from scripts.hermes_runner import validate_output_schema

        row = self.add_turn()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # This test exercises scheduling and the real verification handoff.
        # Native runner outputs are covered by the separate harness checks.
        self.config["automation"]["harnesses"] = {
            "hermes": {"model": "test:model"}, "codex": {"model": "test-model"},
            "opencode": {"model": "test/model"},
        }
        source = self.config["root"]
        approved = {}
        for split in ("train", "holdout"):
            case = {"id": split, "prompt": f"Independent {split} request",
                    "expectations": ["owner present"]}
            content = json.dumps({"evals": [case]}).encode()
            dataset = source / f"{split}.json"
            dataset.write_bytes(content)
            approved[split] = {"path": dataset.name, "sha256": sha256(content).hexdigest(), "fixtures": {}}

        calls = []
        with (patch("scripts.runtime.run_model", side_effect=self.fake_model(self.proposal(row), calls)),
              patch("scripts.learning_worker.approved_runner", return_value=nullcontext())):
            draft = run_batch(self.config, self.db, update=self.fail, reload_config=lambda: self.config, now=now)
            candidate_id = draft["items"][0]["result"]["candidate"]["candidate_id"]
            self.assertEqual("awaiting_checks", draft["status"])
            self.assertEqual(1, len(calls))
            self.assertNotIn("Independent holdout request", calls[0][0])
            self.assertIn("Return exactly one JSON object", calls[0][0])
            self.assertIn('"source_ids"', calls[0][0])
            proposal = self.proposal(row)
            del proposal["matches"]
            validate_output_schema(proposal, calls[0][3]["output_schema"])
            self.assertEqual([], learning_reflection.plan(self.db, self.config, existing=[]))

            self.config["automation_inputs"] = {"cases": {"meeting-actions": approved}}
            def verify(**kwargs):
                self.assertEqual(candidate_id, kwargs["candidate_id"])
                self.assertEqual(["train", "holdout"], [case["id"] for case in
                                 json.loads(kwargs["evals"].read_text())["evals"]])
                return {"trusted_pass": True, "reason": "passed", "candidate": {
                    "verification": {"quality_evidence": {"improved": [{"case_id": "holdout"}]},
                                     "workspace": str(kwargs["workspace"])}}, "report": {}}

            matrix = {("holdout", "owner present"): {"with_skill": [True] * 3}}
            trigger_harnesses = []

            def trigger(_source, _evals, workspace, _model, *, harness, **_kwargs):
                trigger_harnesses.append(harness)
                workspace.mkdir()
                (workspace / "report.html").write_text("<p>Native selection</p>", encoding="utf-8")
                return {"summary": {"controlled": True, "tp": 1, "tn": 1, "fp": 0,
                                    "fn": 0, "valid": 2, "errors": 0}}

            with (patch("scripts.learning_candidates.verify_candidate", side_effect=verify) as verify_model,
                  patch("scripts.learning_candidates._check_matrix", return_value=(matrix, None)),
                  patch("scripts.run_eval.run_eval", side_effect=trigger)):
                result = run_batch(self.config, self.db, update=self.fail,
                                   reload_config=lambda: self.config, now=now.replace(day=9))
            self.assertEqual(3, verify_model.call_count)
            self.assertEqual(["hermes", "codex", "opencode"], trigger_harnesses)
            self.assertEqual(1, len(calls))
        self.assertEqual("qualified", result["items"][0]["result"]["status"])
        self.assertFalse((source / ".agents/skills/meeting-actions").exists())

    def test_conflicting_lesson_stops_pending_automatic_verification(self):
        first = self.add_turn()
        calls = []
        self.reflect_with_model(first, self.proposal(first), calls)
        second = self.add_turn(index=1, excerpt="Always leave the owner field blank.")
        self.config["automation_inputs"] = {"cases": {"meeting-actions": {"train": {}, "holdout": {}}}}
        self.assertEqual(["reflect"], [item["operation"] for item in
                         learning_reflection.plan(self.db, self.config, existing=[])])
        conflict = self.reflect_with_model(second, self.proposal(second, decision="conflict"), calls, batch_id=18)
        self.assertTrue(conflict["contradiction"])
        self.assertEqual([], learning_reflection.plan(self.db, self.config, existing=[]))

    def test_preflight_failure_does_not_consume_feedback(self):
        from scripts.learning_batch import run_batch

        row = self.add_turn()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with (patch("scripts.runtime.run_model", return_value={"status": "preflight_error"}),
              patch("scripts.learning_worker.approved_runner", return_value=nullcontext())):
            blocked = run_batch(self.config, self.db, update=self.fail, reload_config=lambda: self.config, now=now)
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM learning_lessons").fetchone()[0])
        calls = []
        with (patch("scripts.runtime.run_model", side_effect=self.fake_model(self.proposal(row), calls)),
              patch("scripts.learning_worker.approved_runner", return_value=nullcontext())):
            retried = run_batch(self.config, self.db, update=self.fail,
                                reload_config=lambda: self.config, now=now.replace(day=9))
        self.assertEqual("awaiting_checks", retried["status"])
        self.assertEqual(1, len(calls))

    def test_bound_skill_optimizer_override_uses_approved_cross_harness_profile(self):
        source = self.root / ".agents/skills/meeting-actions"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: meeting-actions\ndescription: Create a meeting action list.\n---\n"
            "# Rules\n\nKeep the owner label.\n", encoding="utf-8")
        version = tree_hash(source)
        home = self.home / "hermes-profile"
        self.config["skills"] = {"meeting-actions": {
            "source": ".agents/skills/meeting-actions",
            "harnesses": {"codex": {"model": "codex-eval-model"}},
            "optimizer": {"harness": "hermes", "model": "test:optimizer-model", "home": str(home)},
        }}
        row = self.add_turn(skill="meeting-actions", version=version)
        proposal = self.proposal(row, matches=["meeting-actions"], old_text="Keep the owner label.",
                                 new_text="Keep the owner label on every action.")
        calls = []
        with (patch("scripts.runtime.require_model_consent"),
              patch("scripts.runtime.run_model", side_effect=self.fake_model(proposal, calls)),
              patch("scripts.learning_worker.approved_runner", return_value=nullcontext()) as runner):
            result = learning_reflection.reflect(self.config, self.db, row["topic"], [row], 21, "item-1")

        self.assertEqual("awaiting_checks", result["status"])
        runner.assert_called_once_with(
            self.config, "hermes", {"model": "test:optimizer-model", "home": str(home)},
            skill_name="meeting-actions")
        self.assertEqual("test:optimizer-model", calls[0][2])
        self.assertEqual("hermes", calls[0][3]["harness"])


if __name__ == "__main__":
    unittest.main()
