"""Checks that read the diff (and, for forbid-change, the agent's edits)."""

from __future__ import annotations

from ruleproof import paths
from ruleproof.checks import Param, register
from ruleproof.checks._common import (
    cap,
    clip,
    compile_regex,
    edit_relpath,
    globs_match,
    plural,
    preview,
    quoted,
)
from ruleproof.models import Context, Diff, Evidence, FileChange, Rule, RuleResult

_ACTIONS = ("add", "modify", "delete")
_PAST = {"add": "added", "modify": "modified", "delete": "deleted"}
_STATUS_ACTION = {"added": "add", "modified": "modify", "deleted": "delete"}


def _diff_actions(diff: Diff) -> dict[str, str]:
    """Path -> action. A rename counts as deleting the old path and adding the new one."""
    out: dict[str, str] = {}
    for f in diff.files:
        if f.status == "renamed":
            if f.old_path:
                out[f.old_path] = "delete"
            out[f.path] = "add"
        else:
            out[f.path] = _STATUS_ACTION[f.status]
    return out


def _selected(rule: Rule, path: str, include: list[str] | None, exclude: list[str]) -> bool:
    """In scope, matching ``include`` (None = everything) and not ``exclude``."""
    if not paths.in_scope(path, rule.scope):
        return False
    if include is not None and not globs_match(path, include, rule.scope):
        return False
    return not globs_match(path, exclude, rule.scope)


@register(
    "forbid-change",
    needs={"diff", "session"},
    needs_any=True,
    params={
        "paths": Param("glob_list", required=True, doc="files that must not change"),
        "except": Param("glob_list", default=[], doc="files exempt from `paths`"),
        "actions": Param(
            "str_list", default=list(_ACTIONS), doc="which changes count: add, modify, delete"
        ),
    },
    doc=(
        "Fails when a file matching `paths` was added, modified or deleted, according to the "
        "diff or to the agent's own edits in the transcript (so a file created and removed "
        "again is still caught). A rename counts as deleting the old path and adding the new "
        "one. When the diff lists a path, the diff decides the action (every edit of a file "
        "that is new since the base is an add); the transcript's action is used only for paths "
        "the diff does not list, with an unknown action counting as `modify`. Edits outside "
        "the repo are ignored; use forbid-edit for those. One finding per path."
    ),
)
def forbid_change(rule: Rule, ctx: Context) -> RuleResult:
    actions = set(rule.params["actions"])
    unknown = sorted(actions - set(_ACTIONS))
    if unknown:
        return RuleResult(rule, "skip", f"unknown action(s): {', '.join(unknown)}")
    include: list[str] = rule.params["paths"]
    exclude: list[str] = rule.params["except"]
    diff_actions = _diff_actions(ctx.diff) if ctx.diff is not None else {}

    found: dict[str, Evidence] = {}
    for path, action in diff_actions.items():
        if action in actions and _selected(rule, path, include, exclude):
            found[path] = Evidence(f"{_PAST[action]} {path}", path=path)
    if ctx.session is not None:
        for ev in ctx.session.edits():
            rel = edit_relpath(ev, ctx)
            # A path in the diff was judged above from its base state: a re-write of a file
            # the diff shows as new is still an add, a Write over an existing file a modify.
            if rel is None or rel in diff_actions or not _selected(rule, rel, include, exclude):
                continue
            done = ev.action if ev.action and ev.action != "unknown" else "modify"
            if rel not in found and done in actions:
                found[rel] = Evidence(
                    f"agent {_PAST[done]} {rel} (event #{ev.index})", path=rel, event=ev.index
                )
    if not found:
        return RuleResult(rule, "pass", "no forbidden file changed")
    names = list(found)
    return RuleResult(
        rule,
        "fail",
        f"{plural(len(names), 'forbidden file')} changed: {preview(names)}",
        cap(list(found.values())),
    )


def _changed_paths(f: FileChange) -> list[str]:
    return [f.path, f.old_path] if f.status == "renamed" and f.old_path else [f.path]


@register(
    "require-change",
    needs={"diff"},
    params={
        "if_changed": Param("glob_list", required=True, doc="files whose change triggers it"),
        "then_changed": Param("glob_list", required=True, doc="files that must change too"),
        "except": Param("glob_list", default=[], doc="files exempt from `if_changed`"),
    },
    doc=(
        "Fails when some changed file matches `if_changed` (and not `except`) but no changed "
        "file matches `then_changed`, e.g. source changes without a CHANGELOG entry. Both the "
        "old and new path of a rename count. For a scoped rule only triggering files inside the "
        "scope count; `then_changed` may match anywhere in the repo."
    ),
)
def require_change(rule: Rule, ctx: Context) -> RuleResult:
    assert ctx.diff is not None
    then: list[str] = rule.params["then_changed"]
    all_paths = [p for f in ctx.diff.files for p in _changed_paths(f)]
    triggers = [
        p for p in all_paths if _selected(rule, p, rule.params["if_changed"], rule.params["except"])
    ]
    if not triggers:
        return RuleResult(rule, "pass", "not required: no matching changes")
    satisfied = [p for p in all_paths if globs_match(p, then, rule.scope)]
    want = preview(then)
    if satisfied:
        return RuleResult(rule, "pass", f"{satisfied[0]} changed along with {triggers[0]}")
    if len(triggers) == 1:
        summary = f"{triggers[0]} changed but nothing matching {want} did"
    else:
        summary = f"{len(triggers)} matching files changed but nothing matching {want} did"
    evidence = [Evidence(f"changed without {want}", path=p) for p in triggers]
    return RuleResult(rule, "fail", summary, cap(evidence))


