from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/skillcraft/skills/skillcraft"
sys.path.insert(0, str(SCRIPTS))

from scripts.learning_batch import batch_status, due, run_batch


class LearningBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / "skills" / "demo").mkdir(parents=True)
        (self.root / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.config = {
            "state": self.state,
            "root": self.root,
            "skills": {"demo": {"source": "skills/demo"}},
            "automation": {"create": True, "improve": True, "interval_days": 7,
                           "max_candidates_per_batch": 3, "new_skill": {}},
        }
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def add_observation(self, *, topic="topic-a", session, independent, kind="success", verified=True):
        self.db.execute(
            "INSERT INTO observations(witness,harness,session,turn,kind,topic,skill,version,independent,excerpt,details) "
            "VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?)",
            (f"w-{session}-{independent}", "codex", session, f"t-{independent}", kind, topic,
             independent, "confirmed", json.dumps({"check": {"file": "artifact.txt"}} if verified else {})),
        )

    def test_no_evidence_never_calls_update_and_schedules_next_batch(self):
        self.db.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY, skill TEXT, topic TEXT, kind TEXT, session TEXT, independent TEXT)")
        calls = []
        report = run_batch(self.config, self.db, update=lambda *args: calls.append(args), now=self.now)
        self.assertEqual("done", report["status"])
        self.assertEqual([], calls)
        self.assertEqual(0, report["candidate_count"])
        self.assertFalse(due(self.db, self.config["automation"], now=self.now.replace(day=2)))
        self.assertTrue(due(self.db, {**self.config["automation"], "interval_days": 1},
                            now=self.now.replace(day=2)))
        self.assertEqual("done", batch_status(self.config, self.db)["batch"]["status"])

    def test_create_group_uses_one_verified_case_and_deduplicates_replays(self):
        self.db.execute("""CREATE TABLE observations(
            id INTEGER PRIMARY KEY, witness TEXT UNIQUE, harness TEXT, session TEXT, turn TEXT,
            kind TEXT, topic TEXT, skill TEXT, version TEXT, independent TEXT, excerpt TEXT, details TEXT
        )""")
        self.add_observation(session="s0", independent="family-0")
        self.add_observation(session="s1", independent="family-0")
        self.add_observation(topic="other", session="s0", independent="family-1", verified=False)
        calls = []

        def update(name, settings, rows, batch_id):
            calls.append((name, settings, [row["topic"] for row in rows], batch_id))
            return {"status": "candidate_created", "id": "candidate-1"}

        report = run_batch(self.config, self.db, update=update, now=self.now)
        self.assertEqual(1, len(calls))
        self.assertEqual("topic-a", calls[0][0])
        self.assertEqual(["topic-a"], calls[0][2])
        self.assertEqual("done", report["status"])
        self.assertEqual("candidate_created", report["items"][0]["result"]["status"])

    def test_configurable_limit_applies_to_distinct_candidates_in_one_scope(self):
        from scripts.learning_automation import parse_automation
        from scripts.learning_observations import SCHEMA

        self.db.executescript(SCHEMA)
        self.config["automation"] = parse_automation({
            "selection": "all", "automation": {"create": True, "max_candidates_per_batch": 6},
        })
        for index in range(7):
            self.add_observation(topic=f"topic-{index}", session="same-session", independent=f"case-{index}")
        self.add_observation(topic="topic-0", session="same-session", independent="another-case")
        calls = []

        def evaluate(name, settings, rows, batch_id):
            calls.append((name, len(rows)))
            return {"status": "qualified"}

        report = run_batch(self.config, self.db, update=evaluate, now=self.now)
        self.assertEqual(6, report["candidate_count"])
        self.assertEqual(("topic-0", 2), calls[0])
        self.assertEqual(6, len({name for name, _ in calls}))
        self.assertEqual(7, self.config["automation"]["interval_days"])

    def test_existing_topic_is_reported_and_never_reaches_model_callback(self):
        self.db.execute("""CREATE TABLE observations(
            id INTEGER PRIMARY KEY, witness TEXT UNIQUE, harness TEXT, session TEXT, turn TEXT,
            kind TEXT, topic TEXT, skill TEXT, version TEXT, independent TEXT, excerpt TEXT, details TEXT
        )""")
        for index in range(3):
            self.add_observation(topic="quarterly-invoice", session=f"s{index}", independent=f"family-{index}")
        self.config["skills"]["quarterly-invoice"] = {"source": "skills/demo"}
        calls = []
        report = run_batch(self.config, self.db, update=lambda *args: calls.append(args), now=self.now)
        self.assertEqual([], calls)
        self.assertEqual("ambiguous_existing_skill", report["items"][0]["result"]["status"])

    def test_awaiting_checks_retries_only_after_approval_scope_changes(self):
        self.db.execute("""CREATE TABLE observations(
            id INTEGER PRIMARY KEY, witness TEXT UNIQUE, harness TEXT, session TEXT, turn TEXT,
            kind TEXT, topic TEXT, skill TEXT, version TEXT, independent TEXT, excerpt TEXT, details TEXT
        )""")
        for index in range(3):
            self.add_observation(topic="new-topic", session=f"s{index}", independent=f"family-{index}")
        calls = []

        def waiting(*args):
            calls.append(args)
            return {"status": "awaiting_checks"}

        first = run_batch(self.config, self.db, update=waiting, now=self.now)
        self.assertEqual("awaiting_checks", first["status"])
        second = run_batch(self.config, self.db, update=waiting, now=self.now.replace(day=9))
        self.assertEqual(1, len(calls))
        self.assertEqual(0, second["candidate_count"])
        self.config["automation_inputs"] = {"cases": {"new-topic": {"train": "approved"}}}
        third = run_batch(self.config, self.db, update=waiting, now=self.now.replace(day=17))
        self.assertEqual(2, len(calls))
        self.assertEqual("awaiting_checks", third["status"])

    def test_resume_reuses_durable_rows_and_completed_items_are_not_repeated(self):
        self.db.execute("""CREATE TABLE observations(
            id INTEGER PRIMARY KEY, witness TEXT UNIQUE, harness TEXT, session TEXT, turn TEXT,
            kind TEXT, topic TEXT, skill TEXT, version TEXT, independent TEXT, excerpt TEXT, details TEXT
        )""")
        for index in range(3):
            self.add_observation(session=f"s{index}", independent=f"family-{index}")
        calls = []

        def interrupt(name, settings, rows, batch_id):
            calls.append(name)
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_batch(self.config, self.db, update=interrupt, now=self.now)
        self.assertTrue(due(self.db, self.config["automation"], now=self.now))
        report = run_batch(self.config, self.db, update=lambda *args: {"status": "staged"}, now=self.now)
        self.assertEqual("done", report["status"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, self.db.execute("SELECT COUNT(*) FROM learning_batches").fetchone()[0])
        import json
        self.assertEqual(3, len(json.loads(self.db.execute("SELECT row_ids FROM learning_batch_items").fetchone()[0])))

    def test_resume_skips_items_outside_the_current_approval(self):
        from scripts.learning_observations import SCHEMA

        self.db.executescript(SCHEMA)
        self.add_observation(session="s0", independent="family-0")

        def interrupt(*args):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_batch(self.config, self.db, update=interrupt, now=self.now)
        # Exercise an updated approval and a disabled operation separately.
        for current in ({**self.config, "automation_inputs": {"cases": {}}},
                        {**self.config, "automation": {**self.config["automation"], "create": False}}):
            with self.subTest(automation=current["automation"], inputs=current.get("automation_inputs")):
                self.db.execute("UPDATE learning_batches SET status='running'")
                self.db.execute("UPDATE learning_batch_items SET status='running'")
                self.db.commit()
                with patch("scripts.learning_batch._rows_for_item") as read:
                    report = run_batch(current, self.db, update=lambda *args: self.fail("stale model work"), now=self.now)
                read.assert_not_called()
                self.assertEqual("done", report["status"])
                self.assertEqual("approval_changed", report["items"][0]["result"]["status"])

    def test_improve_uses_only_unconsumed_current_version_evidence(self):
        version = "approved-hash"
        self.db.execute("""CREATE TABLE evidence(
            id INTEGER PRIMARY KEY, witness TEXT UNIQUE, skill TEXT, version TEXT, origin TEXT,
            harness TEXT, session TEXT, turn TEXT, independent TEXT, case_id TEXT, kind TEXT,
            note TEXT, reproducible INTEGER DEFAULT 0, consumed INTEGER DEFAULT 0
        )""")
        self.db.execute(
                "INSERT INTO evidence(witness,skill,version,origin,harness,session,turn,independent,kind,note,reproducible) "
                "VALUES(?,?,?,'tool_check','codex',?,?,?,'failure','not working',1)",
                ("w0", "demo", version, "s0", "t0", "family0"),
            )
        self.db.execute("INSERT INTO evidence(witness,skill,version,origin,harness,session,turn,independent,kind,note,consumed) VALUES('old','demo',?,'session','codex','old','old','old','failure','old',1)", (version,))
        seen = []
        with patch("scripts.learning_batch.tree_hash", return_value=version):
            report = run_batch(self.config, self.db, update=lambda name, settings, rows, batch_id: seen.extend(rows) or {"status": "staged"}, now=self.now)
        self.assertEqual("done", report["status"])
        self.assertEqual(1, len(seen))
        self.assertTrue(all(row["version"] == version for row in seen))

    def test_draft_hides_holdout_from_optimizer_and_failed_check_never_applies(self):
        from scripts.learning_batch import create_candidate

        self.db.execute("""CREATE TABLE observations(
            id INTEGER PRIMARY KEY, witness TEXT UNIQUE, harness TEXT, session TEXT, turn TEXT,
            kind TEXT, topic TEXT, skill TEXT, version TEXT, independent TEXT, excerpt TEXT,
            case_id TEXT, details TEXT
        )""")
        for index in range(3):
            self.db.execute(
                "INSERT INTO observations(witness,harness,session,turn,kind,topic,skill,version,independent,excerpt,details) "
                "VALUES(?, 'codex', ?, ?, 'success', 'draft-demo', NULL, NULL, ?, ?, ?) ",
                (f"obs-{index}", f"session-{index}", f"turn-{index}", f"family-{index}",
                 f"confirmed-{index}", json.dumps({
                     "prompt": f"TASK-{index}: produce a small verified file",
                     "check": {"type": "contains", "text": "The output contains the user-confirmed literal",
                               "file": f"out-{index}.txt", "value": f"ok-{index}"},
                 })),
            )
        rows = self.db.execute("SELECT * FROM observations ORDER BY id").fetchall()
        self.config["automation"] = {**self.config["automation"], "auto_apply": True,
                                      "harnesses": {"codex": {"model": "codex-test"}}}
        settings = {"automation_operation": "create", "automation_topic": "draft-demo",
                    "harnesses": self.config["automation"]["harnesses"]}
        captured = []

        def fake_model(prompt, output, model, **kwargs):
            captured.append(prompt)
            output.mkdir(parents=True)
            (output / "response.txt").write_text(json.dumps({
                "skill_md": "---\nname: draft-demo\ndescription: Create the requested verified file.\n---\n# Steps\n"
            }), encoding="utf-8")
            return {"status": "completed"}

        staged_path = self.state / "candidate" / "draft-demo"
        staged_path.mkdir(parents=True)
        (staged_path / "SKILL.md").write_text("draft", encoding="utf-8")
        stage_result = {"id": "candidate-1", "candidate_path": str(staged_path), "status": "staged"}
        failed_verification = {
            "trusted_pass": False,
            "reason": "candidate_did_not_pass_ab_no_regression_gate",
            "candidate": {"verification": {"quality_evidence": {"improved": []}}},
        }
        with (patch("scripts.runtime.run_model", side_effect=fake_model),
              patch("scripts.learning_candidates.stage_candidate", return_value=stage_result),
              patch("scripts.learning_candidates.verify_candidate", return_value=failed_verification),
              patch("scripts.learning_candidates.apply_candidate") as apply):
            result = create_candidate(self.config, self.db, "draft-demo", settings, rows, 9)
        self.assertEqual("blocked", result["status"])
        self.assertEqual(1, len(captured))
        self.assertIn("TASK-0", captured[0])
        self.assertIn("TASK-1", captured[0])
        self.assertNotIn("TASK-2", captured[0])
        apply.assert_not_called()


    def test_creation_recovery_reuses_only_the_exact_verified_evaluation(self):
        from scripts.learning_batch import resume_verified_creation
        from scripts.utils import digest, write_json

        attempt = self.state.resolve() / "batches/1/demo"
        workspace = attempt / "verification-codex-1"
        workspace.mkdir(parents=True)
        report_path = workspace / "benchmark.json"
        review = workspace / "review.html"
        review.write_text("<html></html>")
        cases = [{"id": "approved", "prompt": "Return the assigned label."}]
        report = {"metadata": {"harness": "codex", "model": "test", "repeats": 3,
                               "timeout_seconds": 180, "evals": cases}}
        write_json(report_path, report)
        proof = {"improved": True}
        candidate = {
            "candidate_eval_hash": "eval-hash", "candidate_hash": "publish-hash",
            "verification": {
                "trusted_pass": True, "workspace": str(workspace), "report": str(report_path),
                "review": str(review), "report_hash": digest(report_path),
                "candidate_hash": "publish-hash", "quality_evidence": proof,
            },
        }
        with patch("scripts.learning_candidates._trusted_pass", return_value=(True, "passed", proof)):
            restored = resume_verified_creation(candidate, attempt, cases, "codex", "test", {}, {})
            self.assertIsNotNone(restored)
            self.assertIsNone(resume_verified_creation(candidate, attempt, [], "codex", "test", {}, {}))
            write_json(report_path, {**report, "extra": "modified"})
            self.assertIsNone(resume_verified_creation(candidate, attempt, cases, "codex", "test", {}, {}))

    def test_single_confirmed_case_stages_without_an_automatic_publication(self):
        from scripts.learning_batch import create_candidate
        from scripts.learning_observations import SCHEMA

        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT INTO observations(witness,harness,session,turn,kind,topic,independent,excerpt,details) "
            "VALUES ('w','codex','only-session','one','success','draft-demo','case-one','verified',?)",
            (json.dumps({"prompt": "Create a small output.",
                         "check": {"type": "contains", "text": "Contains a label", "file": "out.txt", "value": "demo"}}),),
        )
        rows = self.db.execute("SELECT * FROM observations").fetchall()
        settings = {"automation_topic": "draft-demo", "harnesses": {"codex": {"model": "test"}}}
        self.config["automation"]["auto_apply"] = True

        def generate(_prompt, output, _model, **_kwargs):
            output.mkdir()
            (output / "response.txt").write_text(json.dumps({
                "skill_md": "---\nname: draft-demo\ndescription: Create the requested file.\n---\n# Steps\n",
            }))
            return {"status": "completed"}

        with (patch("scripts.runtime.run_model", side_effect=generate),
              patch("scripts.learning_candidates.stage_candidate", return_value={"id": "draft"}) as stage,
              patch("scripts.learning_candidates.verify_candidate") as verify,
              patch("scripts.learning_batch.publish_creation") as publish):
            result = create_candidate(self.config, self.db, "draft-demo", settings, rows, 1)
        self.assertEqual("awaiting_checks", result["status"])
        self.assertEqual("draft", result["candidate"]["id"])
        stage.assert_called_once()
        verify.assert_not_called()
        publish.assert_not_called()

    def test_one_week_batches_the_same_eligible_updates(self):
        from scripts.learning_observations import SCHEMA
        from scripts.utils import write_json

        def executions(interval):
            with closing(sqlite3.connect(":memory:")) as journal:
                journal.row_factory = sqlite3.Row
                journal.executescript(SCHEMA)
                policy = {**self.config, "state": self.state / str(interval),
                          "automation": {"create": True, "interval_days": interval}}
                seen = []

                def evaluate(name, settings, rows, batch_id):
                    seen.append(len(rows))
                    attempt = policy["state"] / "batches" / str(batch_id) / name
                    for folder, tokens in (("optimizer", 30),
                                           ("verification-codex-1/pilot/eval-demo/with_skill/run-001", 10),
                                           ("verification-codex-1/eval-demo/with_skill/run-001", 40),
                                           ("verification-codex-1/eval-demo/with_skill/run-001/judge", 12),
                                           ("trigger-codex/eval-positive/natural/run-001", 8)):
                        write_json(attempt / folder / "usage.json", {"executor": {"total_tokens": tokens}})
                    return {"status": "qualified"}

                empty = run_batch(policy, journal, update=evaluate, now=self.now)
                self.assertEqual(0, empty["resources"]["summary"]["model_runs"])
                tokens = runs = 0
                for day, latest in ((2, 3), (3, 4), (4, 5), (8, 5)):
                    for index in range(latest):
                        journal.execute(
                            "INSERT OR IGNORE INTO observations(witness,harness,session,turn,kind,topic,"
                            "independent,excerpt,details) VALUES(?, 'codex', ?, ?, 'success', 'meeting-actions', ?, '', ?)",
                            (f"w{index}", "same-session", f"t{index}", f"case{index}",
                             json.dumps({"check": {"file": "artifact.txt"}})),
                        )
                    current = run_batch(policy, journal, update=evaluate, now=self.now.replace(day=day))
                    if summary := current.get("resources", {}).get("summary"):
                        runs += summary["model_runs"]
                        tokens += summary["tokens"]["total"]
                return seen, runs, tokens

        self.assertEqual(executions(1), ([3, 4, 5], 15, 300))
        self.assertEqual(executions(7), ([5], 5, 100))

    def test_publication_requires_every_catalog_and_uses_model_references(self):
        from scripts.learning_batch import publish_creation
        from scripts.utils import tree_hash

        target = self.root / "skills/demo"
        config = {**self.config, "fingerprint": "enrolled", "automation_inputs": {"cases": {}, "binary": None}}
        config["automation"] = {**config["automation"], "auto_apply": True}
        harnesses = {"codex": {"model": "codex-model"}, "opencode": {"model": "openai/opencode-model"}}
        observations = []

        def native_probe(root, harness, models):
            self.assertEqual(root, self.root)
            self.assertEqual(models, {harness: harnesses[harness]["model"]})
            def probe(path, **options):
                observations.append((options["harness"], path))
                return {"harness": harness, "path": str(path.resolve()), "hash": tree_hash(path)}
            return probe

        def application(**options):
            catalog = options["catalog_probe"](target, harness=options["harness"])
            self.assertEqual(len(catalog["native_probes"]), 2)
            return {"status": "applied_verified"}

        with (patch("scripts.learning.config_for", return_value=config),
              patch("scripts.learning_automation.automation_approval", return_value=config["automation_inputs"]),
              patch("scripts.learning_candidates.native_catalog_probe", side_effect=native_probe),
              patch("scripts.learning_candidates.apply_candidate", side_effect=application)):
            result = publish_creation(config, {"id": "test"}, harnesses, [])
        self.assertEqual(result["status"], "applied_verified")
        self.assertEqual(observations, [("codex", target), ("opencode", target)])


if __name__ == "__main__":
    unittest.main()
