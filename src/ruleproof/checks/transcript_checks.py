"""Checks that read the session transcript: commands, edits, tool calls and messages."""

from __future__ import annotations

from ruleproof import paths
from ruleproof.checks import Param, register
from ruleproof.checks._common import (
    cap,
    clip,
    command_matches,
    command_outcome,
    commands_run,
    compile_regex,
    counted_edits,
    edit_relpath,
    edits_made,
    globs_match,
    is_powershell,
    path_key,
    plural,
    preview,
    quoted,
    ran_after,
)
from ruleproof.models import Context, Event, EventKind, Evidence, Rule, RuleResult

_MATCH_QUOTED = Param(
    "bool",
    default=False,
    doc="also match inside quoted strings and heredocs (commit messages, echo text)",
)


def _actor(ev: Event) -> str:
    return "" if ev.actor == "main" else f" (subagent {ev.actor})"


@register(
    "forbid-command",
    needs={"session"},
    params={
        "command": Param("regex", required=True, doc="command lines that must not run"),
        "ignore_case": Param("bool", default=False, doc="match case-insensitively"),
        "match_quoted": _MATCH_QUOTED,
    },
    doc=(
        "Fails when the agent (or a subagent) ran a shell command matching `command`. By "
        "default the text of quoted strings containing spaces, heredoc bodies and PowerShell "
        'here-strings is ignored, so `git commit -m "never git push"` does not match '
        "`git\\s+push`, while scripts passed to `bash -c`, `pwsh -Command` or `cmd /c` are "
        'matched as commands; so are `eval`, `ssh host "..."`, `echo ... | sh` and the code '
        "of `python -c`. Comments are ignored. Set `match_quoted = true` to match the raw "
        "command line. Commands that never ran (denied at the permission prompt or blocked by "
        "a hook) do not count; a command that ran and failed does."
    ),
)
def forbid_command(rule: Rule, ctx: Context) -> RuleResult:
    pattern: str = rule.params["command"]
    rx = compile_regex(pattern, rule.params["ignore_case"])
    hits = [
        ev
        for ev in commands_run(ctx)
        if command_matches(rx, ev.text, rule.params["match_quoted"], is_powershell(ev))
    ]
    if not hits:
        return RuleResult(rule, "pass", f"no command matches {quoted(pattern)}")
    evidence = [
        Evidence(f"ran a forbidden command{_actor(ev)}", event=ev.index, excerpt=clip(ev.text))
        for ev in hits
    ]
    if len(hits) == 1:
        summary = f"ran {quoted(hits[0].text)}"
    else:
        summary = f"{len(hits)} commands match {quoted(pattern)}"
    return RuleResult(rule, "fail", summary, cap(evidence))


