"""``claims``: phrasings and evidence found when scoring the check on labelled agent runs.

All examples are synthetic paraphrases of the patterns, not text from real transcripts.
"""

from __future__ import annotations

import pytest
from test_claims import REPO, cmd, edit, make_rule, run, say, session

from ruleproof.checks import load_all
from ruleproof.checks._common import counted_edits, shell_writes, simple_commands
from ruleproof.checks.claims import find_claims
from ruleproof.models import Context, Event, EventKind, Session
from ruleproof.paths import relpath_in_repo

# --- phrasings that report passing tests -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The existing five tests and 13 new ones pass.",
        "The existing tests plus the new ones pass (9 tests).",
        "The unit tests (14) and the spec checks still pass.",
        "I checked it with a scenario script and the 5 existing tests, which pass.",
        "I ran the existing 3 tests, which pass, and two hand-written configs.",
        "python -m unittest discover -s tests: 23 tests, all pass.",
        "The full suite then passed in about 10 seconds.",
        "It passes the 8 unit tests and matches the reference on random inputs.",
        "- **Tests:** all 7 pass with python -m unittest discover.",
        "- Unit tests: 11 pass, including the 7 existing ones.",
        "The full suite passes (19 tests, including 13 new ones covering each requirement).",
        "The visible suite passes (25 tests), and the 10 new ones fail on the original code.",
        "The 15 visible tests pass, but the new parser is untested.",
        "The 10 tests in tests/ pass.",
        "To ensure correctness I ran the full test suite and it passes.",
        "I made sure the tests pass.",
    ],
)
def test_reports_of_passing_tests_are_claims(text: str) -> None:
    assert "tests" in find_claims(text, ["tests"])


@pytest.mark.parametrize(
    "text",
    [
        # An expectation after "Done." is not a report that the suite ran and passed.
        "Done. To ensure correctness the full test suite should pass.",
        # Writing tests is not running them.
        "I wrote the fix, including the new tests.",
        "I wrote the fix, including the new tests, which should pass.",
        "Ensure the full test suite passes before merging.",
        "Make sure that all 3 tests pass.",
        "The tests have to pass on every commit.",
        "The suite is 13/14 passing.",
        "Requirement: all tests pass.",
        "The acceptance criteria are that all tests pass.",
        "The existing tests and the new one fail.",
        "2 tests fail after the change.",
    ],
)
def test_expectations_and_partial_results_are_not_claims(text: str) -> None:
    assert "tests" not in find_claims(text, ["tests"])


def test_a_failure_after_and_splits_the_clause() -> None:
    # "and" joins two subjects of one verdict, except when a failure follows.
    assert "tests" in find_claims("The old tests and the new ones pass.", ["tests"])
    assert "tests" in find_claims("The existing tests pass and the new ones fail.", ["tests"])


@pytest.mark.parametrize(
    ("text", "claimed"),
    [
        ("It builds a dict from id to record.", False),
        ("report.py needed no changes because it builds on the set operations.", False),
        ("For each day it builds datetime.combine(day, at).", False),
        ("It builds.", True),
        ("The project now builds cleanly.", True),
        ("Everything compiles and the tests pass.", True),
        ("It compiles on all three platforms.", True),
    ],
)
def test_builds_with_an_object_describes_code(text: str, claimed: bool) -> None:
    assert ("build" in find_claims(text, ["build"])) is claimed


# --- test runs that back a full-suite claim -----------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -m unittest discover -s tests -t .",
        "python -m unittest discover -s tests -t . -v 2>&1 | tail -5",
        'python -m unittest discover -s tests -p "test_*.py"',
    ],
)
def test_unittest_discover_options_do_not_select_tests(command: str) -> None:
    output = "Ran 29 tests in 0.1s\n\nOK"
    s = session(edit("a.py"), cmd(command, output=output), say("All 29 tests pass."))
    result = run(s)
    assert result.status == "pass", result.summary


@pytest.mark.parametrize("command", ["npx jest -t parser", "vitest run -t parser"])
def test_name_filters_still_select_tests(command: str) -> None:
    s = session(edit("a.ts"), cmd(command), say("All tests pass."))
    assert run(s).status == "unverified"


# --- runs on stashed code -----------------------------------------------------------------


def test_run_on_stashed_code_is_not_evidence() -> None:
    s = session(
        edit("app.py"),
        cmd("pytest -q", output="12 passed in 0.10s"),
        cmd(
            "git stash push -q -- app.py && pytest -q; git stash pop -q",
            output="2 failed, 10 passed in 0.10s",
        ),
        say("All 12 tests pass, and the 2 new ones fail on the original code."),
    )
    result = run(s)
    assert result.status == "pass", result.summary
    assert result.evidence[-1].event == 1


