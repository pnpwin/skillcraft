import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import learning, learning_observations, learning_promotion, learning_worker


class ScheduledPublicationRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        for statement in learning_observations.SCHEMA.split(";"):
            if statement.strip():
                self.db.execute(statement)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_protected_case_commit_is_idempotent_and_hash_bound(self):
        active = self.root / "skill"
        active.mkdir()
        (active / "SKILL.md").write_text("candidate", encoding="utf-8")
        active_hash = learning_promotion._tree_hash(active)
        attempt = self.root / "attempt"
        attempt.mkdir()
        source = str(active.resolve())
        cases = [{"id": "case-1", "group": "family-1", "protect": True,
                  "prompt": "Make the expected file", "checks": []}]
        intent = {"schema_version": 1, "source": source, "name": "example",
                  "before_hash": "before", "after_hash": active_hash,
                  "passed_ab": True, "protected_cases": cases}
        (attempt / "activation-intent.json").write_text(json.dumps(intent), encoding="utf-8")
        promotion = {"source": source, "before_hash": "before", "after_hash": active_hash}

        learning_worker._commit_activation_cases(self.db, "example", active_hash, attempt, promotion)
        learning_worker._commit_activation_cases(self.db, "example", active_hash, attempt, promotion)

        rows = self.db.execute("SELECT * FROM protected_session_cases").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["payload"])["id"], "case-1")
        self.assertEqual(self.db.execute("SELECT version FROM protected_session_cases").fetchone()[0], active_hash)

        with self.assertRaisesRegex(ValueError, "does not match"):
            learning_worker._commit_activation_cases(
                self.db, "example", "third-hash", attempt,
                {**promotion, "after_hash": "third-hash"})

    def test_recovery_of_active_candidate_finishes_catalog_and_case_commit(self):
        project = self.root / "project"
        source = project / ".agents" / "skills" / "example"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("before", encoding="utf-8")
        source = source.resolve()
        before_hash = learning_promotion._tree_hash(source)
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "SKILL.md").write_text("after", encoding="utf-8")
        after_hash = learning_promotion._tree_hash(candidate)
        state = self.root / "state"
        promotion = learning_promotion.promote(source, candidate, before_hash, state,
                                               expected_candidate_hash=after_hash)
        attempt = self.root / "attempt"
        attempt.mkdir()
        cases = [{"id": "approved-case", "group": "approved-family", "protect": True,
                  "prompt": "Make the expected file", "checks": []}]
        intent = {"schema_version": 1, "source": str(source.resolve()), "name": "example",
                  "before_hash": before_hash, "after_hash": after_hash, "passed_ab": True,
                  "protected_cases": cases}
        (attempt / "activation-intent.json").write_text(json.dumps(intent), encoding="utf-8")
        report = {"skill": "example", "source": str(source), "source_canonical": str(source.resolve()),
                  "scheduled": True, "policy_hash": "policy", "candidate": str(candidate),
                  "candidate_hash": after_hash, "status": "verifying_catalog", "promotion": promotion}
        (attempt / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (attempt / "promotion.json").write_text(json.dumps(promotion), encoding="utf-8")
        config = {"root": project, "state": state, "fingerprint": "policy"}
        settings = {"source": ".agents/skills/example", "harnesses": {"codex": {"model": "unused"}}}

        def catalog_probe(_root, harness, _models):
            return lambda target, **_kwargs: {
                "harness": harness, "path": str(target.resolve()),
                "hash": learning_promotion._tree_hash(target), "enabled": True,
            }

        with patch("scripts.learning_candidates.native_catalog_probe", side_effect=catalog_probe), \
                patch("scripts.learning_worker.source_identity", return_value=(source, False)):
            status = learning_worker.recover_attempt(attempt, source, state, config=config,
                                                     settings=settings, db=self.db, name="example")

        self.assertEqual(status, "applied", report)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM protected_session_cases").fetchone()[0], 1)
        self.assertEqual(learning_promotion._tree_hash(source), after_hash)

    def test_verified_intent_recovers_before_publication_without_new_cases(self):
        source = self.root.resolve() / "project/.agents/skills/example"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("before")
        attempt = self.root / "state/revisions/1/example"
        candidate = attempt / "candidate/example"
        candidate.mkdir(parents=True)
        (candidate / "SKILL.md").write_text("after")
        before = learning_promotion._tree_hash(source)
        after = learning_promotion._tree_hash(candidate)
        intent = {
            "source": str(source), "before_hash": before, "after_hash": after, "policy_hash": "policy",
            "passed_ab": True, "auto_apply": True, "candidate_modes": learning_promotion.tree_modes(candidate),
            "protected_cases": [{"group": "held", "id": "hold", "protect": True}],
        }
        report = {
            "skill": "example", "scheduled": True, "policy_hash": "policy", "status": "qualified",
            "candidate": str(candidate), "source_canonical": str(source),
            "source_modes": learning_promotion.tree_modes(source),
        }
        (attempt / "activation-intent.json").write_text(json.dumps(intent))
        (attempt / "report.json").write_text(json.dumps(report))
        config = {"root": self.root / "project", "state": self.root / "state", "fingerprint": "policy"}
        settings = {"auto_apply": True, "harnesses": {"codex": {"model": "unused"}}}
        probe = lambda target, **kwargs: {"harness": kwargs["harness"], "enabled": True,
                                          "path": str(target), "hash": after}
        with (patch("scripts.learning_worker.require_model_approval", return_value={}),
              patch("scripts.learning_worker.approval_still_current", return_value=True),
              patch("scripts.learning_worker.source_identity", return_value=(source, False)),
              patch("scripts.learning_candidates.native_catalog_probe", return_value=probe),
              patch("scripts.learning_worker.run_model") as model):
            for _ in range(2):
                self.assertEqual(learning_worker.recover_attempt(
                    attempt, source, config["state"], config=config, settings=settings, db=self.db,
                    name="example"), "applied", (attempt / "report.json").read_text())
            model.assert_not_called()
        self.assertEqual(learning_promotion._tree_hash(source), after)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM protected_session_cases").fetchone()[0], 1)

    def test_archives_only_verified_legacy_exchange_copies_and_keeps_recovery(self):
        project = self.root.resolve() / "project"
        source = project / ".agents/skills/example"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("before")
        candidate = self.root / "draft"
        candidate.mkdir()
        (candidate / "SKILL.md").write_text("after")
        state = self.root.resolve() / "state"
        config = {"root": project, "state": state,
                  "skills": {"example": {"source": ".agents/skills/example"}}}
        attempt = state / "revisions/1/example"
        with patch.object(learning_promotion, "_exchange_parent", return_value=source.parent):
            record = learning_promotion.promote(
                source, candidate, learning_promotion._tree_hash(source), state,
                record_path=attempt / "promotion.json")
        legacy = Path(record["exchange_stage"])
        unrelated = source.parent / f".example.promotion-{'f' * 32}"
        unrelated.mkdir()
        (unrelated / "SKILL.md").write_text("third version")
        report = {"source_canonical": str(source), "promotion": record}
        (attempt / "report.json").write_text(json.dumps(report))
        with learning.connection(config) as db:
            db.execute("INSERT INTO checkpoints(witness,status) VALUES ('old','done')")
            db.execute("INSERT INTO revisions(checkpoint,skill,version,status,path) VALUES (1,?,?,?,?)",
                       ("example", record["before_hash"], "applied", str(attempt)))
        with patch.object(learning_worker, "source_identity", return_value=(source, False)):
            result = learning_worker.repair_legacy_exchanges(config)
            repeat = learning_worker.repair_legacy_exchanges(config)
        self.assertEqual([str(state / "exchanges" / legacy.name)], result["archived"])
        self.assertEqual([str(unrelated)], result["unverified"])
        self.assertFalse(legacy.exists())
        self.assertTrue(unrelated.is_dir())
        self.assertEqual([], repeat["archived"])
        self.assertTrue(learning_promotion.recover_exchange(source, record, state))
        self.assertEqual(record["after_hash"], learning_promotion._tree_hash(source))

    def test_catalog_failure_restores_prior_revision(self):
        source = self.root / "skill"
        source.mkdir()
        (source / "SKILL.md").write_text("before", encoding="utf-8")
        before_hash = learning_promotion._tree_hash(source)
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "SKILL.md").write_text("after", encoding="utf-8")
        candidate_hash = learning_promotion._tree_hash(candidate)
        promotion = learning_promotion.promote(source, candidate, before_hash, self.root / "state",
                                               expected_candidate_hash=candidate_hash)
        report = {"policy_hash": "policy", "promotion": promotion}
        config = {"fingerprint": "policy", "root": self.root, "state": self.root / "state"}
        settings = {"harnesses": {"codex": {"model": "unused"}}}
        attempt = self.root / "attempt"
        attempt.mkdir()

        with patch("scripts.learning_candidates.native_catalog_probe",
                   return_value=lambda *_a, **kwargs: {"harness": kwargs["harness"]}):
            learning_worker.finish_scheduled_promotion(config, settings, source, report, attempt)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(learning_promotion._tree_hash(source), before_hash)

    def test_catalog_failure_preserves_external_third_hash(self):
        source = self.root / "skill"
        source.mkdir()
        (source / "SKILL.md").write_text("before", encoding="utf-8")
        before_hash = learning_promotion._tree_hash(source)
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "SKILL.md").write_text("after", encoding="utf-8")
        promotion = learning_promotion.promote(source, candidate, before_hash, self.root / "state")
        (source / "SKILL.md").write_text("external edit", encoding="utf-8")
        external_hash = learning_promotion._tree_hash(source)
        report = {"policy_hash": "policy", "promotion": promotion}
        config = {"fingerprint": "policy", "root": self.root, "state": self.root / "state"}
        attempt = self.root / "attempt"
        attempt.mkdir()
        settings = {"harnesses": {"codex": {"model": "unused"}}}

        with patch("scripts.learning_candidates.native_catalog_probe",
                   return_value=lambda *_a, **kwargs: {"harness": kwargs["harness"]}):
            with self.assertRaisesRegex(ValueError, "rollback_hash_mismatch"):
                learning_worker.finish_scheduled_promotion(config, settings, source, report, attempt)

        self.assertEqual(learning_promotion._tree_hash(source), external_hash)


if __name__ == "__main__":
    unittest.main()
