"""Gemini CLI chat recordings: ``~/.gemini/tmp/<project>/chats/session-*.json``.

Best effort: written against the documented recording format (one JSON object with
``sessionId``, ``projectHash``, ``startTime`` and ``messages``) without real samples to test
against. A JSONL variant (a metadata line followed by one message per line) is also accepted.
Shell commands come from ``run_shell_command``; their exit code is read from the
``Exit Code: N`` line of the tool result when present.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ruleproof.errors import TranscriptError
from ruleproof.models import EditAction, Event, EventKind, Session
from ruleproof.transcripts._shell import truncate, unwrap
from ruleproof.transcripts._util import (
    RECORD_ERRORS,
    content_text,
    finish,
    iter_records,
    str_or_none,
    summarize,
    warn,
)

AGENT = "gemini-cli"
SHELL_TOOLS = {"run_shell_command"}
EDIT_TOOLS: dict[str, EditAction] = {"write_file": "unknown", "replace": "modify", "edit": "modify"}
_EXIT_RE = re.compile(r"^Exit Code: (-?\d+)\s*$", re.MULTILINE)
MESSAGE_TYPES = {"user", "gemini", "info", "error", "warning"}


def parse(path: Path, include_subagents: bool = True) -> Session:
    del include_subagents  # Gemini CLI does not record subagents separately
    session = Session(agent=AGENT, id=path.stem, path=str(path))
    warnings: list[str] = []
    messages: list[dict[str, Any]] = []
    recognised = False
    for rec in iter_records(path, warnings):
        if isinstance(rec.get("messages"), list):
            recognised = True
            _meta(session, rec)
            messages.extend(m for m in rec["messages"] if isinstance(m, dict))
        elif rec.get("type") in MESSAGE_TYPES:
            recognised = True
            messages.append(rec)
        elif "sessionId" in rec or "projectHash" in rec:
            recognised = True
            _meta(session, rec)
    if not recognised:
        raise TranscriptError(f"{path}: no Gemini CLI chat recording found")
    for msg in messages:
        try:
            _message(session, msg)
        except RECORD_ERRORS as exc:
            warn(warnings, f"skipped a malformed message ({type(exc).__name__})")
    for w in warnings:
        warn(session.warnings, w)
    return finish(session)


def _meta(session: Session, rec: dict[str, Any]) -> None:
    session.id = str_or_none(rec.get("sessionId")) or session.id
    session.started_at = session.started_at or str_or_none(rec.get("startTime"))
    session.cwd = session.cwd or str_or_none(rec.get("cwd")) or str_or_none(rec.get("projectRoot"))


def _message(session: Session, msg: dict[str, Any]) -> None:
    ts = str_or_none(msg.get("timestamp"))
    mtype = msg.get("type")
    text = content_text(msg.get("content")).strip()
    if mtype == "user" and text:
        session.events.append(Event(0, EventKind.USER, text, ts))
    elif mtype == "gemini":
        session.model = session.model or str_or_none(msg.get("model"))
        if text:
            session.events.append(Event(0, EventKind.ASSISTANT, text, ts))
        calls = msg.get("toolCalls") or []
        if not isinstance(calls, list):
            raise TypeError("toolCalls is not a list")
        for call in calls:
            if isinstance(call, dict):
                session.events.append(_tool_call(call, ts))


def _tool_call(call: dict[str, Any], msg_ts: str | None) -> Event:
    name = str(call.get("name") or "")
    raw_args = call.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    ts = str_or_none(call.get("timestamp")) or msg_ts
    status = call.get("status")
    is_error = status in ("error", "cancelled") if isinstance(status, str) else None
    output = _result_text(call)
    if name in SHELL_TOOLS:
        cmd = args.get("command")
        ev = Event(0, EventKind.COMMAND, unwrap(cmd) if isinstance(cmd, str) else "", ts, name)
        found = _EXIT_RE.findall(output)
        ev.exit_code = int(found[-1]) if found else None
        ev.output, ev.is_error = truncate(output), is_error
        return ev
    if name in EDIT_TOOLS:
        path = str_or_none(args.get("file_path")) or str_or_none(args.get("path"))
        ev = Event(0, EventKind.EDIT, path or "", ts, name, path=path, action=EDIT_TOOLS[name])
        ev.is_error = is_error
        return ev
    ev = Event(0, EventKind.TOOL, summarize(args), ts, name, output=truncate(output))
    ev.is_error = is_error
    return ev


def _result_text(call: dict[str, Any]) -> str:
    """Text of a tool result: a string, or function-response parts, else ``resultDisplay``."""
    result = call.get("result")
    if isinstance(result, str):
        return result
    texts: list[str] = []
    for part in result if isinstance(result, list) else [result]:
        if not isinstance(part, dict):
            continue
        fr = part.get("functionResponse")
        response = fr.get("response") if isinstance(fr, dict) else None
        if isinstance(response, dict):
            for key in ("output", "error", "llmContent"):
                if isinstance(response.get(key), str):
                    texts.append(response[key])
        elif isinstance(part.get("text"), str):
            texts.append(part["text"])
    if texts:
        return "\n".join(texts)
    display = call.get("resultDisplay")
    return display if isinstance(display, str) else ""
