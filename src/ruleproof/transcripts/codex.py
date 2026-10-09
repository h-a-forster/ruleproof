"""Codex CLI transcripts: rollout files and ``codex exec --json`` event streams.

Rollouts live in ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` as ``{timestamp, type,
payload}`` records. Three generations of tool calls are handled:

- function calls ``shell`` / ``exec_command`` / ``local_shell_call`` and ``apply_patch`` (older);
- code-mode ``exec`` cells: JavaScript calling ``tools.exec_command({cmd})``,
  ``tools.apply_patch(...)`` and other ``tools.X`` (see ``_codemode``);
- ``event_msg`` ``item_completed`` records carrying ``CommandExecution`` (argv, exit code) and
  ``FileChange`` items. When a rollout has these, they are authoritative for commands and edits
  and the code-mode cells only contribute the other tool calls.

Subagent threads are separate rollouts whose ``session_meta`` names the parent thread; they are
merged by timestamp with ``actor`` set to the subagent's nickname or id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruleproof.errors import TranscriptError
from ruleproof.models import EditAction, Event, EventKind, Session
from ruleproof.transcripts._codemode import parse_cell, patch_files
from ruleproof.transcripts._shell import truncate, unwrap
from ruleproof.transcripts._util import (
    content_text,
    finish,
    int_or_none,
    iter_records,
    merge_streams,
    ms_to_iso,
    one_line,
    open_text,
    str_or_none,
    summarize,
    warn,
)

AGENT = "codex"
ROLLOUT_TYPES = {
    "session_meta",
    "turn_context",
    "response_item",
    "event_msg",
    "compacted",
    "world_state",
}
RESPONSE_TYPES = {
    "message",
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "local_shell_call",
    "reasoning",
    "web_search_call",
}
_SHELL_FUNCTIONS = {"shell", "container.exec", "shell_command", "local_shell"}
_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
_INJECTED_RE = re.compile(r"^\s*<([A-Za-z_][\w-]*)[^>]*>.*</\1>\s*$", re.S)
_INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>", "<user_instructions>")
_EXIT_MARKERS = (
    re.compile(r'"exit_code"\s*:\s*(-?\d+)'),
    re.compile(r"Process exited with code (-?\d+)"),
    re.compile(r"\bexit code:?\s*(-?\d+)", re.I),
)
_EDIT_KINDS: dict[str, EditAction] = {
    "add": "add",
    "create": "add",
    "update": "modify",
    "modify": "modify",
    "delete": "delete",
    "remove": "delete",
}
MAX_SUBAGENTS = 200


@dataclass(slots=True)
class _State:
    session: Session
    actor: str
    items_mode: bool = False
    events: list[Event] = field(default_factory=list)
    pending: dict[str, list[Event]] = field(default_factory=dict)
    recognised: int = 0
    got_id: bool = False


def parse(path: Path, include_subagents: bool = True) -> Session:
    session = Session(agent=AGENT, id=_id_from_name(path), path=str(path))
    st = _parse_file(path, session, actor="main", primary=True)
    if st.recognised == 0:
        raise TranscriptError(f"{path}: no Codex records found")
    streams = [st.events]
    if include_subagents and st.got_id:
        for actor, child in _subagent_rollouts(path, session.id):
            streams.append(_parse_file(child, session, actor=actor, primary=False).events)
    session.events = merge_streams(streams) if len(streams) > 1 else st.events
    return finish(session)


def _id_from_name(path: Path) -> str:
    m = _ID_RE.search(path.stem)
    return m.group(1) if m else path.stem


def _parse_file(path: Path, session: Session, actor: str, primary: bool) -> _State:
    warnings: list[str] = []
    target = session if primary else Session(agent=AGENT, id="", path=str(path))
    st = _State(session=target, actor=actor, items_mode=_has_items(path))
    for rec in iter_records(path, warnings):
        try:
            _record(st, rec)
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            warn(warnings, f"skipped a malformed {rec.get('type')} record ({type(exc).__name__})")
    prefix = "" if primary else f"{path.name}: "
    for w in warnings:
        warn(session.warnings, prefix + w)
    return st


def _has_items(path: Path) -> bool:
    """Whether the rollout records CommandExecution/FileChange items (a cheap textual scan)."""
    with open_text(path) as fh:
        for line in fh:
            if "item_completed" not in line:
                continue
            if "CommandExecution" in line or "FileChange" in line:
                rec = _json_obj(line)
                p = rec.get("payload")
                item = p.get("item") if isinstance(p, dict) else None
                if isinstance(item, dict) and item.get("type") in (
                    "CommandExecution",
                    "FileChange",
                ):
                    return True
    return False


def _record(st: _State, rec: dict[str, Any]) -> None:
    t = rec.get("type")
    payload = rec.get("payload")
    ts = str_or_none(rec.get("timestamp"))
    if t in ROLLOUT_TYPES and isinstance(payload, dict):
        st.recognised += 1
        if t == "session_meta":
            _meta(st, payload, ts)
        elif t == "turn_context":
            st.session.model = st.session.model or str_or_none(payload.get("model"))
            st.session.cwd = st.session.cwd or str_or_none(payload.get("cwd"))
        elif t == "response_item":
            _response_item(st, payload, ts)
        elif t == "event_msg":
            _event_msg(st, payload, ts)
    elif t in RESPONSE_TYPES and payload is None:  # pre-2025 rollouts: bare response items
        st.recognised += 1
        _response_item(st, rec, ts)
    elif isinstance(t, str) and t.startswith(("thread.", "turn.", "item.")):
        st.recognised += 1
        _exec_json(st, rec)
    elif t is None and "instructions" in rec and "id" in rec:  # pre-2025 rollout header
        st.recognised += 1
        _meta(st, rec, ts)


def _meta(st: _State, p: dict[str, Any], ts: str | None) -> None:
    s = st.session
    sid = str_or_none(p.get("id"))
    if sid and not st.got_id:
        s.id, st.got_id = sid, True
    s.cwd = s.cwd or str_or_none(p.get("cwd"))
    s.started_at = s.started_at or str_or_none(p.get("timestamp")) or ts


def _add(st: _State, ev: Event) -> Event:
    ev.actor = st.actor
    st.events.append(ev)
    return ev


def _injected(text: str) -> bool:
    return text.lstrip().startswith(_INJECTED_PREFIXES) or bool(_INJECTED_RE.match(text))


def _response_item(st: _State, p: dict[str, Any], ts: str | None) -> None:
    ptype = p.get("type")
    call_id = str(p.get("call_id") or p.get("id") or "")
    if ptype == "message":
        text = content_text(p.get("content")).strip()
        role = p.get("role")
        if not text:
            return
        if role == "user" and not _injected(text):
            _add(st, Event(0, EventKind.USER, text, ts))
        elif role == "assistant":
            _add(st, Event(0, EventKind.ASSISTANT, text, ts))
    elif ptype == "function_call":
        name = str(p.get("name") or "")
        args = _json_obj(p.get("arguments"))
        st.pending[call_id] = _function_call(st, name, args, p.get("arguments"), ts)
    elif ptype == "custom_tool_call":
        name = str(p.get("name") or "")
        source = str(p.get("input") or "")
        if name == "exec":
            st.pending[call_id] = _code_cell(st, source, ts)
        elif name == "apply_patch":
            st.pending[call_id] = [] if st.items_mode else _patch_events(st, source, ts)
        else:
            st.pending[call_id] = [_add(st, Event(0, EventKind.TOOL, summarize(source), ts, name))]
    elif ptype == "local_shell_call":
        action = p.get("action") or {}
        cmd = action.get("command")
        if not st.items_mode and isinstance(cmd, (list, str)):
            ev = _add(st, Event(0, EventKind.COMMAND, unwrap(cmd), ts, "local_shell"))
            st.pending[call_id] = [ev]
    elif ptype == "web_search_call":
        query = (p.get("action") or {}).get("query")
        _add(st, Event(0, EventKind.TOOL, str(query or ""), ts, "web_search"))
    elif ptype in ("function_call_output", "custom_tool_call_output"):
        _call_output(st, st.pending.get(call_id, []), p.get("output"))


def _json_obj(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _function_call(
    st: _State, name: str, args: dict[str, Any], raw: object, ts: str | None
) -> list[Event]:
    cmd: object = None
    if name in _SHELL_FUNCTIONS:
        cmd = args.get("command")
    elif name == "exec_command":
        cmd = args.get("cmd", args.get("command"))
    if isinstance(cmd, (str, list)) and cmd:
        if st.items_mode:
            return []
        return [_add(st, Event(0, EventKind.COMMAND, unwrap(cmd), ts, name))]
    if name == "apply_patch":
        patch = args.get("input", args.get("patch"))
        if isinstance(patch, str):
            return [] if st.items_mode else _patch_events(st, patch, ts)
    return [_add(st, Event(0, EventKind.TOOL, summarize(args or raw), ts, name))]


def _patch_events(st: _State, patch: str, ts: str | None) -> list[Event]:
    out = []
    for action, path in patch_files(patch):
        out.append(
            _add(st, Event(0, EventKind.EDIT, path, ts, "apply_patch", path=path, action=action))
        )
    return out


def _code_cell(st: _State, source: str, ts: str | None) -> list[Event]:
    cell = parse_cell(source)
    out: list[Event] = []
    for call in cell.calls:
        if call.tool == "apply_patch":
            continue
        if call.tool == "exec_command":
            if st.items_mode:
                continue
            if call.command is not None:
                out.append(_add(st, Event(0, EventKind.COMMAND, unwrap(call.command), ts, "exec")))
                continue
        out.append(_add(st, Event(0, EventKind.TOOL, one_line(call.args), ts, call.tool)))
    if not st.items_mode:
        for action, path in cell.patches:
            ev = Event(0, EventKind.EDIT, path, ts, "apply_patch", path=path, action=action)
            out.append(_add(st, ev))
    if not cell.calls:
        out.append(_add(st, Event(0, EventKind.TOOL, one_line(source), ts, "exec")))
    return out


def _output_text(output: object) -> tuple[str, int | None]:
    """Text of a call output, plus an exit code from ``{"output", "metadata"}`` JSON."""
    text = content_text(output) if isinstance(output, list) else str(output or "")
    if text.startswith("{"):
        obj = _json_obj(text)
        if isinstance(obj.get("output"), str):
            meta = obj.get("metadata")
            code = int_or_none(meta.get("exit_code")) if isinstance(meta, dict) else None
            return obj["output"], code
    return text, None


def _exit_marker(text: str) -> int | None:
    for pattern in _EXIT_MARKERS:
        found = pattern.findall(text)
        if len(found) == 1:
            return int(found[0])
        if found:
            return None
    return None


def _call_output(st: _State, events: list[Event], output: object) -> None:
    text, code = _output_text(output)
    for ev in events:
        if ev.kind is not EventKind.EDIT:
            ev.output = truncate(text)
    commands = [e for e in events if e.kind is EventKind.COMMAND]
    if len(commands) == 1:
        commands[0].exit_code = code if code is not None else _exit_marker(text)


def _event_msg(st: _State, p: dict[str, Any], ts: str | None) -> None:
    ptype = p.get("type")
    if ptype == "item_completed" and isinstance(p.get("item"), dict):
        ts = ms_to_iso(p.get("started_at_ms")) or ts
        _rollout_item(st, p["item"], ts)
    elif ptype == "exec_command_end":
        events = st.pending.get(str(p.get("call_id") or ""), [])
        commands = [e for e in events if e.kind is EventKind.COMMAND]
        output = str(p.get("aggregated_output") or p.get("formatted_output") or "")
        if not output:
            output = "\n".join(str(p.get(k) or "") for k in ("stdout", "stderr")).strip()
        if len(commands) == 1:
            ev = commands[0]
            if ev.exit_code is None:
                ev.exit_code = int_or_none(p.get("exit_code"))
            ev.output = ev.output or truncate(output)
        elif not events and not st.items_mode and isinstance(p.get("command"), (str, list)):
            ev = _add(st, Event(0, EventKind.COMMAND, unwrap(p["command"]), ts, "exec_command"))
            ev.exit_code, ev.output = int_or_none(p.get("exit_code")), truncate(output)
    elif ptype == "patch_apply_end":
        for ev in st.pending.get(str(p.get("call_id") or ""), []):
            if isinstance(p.get("success"), bool):
                ev.is_error = not p["success"]


def _rollout_item(st: _State, item: dict[str, Any], ts: str | None) -> None:
    itype = item.get("type")
    if itype == "CommandExecution":
        cmd = item.get("command")
        ev = Event(0, EventKind.COMMAND, unwrap(cmd) if isinstance(cmd, (str, list)) else "", ts)
        ev.tool = "exec_command"
        ev.exit_code = int_or_none(item.get("exit_code"))
        if isinstance(item.get("status"), str):
            ev.is_error = item["status"] == "failed"
        output = item.get("aggregated_output") or item.get("formatted_output")
        if not output:
            output = "\n".join(str(item.get(k) or "") for k in ("stdout", "stderr")).strip()
        ev.output = truncate(str(output))
        _add(st, ev)
    elif itype == "FileChange":
        failed = item.get("status") == "failed"
        for action, path in _changes(item.get("changes")):
            ev = Event(0, EventKind.EDIT, path, ts, "apply_patch", path=path, action=action)
            ev.is_error = failed or None
            _add(st, ev)


def _changes(changes: object) -> list[tuple[EditAction, str]]:
    """(action, path) pairs from a FileChange ``changes`` dict or an exec-json changes list."""
    out: list[tuple[EditAction, str]] = []
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(changes, dict):
        entries = [(str(k), v if isinstance(v, dict) else {}) for k, v in changes.items()]
    elif isinstance(changes, list):
        entries = [(str(c.get("path") or ""), c) for c in changes if isinstance(c, dict)]
    for path, change in entries:
        if not path:
            continue
        kind = str(change.get("type") or change.get("kind") or "")
        action = _EDIT_KINDS.get(kind.lower(), "unknown")
        move = str_or_none(change.get("move_path"))
        if move:
            out += [("delete", path), ("add", move)]
        else:
            out.append((action, path))
    return out


def _exec_json(st: _State, rec: dict[str, Any]) -> None:
    t = rec["type"]
    if t == "thread.started" and str_or_none(rec.get("thread_id")) and not st.got_id:
        st.session.id, st.got_id = rec["thread_id"], True
    if t != "item.completed" or not isinstance(rec.get("item"), dict):
        return
    item = dict(rec["item"])
    if isinstance(item.get("details"), dict):  # early exec-json nested the fields
        item.update(item["details"])
    itype = item.get("type", item.get("item_type"))
    ts = str_or_none(rec.get("timestamp"))
    if itype == "command_execution":
        cmd = item.get("command")
        ev = Event(0, EventKind.COMMAND, unwrap(cmd) if isinstance(cmd, (str, list)) else "", ts)
        ev.tool = "exec_command"
        ev.exit_code = int_or_none(item.get("exit_code"))
        if isinstance(item.get("status"), str):
            ev.is_error = item["status"] == "failed"
        ev.output = truncate(str(item.get("aggregated_output") or ""))
        _add(st, ev)
    elif itype == "file_change":
        failed = item.get("status") == "failed"
        for action, path in _changes(item.get("changes")):
            ev = Event(0, EventKind.EDIT, path, ts, "apply_patch", path=path, action=action)
            ev.is_error = failed or None
            _add(st, ev)
    elif itype == "agent_message":
        text = str(item.get("text") or "").strip()
        if text:
            _add(st, Event(0, EventKind.ASSISTANT, text, ts))
    elif itype == "mcp_tool_call":
        tool = ".".join(str(item[k]) for k in ("server", "tool") if item.get(k))
        ev = Event(0, EventKind.TOOL, summarize(item.get("arguments")), ts, tool or "mcp")
        ev.is_error = item.get("status") == "failed" or None
        _add(st, ev)
    elif itype == "web_search":
        _add(st, Event(0, EventKind.TOOL, str(item.get("query") or ""), ts, "web_search"))


def _subagent_rollouts(path: Path, parent_id: str) -> list[tuple[str, Path]]:
    """Rollouts spawned (directly or transitively) by ``parent_id``, read by their first line."""
    root = next((a for a in path.parents if a.name in ("sessions", "archived_sessions")), None)
    if root is None:
        return []
    stamp = path.name[:27]
    candidates = sorted(p for p in root.rglob("rollout-*.jsonl") if p.name[:27] >= stamp)
    heads = {p: _first_meta(p) for p in candidates if p != path}
    found: list[tuple[str, Path]] = []
    parents, seen = {parent_id}, {parent_id}
    while parents and len(found) < MAX_SUBAGENTS:
        nxt: set[str] = set()
        for p, meta in heads.items():
            if meta is None or meta.get("_parent") not in parents:
                continue
            cid = str(meta.get("id") or p.stem)
            if cid in seen:
                continue
            seen.add(cid)
            nxt.add(cid)
            actor = str_or_none(meta.get("agent_nickname")) or str_or_none(meta.get("agent_path"))
            found.append((actor or cid, p))
        parents = nxt
    return found[:MAX_SUBAGENTS]


def _first_meta(path: Path) -> dict[str, Any] | None:
    """The session_meta payload of a spawned subagent rollout, with ``_parent`` set."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            line = fh.readline()
    except OSError:
        return None
    rec = _json_obj(line)
    p = rec.get("payload")
    if rec.get("type") != "session_meta" or not isinstance(p, dict):
        return None
    source = p.get("source")
    sub = source.get("subagent") if isinstance(source, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    if not isinstance(spawn, dict):
        return None  # not spawned by an agent (e.g. a guardian review)
    parent = str_or_none(spawn.get("parent_thread_id")) or str_or_none(p.get("parent_thread_id"))
    return {**p, "_parent": parent}
