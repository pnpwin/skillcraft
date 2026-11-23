import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/skillcraft/skills/skillcraft"
sys.path.insert(0, str(SCRIPTS))

from scripts.learning_resources import aggregate_batch_resources, resource_totals


class LearningResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.config = {"state": self.state}

    def write_json(self, path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_totals_preserve_unknowns_instead_of_zero_filling(self):
        self.assertEqual({"known_sum": 9, "known_count": 2, "unknown_count": 1, "total": None},
                         resource_totals([4, None, 5]))
        self.assertEqual({"known_sum": None, "known_count": 0, "unknown_count": 1, "total": None},
                         resource_totals([None]))

    def test_creation_counts_actual_attempts_including_failed_judge_and_cache(self):
        attempt = self.state / "batches/7/demo"
        self.write_json(attempt / "optimizer/run.json", {"harness": "codex", "status": "completed"})
        self.write_json(attempt / "optimizer/usage.json", {
            "executor": {"total_tokens": 11, "cached_tokens": 3},
        })
        workspace = attempt / "verification-codex-1"
        executor = workspace / "eval-case/with_skill/run-001"
        self.write_json(executor / "usage.json", {"executor": {
            "total_tokens": 20, "cost": 0.2, "tokens": {"cache": {"read": 5}},
        }})
        self.write_json(executor / "judge/run.json", {"status": "timeout", "harness": "codex"})
        self.write_json(workspace / "pilot/eval-case/with_skill/run-001/usage.json", {
            "executor": {"total_tokens": 7},
        })
        self.write_json(attempt / "trigger-codex/eval-case/natural/run-001/usage.json", {
            "executor": {"total_tokens": 4, "cost": 0.04},
        })
        # Dispatched runs count even when the final benchmark was never written.
        item = {"operation": "create", "name": "demo", "result": {
            "status": "blocked", "candidate": {"status": "blocked"},
        }}
        report = aggregate_batch_resources(self.config, 7, [item])
        self.assertEqual(5, report["summary"]["model_runs"])
        self.assertEqual(1, report["summary"]["failed_runs"])
        self.assertEqual(42, report["summary"]["tokens"]["known_sum"])
        self.assertEqual(1, report["summary"]["tokens"]["unknown_count"])
        self.assertEqual(8, report["summary"]["cached_tokens"]["known_sum"])
        self.assertEqual(5, report["harnesses"]["codex"]["model_runs"])
        self.assertEqual(4, report["phases"]["trigger"]["tokens"]["total"])
        self.assertEqual(7, report["phases"]["pilot"]["tokens"]["total"])
        html = Path(report["report_html"]).read_text(encoding="utf-8")
        self.assertIn("Automatic Learning resources", html)
        self.assertIn("judge", html)
        self.assertIn("42 + unknown (1)", html)

    def test_revision_reads_pilot_and_main_usage_under_the_named_harness(self):
        report_path = self.write_json(self.state / "revisions/3/demo/report.json", {})
        workspace = report_path.parent / "ab-hermes"
        for prefix, tokens in (("", 10), ("pilot/", 6)):
            self.write_json(workspace / prefix / "eval-case/with_skill/run-001/usage.json", {
                "executor": {"total_tokens": tokens},
            })
        report = aggregate_batch_resources(self.config, 3, [{
            "operation": "improve", "name": "demo", "result": {"status": "applied",
                          "candidate": {"report": str(report_path)}},
        }])
        self.assertEqual(6, report["phases"]["pilot"]["tokens"]["total"])
        self.assertEqual(10, report["phases"]["main_ab"]["tokens"]["total"])
        self.assertEqual(16, report["harnesses"]["hermes"]["tokens"]["total"])

    def test_report_path_outside_state_and_empty_batch_count_no_model_work(self):
        outside = self.write_json(Path(self.temp.name) / "outside.json", {})
        self.write_json(outside.parent / "optimizer/usage.json", {"executor": {"total_tokens": 999}})
        report = aggregate_batch_resources(self.config, 9, [{
            "operation": "improve", "name": "demo", "result": {"report": str(outside)},
        }])
        self.assertEqual(0, report["summary"]["model_runs"])
        self.assertEqual(0, report["summary"]["tokens"]["total"])
        self.assertEqual({}, report["phases"])


if __name__ == "__main__":
    unittest.main()