@register(
    "require-command",
    needs={"session"},
    params={
        "command": Param("regex", required=True, doc="the command that must run"),
        "must_succeed": Param("bool", default=True, doc="the last run must succeed"),
        "after_last_edit": Param("bool", default=True, doc="it must run after the last edit"),
        "when_paths": Param(
            "glob_list", nonempty=True, doc="only required when a matching file changed"
        ),
        "edit_paths": Param(
            "glob_list", nonempty=True, doc="edits that count as the last edit (default: all)"
        ),
        "ignore_edit_paths": Param("glob_list", default=[], doc="edits that never count"),
        "match_quoted": _MATCH_QUOTED,
    },
    doc=(
        "Fails when no command matching `command` ran after the agent's last counted edit, or "
        "when the last such run failed. `command` is searched anywhere in the command line "
        "(quoted prose and comments excluded, as in forbid-command), so anchor it if needed. "
        "Edits are file-tool edits that took effect inside the repo plus shell commands that "
        "obviously write files (redirects, `tee`, `sed -i`, `git apply`, `--fix`, "
        "formatters); `edit_paths` and `ignore_edit_paths` narrow them. The verdict comes "
        "from the last matching run that was not denied or blocked: exit 0 passes unless the "
        "output shows failures (`pytest | tail`), a non-zero exit or a runtime error fails, "
        "and an unknown exit code is judged from the output (pytest, jest, cargo, ... "
        "summaries) or reported as `unverified`. Not required when nothing was edited and "
        "the diff has no changes in scope; with `when_paths`, only when a changed or edited "
        "file matches; for a scoped rule, only when a file in its scope changed."
    ),
)
def require_command(rule: Rule, ctx: Context) -> RuleResult:
    pattern: str = rule.params["command"]
    rx = compile_regex(pattern)
    when: list[str] | None = rule.params["when_paths"]
    if when is not None or rule.scope:
        touched = _touched(ctx, rule.scope)
        if when is not None:
            touched = [p for p in touched if globs_match(p, when, rule.scope)]
        if not touched:
            return RuleResult(rule, "pass", "not required: no matching changes")

    runs = [
        ev
        for ev in commands_run(ctx)
        if command_matches(rx, ev.text, rule.params["match_quoted"], is_powershell(ev))
    ]
    last_edit: Event | None = None
    last_rel: str | None = None
    if rule.params["after_last_edit"]:
        edits = counted_edits(
            ctx, rule.scope, rule.params["edit_paths"], rule.params["ignore_edit_paths"]
        )
        if edits:
            last_edit, last_rel = edits[-1]
        elif not _diff_in_scope(ctx, rule.scope):
            return RuleResult(rule, "pass", "not required: no edits")
    window = [ev for ev in runs if last_edit is None or ran_after(ev, last_edit)]
    where = f" after the last edit (#{last_edit.index})" if last_edit else ""

    if not window:
        if runs and last_edit is not None:
            before = runs[-1]
            evidence = [
                Evidence(
                    f"last run was before edit #{last_edit.index}",
                    event=before.index,
                    excerpt=clip(before.text),
                ),
                Evidence("last counted edit", path=last_rel, event=last_edit.index),
            ]
            return RuleResult(
                rule, "fail", f"{quoted(before.text)} ran before the last edit, not after", evidence
            )
        return RuleResult(rule, "fail", f"no command matching {quoted(pattern)} ran{where}")

    last = window[-1]
    ran = f"ran {quoted(last.text)}{where}"
    evidence = [
        Evidence(f"last matching run{_actor(last)}", event=last.index, excerpt=clip(last.text))
    ]
    if not rule.params["must_succeed"]:
        return RuleResult(rule, "pass", ran, evidence)
    outcome, reason = command_outcome(last)
    if outcome == "fail":
        return RuleResult(rule, "fail", f"{ran} but it failed ({reason})", evidence)
    return RuleResult(rule, outcome, f"{ran} ({reason})", evidence)


def _diff_in_scope(ctx: Context, scope: str | None) -> bool:
    if ctx.diff is None:
        return False
    return any(paths.in_scope(f.path, scope) for f in ctx.diff.files)


def _touched(ctx: Context, scope: str | None) -> list[str]:
    """Changed (diff) and agent-edited in-repo paths inside ``scope``."""
    key = path_key(ctx)
    found: dict[str, str] = {}
    if ctx.diff is not None:
        for f in ctx.diff.files:
            for p in (f.path, f.old_path):
                if p:
                    found.setdefault(key(p), p)
    for ev in edits_made(ctx):
        rel = edit_relpath(ev, ctx)
        if rel is not None:
            found.setdefault(key(rel), rel)
    return [p for p in found.values() if paths.in_scope(p, scope)]


