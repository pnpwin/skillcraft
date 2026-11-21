"""Recover feedback from one already-recorded local Hermes chat.

This module deliberately reads one requested session from the selected Hermes
profile. Transcript rows alone are not historical proof of a SkillCraft read:
the profile's SkillCraft journal must already contain the assistant binding and
the hashed native reply UID recorded when that read was verified.
"""

from hashlib import sha256
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .hermes_learning import run_learning

LOCAL_SOURCES = {"cli", "tui", "desktop"}
HEX_SHA256 = frozenset("0123456789abcdef")


def _readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _decode_content(value):
    # Hermes stores structured content with this NUL-prefixed envelope. Text
    # rows are returned unchanged; malformed envelopes are rejected by callers.
    if isinstance(value, str) and value.startswith("\x00json:"):
        return json.loads(value[len("\x00json:"):])
    return value


def _uid(row):
    value = row.get("message_uid")
    return value if isinstance(value, str) and value else None


def _tool_calls(row):
    calls = row.get("tool_calls")
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except (TypeError, ValueError):
            return None
    if calls is None:
        return []
    return calls if isinstance(calls, list) else None


def _complete_final(rows):
    """Return the final durable assistant row only for a complete tool chain."""
    if not rows:
        return None
    seen_calls = set()
    seen_results = set()
    for row in rows:
        role = row["role"]
        if role == "assistant":
            calls = _tool_calls(row)
            if calls is None:
                return None
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
                    return None
                if call["id"] in seen_calls:
                    return None
                seen_calls.add(call["id"])
        elif role == "tool":
            call_id = row.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in seen_calls or call_id in seen_results:
                return None
            seen_results.add(call_id)
        elif role not in {"user", "system", "developer"}:
            return None
    if seen_calls != seen_results:
        return None
    final = rows[-1]
    if (final["role"] != "assistant" or _tool_calls(final) != []
            or not isinstance(final.get("content"), str) or not final["content"].strip()
            or not _uid(final)):
        return None
    return final


