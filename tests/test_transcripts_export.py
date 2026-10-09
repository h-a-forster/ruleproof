from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ruleproof.models import Event, EventKind, Session
from ruleproof.transcripts import load_session
from ruleproof.transcripts.export import render_text, to_dict, to_json, write_sqlite

FIX = Path(__file__).parent / "fixtures" / "transcripts"


def _session(sid: str, n: int) -> Session:
    events = [
        Event(0, EventKind.USER, "do it", "2026-01-01T10:00:00Z"),
        Event(
            1,
            EventKind.COMMAND,
            "pytest -q",
            "2026-01-01T10:00:05Z",
            "Bash",
            exit_code=1,
            output="1 failed",
            is_error=True,
        ),
        Event(2, EventKind.EDIT, "a.py", None, "Edit", path="a.py", action="modify", actor="sub1"),
        Event(3, EventKind.ASSISTANT, "done\nsecond line"),
    ][:n]
    return Session("claude-code", sid, f"/t/{sid}.jsonl", cwd="/w", events=events, warnings=["w1"])


def test_render_text() -> None:
    text = render_text(_session("s1", 4))
    lines = text.splitlines()
    assert lines[0] == "session s1  agent=claude-code"
    assert "0  10:00:00  user      do it" in text
    assert "1  10:00:05  command   pytest -q  [exit 1]" in text
    assert "2  --:--:--  [sub1] edit      modify a.py" in text
    assert "3  --:--:--  assistant done second line" in text
    assert lines[-1] == "  w1"


def test_render_text_real_fixture() -> None:
    text = render_text(load_session(FIX / "codex_codemode.jsonl"))
    assert "[exit ?]" in text and "[exit 0]" in text


def test_to_json_round_trip() -> None:
    s = load_session(FIX / "gemini_session.json")
    data = json.loads(to_json(s))
    assert data == json.loads(json.dumps(to_dict(s)))
    assert data["id"] == s.id
    assert [e["kind"] for e in data["events"]] == [e.kind.value for e in s.events]
    assert data["events"][4]["exit_code"] == 0


def test_write_sqlite_replaces_rows(tmp_path: Path) -> None:
    db = tmp_path / "out.db"
    write_sqlite([_session("s1", 4), _session("s2", 2)], db)
    write_sqlite([_session("s1", 1)], db)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT id, event_count FROM sessions ORDER BY id").fetchall() == [
            ("s1", 1),
            ("s2", 2),
        ]
        rows = conn.execute(
            "SELECT session_id, idx, kind, exit_code, is_error FROM events ORDER BY session_id, idx"
        ).fetchall()
        assert rows == [
            ("s1", 0, "user", None, None),
            ("s2", 0, "user", None, None),
            ("s2", 1, "command", 1, 1),
        ]
        (warnings,) = conn.execute("SELECT warnings FROM sessions WHERE id = 's2'").fetchone()
        assert json.loads(warnings) == ["w1"]
        fks = conn.execute("PRAGMA foreign_key_list(events)").fetchall()
        assert fks[0][2] == "sessions"


def test_write_sqlite_real_fixture(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    s = load_session(FIX / "claude" / "11111111-2222-3333-4444-555555555555.jsonl")
    write_sqlite([s], db)
    with closing(sqlite3.connect(db)) as conn:
        (count,) = conn.execute("SELECT count(*) FROM events").fetchone()
        actors = {a for (a,) in conn.execute("SELECT DISTINCT actor FROM events")}
    assert count == len(s.events)
    assert actors == {"main", "a0b1c2"}
