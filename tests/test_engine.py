from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ruleproof import checks
from ruleproof.engine import run_rule
from ruleproof.instructions import find_instruction_files
from ruleproof.models import (
    Context,
    Diff,
    Event,
    EventKind,
    Report,
    Rule,
    RuleResult,
    Session,
)


@pytest.fixture
def fake_checks() -> Iterator[None]:
    checks.load_all()
    added: list[str] = []

    def reg(name: str, needs: set[checks.Input], needs_any: bool = False) -> None:
        def fn(rule: Rule, ctx: Context) -> RuleResult:
            if rule.params.get("boom"):
                raise RuntimeError("kaboom")
            return RuleResult(rule, "pass", "ok")

        checks.register(name, needs=needs, needs_any=needs_any)(fn)
        added.append(name)

    reg("t-diff", {"diff"})
    reg("t-session", {"session"})
    reg("t-either", {"diff", "session"}, needs_any=True)
    yield
    for name in added:
        checks.REGISTRY.pop(name, None)


def _session() -> Session:
    return Session(agent="generic", id="s", path="s.jsonl", events=[Event(0, EventKind.USER)])


@pytest.mark.usefixtures("fake_checks")
@pytest.mark.parametrize(
    ("check", "has_diff", "has_session", "status", "summary"),
    [
        ("t-diff", True, False, "pass", "ok"),
        ("t-diff", False, True, "skip", "no diff"),
        ("t-session", True, False, "skip", "no transcript"),
        ("t-either", False, True, "pass", "ok"),
        ("t-either", True, False, "pass", "ok"),
        ("t-either", False, False, "skip", "no diff, no transcript"),
    ],
)
def test_inputs_gate_rules(
    check: str, has_diff: bool, has_session: bool, status: str, summary: str
) -> None:
    ctx = Context(
        repo=None,
        diff=Diff(base="HEAD") if has_diff else None,
        session=_session() if has_session else None,
    )
    result = run_rule(Rule(id="r", check=check), ctx)
    assert (result.status, result.summary) == (status, summary)


@pytest.mark.usefixtures("fake_checks")
def test_crash_is_isolated() -> None:
    result = run_rule(
        Rule(id="r", check="t-diff", params={"boom": True}), Context(None, Diff(None))
    )
    assert result.status == "skip"
    assert "RuntimeError: kaboom" in result.summary


def test_unknown_check_is_skipped() -> None:
    result = run_rule(Rule(id="r", check="no-such-check"), Context(None))
    assert result.status == "skip"


def test_report_failed_respects_severity_and_strict() -> None:
    def res(status: str, severity: str) -> RuleResult:
        return RuleResult(Rule(id=f"{status}-{severity}", check="x", severity=severity), status, "")  # type: ignore[arg-type]

    report = Report(
        kind="check",
        tool_version="0",
        repo=None,
        results=[res("fail", "error"), res("fail", "warning"), res("unverified", "error")],
    )
    assert [r.rule.id for r in report.failed("error")] == ["fail-error"]
    assert [r.rule.id for r in report.failed("warning")] == ["fail-error", "fail-warning"]
    assert len(report.failed("error", strict=True)) == 2


def test_session_helpers() -> None:
    s = Session(
        agent="generic",
        id="s",
        path="p",
        events=[
            Event(0, EventKind.USER, "do it"),
            Event(1, EventKind.EDIT, path="a.py", action="modify"),
            Event(2, EventKind.COMMAND, "pytest", exit_code=0),
            Event(3, EventKind.ASSISTANT, "sub", actor="agent-1"),
            Event(4, EventKind.ASSISTANT, "done"),
        ],
    )
    assert [e.index for e in s.commands()] == [2]
    assert [e.index for e in s.edits()] == [1]
    final = s.final_message()
    assert final is not None and final.text == "done"


def test_find_instruction_files(tmp_path: Path) -> None:
    for rel in [
        "AGENTS.md",
        "CLAUDE.md",
        "pkg/AGENTS.md",
        "node_modules/x/AGENTS.md",
        ".hidden/AGENTS.md",
        ".github/copilot-instructions.md",
        ".cursor/rules/style.mdc",
        "README.md",
    ]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    found = [p.relative_to(tmp_path).as_posix() for p in find_instruction_files(tmp_path)]
    assert found == [
        "AGENTS.md",
        "CLAUDE.md",
        ".github/copilot-instructions.md",
        "pkg/AGENTS.md",
        ".cursor/rules/style.mdc",
    ]