def _journal_proofs(journal, session_id, current_versions):
    proofs = {}
    with closing(_readonly(journal)) as db:
        db.row_factory = sqlite3.Row
        if ({"harness", "session", "turn", "role", "bindings"} - _columns(db, "messages")
                or {"session", "turn", "uid"} - _columns(db, "hermes_replies")):
            return proofs
        replies = db.execute(
            "SELECT turn, uid FROM hermes_replies WHERE session=?", (session_id,)
        ).fetchall()
        for reply in replies:
            turn, hashed_uid = reply["turn"], reply["uid"]
            if (not isinstance(hashed_uid, str) or len(hashed_uid) != 64
                    or any(char not in HEX_SHA256 for char in hashed_uid)):
                continue
            rows = db.execute(
                "SELECT role, bindings FROM messages WHERE harness='hermes' AND session=? AND turn=?",
                (session_id, turn),
            ).fetchall()
            user_seen = any(row["role"] == "user" for row in rows)
            assistant = next((row for row in rows if row["role"] == "assistant"), None)
            if not user_seen or assistant is None:
                continue
            try:
                bindings = json.loads(assistant["bindings"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(bindings, dict):
                continue
            matched = {name: version for name, version in bindings.items()
                       if isinstance(name, str) and current_versions.get(name) == version}
            if matched:
                proofs[hashed_uid] = (turn, matched)
    return proofs


def recover(home, project, session_id):
    """Audit and return recoverable feedback from one enrolled local session.

    The return value is a JSON-safe envelope for ``recover-hermes``. No native
    transcript content is imported as a synthetic message. The only journal
    write belongs to the caller after this audit returns.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("A single Hermes session id is required")
    home = Path(home).expanduser().resolve(strict=True)
    project = Path(project).expanduser().resolve(strict=True)
    inventory = run_learning(home, "inventory", str(project))
    if not isinstance(inventory, dict):
        return {"session": session_id, "items": [], "audited": 0, "skipped": 0,
                "reason": "project_not_enrolled"}
    root = Path(inventory.get("root", "")).resolve(strict=True)
    if root != project:
        raise ValueError("The selected project must be the exact enrolled project root")
    current_versions = {}
    for item in inventory.get("versions", []):
        if (isinstance(item, dict) and isinstance(item.get("path"), str)
                and isinstance(item.get("snapshot_hash"), str)):
            current_versions[Path(item["path"]).parent.name] = item["snapshot_hash"]
    if not current_versions:
        return {"session": session_id, "items": [], "audited": 0, "skipped": 0}

    db_path = home / "state.db"
    if not db_path.is_file() or db_path.is_symlink():
        return {"session": session_id, "items": [], "audited": 0, "skipped": 0,
                "reason": "profile_database_unavailable"}
    try:
        with closing(_readonly(db_path)) as db:
            db.row_factory = sqlite3.Row
            if ({"id", "source", "cwd", "parent_session_id"} - _columns(db, "sessions")
                    or {"id", "session_id", "role", "content", "tool_call_id", "tool_calls",
                        "tool_name", "message_uid", "active"} - _columns(db, "messages")):
                return {"session": session_id, "items": [], "audited": 0, "skipped": 0,
                        "reason": "unsupported_profile_schema"}
            session = db.execute(
                "SELECT id, source, cwd, parent_session_id FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                return {"session": session_id, "items": [], "audited": 0, "skipped": 0,
                        "reason": "session_not_found"}
            recorded_cwd = session["cwd"]
            if not isinstance(recorded_cwd, str) or not recorded_cwd.strip() or not Path(recorded_cwd).is_absolute():
                raise ValueError("The selected session has no absolute recorded project directory")
            cwd = Path(recorded_cwd).expanduser().resolve()
            if (session["source"] not in LOCAL_SOURCES or session["parent_session_id"]
                    or not cwd.is_relative_to(project)):
                raise ValueError("Only a local, non-child session inside the enrolled project can be recovered")
            # Active rows are Hermes' current transcript projection. Rewound and
            # compaction-archived variants are excluded to avoid replaying duplicates.
            native_rows = [dict(row) for row in db.execute(
                "SELECT id, role, content, tool_call_id, tool_calls, tool_name, message_uid "
                "FROM messages WHERE session_id=? AND active=1 ORDER BY id",
                (session_id,),
            )]
    except sqlite3.Error:
        return {"session": session_id, "items": [], "audited": 0, "skipped": 0,
                "reason": "profile_database_unreadable"}

    journal = home / "skillcraft" / "learning" / sha256(str(project).encode()).hexdigest() / "journal.sqlite"
    if not journal.is_file() or journal.is_symlink():
        return {"session": session_id, "items": [], "audited": 0, "skipped": 0,
                "reason": "skillcraft_journal_unavailable"}
    proofs = _journal_proofs(journal, session_id, current_versions)

    normalized = []
    for raw in native_rows:
        row = dict(raw)
        try:
            row["content"] = _decode_content(row.get("content"))
        except (TypeError, ValueError):
            row["content"] = None
        normalized.append(row)

    turns = []
    current = []
    for row in normalized:
        if row["role"] == "user":
            if current:
                turns.append(current)
            current = [row]
        elif current:
            current.append(row)
    if current:
        turns.append(current)

    items = []
    skipped = 0
    for index, turn_rows in enumerate(turns):
        user = turn_rows[0]
        if len(turn_rows) < 2 or not isinstance(user.get("content"), str) or not _uid(user):
            skipped += 1
            continue
        final = _complete_final(turn_rows[1:])
        if final is None:
            skipped += 1
            continue
        if index == 0:
            continue
        prior_rows = turns[index - 1]
        prior_final = _complete_final(prior_rows[1:]) if len(prior_rows) > 1 else None
        if (prior_final is None or prior_rows[-1] is not prior_final
                or prior_final.get("role") != "assistant"):
            skipped += 1
            continue
        reply_uid = _uid(prior_final)
        reply_hash = sha256(reply_uid.encode()).hexdigest()
        proof = proofs.get(reply_hash)
        if not proof:
            continue
        target_turn, bindings = proof
        # A proof only counts if at least one exact currently enrolled version
        # is present. The tool read itself is proven by the captured binding;
        # current transcript tool output cannot manufacture a past tree hash.
        items.append({
            "target_turn": target_turn,
            "feedback_turn": _uid(user),
            "feedback": user["content"],
            "reply_uid_sha256": reply_hash,
            "final_reply_uid": _uid(final),
            "bindings": bindings,
        })

    return {"session": session_id, "items": items, "audited": len(turns), "skipped": skipped}