@register(
    "forbid-edit",
    needs={"session"},
    params={
        "paths": Param("glob_list", nonempty=True, doc="repo files the agent must not edit"),
        "outside_repo": Param(
            "bool", default=False, doc="forbid editing any file outside the repo"
        ),
    },
    doc=(
        "Fails when the agent's file tools touched a path matching `paths`, or with "
        "`outside_repo = true` any path outside the repo, even if the change was later "
        "reverted and never reaches the diff. Failed or blocked edits changed nothing and do "
        "not count. Paths outside the repo are reported as recorded in the transcript. Shell "
        "commands that write files are not seen; pair with forbid-change for those. Set "
        "`paths`, `outside_repo` or both."
    ),
    one_of=("paths", "outside_repo"),
)
def forbid_edit(rule: Rule, ctx: Context) -> RuleResult:
    globs: list[str] | None = rule.params["paths"]
    outside: bool = rule.params["outside_repo"]
    if not globs and not outside:
        return RuleResult(rule, "skip", "nothing to check: set paths or outside_repo")
    found: dict[str, Evidence] = {}
    for ev in edits_made(ctx):
        if not ev.path:
            continue
        rel = edit_relpath(ev, ctx)
        if rel is None:
            if not outside:
                continue
            key, message = ev.path, f"edited {ev.path} outside the repo"
        elif globs and paths.in_scope(rel, rule.scope) and globs_match(rel, globs, rule.scope):
            key, message = rel, f"edited {rel}"
        else:
            continue
        if key not in found:
            found[key] = Evidence(
                message + _actor(ev), path=rel, event=ev.index, excerpt=ev.tool or None
            )
    if not found:
        return RuleResult(rule, "pass", "no forbidden file edited")
    names = list(found)
    return RuleResult(
        rule,
        "fail",
        f"edited {plural(len(names), 'forbidden file')}: {preview(names)}",
        cap(list(found.values())),
    )


_TOOL_KINDS = (EventKind.TOOL, EventKind.COMMAND, EventKind.EDIT)


@register(
    "forbid-tool",
    needs={"session"},
    params={"tool": Param("regex", required=True, doc="tool names the agent must not call")},
    doc=(
        "Fails when the agent or a subagent called a tool whose name matches `tool` (searched "
        "case-sensitively in the agent's own tool name: `WebFetch`, `mcp__github__.*`, "
        "`apply_patch`, ...). Shell and file-edit tools count as well as other tools, and so "
        "does a call that was denied or failed: the agent still tried."
    ),
)
def forbid_tool(rule: Rule, ctx: Context) -> RuleResult:
    assert ctx.session is not None
    pattern: str = rule.params["tool"]
    rx = compile_regex(pattern)
    hits = [
        ev for ev in ctx.session.events if ev.kind in _TOOL_KINDS and ev.tool and rx.search(ev.tool)
    ]
    if not hits:
        return RuleResult(rule, "pass", f"no tool call matches {quoted(pattern)}")
    evidence = [
        Evidence(f"called {ev.tool}{_actor(ev)}", event=ev.index, excerpt=clip(ev.text) or None)
        for ev in hits
    ]
    tools = list(dict.fromkeys(ev.tool or "" for ev in hits))
    times = "once" if len(hits) == 1 else f"{len(hits)} times"
    return RuleResult(rule, "fail", f"called {preview(tools)} {times}", cap(evidence))


@register(
    "forbid-message",
    needs={"session"},
    params={
        "pattern": Param("regex", required=True, doc="text the agent must not say"),
        "ignore_case": Param("bool", default=False, doc="match case-insensitively"),
        "subagents": Param("bool", default=False, doc="also check subagents' messages"),
    },
    doc=(
        "Fails when an assistant message matches `pattern`. Only the main agent's messages "
        "(what the user reads) are checked unless `subagents = true`. The excerpt is the "
        "first matching line."
    ),
)
def forbid_message(rule: Rule, ctx: Context) -> RuleResult:
    assert ctx.session is not None
    pattern: str = rule.params["pattern"]
    rx = compile_regex(pattern, rule.params["ignore_case"])
    evidence: list[Evidence] = []
    for ev in ctx.session.assistant_messages():
        if ev.actor != "main" and not rule.params["subagents"]:
            continue
        m = rx.search(ev.text)
        if m:
            start = ev.text.rfind("\n", 0, m.start()) + 1
            end = ev.text.find("\n", m.end())
            line = ev.text[start : end if end != -1 else len(ev.text)]
            evidence.append(
                Evidence(f"message matches{_actor(ev)}", event=ev.index, excerpt=clip(line))
            )
    if not evidence:
        return RuleResult(rule, "pass", f"no message matches {quoted(pattern)}")
    verb = "matches" if len(evidence) == 1 else "match"
    summary = f"{plural(len(evidence), 'assistant message')} {verb} {quoted(pattern)}"
    return RuleResult(rule, "fail", summary, cap(evidence))
