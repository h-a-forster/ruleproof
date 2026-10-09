"""Claude Code hooks: ``Stop`` (keep working while rules fail) and ``PreToolUse`` (refuse a
tool call that would break a forbid-* rule before it runs).

Both read the hook payload as JSON on stdin and always exit 0.

- Stop: printing ``{"decision": "block", "reason": ...}`` makes Claude continue with ``reason``
  as its instruction; no output lets it stop. ``stop_hook_active`` is true when Claude is
  already continuing because of a stop hook; the hook then never blocks again, so it cannot
  trap the agent in a loop. The transcript file may not hold the final message yet at Stop
  time, so ``last_assistant_message`` from the payload is added when missing.
- PreToolUse: printing ``{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny", "permissionDecisionReason": ...}}`` stops the call and shows
  the reason to Claude; no output leaves the decision to the normal permission flow.

The hooks must never get in the way: any internal problem (no rules, a broken transcript, a
bug) exits 0 without a decision and with a one-line diagnostic on stderr.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from ruleproof.models import EditAction, Event, EventKind, Report, RuleResult, Session
from ruleproof.report import evidence_line, one_line, split_source

if TYPE_CHECKING:
    from ruleproof.models import Rule

DISABLE_ENV = "RULEPROOF_HOOK_DISABLE"
PRETOOL_CHECKS = frozenset({"forbid-command", "forbid-edit", "forbid-tool", "forbid-change"})
"""Checks that can judge a single proposed tool call."""

SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
EDIT_TOOLS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
"""Claude Code file tools and the ``tool_input`` key holding the path."""

_MAX_RULES = 8
_MAX_EVIDENCE = 3
_MAX_REASON = 3000


class _Skip(Exception):
    """The hook has nothing to do; the message explains why."""


Evaluator = Callable[[Mapping[str, Any], TextIO], "str | None"]


def _run(stdin: TextIO, stderr: TextIO, env: Mapping[str, str] | None, fn: Evaluator) -> str | None:
    """Shared wrapper: honour the disable switch, parse stdin, and swallow every failure."""
    env = os.environ if env is None else env
    if env.get(DISABLE_ENV, "") not in ("", "0"):
        return None
    try:
        return fn(_parse_payload(stdin.read()), stderr)
    except _Skip as exc:
        print(f"ruleproof hook: {exc}", file=stderr)
    except Exception as exc:  # never get in the agent's way because ruleproof itself failed
        print(f"ruleproof hook: internal error: {type(exc).__name__}: {exc}", file=stderr)
    return None


def _parse_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Skip(f"stdin is not JSON ({exc.msg}); nothing checked") from None
    if not isinstance(payload, dict):
        raise _Skip("stdin is not a JSON object; nothing checked")
    return payload


def _payload_cwd(payload: Mapping[str, Any]) -> str:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise _Skip("payload has no cwd; nothing checked")
    if not Path(cwd).is_dir():
        raise _Skip(f"cwd {cwd} does not exist; nothing checked")
    return cwd


def _find_repo(cwd: str) -> tuple[Path, bool]:
    """The git root of ``cwd`` and True, or ``cwd`` itself and False outside git."""
    from ruleproof.diff import repo_root
    from ruleproof.errors import GitError

    try:
        return repo_root(Path(cwd)), True
    except GitError:
        return Path(cwd).resolve(), False


def _load_rules(repo: Path, cwd: str, rules_file: str | None) -> list[Rule]:
    from ruleproof.errors import RuleproofError
    from ruleproof.rules import load_rules

    try:
        rules_path = Path(cwd) / os.path.expanduser(rules_file) if rules_file else None
        rules = load_rules(repo, rules_file=rules_path)
    except RuleproofError as exc:
        raise _Skip(str(exc)) from None
    if not rules:
        raise _Skip(f"no rules found for {repo}; nothing checked")
    return rules


def _check_event(payload: Mapping[str, Any], expected: str) -> None:
    event = payload.get("hook_event_name")
    if event not in (None, expected):
        raise _Skip(f"unsupported hook event {event!r}; expected {expected}")


# --------------------------------------------------------------------------- Stop


def claude_stop(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    env: Mapping[str, str] | None = None,
    *,
    rules_file: str | None = None,
    base: str = "HEAD",
) -> int:
    """Entry point for ``ruleproof hook claude-stop``. Always returns 0.

    ``rules_file`` may live outside the repo; a relative path resolves against the payload's
    ``cwd``. ``base`` is the git ref the working tree is compared with.
    """

    def fn(payload: Mapping[str, Any], err: TextIO) -> str | None:
        return evaluate(payload, err, rules_file=rules_file, base=base)

    reason = _run(stdin, stderr, env, fn)
    if reason:
        stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    return 0


def evaluate(
    payload: Mapping[str, Any],
    stderr: TextIO,
    *,
    rules_file: str | None = None,
    base: str = "HEAD",
) -> str | None:
    """Run the check for a Stop payload; return the block reason, or None to let it stop."""
    from ruleproof import __version__
    from ruleproof.diff import from_git
    from ruleproof.engine import run_rules
    from ruleproof.errors import RuleproofError
    from ruleproof.models import Context

    _check_event(payload, "Stop")
    if payload.get("stop_hook_active") is True:
        return None
    cwd = _payload_cwd(payload)
    repo, in_git = _find_repo(cwd)
    if not in_git:
        print(
            f"ruleproof hook: {cwd} is not inside a git repository; diff rules skipped, "
            "transcript rules still run",
            file=stderr,
        )
    rules = _load_rules(repo, cwd, rules_file)

    diff = None
    if in_git:
        try:
            diff = from_git(repo, base=base, include_untracked=True)
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
    """Add the final assistant message when the transcript has not caught up with it yet."""
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
        lines += _rule_details(r)
    if n > _MAX_RULES:
        lines.append(f"... and {n - _MAX_RULES} more; run `ruleproof check` for the full list.")
    lines.append(
        "If a rule cannot be satisfied, stop and tell the user which rule and why "
        "instead of working around it."
    )
    return _bounded("\n".join(lines))


def _rule_details(r: RuleResult) -> list[str]:
    lines: list[str] = []
    if r.rule.description:
        where = f" ({r.rule.source})" if r.rule.source else ""
        lines.append(f"   rule{where}: {one_line(r.rule.description, 200)}")
    lines += [f"   - {evidence_line(ev, 160)}" for ev in r.evidence[:_MAX_EVIDENCE]]
    if len(r.evidence) > _MAX_EVIDENCE:
        lines.append(f"   - ... and {len(r.evidence) - _MAX_EVIDENCE} more")
    return lines


def _bounded(reason: str) -> str:
    return reason if len(reason) <= _MAX_REASON else reason[: _MAX_REASON - 3].rstrip() + "..."


# --------------------------------------------------------------------------- PreToolUse


def claude_pretool(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    env: Mapping[str, str] | None = None,
    *,
    rules_file: str | None = None,
) -> int:
    """Entry point for ``ruleproof hook claude-pretool``. Always returns 0.

    Denies the proposed tool call when an error-severity forbid-command, forbid-edit,
    forbid-tool or forbid-change rule fails on it; otherwise prints nothing.
    """

    def fn(payload: Mapping[str, Any], err: TextIO) -> str | None:
        return evaluate_pretool(payload, err, rules_file=rules_file)

    reason = _run(stdin, stderr, env, fn)
    if reason:
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        stdout.write(json.dumps(decision) + "\n")
    return 0


def evaluate_pretool(
    payload: Mapping[str, Any], stderr: TextIO, *, rules_file: str | None = None
) -> str | None:
    """Judge a PreToolUse payload; return the deny reason, or None to stay out of the way."""
    from ruleproof.engine import run_rules
    from ruleproof.models import Context

    _check_event(payload, "PreToolUse")
    cwd = _payload_cwd(payload)
    session = proposed_call_session(payload, cwd)
    repo = _nearest_git_root(Path(cwd))
    rules = [
        r
        for r in _load_rules(repo, cwd, rules_file)
        if r.check in PRETOOL_CHECKS and r.severity == "error"
    ]
    if not rules:
        return None
    results = run_rules(rules, Context(repo=repo, diff=None, session=session))
    failed = [r for r in results if r.status == "fail"]
    return deny_reason(failed) if failed else None


def _nearest_git_root(cwd: Path) -> Path:
    """The closest ancestor holding ``.git`` (a directory, or a file for worktrees and
    submodules), else ``cwd``. Looked up on disk because spawning git costs too much here."""
    start = cwd.resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def proposed_call_session(payload: Mapping[str, Any], cwd: str) -> Session:
    """A one-event ``Session`` describing the tool call Claude is about to make."""
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        raise _Skip("payload has no tool_name; nothing checked")
    raw_input = payload.get("tool_input")
    tool_input: Mapping[str, Any] = raw_input if isinstance(raw_input, Mapping) else {}

    event: Event
    command = tool_input.get("command")
    path = tool_input.get(EDIT_TOOLS.get(tool, ""))
    if tool in SHELL_TOOLS and isinstance(command, str):
        from ruleproof.transcripts._shell import unwrap

        event = Event(0, EventKind.COMMAND, text=unwrap(command), tool=tool)
    elif tool in EDIT_TOOLS and isinstance(path, str) and path:
        full = Path(cwd) / os.path.expanduser(path)
        action: EditAction = "modify" if full.exists() else "add"
        event = Event(0, EventKind.EDIT, text=path, tool=tool, path=path, action=action)
    else:
        summary = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
        event = Event(0, EventKind.TOOL, text=one_line(summary, 300), tool=tool)

    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    return Session(
        agent="claude-code",
        id=session_id if isinstance(session_id, str) else "",
        path=transcript if isinstance(transcript, str) else "",
        cwd=cwd,
        events=[event],
    )


def deny_reason(failed: list[RuleResult]) -> str:
    """Why the call was refused, addressed to the agent."""
    n = len(failed)
    noun = "rule" if n == 1 else "rules"
    lines = [f"ruleproof: this tool call would break {n} project {noun}, so it was not run:"]
    for i, r in enumerate(failed[:_MAX_RULES], start=1):
        lines.append(f"{i}. {r.rule.id}: {one_line(r.summary, 200)}")
        lines += _rule_details(r)
    if n > _MAX_RULES:
        lines.append(f"... and {n - _MAX_RULES} more.")
    lines.append(
        "Follow the rule instead (its text may say what to do). If the task cannot be done "
        "without breaking it, stop and ask the user; do not work around the rule."
    )
    return _bounded("\n".join(lines))
