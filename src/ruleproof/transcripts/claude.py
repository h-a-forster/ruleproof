"""Claude Code transcripts: ``~/.claude/projects/<encoded-cwd>/<id>.jsonl``.

Also reads headless ``claude -p --output-format stream-json --verbose`` output (JSONL or a JSON
array of the same records). Subagent transcripts in ``<id>/subagents/**/agent-*.jsonl`` are
merged by timestamp with ``actor`` set to the subagent id.

User events are what a person typed: prompts, messages queued while the agent worked
(``queued_command`` attachments) and slash commands (as ``/name args``). Text the harness
injects as a user turn (background-task notifications, messages from other sessions, local
command output, system reminders) is not a user event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    merge_streams,
    str_or_none,
    summarize,
    tool_input,
    warn,
)

AGENT = "claude-code"
SHELL_TOOLS = {"Bash", "PowerShell"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_EXIT_RE = re.compile(r"^(?:Error: )?Exit code (-?\d+)")
_WRITE_ACTIONS: dict[str, EditAction] = {"create": "add", "update": "modify"}
_CLAUDE_TYPES = {"user", "assistant", "system", "result"}
# User-turn text the harness writes, not the person (older transcripts have no `origin`).
_INJECTED_PREFIXES = (
    "<task-notification>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "Another Claude session sent",
)
_SLASH_RE = re.compile(r"<command-name>\s*(/?[^<]*?)\s*</command-name>")
_SLASH_ARGS_RE = re.compile(r"<command-args>([\s\S]*?)</command-args>")


@dataclass(slots=True)
class _Pending:
    event: Event
    background: bool = False


@dataclass(slots=True)
class _State:
    session: Session
    actor: str
    events: list[Event] = field(default_factory=list)
    pending: dict[str, _Pending] = field(default_factory=dict)
    seen_text: set[tuple[str, str]] = field(default_factory=set)
    recognised: int = 0
    got_id: bool = False


def parse(path: Path, include_subagents: bool = True) -> Session:
    session = Session(agent=AGENT, id=path.stem, path=str(path))
    main = _parse_stream(path, session, actor="main")
    if main.recognised == 0:
        raise TranscriptError(f"{path}: no Claude Code records found")
    streams = [main.events]
    if include_subagents:
        sub_dir = path.with_suffix("") / "subagents"
        if sub_dir.is_dir():
            for sub in sorted(sub_dir.rglob("agent-*.jsonl")):
                actor = sub.stem.removeprefix("agent-")
                state = _parse_stream(sub, session, actor=actor, primary=False)
                streams.append(state.events)
    session.events = merge_streams(streams) if len(streams) > 1 else main.events
    return finish(session)


def _parse_stream(path: Path, session: Session, actor: str, primary: bool = True) -> _State:
    st = _State(session=session, actor=actor)
    warnings: list[str] = []
    for rec in iter_records(path, warnings):
        if not isinstance(rec.get("type"), str):
            continue
        if rec["type"] in _CLAUDE_TYPES or rec.get("sessionId"):
            st.recognised += 1
        try:
            if primary:
                _session_info(st, rec)
            if rec["type"] == "assistant":
                _assistant(st, rec)
            elif rec["type"] == "user":
                _user(st, rec)
            elif rec["type"] == "attachment":
                _attachment(st, rec)
        except RECORD_ERRORS as exc:
            warn(warnings, f"skipped a malformed {rec['type']} record ({type(exc).__name__})")
    prefix = "" if primary else f"{path.name}: "
    for w in warnings:
        warn(session.warnings, prefix + w)
    return st


def _session_info(st: _State, rec: dict[str, Any]) -> None:
    session = st.session
    sid = str_or_none(rec.get("sessionId")) or str_or_none(rec.get("session_id"))
    if sid and not st.got_id:
        session.id, st.got_id = sid, True
    if session.cwd is None:
        session.cwd = str_or_none(rec.get("cwd"))
    if session.started_at is None:
        session.started_at = str_or_none(rec.get("timestamp"))
    if session.model is None:
        if rec.get("type") == "system":
            session.model = str_or_none(rec.get("model"))
        elif rec.get("type") == "assistant" and isinstance(rec.get("message"), dict):
            model = str_or_none(rec["message"].get("model"))
            if model and not model.startswith("<"):
                session.model = model


def _actor(st: _State, rec: dict[str, Any]) -> str:
    if st.actor != "main":
        return st.actor
    parent = str_or_none(rec.get("parent_tool_use_id"))  # stream-json subagent records
    if parent:
        return parent
    if rec.get("isSidechain") is True:
        return str_or_none(rec.get("agentId")) or "sidechain"
    return "main"


def _assistant(st: _State, rec: dict[str, Any]) -> None:
    msg = rec.get("message") or {}
    content = msg.get("content")
    ts = str_or_none(rec.get("timestamp"))
    actor = _actor(st, rec)
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return
    msg_id = str(msg.get("id") or rec.get("uuid") or "")
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text") or "").strip()
            key = (msg_id, text)
            if text and (not msg_id or key not in st.seen_text):
                st.seen_text.add(key)
                st.events.append(Event(0, EventKind.ASSISTANT, text, ts, actor=actor))
        elif btype == "tool_use":
            tool_id = str(block.get("id") or "")
            if tool_id and tool_id in st.pending:
                continue  # the same block re-emitted by a streaming writer
            ev, background = _tool_event(block, ts, actor)
            st.events.append(ev)
            if tool_id:
                st.pending[tool_id] = _Pending(ev, background)


def _tool_event(block: dict[str, Any], ts: str | None, actor: str) -> tuple[Event, bool]:
    name = str(block.get("name") or "")
    inp = block.get("input")
    if not isinstance(inp, dict):
        inp = {}
    if name in SHELL_TOOLS:
        cmd = inp.get("command")
        text = unwrap(cmd) if isinstance(cmd, (str, list)) else ""
        ev = Event(0, EventKind.COMMAND, text, ts, tool=name, actor=actor)
        return ev, bool(inp.get("run_in_background"))
    if name in EDIT_TOOLS:
        path = str_or_none(inp.get("file_path")) or str_or_none(inp.get("notebook_path"))
        action: EditAction = "unknown" if name == "Write" else "modify"
        ev = Event(0, EventKind.EDIT, path or "", ts, tool=name, path=path, action=action)
        ev.actor = actor
        return ev, False
    ev = Event(0, EventKind.TOOL, summarize(inp), ts, tool=name, actor=actor)
    ev.input = tool_input(inp)
    return ev, False


def _human_text(text: str) -> str | None:
    """What the person typed, or None for text the harness injected as a user turn."""
    text = text.strip()
    if not text or text.startswith(_INJECTED_PREFIXES):
        return None
    slash = _SLASH_RE.search(text)
    if slash and text.startswith(("<command-name>", "<command-message>")):
        args = _SLASH_ARGS_RE.search(text)
        name = slash.group(1)
        name = name if name.startswith("/") else "/" + name
        return f"{name} {args.group(1).strip()}".strip() if args else name
    return text


def _from_person(rec: dict[str, Any]) -> bool:
    """False for a user turn the harness or another session produced (``origin.kind``)."""
    origin = rec.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return kind == "human" if isinstance(kind, str) else True


def _add_user(st: _State, rec: dict[str, Any], text: str, ts: str | None) -> None:
    human = _human_text(text)
    if human:
        st.events.append(Event(0, EventKind.USER, human, ts, actor=_actor(st, rec)))


def _user(st: _State, rec: dict[str, Any]) -> None:
    if rec.get("isMeta") is True or rec.get("isCompactSummary") is True:
        return
    msg = rec.get("message") or {}
    content = msg.get("content")
    ts = str_or_none(rec.get("timestamp"))
    person = _from_person(rec)
    if isinstance(content, str):
        if person:
            _add_user(st, rec, content, ts)
        return
    if not isinstance(content, list):
        return
    results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    texts = [
        str(b.get("text") or "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and str(b.get("text") or "").strip()
    ]
    if texts and not results and person:
        _add_user(st, rec, "\n".join(texts), ts)
    extra = rec.get("toolUseResult", rec.get("tool_use_result")) if len(results) == 1 else None
    for block in results:
        pending = st.pending.get(str(block.get("tool_use_id") or ""))
        if pending is not None:
            _apply_result(pending, block, extra)


def _attachment(st: _State, rec: dict[str, Any]) -> None:
    """A message the person typed while the agent worked (delivered as an attachment)."""
    att = rec.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command":
        return
    if not _from_person(att) or att.get("commandMode") not in (None, "prompt"):
        return
    ts = str_or_none(rec.get("timestamp")) or str_or_none(att.get("timestamp"))
    _add_user(st, rec, content_text(att.get("prompt")), ts)


def _apply_result(pending: _Pending, block: dict[str, Any], extra: object) -> None:
    ev = pending.event
    output = content_text(block.get("content"))
    is_error = block.get("is_error") is True
    ev.is_error = is_error
    if ev.kind is EventKind.EDIT:
        if ev.tool == "Write" and not is_error:
            ev.action = _write_action(extra, output)
        return
    ev.output = truncate(output)
    if ev.kind is not EventKind.COMMAND:
        return
    m = _EXIT_RE.match(output.lstrip()) or (
        _EXIT_RE.match(extra.lstrip()) if isinstance(extra, str) else None
    )
    background = pending.background or (  # also set when the user backgrounded the command
        isinstance(extra, dict) and bool(extra.get("backgroundTaskId"))
    )
    interrupted = isinstance(extra, dict) and extra.get("interrupted") is True
    if m:
        ev.exit_code = int(m.group(1))
    elif not is_error and not background and not interrupted:
        ev.exit_code = 0


def _write_action(extra: object, output: str) -> EditAction:
    """add/modify for a Write call, from ``toolUseResult.type`` or the result text."""
    if isinstance(extra, dict) and extra.get("type") in _WRITE_ACTIONS:
        return _WRITE_ACTIONS[str(extra["type"])]
    if output.startswith("File created successfully"):
        return "add"
    if "has been updated successfully" in output:
        return "modify"
    return "unknown"
