"""Regressions from a review of the claims check over real Windows sessions (synthetic data)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_claims import REPO, cmd, edit, make_rule, run, say, session

from ruleproof.checks import load_all
from ruleproof.checks._common import is_powershell
from ruleproof.checks.claims import find_claims
from ruleproof.models import Context, Event, EventKind, Session
from ruleproof.transcripts import load_session

# --- Windows command lines --------------------------------------------------------------

WINDOWS_TEST_RUNS = [
    r".venv\Scripts\python.exe -m unittest",
    r".venv\Scripts\pytest.exe -q",
    r"C:\work\repo\.venv\Scripts\python.exe -m pytest tests",
    r"& .\.venv\Scripts\python.exe -m pytest",
    r"cd C:\work\repo; .\.venv\Scripts\pytest -q",
]


@pytest.mark.parametrize("command", WINDOWS_TEST_RUNS)
def test_windows_test_runs_back_the_claim(command: str) -> None:
    result = run(session(edit("a.py"), cmd(command), say("All tests pass.")))
    assert result.status == "pass", result.summary


@pytest.mark.parametrize("command", WINDOWS_TEST_RUNS)
def test_windows_run_supersedes_an_older_failure(command: str) -> None:
    s = session(edit("a.py"), cmd("pytest -q", exit_code=1), cmd(command), say("Tests pass."))
    result = run(s)
    assert result.status == "pass", result.summary
    assert "pytest -q" not in result.summary


@pytest.mark.parametrize("command", [*WINDOWS_TEST_RUNS, "uv run pytest", "python -m unittest"])
def test_claims_and_require_command_agree(command: str) -> None:
    s = session(edit("a.py"), cmd(command), say("Tests pass."))
    ctx = Context(REPO, None, s)
    required = make_rule("require-command", command=r"\b(?:pytest|unittest)\b")
    assert load_all()["require-command"].fn(required, ctx).status == "pass"
    assert run(s).status == "pass"


def _codex_rollout(path: Path, cwd: str, command: str, exit_code: int, text: str) -> None:
    meta = {"id": "019e0000-0000-7000-8000-000000000001", "timestamp": "2026-10-01T10:00:00Z"}
    records: list[dict[str, Any]] = [
        {
            "timestamp": "2026-10-01T10:00:00Z",
            "type": "session_meta",
            "payload": {**meta, "cwd": cwd},
        },
        {
            "timestamp": "2026-10-01T10:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": command}),
                "call_id": "c1",
            },
        },
        {
            "timestamp": "2026-10-01T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps(
                    {"output": "Ran 4 tests in 0.01s\n\nOK", "metadata": {"exit_code": exit_code}}
                ),
            },
        },
        {
            "timestamp": "2026-10-01T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_codex_on_windows(tmp_path: Path) -> None:
    path = tmp_path / "rollout-2026-10-01T10-00-00-019e0000-0000-7000-8000-000000000001.jsonl"
    _codex_rollout(
        path, "C:\\work\\repo", r".venv\Scripts\python.exe -m unittest", 0, "All tests pass."
    )
    s = load_session(path)
    (command,) = s.commands()
    ctx = Context(REPO, None, s)
    assert is_powershell(command, ctx)
    result = load_all()["claims"].fn(make_rule("claims"), ctx)
    assert result.status == "pass", result.summary


def test_codex_shell_is_powershell_only_on_windows() -> None:
    ev = Event(0, EventKind.COMMAND, "pytest", tool="exec_command")
    posix = Session("codex", "s", "t.jsonl", cwd="/home/u/repo", events=[ev])
    windows = Session("codex", "s", "t.jsonl", cwd="D:/src/repo", events=[ev])
    claude = Session("claude-code", "s", "t.jsonl", cwd="D:/src/repo", events=[ev])
    assert not is_powershell(ev, Context(REPO, None, posix))
    assert is_powershell(ev, Context(REPO, None, windows))
    assert not is_powershell(ev, Context(REPO, None, claude))


# --- more runners and project commands --------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -X utf8 -m unittest",
        "py -3 -m pytest",
        "python -W error -m pytest -q",
        "python manage.py test",
        "python runtests.py",
        "./scripts/test.sh",
        "bash scripts/test.sh",
        r".\scripts\run-tests.ps1",
        "bazel test //...",
    ],
)
def test_more_test_runners(command: str) -> None:
    result = run(session(edit("a.py"), cmd(command), say("All tests pass.")))
    assert result.status == "pass", result.summary


@pytest.mark.parametrize(
    "command", ["test -f setup.py", "cat scripts/test.sh", "python manage.py migrate"]
)
def test_runner_lookalikes(command: str) -> None:
    assert run(session(edit("a.py"), cmd(command), say("All tests pass."))).status == "fail"


def test_project_test_command_from_config() -> None:
    s = session(edit("a.py"), cmd("make ci-fast"), say("All tests pass."))
    assert run(s).status == "fail"
    assert run(s, test_commands=[r"\bmake\s+ci-fast\b"]).status == "pass"
    lint = session(edit("a.py"), cmd("./tools/lint-all"), say("Lint is clean."))
    assert run(lint, lint_commands=[r"lint-all"]).status == "pass"


# --- chained commands -------------------------------------------------------------------

CHAIN = "ruff check . && ruff format --check ."
CHAIN_OUTPUT = "All checks passed!\nWould reformat: src/a.py\n1 file would be reformatted"


def test_chain_attributes_output_to_each_tool() -> None:
    s = session(edit("a.py"), cmd(CHAIN, exit_code=1, output=CHAIN_OUTPUT), say("Lint is clean."))
    assert run(s).status == "pass"
    s = session(
        edit("a.py"), cmd(CHAIN, exit_code=1, output=CHAIN_OUTPUT), say("Code is formatted.")
    )
    assert run(s).status == "fail"


def test_chain_without_attributable_output_is_unverified() -> None:
    s = session(
        edit("a.py"), cmd("eslint . && tsc", exit_code=2, output="x"), say("Lint is clean.")
    )
    result = run(s)
    assert result.status == "unverified"
    assert "not attributable" in result.summary


def test_chain_with_quiet_commands_keeps_the_exit_code() -> None:
    s = session(edit("a.py"), cmd("cd pkg && pytest -q", exit_code=1), say("Tests pass."))
    assert run(s).status == "fail"


# --- quoted rules and prescriptions -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'AGENTS.md says "make sure the tests pass" before finishing.',
        "The rule \u201ctests pass before you commit\u201d applies here.",
        "> All tests pass and lint is clean.",
        "Make sure the tests pass.",
        "Ensure lint is clean before pushing.",
        "The tests must pass on every commit.",
    ],
)
def test_quoted_or_prescriptive_text_is_not_a_claim(text: str) -> None:
    assert find_claims(text, ["tests", "lint", "push"]) == {}


# --- missed and partial claims ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Everything works and the fix is verified.",
        "The fix has been verified.",
        "I verified that the change works.",
    ],
)
def test_general_success_reports_are_claims(text: str) -> None:
    assert "works" in find_claims(text, ["works"])


def test_everything_works_without_tests_fails() -> None:
    s = session(edit("a.py"), say("Everything works and the fix is verified."))
    result = run(s)
    assert result.status == "fail"
    assert "no test command ran" in result.summary
    assert run(session(edit("a.py"), cmd("pytest"), say("Everything works."))).status == "pass"


def test_subset_run_is_partial_verification() -> None:
    s = session(edit("a.py"), cmd("pytest tests/test_app.py"), say("All 120 tests pass."))
    result = run(s)
    assert result.status == "unverified"
    assert "backs only part of the claim" in result.summary


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("pytest -k parser", ""),
        ("pytest tests/test_app.py::test_slug", ""),
        ("python -m unittest tests.test_app", ""),
        ("pytest -q", "12 passed in 0.40s"),
    ],
)
def test_more_partial_runs(command: str, output: str) -> None:
    s = session(edit("a.py"), cmd(command, output=output), say("All 120 tests pass."))
    assert run(s).status == "unverified"


@pytest.mark.parametrize(
    ("command", "output", "claim"),
    [
        ("pytest -q", "120 passed in 3.10s", "All 120 tests pass."),
        ("pytest tests/test_new.py", "3 passed in 0.10s", "All 3 new tests pass."),
        ("python -m unittest discover -s tests", "", "The full suite passes."),
    ],
)
def test_full_runs_back_full_claims(command: str, output: str, claim: str) -> None:
    s = session(edit("a.py"), cmd(command, output=output), say(claim))
    assert run(s).status == "pass"
