import tempfile
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import learning_candidates


class LearningCandidates(unittest.TestCase):
    def tree(self, path, text):
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(text, encoding="utf-8")
        return path

    def quality_report(self, skill, workspace, previous=None, *, controlled=True,
                       checks=None, include_baseline=True):
        baseline = "previous_skill" if previous else "without_skill"
        candidate_hash = learning_candidates._evaluation_hash(skill)
        baseline_hash = learning_candidates._evaluation_hash(previous) if previous else None
        checks = checks or [("required check", False, True, "deterministic")]
        arms = [(baseline, [(name, before, source) for name, before, _, source in checks]),
                ("with_skill", [(name, after, source) for name, _, after, source in checks])]
        if not include_baseline:
            arms = arms[1:]
        for arm, arm_checks in arms:
            run = workspace / "eval-case-a" / arm / "run-001"
            run.mkdir(parents=True)
            grade = {"evaluation_complete": True, "expectations": [
                {"text": name, "passed": passed, "source": source}
                for name, passed, source in arm_checks]}
            grade_bytes = json.dumps(grade, sort_keys=True).encode()
            (run / "grading.json").write_bytes(grade_bytes)
            rubric = {"expectations": ["required check"]}
            (run / "rubric.json").write_text(json.dumps(rubric))
            rubric_value = {key: rubric.get(key) for key in
                            ["expectations", "assertions", "checks", "expected_output", "required_evidence"]}
            rubric_hash = hashlib.sha256(json.dumps(
                rubric_value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            (run / "grading-manifest.json").write_text(json.dumps({
                "status": "completed", "result_sha256": hashlib.sha256(grade_bytes).hexdigest(),
                "rubric_hash": rubric_hash}))
        configs = {"without_skill": {"snapshot_hash": None}, "with_skill": {"snapshot_hash": candidate_hash}}
        if previous:
            configs["previous_skill"] = {"snapshot_hash": baseline_hash}
        report = {"errors": 0, "validation_errors": [],
                  "metadata": {"controlled_requested": controlled, "environment_consistent": True,
                               "candidate_hash": candidate_hash, "repeats": 1,
                               "evals": [{"id": "case-a"}], "configurations": configs},
                  "runs": [{"configuration": arm, "included": True, "status": "completed",
                            "quality_complete": True} for arm in (baseline, "with_skill") if
                           include_baseline or arm == "with_skill"]}
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "benchmark.json").write_text(json.dumps(report, sort_keys=True))
        return report

    def render_review(self, workspace):
        output = workspace / "review.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("<html>synthetic review</html>")
        return output

    def stage(self, root, kind="new", source=None, collision_roots=()):
        content = self.tree(root / "proposal", "candidate\n")
        (root / "project").mkdir(exist_ok=True)
        return learning_candidates.stage_candidate(
            content_path=content, state_dir=root / "state", project_root=root / "project",
            name="demo", kind=kind, source_path=source, candidate_id="candidate-1",
            collision_roots=collision_roots)

    def test_manual_new_apply_and_hash_checked_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)
            with self.assertRaisesRegex(ValueError, "candidate_verification_required"):
                learning_candidates.apply_candidate(candidate_id=record["id"], state_dir=root / "state",
                                                    project_root=root / "project")
            applied = learning_candidates.apply_candidate(candidate_id=record["id"], state_dir=root / "state",
                                                           project_root=root / "project", manual=True)
            target = Path(applied["target_path"])
            self.assertTrue(target.is_dir())
            self.assertEqual(learning_candidates._hash(target), record["candidate_hash"])
            learning_candidates.rollback_candidate(candidate_id=record["id"], state_dir=root / "state")
            self.assertFalse(target.exists())

    def test_new_publish_recovers_after_crash_immediately_after_atomic_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "project" / ".agents" / "skills"
            other_catalog = root / "project" / ".codex" / "skills"
            roots = [catalog, other_catalog]
            record = self.stage(root, collision_roots=roots)
            rename = learning_candidates._rename_noreplace
            apply = dict(candidate_id=record["id"], state_dir=root / "state",
                         project_root=root / "project", manual=True, collision_roots=roots)

            def rename_then_crash(source, destination):
                rename(source, destination)
                raise RuntimeError("simulated process loss")

            with patch.object(learning_candidates, "_rename_noreplace", side_effect=rename_then_crash):
                with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                    learning_candidates.apply_candidate(**apply)
            target = Path(record["target_path"])
            self.assertEqual(learning_candidates._hash(target), record["candidate_hash"])
            shadow = self.tree(other_catalog / record["name"], "unrelated\n")
            with self.assertRaisesRegex(ValueError, "candidate_collision"):
                learning_candidates.apply_candidate(**apply)
            self.assertEqual("unrelated\n", (shadow / "SKILL.md").read_text())
            (shadow / "SKILL.md").unlink()
            shadow.rmdir()
            skill = target / "SKILL.md"
            content = skill.read_bytes()
            skill.write_text("user edits\n")
            with self.assertRaisesRegex(ValueError, "candidate_collision"):
                learning_candidates.apply_candidate(**apply)
            self.assertEqual("user edits\n", skill.read_text())
            skill.write_bytes(content)
            reconciled = learning_candidates.apply_candidate(**apply)
            self.assertEqual(reconciled["status"], "applied_manual_unverified")
            self.assertFalse(reconciled["promotion"]["pending"])

    def test_atomic_publish_collision_keeps_unrelated_shadow_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)
            target = Path(record["target_path"])
            rename = learning_candidates._rename_noreplace

            def race_with_unrelated_skill(source, destination):
                destination.mkdir(parents=True)
                (destination / "SKILL.md").write_text("unrelated shadow\n")
                return rename(source, destination)

            with patch.object(learning_candidates, "_rename_noreplace", side_effect=race_with_unrelated_skill):
                with self.assertRaises(FileExistsError):
                    learning_candidates.apply_candidate(candidate_id=record["id"], state_dir=root / "state",
                                                        project_root=root / "project", manual=True)
            self.assertEqual((target / "SKILL.md").read_text(), "unrelated shadow\n")

    def test_verify_cannot_clobber_concurrent_apply_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)

            def fake_quality(skill, evals, workspace, model, **kwargs):
                learning_candidates.apply_candidate(candidate_id=record["id"], state_dir=root / "state",
                                                    project_root=root / "project", manual=True)
                return self.quality_report(skill, workspace)

            with self.assertRaisesRegex(ValueError, "candidate_state_conflict"):
                learning_candidates.verify_candidate(
                    candidate_id=record["id"], state_dir=root / "state", evals=root / "evals.json",
                    model="mock", workspace=root / "fresh-workspace", run_quality=fake_quality,
                    render_review=self.render_review)
            latest, _ = learning_candidates.load_candidate(candidate_id=record["id"], state_dir=root / "state")
            self.assertEqual(latest["status"], "applied_manual_unverified")

    def test_auto_apply_rejects_mutated_benchmark_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)

            def fake_quality(skill, evals, workspace, model, **kwargs):
                return self.quality_report(skill, workspace)

            verified = learning_candidates.verify_candidate(
                candidate_id=record["id"], state_dir=root / "state", evals=root / "evals.json",
                model="mock", workspace=root / "fresh-workspace", run_quality=fake_quality,
                render_review=self.render_review)
            self.assertTrue(verified["trusted_pass"])
            (root / "fresh-workspace" / "benchmark.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "candidate_verification_required"):
                learning_candidates.apply_candidate(
                    candidate_id=record["id"], state_dir=root / "state", project_root=root / "project",
                    harness="codex", catalog_probe=lambda target, harness: {
                        "harness": harness, "path": str(target), "hash": learning_candidates._hash(target)})

    def test_name_collision_across_configured_harness_roots_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow = self.tree(root / "other-harness" / "Demo", "shadow\n")
            with self.assertRaisesRegex(ValueError, "candidate_collision"):
                self.stage(root, collision_roots=[shadow.parent])

    def test_new_target_collision_is_denied_even_for_manual_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)
            target = root / "project" / ".agents" / "skills" / "demo"
            self.tree(target, "pre-existing\n")
            with self.assertRaisesRegex(ValueError, "candidate_collision"):
                learning_candidates.apply_candidate(candidate_id=record["id"], state_dir=root / "state",
                                                    project_root=root / "project", manual=True)

    def test_revision_uses_atomic_promotion_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self.tree(root / "project" / ".agents" / "skills" / "demo", "before\n")
            content = self.tree(root / "proposal", "after\n")
            record = learning_candidates.stage_candidate(
                content_path=content, state_dir=root / "state", project_root=root / "project",
                name="demo", kind="revision", source_path=target, candidate_id="revision-1",
                collision_roots=[target.parent])
            applied = learning_candidates.apply_candidate(candidate_id=record["id"], state_dir=root / "state",
                                                           project_root=root / "project", manual=True)
            self.assertEqual((target / "SKILL.md").read_text(), "after\n")
            learning_candidates.rollback_candidate(candidate_id=record["id"], state_dir=root / "state")
            self.assertEqual((target / "SKILL.md").read_text(), "before\n")
            self.assertTrue(applied["promotion"]["backup"])

    def test_revision_verification_binds_source_hash_and_native_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "project").mkdir()
            target = self.tree(root / "project" / ".agents" / "skills" / "demo", "before\n")
            content = self.tree(root / "proposal", "after\n")
            record = learning_candidates.stage_candidate(
                content_path=content, state_dir=root / "state", project_root=root / "project",
                name="demo", kind="revision", source_path=target, candidate_id="revision-verified",
                collision_roots=[target.parent])

            def fake_quality(skill, evals, workspace, model, **kwargs):
                return self.quality_report(skill, workspace, previous=kwargs["previous"])

            verified = learning_candidates.verify_candidate(
                candidate_id=record["id"], state_dir=root / "state", evals=root / "evals.json",
                model="mock", workspace=root / "fresh-workspace", run_quality=fake_quality,
                render_review=self.render_review)
            self.assertTrue(verified["trusted_pass"])
            applied = learning_candidates.apply_candidate(
                candidate_id=record["id"], state_dir=root / "state", project_root=root / "project",
                harness="codex", catalog_probe=lambda path, harness: {
                    "harness": harness, "path": str(path), "hash": learning_candidates._hash(path)})
            self.assertEqual(applied["status"], "applied_verified")
            self.assertEqual((target / "SKILL.md").read_text(), "after\n")
            learning_candidates.rollback_candidate(candidate_id=record["id"], state_dir=root / "state")
            self.assertEqual((target / "SKILL.md").read_text(), "before\n")

    def test_revision_supports_approved_local_source_roots_and_preserves_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            source = self.tree(project / ".codex" / "skills" / "category" / "demo", "before\n")
            (source / "assets").mkdir()
            (source / "assets" / "guide.txt").write_text("preserved")
            proposal = self.tree(root / "proposal", "after\n")
            (proposal / "evals").mkdir()
            (proposal / "evals" / "evals.json").write_text("[]")
            record = learning_candidates.stage_candidate(
                content_path=proposal, state_dir=root / "state", project_root=project,
                name="demo", kind="revision", source_path=source, candidate_id="local-root")
            candidate = Path(record["candidate_path"])
            self.assertEqual(Path(record["target_path"]).parent.resolve(), source.parent.resolve())
            self.assertEqual((candidate / "assets" / "guide.txt").read_text(), "preserved")
            self.assertTrue((candidate / "evals" / "evals.json").is_file())
            self.assertNotEqual(record["candidate_hash"], record["candidate_eval_hash"])

    def test_verify_requires_fresh_bound_controlled_pass_before_auto_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)

            def fake_quality(skill, evals, workspace, model, **kwargs):
                return self.quality_report(skill, workspace)

            checked = learning_candidates.verify_candidate(
                candidate_id=record["id"], state_dir=root / "state", evals=root / "evals.json",
                model="mock", workspace=root / "fresh-workspace", run_quality=fake_quality,
                render_review=self.render_review)
            self.assertTrue(checked["trusted_pass"])
            result = learning_candidates.apply_candidate(candidate_id=record["id"], state_dir=root / "state",
                                                         project_root=root / "project", harness="codex",
                                                         catalog_probe=lambda target, harness: {
                                                             "harness": harness, "path": str(target),
                                                             "hash": learning_candidates._hash(target)})
            self.assertEqual(result["status"], "applied_verified")

    def test_uncontrolled_or_hash_mismatched_report_never_auto_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)

            def fake_quality(skill, evals, workspace, model, **kwargs):
                return self.quality_report(skill, workspace, controlled=False)

            checked = learning_candidates.verify_candidate(
                candidate_id=record["id"], state_dir=root / "state", evals=root / "evals.json",
                model="mock", workspace=root / "fresh-workspace", run_quality=fake_quality,
                render_review=self.render_review)
            self.assertFalse(checked["trusted_pass"])
            with self.assertRaisesRegex(ValueError, "candidate_verification_required"):
                learning_candidates.apply_candidate(candidate_id=record["id"], state_dir=root / "state",
                                                    project_root=root / "project")

    def test_candidate_must_not_regress_any_individual_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)

            def fake_quality(skill, evals, workspace, model, **kwargs):
                return self.quality_report(skill, workspace,
                                           checks=[("gain", False, True, "deterministic"),
                                                   ("regression", True, False, "judge")])

            result = learning_candidates.verify_candidate(
                candidate_id=record["id"], state_dir=root / "state", evals=root / "evals.json",
                model="mock", workspace=root / "fresh-workspace", run_quality=fake_quality,
                render_review=self.render_review)
            self.assertFalse(result["trusted_pass"])
            self.assertEqual(result["reason"], "candidate_regressed_check_results")

    def test_auto_apply_requires_complete_baseline_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)

            def fake_quality(skill, evals, workspace, model, **kwargs):
                return self.quality_report(skill, workspace, include_baseline=False)

            result = learning_candidates.verify_candidate(
                candidate_id=record["id"], state_dir=root / "state", evals=root / "evals.json",
                model="mock", workspace=root / "fresh-workspace", run_quality=fake_quality,
                render_review=self.render_review)
            self.assertFalse(result["trusted_pass"])
            self.assertEqual(result["reason"], "evaluation_has_no_complete_baseline_arm")

    def test_catalog_probe_mismatch_rolls_back_automatic_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.stage(root)

            def fake_quality(skill, evals, workspace, model, **kwargs):
                return self.quality_report(skill, workspace)

            learning_candidates.verify_candidate(
                candidate_id=record["id"], state_dir=root / "state", evals=root / "evals.json",
                model="mock", workspace=root / "fresh-workspace", run_quality=fake_quality,
                render_review=self.render_review)
            with self.assertRaisesRegex(ValueError, "promotion rolled back"):
                learning_candidates.apply_candidate(
                    candidate_id=record["id"], state_dir=root / "state", project_root=root / "project",
                    harness="codex",
                    catalog_probe=lambda target, harness: {"harness": harness, "path": str(target), "hash": "wrong"})
            self.assertFalse(Path(record["target_path"]).exists())

    def test_target_outside_approved_project_or_home_scope_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = self.tree(root / "proposal", "candidate\n")
            (root / "project").mkdir()
            with self.assertRaisesRegex(ValueError, "candidate_scope_denied"):
                learning_candidates.stage_candidate(
                    content_path=content, state_dir=root / "state", project_root=root / "project",
                    name="demo", target_root=root / "arbitrary", candidate_id="outside")

    def test_full_review_renders_real_run_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "quality-workspace"
            run = workspace / "eval-case-a" / "with_skill" / "run-001"
            (run / "outputs").mkdir(parents=True)
            (run / "outputs" / "evidence.txt").write_text("observed run evidence")
            (run / "run.json").write_text(json.dumps({"run_id": "fresh-run", "status": "completed"}))
            (workspace / "benchmark.json").write_text(json.dumps({"runs": []}))
            review = learning_candidates.render_quality_review(workspace)
            html = review.read_text(encoding="utf-8")
            self.assertIn("evidence.txt", html)
            self.assertIn("observed run evidence", html)


if __name__ == "__main__":
    unittest.main()
