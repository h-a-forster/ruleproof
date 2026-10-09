"""ruleproof's own transcript format: JSONL, one ``Event`` per line.

Each line is an object with ``kind`` (user, assistant, command, edit, tool) and any ``Event``
field. ``index`` is optional and ignored: events are numbered in file order. The first line may
be ``{"session": {"agent": ..., "id": ..., "cwd": ..., "started_at": ..., "model": ...}}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

from ruleproof.errors import TranscriptError
from ruleproof.models import EditAction, Event, EventKind, Session
from ruleproof.transcripts._shell import truncate
from ruleproof.transcripts._util import finish, int_or_none, iter_records, str_or_none, warn

AGENT = "generic"
KINDS = {k.value for k in EventKind}
_ACTIONS: set[str] = set(get_args(EditAction))
_STR_FIELDS = ("text", "timestamp", "tool", "path", "output", "actor")


def parse(path: Path, include_subagents: bool = True) -> Session:
    del include_subagents
    session = Session(agent=AGENT, id=path.stem, path=str(path))
    warnings: list[str] = []
    recognised = False
    for n, rec in enumerate(iter_records(path, warnings), 1):
        if n == 1 and isinstance(rec.get("session"), dict):
            recognised = True
            _header(session, rec["session"])
            continue
        kind = rec.get("kind")
        if kind not in KINDS:
            warn(warnings, f"record {n}: unknown kind {kind!r}")
            continue
        recognised = True
        session.events.append(_event(rec, EventKind(kind), warnings, n))
    if not recognised:
        raise TranscriptError(f"{path}: no ruleproof events found")
    for w in warnings:
        warn(session.warnings, w)
    return finish(session)


def _header(session: Session, info: dict[str, Any]) -> None:
    session.agent = str_or_none(info.get("agent")) or AGENT
    session.id = str_or_none(info.get("id")) or session.id
    session.cwd = str_or_none(info.get("cwd"))
    session.started_at = str_or_none(info.get("started_at"))
    session.model = str_or_none(info.get("model"))


def _event(rec: dict[str, Any], kind: EventKind, warnings: list[str], n: int) -> Event:
    ev = Event(0, kind)
    for name in _STR_FIELDS:
        value = rec.get(name)
        if value is None:
            continue
        if isinstance(value, str):
            setattr(ev, name, value)
        else:
            warn(warnings, f"record {n}: {name} should be a string")
    ev.actor = ev.actor or "main"
    ev.output = truncate(ev.output)
    if rec.get("exit_code") is not None:
        ev.exit_code = int_or_none(rec["exit_code"])
        if ev.exit_code is None:
            warn(warnings, f"record {n}: exit_code should be an integer")
    if isinstance(rec.get("is_error"), bool):
        ev.is_error = rec["is_error"]
    action = rec.get("action")
    if kind is EventKind.EDIT:
        if action in _ACTIONS:
            ev.action = action
        else:
            if action is not None:
                warn(warnings, f"record {n}: unknown edit action {action!r}")
            ev.action = "unknown"
        if ev.path is None and ev.text:
            ev.path = ev.text
    return ev
