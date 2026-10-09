"""Core data model shared by parsers, checks, doctor and reporters.

Everything here is plain data. Parsers produce ``Session`` and ``Diff``; the rule loader
produces ``Rule``; checks turn a ``Rule`` plus a ``Context`` into a ``RuleResult``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

Severity = Literal["error", "warning", "info"]
SEVERITIES: tuple[Severity, ...] = ("error", "warning", "info")

Status = Literal["pass", "fail", "unverified", "skip"]
"""``unverified``: the rule may hold but the evidence cannot confirm it (e.g. exit code unknown)."""

EditAction = Literal["add", "modify", "delete", "unknown"]
ChangeStatus = Literal["added", "modified", "deleted", "renamed"]

OUTPUT_LIMIT = 4000
"""Parsers truncate command/tool output to this many characters (head and tail kept)."""


class EventKind(StrEnum):
    USER = "user"  # a user message
    ASSISTANT = "assistant"  # assistant prose shown to the user
    COMMAND = "command"  # a shell command the agent ran
    EDIT = "edit"  # a file the agent created, changed or deleted through a tool
    TOOL = "tool"  # any other tool call (web fetch, MCP, subagent spawn, ...)


@dataclass(slots=True)
class Event:
    index: int  # 0-based position in the merged session; strictly increasing
    kind: EventKind
    text: str = ""  # message text, command line, or a one-line summary of tool input
    timestamp: str | None = None  # ISO-8601 as recorded by the agent, if any
    tool: str | None = None  # agent's tool name: Bash, exec_command, write_file, ...
    path: str | None = None  # EDIT: path as recorded (often absolute)
    action: EditAction | None = None  # EDIT only
    exit_code: int | None = None  # COMMAND: None when the transcript does not say
    output: str = ""  # COMMAND/TOOL: truncated to OUTPUT_LIMIT
    is_error: bool | None = None  # the agent runtime flagged the call as failed
    actor: str = "main"  # "main", or a subagent id for events from subagent transcripts


@dataclass(slots=True)
class Session:
    agent: str  # "claude-code" | "codex" | "gemini-cli" | "generic"
    id: str
    path: str  # transcript file the session was read from
    cwd: str | None = None
    started_at: str | None = None
    model: str | None = None
    events: list[Event] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # parse problems worth surfacing

    def of_kind(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind is kind]

    def commands(self) -> list[Event]:
        return self.of_kind(EventKind.COMMAND)

    def edits(self) -> list[Event]:
        return self.of_kind(EventKind.EDIT)

    def assistant_messages(self) -> list[Event]:
        return self.of_kind(EventKind.ASSISTANT)

    def final_message(self) -> Event | None:
        msgs = [e for e in self.assistant_messages() if e.actor == "main"]
        return msgs[-1] if msgs else None


@dataclass(slots=True)
class FileChange:
    path: str  # posix path relative to the repo root (the new path for renames)
    status: ChangeStatus
    old_path: str | None = None  # renames only
    added: list[tuple[int, str]] = field(default_factory=list)  # (new line number, text)
    removed: int = 0  # number of removed lines
    binary: bool = False


@dataclass(slots=True)
class Diff:
    base: str | None  # the ref compared against, or None for a patch file
    files: list[FileChange] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [f.path for f in self.files]


@dataclass(slots=True)
class Rule:
    id: str
    check: str  # name of a registered check, e.g. "forbid-command"
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    severity: Severity = "error"
    source: str | None = None  # where the prose rule lives, e.g. "AGENTS.md:12"
    origin: str = ""  # where the check was defined, e.g. "ruleproof.toml:31"
    scope: str | None = None  # posix dir prefix; diff checks only see files under it


@dataclass(slots=True)
class Evidence:
    message: str
    path: str | None = None  # file location (repo-relative posix)
    line: int | None = None
    event: int | None = None  # transcript Event.index
    excerpt: str | None = None  # short quote: the offending line, command or sentence


@dataclass(slots=True)
class RuleResult:
    rule: Rule
    status: Status
    summary: str  # one line, no trailing period
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(slots=True)
class SessionInfo:
    agent: str
    id: str
    path: str


@dataclass(slots=True)
class Report:
    kind: Literal["check", "doctor"]
    tool_version: str
    repo: str | None
    base: str | None = None
    session: SessionInfo | None = None
    results: list[RuleResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. "no transcript: 4 rules skipped"

    def failed(self, at_least: Severity = "error", strict: bool = False) -> list[RuleResult]:
        """Results that should fail the run: ``fail`` at or above ``at_least``.

        With ``strict``, ``unverified`` results count as failures too.
        """
        rank = {s: i for i, s in enumerate(SEVERITIES)}
        bad: set[str] = {"fail", "unverified"} if strict else {"fail"}
        return [
            r
            for r in self.results
            if r.status in bad and rank[r.rule.severity] <= rank[at_least]
        ]


@dataclass(slots=True)
class Context:
    """Inputs available to checks. A check that needs a missing input is skipped."""

    repo: Path | None
    diff: Diff | None = None
    session: Session | None = None
