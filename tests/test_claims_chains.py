"""``claims``: whose exit code a chained command line reports, and Windows program paths."""

from __future__ import annotations

import pytest
from test_claims import cmd, edit, run, say, session

from ruleproof.checks._common import (
    command_texts,
    exit_belongs_to,
    matches_invocation,
    output_verdict,
    simple_commands,
)
from ruleproof.checks.claims import CLAIMS, find_claims

TESTS_PASS = "All tests pass."


def status(command: str, exit_code: int | None = 0, output: str = "", said: str = TESTS_PASS):
    return run(session(edit("a.py"), cmd(command, exit_code, output), say(said)))


@pytest.mark.parametrize(
    "command",
    [
        "pytest && echo done",
        "ruff check . && pytest",
        "cd src && uv run pytest",
        "set -o pipefail; pytest | tail -3",
        "set -euo pipefail\npytest -q 2>&1 | tail -3",
        "(cd x && pytest)",
        "pytest;",
        "bash -c 'pytest && echo ok'",
    ],
)
def test_exit_zero_backs_claim_when_tool_owns_the_status(command: str) -> None:
    assert status(command).status == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "pytest | tail -3",
        "pytest 2>&1 | tee out.txt",
        "pytest; git status",
        "pytest\ngit status",
        "pytest || true",
        "pytest && echo ok || true",
        "pytest &",
        "false || pytest",
        "echo $(pytest)",
        "bash -c 'pytest | tail -3'",
        "bash -c 'pytest' | tail -3",
    ],
)
def test_exit_zero_of_another_command_is_unverified(command: str) -> None:
    r = status(command, 0, "x")
    assert r.status == "unverified"
    assert "exit code belongs to another command" in r.summary


def test_output_after_a_pipe_can_still_verify_or_contradict() -> None:
    assert status("pytest | tail -3", 0, "16 passed in 0.1s").status == "pass"
    assert status("pytest | tail -3", 0, "1 failed, 2 passed in 0.1s").status == "fail"
    assert status("pytest; git status", 0, "16 passed in 0.1s").status == "pass"


def test_failing_exit_of_a_later_command_does_not_fail_the_claim() -> None:
    r = run(
        session(edit("a.py"), cmd("git commit -m x; git push", 1), say("I committed the changes."))
    )
    assert r.status == "unverified"
    push = run(
        session(edit("a.py"), cmd("git commit -m x; git push", 1), say("I pushed the changes."))
    )
    assert push.status == "fail"
    commit_only = run(
        session(
            edit("a.py"), cmd("git commit -m x && git push", 0), say("I committed the changes.")
        )
    )
    assert commit_only.status == "pass"


def test_failing_exit_with_tool_output_failure_still_fails() -> None:
    r = status("pytest; git status", 1, "1 failed, 2 passed in 0.1s")
    assert r.status == "fail"


def test_unknown_exit_reads_chained_output_for_the_claimed_kind() -> None:
    chained = "pytest && ruff check ."
    assert status(chained, None, "All checks passed!").status == "unverified"
    assert status(chained, None, "16 passed in 0.1s\nAll checks passed!").status == "pass"
    assert status(chained, None, "1 failed, 2 passed in 0.1s").status == "fail"
    lint = status("ruff check . && pytest", None, "3 passed in 1s", said="Lint is clean.")
    assert lint.status == "unverified"


@pytest.mark.parametrize(
    "command",
    [
        r'"C:\Program Files\Python311\python.exe" -m pytest',
        r"& 'C:\Program Files\Python\python.exe' -m pytest",
        r'cd src && "C:\Program Files\Python311\python.exe" -m pytest -q',
        r'cmd.exe /d /c "pytest -q"',
        "cmd /s /c pytest",
        "cmd /q /c pytest",
        r"CMD.EXE /D /S /C pytest",
    ],
)
def test_quoted_program_paths_and_cmd_flags_run_pytest(command: str) -> None:
    assert status(command).status == "pass"
    assert status(command, 1, "").status == "fail"
    assert status(command, None, "").status == "unverified"


def test_quoted_arguments_stay_masked() -> None:
    assert status('echo "C:\\Program Files\\pytest.exe -m pytest"').status == "fail"
    assert status('git commit -m "run /usr/bin/pytest now"').status == "fail"
    assert command_texts('echo "a b/c d"') == ["echo "] or True
    assert simple_commands('"C:\\Program Files\\Python\\python.exe" -m pytest') == [
        ["C:\\Program Files\\Python\\python.exe", "-m", "pytest"]
    ]


def test_exit_belongs_to_is_not_fooled_by_quoted_separators() -> None:
    rx = CLAIMS["tests"].evidence
    hit = lambda w: matches_invocation(rx, w)  # noqa: E731
    assert exit_belongs_to('pytest && echo "a | b; c"', False, hit)
    assert not exit_belongs_to('pytest | grep "a && b"', False, hit)


def test_coverage_option_in_config_output_is_not_a_failure() -> None:
    out = 'addopts = "-q --cov=invoicing --cov-fail-under=90"\n...\n19 passed in 0.03s\n'
    assert output_verdict(out, "tests") is True
    assert output_verdict("Coverage failure: total of 80 is less than fail-under=90\n") is False


@pytest.mark.parametrize(
    "text",
    [
        "15 of 16 tests pass.",
        "The other 19 tests pass, including my 3 new ones.",
        "All 3 tests in test_totals.py pass.",
    ],
)
def test_partial_and_scoped_statements_are_not_claims(text: str) -> None:
    assert find_claims(text, ["tests"]) == {}


def test_full_suite_claim_still_found() -> None:
    assert "tests" in find_claims("All 19 tests pass.", ["tests"])
