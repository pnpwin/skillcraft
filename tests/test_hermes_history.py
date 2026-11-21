import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch
import importlib.util


# Load the isolated module without importing the Hermes hook adapter (and its
# optional PyYAML runtime dependency) in these database-only tests.
_package = ModuleType("_skillcraft_history_test")
_package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins/skillcraft/src/hermes")]
sys.modules[_package.__name__] = _package
_adapter = ModuleType(_package.__name__ + ".hermes_learning")
_adapter.run_learning = lambda *args, **kwargs: None
sys.modules[_adapter.__name__] = _adapter
_spec = importlib.util.spec_from_file_location(
    _package.__name__ + ".hermes_history", Path(_package.__path__[0]) / "hermes_history.py")
hermes_history = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = hermes_history
_spec.loader.exec_module(hermes_history)


class HermesHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "profile"
        self.home.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.skill = self.project / ".agents/skills/demo/SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("historical demo")
        self.session = "local-test-session"
        self.version = "a" * 64
        self.reply_uid = "native-reply-uid"
        self.feedback_uid = "native-feedback-uid"
        self.final_uid = "native-feedback-final-uid"
        self.journal_path = self.home / "skillcraft" / "learning" / hashlib.sha256(
            str(self.project).encode()).hexdigest() / "journal.sqlite"
        self.journal_path.parent.mkdir(parents=True)
        self._create_state_db()
        self._create_journal(proven=True)
        self.inventory = {
            "root": str(self.project),
            "versions": [{"path": str(self.skill), "snapshot_hash": self.version,
                          "content_hash": "b" * 64}],
        }
        self.inventory_patch = patch.object(hermes_history, "run_learning", return_value=self.inventory)
        self.inventory_patch.start()
        self.addCleanup(self.inventory_patch.stop)

    def _create_state_db(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            db.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, cwd TEXT, parent_session_id TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, message_uid TEXT,
                    active INTEGER DEFAULT 1
                );
            """)
            db.execute("INSERT INTO sessions VALUES (?, 'cli', ?, NULL)",
                       (self.session, str(self.project)))
            rows = [
                (1, "user", "Use demo for this request.", None, None, "prompt-uid"),
                (2, "assistant", "Here is the result.", None, None, self.reply_uid),
                (3, "user", "I verified it works.", None, None, self.feedback_uid),
                (4, "assistant", "Thanks for confirming.", None, None, self.final_uid),
            ]
            for row_id, role, content, call_id, calls, uid in rows:
                db.execute("INSERT INTO messages(id, session_id, role, content, tool_call_id, tool_calls, message_uid) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (row_id, self.session, role, content, call_id, calls, uid))
            db.commit()

    def _create_journal(self, *, proven):
        with closing(sqlite3.connect(self.journal_path)) as db:
            db.executescript("""
                CREATE TABLE messages (
                    seq INTEGER PRIMARY KEY, harness TEXT, session TEXT, turn TEXT,
                    role TEXT, bindings TEXT, diagnostic TEXT, case_key TEXT, excerpt TEXT
                );
                CREATE TABLE hermes_replies (session TEXT, turn TEXT, uid TEXT,
                    PRIMARY KEY(session, turn), UNIQUE(session, uid));
            """)
            db.execute("INSERT INTO messages(harness, session, turn, role, bindings) VALUES "
                       "('hermes', ?, 'skill-turn', 'user', '{}')", (self.session,))
            binding = json.dumps({"demo": self.version}) if proven else "{}"
            db.execute("INSERT INTO messages(harness, session, turn, role, bindings) VALUES "
                       "('hermes', ?, 'skill-turn', 'assistant', ?)", (self.session, binding))
            db.execute("INSERT INTO hermes_replies VALUES (?, 'skill-turn', ?)",
                       (self.session, hashlib.sha256(self.reply_uid.encode()).hexdigest()))
            db.commit()

    def recover(self):
        return hermes_history.recover(self.home, self.project, self.session)

    def test_recovers_feedback_only_with_existing_binding_and_reply_uid_proof(self):
        result = self.recover()
        self.assertEqual(result["audited"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["items"], [{
            "target_turn": "skill-turn",
            "feedback_turn": self.feedback_uid,
            "feedback": "I verified it works.",
            "reply_uid_sha256": hashlib.sha256(self.reply_uid.encode()).hexdigest(),
            "final_reply_uid": self.final_uid,
            "bindings": {"demo": self.version},
        }])

    def test_tool_record_or_current_skill_hash_cannot_replace_historical_journal_proof(self):
        self.journal_path.unlink()
        self._create_journal(proven=False)
        self.assertEqual(self.recover()["items"], [])

    def test_incomplete_feedback_turn_is_not_replayed(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            db.execute("DELETE FROM messages WHERE id=4")
            db.commit()
        result = self.recover()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["skipped"], 1)

    def test_only_one_local_session_inside_exact_enrolled_project_is_accepted(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            db.execute("UPDATE sessions SET source='telegram' WHERE id=?", (self.session,))
            db.commit()
        with self.assertRaisesRegex(ValueError, "local, non-child"):
            self.recover()

    def test_missing_recorded_cwd_fails_closed(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            db.execute("UPDATE sessions SET cwd=NULL WHERE id=?", (self.session,))
            db.commit()
        with self.assertRaisesRegex(ValueError, "absolute recorded project directory"):
            self.recover()

    def test_incompatible_hermes_schema_returns_audit_reason(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            db.executescript("DROP TABLE messages; CREATE TABLE messages (id INTEGER PRIMARY KEY);")
            db.commit()
        result = self.recover()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["reason"], "unsupported_profile_schema")

    def test_missing_profile_journal_returns_empty_audit(self):
        self.journal_path.unlink()
        result = self.recover()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["reason"], "skillcraft_journal_unavailable")


if __name__ == "__main__":
    unittest.main()
