"""Tests for the diff checks: forbid-change, require-change, forbid-text, require-text, max-diff."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ruleproof.checks import load_all
from ruleproof.engine import run_rule
from ruleproof.models import (
    ChangeStatus,
    Context,
    Diff,
    EditAction,
    Event,
    EventKind,
    FileChange,
    Rule,
    RuleResult,
    Session,
)

REPO = Path("C:/work/repo")


def make_rule(check: str, scope: str | None = None, **params: Any) -> Rule:
    """A rule with params completed from the registered schema, as the loader would."""
    spec = load_all()[check]
    missing = [k for k, p in spec.params.items() if p.required and k not in params]
    assert not missing, f"test forgot required params {missing}"
    full = {k: copy.deepcopy(p.default) for k, p in spec.params.items()}
    full.update(params)
    return Rule(id="r", check=check, params=full, scope=scope)


def run(rule: Rule, ctx: Context) -> RuleResult:
    return load_all()[rule.check].fn(rule, ctx)


def fc(
    path: str,
    status: ChangeStatus = "modified",
    added: list[str] | None = None,
    removed: int = 0,
    old_path: str | None = None,
    binary: bool = False,
) -> FileChange:
    lines = [(i + 1, t) for i, t in enumerate(added or [])]
    return FileChange(path, status, old_path, lines, removed, binary)


def diff(*files: FileChange) -> Diff:
    return Diff("HEAD", list(files))


def edits_in(repo: Path, *items: tuple[str, EditAction | None]) -> Session:
    events = [
        Event(i, EventKind.EDIT, path=str(repo / p), action=a, tool="Write")
        for i, (p, a) in enumerate(items)
    ]
    return Session("generic", "s", "t.jsonl", cwd=str(repo), events=events)


def edits(*items: tuple[str, EditAction | None]) -> Session:
    events = [
        Event(i, EventKind.EDIT, path=p, action=a, tool="Write") for i, (p, a) in enumerate(items)
    ]
    return Session("generic", "s", "t.jsonl", cwd=str(REPO), events=events)


def test_all_checks_have_docs_and_param_docs() -> None:
    for name, spec in load_all().items():
        assert spec.doc, name
        for pname, p in spec.params.items():
            assert p.doc, f"{name}.{pname}"
            assert not (p.required and p.default is not None), f"{name}.{pname}"


# --- forbid-change ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "params", "status", "flagged"),
    [
        ([fc("a.bak", "added")], {"paths": ["*.bak"]}, "fail", ["a.bak"]),
        ([fc("src/x.bak", "added")], {"paths": ["*.bak"]}, "fail", ["src/x.bak"]),
        ([fc("a.py", "added")], {"paths": ["*.bak"]}, "pass", []),
        ([fc("a.bak", "modified")], {"paths": ["*.bak"], "actions": ["add"]}, "pass", []),
        ([fc("a.bak", "deleted")], {"paths": ["*.bak"], "actions": ["delete"]}, "fail", ["a.bak"]),
        (
            [fc("gen/a.py"), fc("gen/keep.py")],
            {"paths": ["gen/"], "except": ["gen/keep.py"]},
            "fail",
            ["gen/a.py"],
        ),
        # renames: old path deleted, new path added
        (
            [fc("new.py", "renamed", old_path="gen/old.py")],
            {"paths": ["gen/"], "actions": ["delete"]},
            "fail",
            ["gen/old.py"],
        ),
        (
            [fc("new.py", "renamed", old_path="gen/old.py")],
            {"paths": ["gen/"], "actions": ["add", "modify"]},
            "pass",
            [],
        ),
        (
            [fc("gen/new.py", "renamed", old_path="old.py")],
            {"paths": ["gen/"], "actions": ["add"]},
            "fail",
            ["gen/new.py"],
        ),
    ],
)
def test_forbid_change_diff(
    files: list[FileChange], params: dict[str, Any], status: str, flagged: list[str]
) -> None:
    r = run(make_rule("forbid-change", **params), Context(REPO, diff(*files)))
    assert r.status == status
    assert [e.path for e in r.evidence] == flagged


def test_forbid_change_transcript_only_catches_created_then_removed() -> None:
    s = edits((r"C:\work\repo\notes.bak", "add"), (r"C:\work\repo\notes.bak", "delete"))
    r = run(make_rule("forbid-change", paths=["*.bak"], actions=["add"]), Context(REPO, diff(), s))
    assert r.status == "fail"
    assert len(r.evidence) == 1
    assert (r.evidence[0].path, r.evidence[0].event) == ("notes.bak", 0)


def test_forbid_change_without_diff_uses_transcript() -> None:
    s = edits(("C:/work/repo/api/gen/client.py", "modify"))
    r = run(make_rule("forbid-change", paths=["api/gen/"]), Context(REPO, None, s))
    assert r.status == "fail"
    assert r.summary == "1 forbidden file changed: api/gen/client.py"


def test_forbid_change_one_evidence_per_path_prefers_diff() -> None:
    s = edits(("C:/work/repo/a.bak", "add"), ("C:/work/repo/a.bak", "modify"))
    r = run(
        make_rule("forbid-change", paths=["*.bak"]), Context(REPO, diff(fc("a.bak", "added")), s)
    )
    assert len(r.evidence) == 1
    assert r.evidence[0].event is None


def test_forbid_change_ignores_edits_outside_repo() -> None:
    s = edits(("C:/elsewhere/x.bak", "add"), ("C:/work/repo2/y.bak", "add"))
    r = run(make_rule("forbid-change", paths=["*.bak"]), Context(REPO, None, s))
    assert r.status == "pass"


@pytest.mark.parametrize(
    ("diff_status", "exists", "actions", "status"),
    [
        (None, True, ["modify"], "fail"),  # not in the diff, still there: modify
        (None, True, ["add", "delete"], "pass"),
        (None, False, ["add"], "fail"),  # not in the diff and gone: created, then removed
        (None, False, ["modify"], "pass"),
        ("added", True, ["modify"], "pass"),  # the diff says it is new: add
        ("deleted", False, ["modify"], "pass"),
        ("modified", True, ["modify"], "fail"),
    ],
)
def test_forbid_change_unknown_edit_action(
    tmp_path: Path,
    diff_status: ChangeStatus | None,
    exists: bool,
    actions: list[str],
    status: str,
) -> None:
    if exists:
        (tmp_path / "x.lock").write_text("x", encoding="utf-8")
    s = edits_in(tmp_path, ("x.lock", "unknown"))
    d = diff(fc("x.lock", diff_status)) if diff_status else diff()
    rule = make_rule("forbid-change", paths=["*.lock"], actions=actions)
    assert run(rule, Context(tmp_path, d, s)).status == status


def test_forbid_change_unknown_action_without_repo_counts_as_modify() -> None:
    s = edits(("C:/work/repo/x.lock", "unknown"))
    rule = make_rule("forbid-change", paths=["*.lock"], actions=["modify"])
    assert run(rule, Context(None, None, s)).status == "fail"


@pytest.mark.parametrize(
    ("edit_actions", "diff_status", "actions", "status"),
    [
        # regression: a new test file written twice is an add, not a modify
        (["add", "modify"], "added", ["modify", "delete"], "pass"),
        (["add", "add"], "added", ["modify", "delete"], "pass"),
        (["unknown", "unknown"], "added", ["modify", "delete"], "pass"),
        (["add", "modify"], "added", ["add"], "fail"),
        # a Write ("add") over a file that existed at base is a modify
        (["add"], "modified", ["add"], "pass"),
        (["add"], "modified", ["modify"], "fail"),
        (["modify"], "deleted", ["modify"], "pass"),
        # not in the diff (created then removed): the transcript's actions decide
        (["add", "delete"], None, ["add"], "fail"),
        (["add", "delete"], None, ["modify"], "pass"),
    ],
)
def test_forbid_change_diff_base_state_decides(
    edit_actions: list[EditAction],
    diff_status: ChangeStatus | None,
    actions: list[str],
    status: str,
) -> None:
    s = edits(*[("C:/work/repo/tests/test_new.py", a) for a in edit_actions])
    d = diff(fc("tests/test_new.py", diff_status)) if diff_status else diff()
    rule = make_rule("forbid-change", paths=["tests/**"], actions=actions)
    assert run(rule, Context(REPO, d, s)).status == status


def test_forbid_change_scoped_rule() -> None:
    files = [fc("pkg/gen/a.py"), fc("gen/b.py"), fc("other/gen/c.py")]
    rule = make_rule("forbid-change", scope="pkg", paths=["gen/"])
    r = run(rule, Context(REPO, diff(*files)))
    assert [e.path for e in r.evidence] == ["pkg/gen/a.py"]
    rule = make_rule("forbid-change", scope="pkg", paths=["pkg/gen/"])
    assert [e.path for e in run(rule, Context(REPO, diff(*files))).evidence] == ["pkg/gen/a.py"]


def test_param_constraints_are_declared() -> None:
    specs = load_all()
    assert specs["forbid-change"].params["actions"].choices == ("add", "modify", "delete")
    for check, name in [
        ("forbid-change", "paths"),
        ("require-change", "if_changed"),
        ("require-change", "then_changed"),
        ("require-command", "when_paths"),
        ("forbid-edit", "paths"),
    ]:
        assert specs[check].params[name].nonempty, (check, name)
    assert specs["max-diff"].one_of == ("max_files", "max_lines")
    assert specs["forbid-edit"].one_of == ("paths", "outside_repo")


def test_forbid_change_runs_with_either_input_via_engine() -> None:
    rule = make_rule("forbid-change", paths=["*.bak"])
    assert (
        run_rule(rule, Context(REPO, None, edits(("C:/work/repo/a.bak", "add")))).status == "fail"
    )
    assert run_rule(rule, Context(REPO, diff(fc("a.bak", "added")), None)).status == "fail"
    assert run_rule(rule, Context(REPO, None, None)).status == "skip"


# --- require-change ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "status", "summary"),
    [
        ([fc("README.md")], "pass", "not required: no matching changes"),
        ([fc("src/a.py"), fc("CHANGELOG.md")], "pass", "CHANGELOG.md changed along with src/a.py"),
        ([fc("src/a.py")], "fail", "src/a.py changed but nothing matching CHANGELOG.md did"),
        (
            [fc("src/a.py"), fc("src/b.py")],
            "fail",
            "2 matching files changed but nothing matching CHANGELOG.md did",
        ),
        ([fc("src/gen/x.py")], "pass", "not required: no matching changes"),  # except
        ([fc("lib/a.py", "renamed", old_path="src/a.py")], "fail", None),  # old path counts
    ],
)
def test_require_change(files: list[FileChange], status: str, summary: str | None) -> None:
    rule = make_rule(
        "require-change",
        if_changed=["src/**"],
        then_changed=["CHANGELOG.md"],
        **{"except": ["src/gen/"]},
    )
    r = run(rule, Context(REPO, diff(*files)))
    assert r.status == status
    if summary:
        assert r.summary == summary


def test_require_change_scoped_then_changed_may_be_outside_scope() -> None:
    rule = make_rule(
        "require-change", scope="pkg", if_changed=["src/**"], then_changed=["CHANGELOG.md"]
    )
    assert run(rule, Context(REPO, diff(fc("src/a.py")))).status == "pass"  # out of scope
    assert run(rule, Context(REPO, diff(fc("pkg/src/a.py")))).status == "fail"
    files = diff(fc("pkg/src/a.py"), fc("CHANGELOG.md"))
    assert run(rule, Context(REPO, files)).status == "pass"


# --- forbid-text ------------------------------------------------------------------------


def test_forbid_text_reports_each_added_line() -> None:
    d = diff(
        fc("a.py", added=["x = 1", "print(x)", "  print('y')"]),
        fc("b.md", added=["print(this) in docs"]),
        fc("c.png", "added", binary=True),
    )
    rule = make_rule("forbid-text", pattern=r"print\(", paths=["*.py"])
    r = run(rule, Context(REPO, d))
    assert r.status == "fail"
    assert r.summary == '2 added lines match "print\\("'
    assert [(e.path, e.line, e.excerpt) for e in r.evidence] == [
        ("a.py", 2, "print(x)"),
        ("a.py", 3, "print('y')"),
    ]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"pattern": "TODO"}, "pass"),
        ({"pattern": "TODO", "ignore_case": True}, "fail"),
        ({"pattern": "todo", "except": ["*.py"]}, "pass"),
    ],
)
def test_forbid_text_options(params: dict[str, Any], status: str) -> None:
    r = run(make_rule("forbid-text", **params), Context(REPO, diff(fc("a.py", added=["# todo"]))))
    assert r.status == status


def test_forbid_text_excerpt_capped_and_singular_summary() -> None:
    r = run(
        make_rule("forbid-text", pattern="SECRET"),
        Context(REPO, diff(fc("a", added=["SECRET" + "x" * 500]))),
    )
    assert r.summary == '1 added line matches "SECRET"'
    excerpt = r.evidence[0].excerpt
    assert excerpt is not None and len(excerpt) == 200


def test_forbid_text_scope() -> None:
    d = diff(fc("pkg/a.py", added=["breakpoint()"]), fc("b.py", added=["breakpoint()"]))
    r = run(make_rule("forbid-text", scope="pkg", pattern="breakpoint"), Context(REPO, d))
    assert [e.path for e in r.evidence] == ["pkg/a.py"]


def test_forbid_text_caps_evidence() -> None:
    d = diff(fc("a.py", added=["bad"] * 80))
    r = run(make_rule("forbid-text", pattern="bad"), Context(REPO, d))
    assert r.summary.startswith("80 added lines")
    assert len(r.evidence) == 51
    assert r.evidence[-1].message == "... and 30 more"


# --- require-text -----------------------------------------------------------------------


def test_require_text_new_files_only() -> None:
    d = diff(
        fc("src/a.py", "added", ["# SPDX-License-Identifier: MIT", "x = 1"]),
        fc("src/b.py", "added", ["x = 2"]),
        fc("src/c.py", "modified", ["y = 3"]),
        fc("README.md", "added", ["hi"]),
    )
    rule = make_rule("require-text", pattern=r"^# SPDX-License-Identifier:", paths=["*.py"])
    r = run(rule, Context(REPO, d))
    assert r.status == "fail"
    assert r.summary == '1 new file lacks "^# SPDX-License-Identifier:"'
    assert [e.path for e in r.evidence] == ["src/b.py"]


def test_require_text_modified_files_read_from_repo(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("# header\nold\nnew\n", encoding="utf-8")
    d = diff(fc("a.py", "modified", ["new"]), fc("gone.py", "modified", ["z"]))
    rule = make_rule("require-text", pattern="^# header", new_files_only=False)
    r = run(rule, Context(tmp_path, d))
    assert [e.path for e in r.evidence] == ["gone.py"]  # unreadable: falls back to added lines


@pytest.mark.parametrize(
    ("files", "summary"),
    [
        ([], "no new files in scope"),
        ([fc("x.py", "deleted"), fc("y.bin", "added", binary=True)], "no new files in scope"),
        ([fc("x.py", "added", ["MIT"])], '1 new file match "MIT"'),
    ],
)
def test_require_text_pass(files: list[FileChange], summary: str) -> None:
    r = run(make_rule("require-text", pattern="MIT"), Context(REPO, diff(*files)))
    assert (r.status, r.summary) == ("pass", summary)


# --- max-diff ---------------------------------------------------------------------------


def test_max_diff() -> None:
    d = diff(
        fc("a.py", added=["x"] * 30, removed=10),
        fc("b.py", added=["x"] * 5),
        fc("uv.lock", added=["x"] * 1000),
    )
    rule = make_rule("max-diff", max_files=1, max_lines=40, **{"except": ["*.lock"]})
    r = run(rule, Context(REPO, d))
    assert r.status == "fail"
    assert r.summary == "diff touches 2 files (max 1) and changes 45 lines (max 40)"
    assert [e.path for e in r.evidence] == ["a.py", "b.py"]
    rule = make_rule("max-diff", max_lines=45, **{"except": ["*.lock"]})
    assert run(rule, Context(REPO, d)).summary == "diff within limits: 2 files, 45 lines"


def test_max_diff_without_limits_is_skipped() -> None:
    assert run(make_rule("max-diff"), Context(REPO, diff())).status == "skip"


def test_max_diff_scope() -> None:
    d = diff(fc("pkg/a.py", added=["x"]), fc("b.py", added=["x"] * 100))
    assert run(make_rule("max-diff", scope="pkg", max_lines=1), Context(REPO, d)).status == "pass"


# --- regressions from review ------------------------------------------------------------


def test_forbid_change_failed_edit_does_not_count() -> None:
    s = edits(("C:/work/repo/migrations/001.py", "modify"))
    s.events[0].is_error = True  # "String to replace not found" / blocked by a hook
    rule = make_rule("forbid-change", paths=["migrations/**"])
    assert run(rule, Context(REPO, diff(), s)).status == "pass"


def test_forbid_change_skips_gitignored_transcript_edits(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    s = edits_in(tmp_path, (".env", "add"))
    rule = make_rule("forbid-change", paths=[".env"], actions=["add"])
    assert run(rule, Context(tmp_path, diff(), s)).status == "pass"
    rule = make_rule("forbid-change", paths=[".env"], actions=["add"], include_ignored=True)
    assert run(rule, Context(tmp_path, diff(), s)).status == "fail"


def test_forbid_change_windows_paths_compared_case_insensitively() -> None:
    s = edits((r"C:\WORK\Repo\Tests\Test_New.py", "modify"))
    d = diff(fc("Tests/Test_New.py", "added"))
    rule = make_rule("forbid-change", paths=["Tests/**", "tests/**"], actions=["modify"])
    assert run(rule, Context(REPO, d, s)).status == "pass"  # the diff says added


@pytest.mark.parametrize(
    ("path", "lines", "flagged"),
    [
        ("ruleproof.toml", ['pattern = "TODO"'], []),
        (".ruleproof.toml", ['pattern = "TODO"'], []),
        ("AGENTS.md", ["- No TODOs.", '  <!-- ruleproof: forbid-text pattern="TODO" -->'], [1]),
        (
            "AGENTS.md",
            ["<!-- ruleproof: forbid-text", '     pattern="TODO" -->', "TODO: later"],
            [3],
        ),
        (
            "pyproject.toml",
            ["[tool.ruleproof]", 'pattern = "TODO"', "[project]", 'name = "TODO"'],
            [4],
        ),
        ("src/a.py", ["# TODO"], [1]),
    ],
)
def test_forbid_text_skips_rule_definitions(
    path: str, lines: list[str], flagged: list[int]
) -> None:
    r = run(make_rule("forbid-text", pattern="TODO"), Context(REPO, diff(fc(path, added=lines))))
    assert [e.line for e in r.evidence] == flagged


def test_forbid_text_redact_and_token_masking() -> None:
    line = 'API_KEY = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"'
    d = diff(fc("a.py", added=[line]))
    r = run(make_rule("forbid-text", pattern="ghp_[A-Za-z0-9]+"), Context(REPO, d))
    assert r.evidence[0].excerpt == 'API_KEY = "ghp_<redacted:36 chars>"'
    r = run(make_rule("forbid-text", pattern="ghp_[A-Za-z0-9]+", redact=True), Context(REPO, d))
    assert r.evidence[0].excerpt == 'API_KEY = "<redacted:40 chars>"'
    d = diff(fc("a.py", added=['password = "hunter2hunter2"']))
    r = run(make_rule("forbid-text", pattern="hunter2+", redact=True), Context(REPO, d))
    excerpt = r.evidence[0].excerpt
    assert excerpt is not None and "hunter" not in excerpt
