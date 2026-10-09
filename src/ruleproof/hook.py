"""Claude Code ``Stop`` hook: keep the agent working while project rules fail.

Claude Code runs the hook when the agent is about to finish, passing JSON on stdin
(``session_id``, ``transcript_path``, ``cwd``, ``hook_event_name``, ``stop_hook_active``,
``last_assistant_message``). Printing ``{"decision": "block", "reason": ...}`` and exiting 0
makes Claude continue with ``reason`` as its instruction; exiting 0 with no output lets it
stop. ``stop_hook_active`` is true when Claude is already continuing because of a stop hook;
the hook then never blocks again, so it cannot trap the agent in a loop.

The hook must never get in the way: any internal problem (not a git repo, no rules, a
broken transcript, a bug) exits 0 silently with a one-line diagnostic on stderr.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from ruleproof.models import Event, EventKind, Report, RuleResult, Session
from ruleproof.report import evidence_line, one_line, split_source

DISABLE_ENV = "RULEPROOF_HOOK_DISABLE"
_MAX_RULES = 8
_MAX_EVIDENCE = 3
_MAX_REASON = 3000


def claude_stop(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    env: Mapping[str, str] | None = None,
) -> int:
    """Entry point for ``ruleproof hook claude-stop``. Always returns 0."""
    env = os.environ if env is None else env
    if env.get(DISABLE_ENV, "") not in ("", "0"):
        return 0
    try:
        reason = evaluate(_parse_payload(stdin.read()), stderr)
    except _Skip as exc:
        print(f"ruleproof hook: {exc}", file=stderr)
        return 0
    except Exception as exc:  # never block the agent because ruleproof itself failed
        print(f"ruleproof hook: internal error: {type(exc).__name__}: {exc}", file=stderr)
        return 0
    if reason:
        stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    return 0


class _Skip(Exception):
    """The hook has nothing to do; the message explains why."""


def _parse_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Skip(f"stdin is not JSON ({exc.msg}); nothing checked") from None
    if not isinstance(payload, dict):
        raise _Skip("stdin is not a JSON object; nothing checked")
    return payload


def evaluate(payload: Mapping[str, Any], stderr: TextIO) -> str | None:
    """Run the check for a hook payload; return the block reason, or None to let it stop."""
    from ruleproof import __version__
    from ruleproof.diff import from_git, repo_root
    from ruleproof.engine import run_rules
    from ruleproof.errors import RuleproofError
    from ruleproof.models import Context
    from ruleproof.rules import load_rules

    event = payload.get("hook_event_name")
    if event not in (None, "Stop"):
        raise _Skip(f"unsupported hook event {event!r}; expected Stop")
    if payload.get("stop_hook_active") is True:
        return None

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise _Skip("payload has no cwd; nothing checked")
    try:
        repo = repo_root(Path(cwd))
        rules = load_rules(repo)
    except RuleproofError as exc:
        raise _Skip(str(exc)) from None
    if not rules:
        raise _Skip(f"no rules found for {repo}; nothing checked")

    diff = None
    try:
        diff = from_git(repo, base="HEAD", include_untracked=True)
    except RuleproofError as exc:
        print(f"ruleproof hook: diff unavailable, diff rules skipped: {exc}", file=stderr)

    session = _load_transcript(payload, stderr)
    results = run_rules(rules, Context(repo=repo, diff=diff, session=session))
    report = Report(kind="check", tool_version=__version__, repo=str(repo), results=results)
    failed = report.failed(at_least="error", strict=False)
    return block_reason(failed) if failed else None


def _load_transcript(payload: Mapping[str, Any], stderr: TextIO) -> Session | None:
    from ruleproof.errors import RuleproofError
    from ruleproof.transcripts import load_session

    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        print(
            "ruleproof hook: payload has no transcript_path; transcript rules skipped", file=stderr
        )
        return None
    try:
        session = load_session(Path(os.path.expanduser(path)), agent="claude-code")
    except RuleproofError as exc:
        print(f"ruleproof hook: transcript rules skipped: {exc}", file=stderr)
        return None
    _append_last_message(session, payload.get("last_assistant_message"))
    return session


def _append_last_message(session: Session, text: object) -> None:
    """Add the final assistant message when the transcript has not caught up with it yet.

    Claude Code documents that the transcript file may not contain the final message at Stop
    time, and passes it as ``last_assistant_message`` instead. Claim checks need it.
    """
    if not isinstance(text, str) or not text.strip():
        return
    last = session.final_message()
    if last is not None and last.text.strip() == text.strip():
        return
    session.events.append(Event(index=len(session.events), kind=EventKind.ASSISTANT, text=text))


def block_reason(failed: list[RuleResult]) -> str:
    """Short, actionable instructions for the agent listing the failed rules and evidence."""
    files = sorted({src[0] for r in failed if (src := split_source(r.rule.source))})
    origin = f" from {', '.join(files)}" if files else ""
    n = len(failed)
    noun = "rule" if n == 1 else "rules"
    verb = "is" if n == 1 else "are"
    lines = [
        f"ruleproof: {n} project {noun}{origin} {verb} not satisfied yet. "
        "Fix them before finishing:"
    ]
    for i, r in enumerate(failed[:_MAX_RULES], start=1):
        lines.append(f"{i}. {r.rule.id}: {one_line(r.summary, 200)}")
        if r.rule.description:
            where = f" ({r.rule.source})" if r.rule.source else ""
            lines.append(f"   rule{where}: {one_line(r.rule.description, 200)}")
        lines += [f"   - {evidence_line(ev, 160)}" for ev in r.evidence[:_MAX_EVIDENCE]]
        if len(r.evidence) > _MAX_EVIDENCE:
            lines.append(f"   - ... and {len(r.evidence) - _MAX_EVIDENCE} more")
    if n > _MAX_RULES:
        lines.append(f"... and {n - _MAX_RULES} more; run `ruleproof check` for the full list.")
    lines.append(
        "If a rule cannot be satisfied, stop and tell the user which rule and why "
        "instead of working around it."
    )
    reason = "\n".join(lines)
    if len(reason) > _MAX_REASON:
        reason = reason[: _MAX_REASON - 3].rstrip() + "..."
    return reason