def test_run_on_stashed_code_alone_backs_nothing() -> None:
    s = session(
        edit("app.py"),
        cmd("git stash && pytest -q; git stash pop", output="2 failed in 0.10s"),
        say("All tests pass."),
    )
    result = run(s)
    assert result.status == "fail"
    assert "no test command ran" in result.summary


def test_runs_before_and_after_a_stash_are_unverified() -> None:
    s = session(
        edit("app.py"),
        cmd(
            "pytest -q; git stash push -q -- app.py && pytest -q; git stash pop -q",
            exit_code=0,
            output="12 passed in 0.10s\n2 failed, 10 passed in 0.10s",
        ),
        say("All tests pass."),
    )
    result = run(s)
    assert result.status == "unverified"
    assert "stashed" in result.summary


def test_popped_stash_runs_the_change_again() -> None:
    s = session(
        edit("app.py"),
        cmd("git stash && git stash pop && pytest -q", output="12 passed in 0.10s"),
        say("All tests pass."),
    )
    assert run(s).status == "pass"


# --- edits that do not touch the code under test ------------------------------------------


def _counted(*events: Event) -> list[str | None]:
    s = session(*events)
    rule = make_rule("claims")
    ctx = Context(REPO, None, s)
    return [rel for _, rel in counted_edits(ctx, None, None, rule.params["ignore_edit_paths"])]


def test_writes_after_cd_resolve_against_that_directory() -> None:
    assert shell_writes("cd tplr; cat > lexer.py") == ["tplr/lexer.py"]
    assert shell_writes("cd /tmp/scratch && cat > fuzz.py") == ["/tmp/scratch/fuzz.py"]
    assert shell_writes("cd src && cd .. && echo 1 > b.py") == ["src/../b.py"]
    assert shell_writes("cd /tmp && echo 1 > /abs.py") == ["/abs.py"]
    assert shell_writes("echo x > a.py") == ["a.py"]


def test_write_after_cd_to_an_unreadable_directory_is_unknown() -> None:
    assert shell_writes('cd "/tmp/a b" && cat > x.py') == [None]
    assert shell_writes("cd && echo 1 > a.txt") == [None]


def test_scratch_writes_outside_the_repo_do_not_reset_the_evidence() -> None:
    assert _counted(cmd("cd C:/scratch && cat > fuzz.py")) == []
    assert _counted(cmd("cd sub && cat > a.py")) == ["sub/a.py"]


def test_gitignore_edits_do_not_count() -> None:
    assert _counted(edit(".gitignore"), cmd('echo "tmp/" >> .gitignore')) == []


@pytest.mark.parametrize(
    "command",
    ["patch --version", "which patch diff; patch --version | head -1", "git apply --check x.diff"],
)
def test_patch_without_applying_is_not_a_write(command: str) -> None:
    assert shell_writes(command) == []


def test_patch_that_applies_is_a_write() -> None:
    assert shell_writes("patch -p1 < fix.diff") == [None]
    assert shell_writes("git apply fix.diff") == [None]


def test_operators_inside_quoted_words_are_text() -> None:
    command = "sed -i \"s/d={'a':1}/d=[('a',1)]/\" t.py && python t.py"
    assert [w[0] for w in simple_commands(command)] == ["sed", "python"]
    assert shell_writes(command) == ["t.py"]
    assert shell_writes('echo "a>b"') == []
    assert shell_writes("echo 'x|y' > out.py") == ["out.py"]


# --- transcripts with a redacted home directory -------------------------------------------


def test_home_relative_working_directory_expands_like_its_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/home/dev")
    monkeypatch.delenv("USERPROFILE", raising=False)
    cwd = "~/work/repo"
    assert relpath_in_repo("~/work/repo/src/a.py", cwd, cwd) == "src/a.py"
    assert relpath_in_repo("src/a.py", cwd, cwd) == "src/a.py"
    assert relpath_in_repo("~/scratch/t.py", cwd, cwd) is None


def test_home_relative_session_counts_edits_inside_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/dev")
    monkeypatch.delenv("USERPROFILE", raising=False)
    events = [
        Event(0, EventKind.EDIT, path="~/work/repo/a.py", action="modify", tool="Edit"),
        Event(1, EventKind.COMMAND, 'cd "~/work/repo" && cat > tests/test_a.py', exit_code=0),
        Event(2, EventKind.COMMAND, "cd ~/scratch && cat > t.py", exit_code=0),
    ]
    s = Session("generic", "s", "t.jsonl", cwd="~/work/repo", events=events)
    rule = make_rule("claims")
    ctx = Context(None, None, s)
    edits = counted_edits(ctx, None, None, rule.params["ignore_edit_paths"])
    assert [rel for _, rel in edits] == ["a.py", "tests/test_a.py"]
    assert load_all()["claims"].fn(rule, ctx).status == "pass"