@register(
    "forbid-text",
    needs={"diff"},
    params={
        "pattern": Param("regex", required=True, doc="text that must not be added"),
        "paths": Param("glob_list", doc="files to search (default: all)"),
        "except": Param("glob_list", default=[], doc="files not searched"),
        "ignore_case": Param("bool", default=False, doc="match case-insensitively"),
    },
    doc=(
        "Fails when a line added by the diff matches `pattern` (searched line by line). Only "
        "added lines count, so pre-existing occurrences are fine. Binary files are skipped. "
        "One finding per line, with the line number and the line itself."
    ),
)
def forbid_text(rule: Rule, ctx: Context) -> RuleResult:
    assert ctx.diff is not None
    pattern: str = rule.params["pattern"]
    rx = compile_regex(pattern, rule.params["ignore_case"])
    evidence: list[Evidence] = []
    for f in ctx.diff.files:
        if f.binary or not _selected(rule, f.path, rule.params["paths"], rule.params["except"]):
            continue
        for line_no, text in f.added:
            if rx.search(text):
                evidence.append(
                    Evidence(
                        f"added line matches {quoted(pattern)}",
                        path=f.path,
                        line=line_no,
                        excerpt=clip(text),
                    )
                )
    if not evidence:
        return RuleResult(rule, "pass", f"no added line matches {quoted(pattern)}")
    verb = "matches" if len(evidence) == 1 else "match"
    summary = f"{plural(len(evidence), 'added line')} {verb} {quoted(pattern)}"
    return RuleResult(rule, "fail", summary, cap(evidence))


@register(
    "require-text",
    needs={"diff"},
    params={
        "pattern": Param("regex", required=True, doc="text each file must contain"),
        "paths": Param("glob_list", doc="files that must contain it (default: all)"),
        "new_files_only": Param("bool", default=True, doc="only check files the diff adds"),
    },
    doc=(
        "Fails when a file in scope lacks a match for `pattern` (multiline: `^` and `$` match "
        "at line ends), e.g. a license header in every new source file. By default only files "
        "the diff adds are checked; with `new_files_only = false` modified and renamed files "
        "are checked too, against their full content read from the repo when available, else "
        "against the added lines. Deleted and binary files are skipped."
    ),
)
def require_text(rule: Rule, ctx: Context) -> RuleResult:
    assert ctx.diff is not None
    pattern: str = rule.params["pattern"]
    rx = compile_regex(pattern, multiline=True)
    new_only: bool = rule.params["new_files_only"]
    files = [
        f
        for f in ctx.diff.files
        if not f.binary
        and f.status != "deleted"
        and (f.status == "added" or not new_only)
        and _selected(rule, f.path, rule.params["paths"], [])
    ]
    kind = "new file" if new_only else "changed file"
    if not files:
        return RuleResult(rule, "pass", f"no {kind}s in scope")
    missing = [f.path for f in files if not rx.search(_content(f, ctx))]
    if not missing:
        return RuleResult(rule, "pass", f"{plural(len(files), kind)} match {quoted(pattern)}")
    verb = "lacks" if len(missing) == 1 else "lack"
    summary = f"{plural(len(missing), kind)} {verb} {quoted(pattern)}"
    evidence = [Evidence(f"no match for {quoted(pattern)}", path=p) for p in missing]
    return RuleResult(rule, "fail", summary, cap(evidence))


def _content(f: FileChange, ctx: Context) -> str:
    added = "\n".join(text for _, text in f.added)
    if f.status == "added" or ctx.repo is None:
        return added
    try:
        return (ctx.repo / f.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return added


@register(
    "max-diff",
    needs={"diff"},
    params={
        "max_files": Param("int", doc="most files the diff may touch"),
        "max_lines": Param("int", doc="most changed lines (added + removed)"),
        "except": Param("glob_list", default=[], doc="files not counted (e.g. lockfiles)"),
    },
    doc=(
        "Fails when the diff touches more than `max_files` files or changes more than "
        "`max_lines` lines (added plus removed; binary files count as files only). Set at "
        "least one limit. Files matching `except`, and for a scoped rule files outside the "
        "scope, are not counted. The largest files are listed as evidence."
    ),
)
def max_diff(rule: Rule, ctx: Context) -> RuleResult:
    assert ctx.diff is not None
    max_files: int | None = rule.params["max_files"]
    max_lines: int | None = rule.params["max_lines"]
    if max_files is None and max_lines is None:
        return RuleResult(rule, "skip", "no limit set: give max_files or max_lines")
    files = [f for f in ctx.diff.files if _selected(rule, f.path, None, rule.params["except"])]
    sizes = {f.path: len(f.added) + f.removed for f in files}
    n_lines = sum(sizes.values())
    problems: list[str] = []
    if max_files is not None and len(files) > max_files:
        problems.append(f"touches {plural(len(files), 'file')} (max {max_files})")
    if max_lines is not None and n_lines > max_lines:
        problems.append(f"changes {plural(n_lines, 'line')} (max {max_lines})")
    if not problems:
        return RuleResult(
            rule,
            "pass",
            f"diff within limits: {plural(len(files), 'file')}, {plural(n_lines, 'line')}",
        )
    largest = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    evidence = [Evidence(f"{plural(n, 'line')} changed", path=p) for p, n in largest]
    return RuleResult(rule, "fail", "diff " + " and ".join(problems), evidence)
