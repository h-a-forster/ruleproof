"""Turn agent session logs into ``Session`` objects.

``load_session`` reads a transcript file in any supported format; ``detect_agent`` tells the
formats apart by content. ``discover`` finds sessions on disk and ``export`` renders them.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from itertools import islice
from pathlib import Path
from typing import Any

from ruleproof.errors import TranscriptError
from ruleproof.models import EventKind, Session
from ruleproof.transcripts import claude, codex, gemini, generic
from ruleproof.transcripts._util import iter_records

AGENTS = ("claude-code", "codex", "gemini-cli", "generic")

_PARSERS: dict[str, Callable[[Path, bool], Session]] = {
    claude.AGENT: claude.parse,
    codex.AGENT: codex.parse,
    gemini.AGENT: gemini.parse,
    generic.AGENT: generic.parse,
}
_HEAD_LINES = 200
_CLAUDE_ONLY_TYPES = {"queue-operation", "file-history-snapshot", "summary", "attachment"}

__all__ = ["AGENTS", "TranscriptError", "detect_agent", "load_session"]


def load_session(path: str | Path, agent: str = "auto", include_subagents: bool = True) -> Session:
    """Parse a transcript. ``agent`` is ``auto`` or one of ``AGENTS``."""
    p = _existing_file(path)
    name = detect_agent(p) if agent == "auto" else agent
    parser = _PARSERS.get(name)
    if parser is None:
        raise TranscriptError(
            f"unknown agent {agent!r}; expected auto or one of {', '.join(AGENTS)}"
        )
    return parser(p, include_subagents)


def detect_agent(path: str | Path) -> str:
    """``claude-code``, ``codex``, ``gemini-cli`` or ``generic``, judged by file content."""
    p = _existing_file(path)
    for rec in _head_records(p):
        agent = _classify(rec)
        if agent:
            return agent
    raise TranscriptError(
        f"{p}: not a recognised transcript (Claude Code, Codex, Gemini CLI or ruleproof JSONL)"
    )


def _existing_file(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise TranscriptError(f"transcript not found: {p}")
    if not p.is_file():
        raise TranscriptError(f"transcript is not a file: {p}")
    return p


def _head_records(path: Path) -> list[dict[str, Any]]:
    """The first records of a transcript (whole lines, however long; at most ``_HEAD_LINES``)."""
    with closing(iter_records(path, [])) as records:
        return list(islice(records, _HEAD_LINES))


def _classify(rec: dict[str, Any]) -> str | None:
    if isinstance(rec.get("session"), dict) or rec.get("kind") in {k.value for k in EventKind}:
        return generic.AGENT
    if isinstance(rec.get("messages"), list) and (
        "sessionId" in rec or "projectHash" in rec or "startTime" in rec
    ):
        return gemini.AGENT
    t = rec.get("type")
    if not isinstance(t, str):
        if "instructions" in rec and "id" in rec and "timestamp" in rec:
            return codex.AGENT
        return None
    if t in codex.ROLLOUT_TYPES and isinstance(rec.get("payload"), dict):
        return codex.AGENT
    if t.startswith(("thread.", "turn.", "item.")):
        return codex.AGENT
    if t in codex.RESPONSE_TYPES and "payload" not in rec and "message" not in rec:
        return codex.AGENT
    if t in ("user", "assistant") and isinstance(rec.get("message"), dict):
        return claude.AGENT
    if t in ("system", "result") and ("session_id" in rec or "sessionId" in rec):
        return claude.AGENT
    if t in _CLAUDE_ONLY_TYPES:
        return claude.AGENT
    if t == "gemini" or (t in gemini.MESSAGE_TYPES and "content" in rec and "id" in rec):
        return gemini.AGENT
    return None
