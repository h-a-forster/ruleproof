"""Render sessions as a text timeline, JSON, or a SQLite database (Datasette-friendly)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ruleproof.models import Event, EventKind, Session
from ruleproof.transcripts._util import one_line

_TEXT_WIDTH = 120

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    path TEXT NOT NULL,
    cwd TEXT,
    started_at TEXT,
    model TEXT,
    event_count INTEGER NOT NULL,
    warnings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    kind TEXT NOT NULL,
    timestamp TEXT,
    actor TEXT NOT NULL,
    tool TEXT,
    text TEXT NOT NULL,
    path TEXT,
    action TEXT,
    exit_code INTEGER,
    is_error INTEGER,
    output TEXT NOT NULL,
    PRIMARY KEY (session_id, idx)
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
"""


def render_text(session: Session) -> str:
    """A compact timeline: one line per event."""
    lines = [
        f"session {session.id}  agent={session.agent}"
        + (f"  model={session.model}" if session.model else ""),
        f"  path: {session.path}",
    ]
    if session.cwd:
        lines.append(f"  cwd:  {session.cwd}")
    if session.started_at:
        lines.append(f"  started: {session.started_at}")
    lines.append(f"  events: {len(session.events)}")
    lines.append("")
    width = len(str(max(len(session.events) - 1, 0)))
    for ev in session.events:
        lines.append(_event_line(ev, width))
    if session.warnings:
        lines.append("")
        lines.append(f"warnings ({len(session.warnings)}):")
        lines.extend(f"  {w}" for w in session.warnings)
    return "\n".join(lines) + "\n"


def _event_line(ev: Event, width: int) -> str:
    time = _clock(ev.timestamp)
    actor = "" if ev.actor == "main" else f"[{ev.actor}] "
    if ev.kind is EventKind.EDIT:
        body = f"{ev.action or 'unknown'} {ev.path or ev.text}"
    elif ev.kind in (EventKind.TOOL, EventKind.COMMAND) and ev.tool:
        body = f"{ev.tool}: {ev.text}" if ev.kind is EventKind.TOOL else ev.text
    else:
        body = ev.text
    suffix = ""
    if ev.kind is EventKind.COMMAND:
        suffix = f"  [exit {ev.exit_code if ev.exit_code is not None else '?'}]"
    elif ev.is_error:
        suffix = "  [error]"
    text = one_line(body, _TEXT_WIDTH)
    return f"{ev.index:>{width}}  {time}  {actor}{ev.kind.value:<9} {text}{suffix}"


def _clock(timestamp: str | None) -> str:
    if timestamp and len(timestamp) >= 19 and timestamp[10] in "T ":
        return timestamp[11:19]
    return "--:--:--"


def to_dict(session: Session) -> dict[str, Any]:
    data = asdict(session)
    for ev in data["events"]:
        ev["kind"] = str(ev["kind"])
    return data


def to_json(session: Session) -> str:
    return json.dumps(to_dict(session), ensure_ascii=False, indent=2) + "\n"


def write_sqlite(sessions: list[Session], path: Path) -> None:
    """Write sessions to a SQLite file; re-writing a session id replaces its rows."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        with conn:
            for s in sessions:
                conn.execute("DELETE FROM events WHERE session_id = ?", (s.id,))
                conn.execute(
                    "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        s.id,
                        s.agent,
                        s.path,
                        s.cwd,
                        s.started_at,
                        s.model,
                        len(s.events),
                        json.dumps(s.warnings, ensure_ascii=False),
                    ),
                )
                conn.executemany(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            s.id,
                            e.index,
                            e.kind.value,
                            e.timestamp,
                            e.actor,
                            e.tool,
                            e.text,
                            e.path,
                            e.action,
                            e.exit_code,
                            None if e.is_error is None else int(e.is_error),
                            e.output,
                        )
                        for e in s.events
                    ],
                )
