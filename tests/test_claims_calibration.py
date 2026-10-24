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


@pytest.mark.parametrize(
    "command",
    [
        "npx jest -t parser",
        "vitest run -t parser",
        "npm test -- -t parser",
        "npm run test -- -t parser",
        "yarn test -t parser",
    ],
)
def test_name_filters_still_select_tests(command: str) -> None:
    output = "Tests: 3 passed, 3 total"
    for claim in ("All tests pass.", "The full test suite passes."):
        s = session(edit("a.ts"), cmd(command, output=output), say(claim))
        result = run(s)
        assert result.status == "unverified", (command, claim)
        assert "selects tests" in result.summary


# Built-in evidence starts at the tool; a project's own test command (`test_commands`) is the
# whole simple command, runner prefixes and coverage wrappers included.
UNITTEST_COMMANDS = [r"\bunittest\b"]
MOCHA_COMMANDS = [r"\bmocha\b"]
COVERAGE_COMMANDS = [r"\bcoverage\s+run\b"]


@pytest.mark.parametrize("test_commands", [[], UNITTEST_COMMANDS])
@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m unittest discover -t .",
        "uv run --with coverage python -m unittest discover -s tests -t .",
        "poetry run python -m unittest discover -s tests -t .",
        "pipenv run python3 -m unittest discover -t . -p test_*.py",
        "pdm run python -X utf8 -m unittest discover -s tests -t .",
        "hatch run python -m unittest discover -t .",
        "env PYTHONPATH=src python -m unittest discover -t .",
        r".venv\Scripts\python.exe -m unittest discover -s tests -t .",
        "py -3 -m unittest discover -t .",
    ],
)
def test_unittest_top_level_dir_behind_runner_prefixes(
    command: str, test_commands: list[str]
) -> None:
    output = "Ran 29 tests in 0.1s\n\nOK"
    s = session(edit("a.py"), cmd(command, output=output), say("All 29 tests pass."))
    result = run(s, test_commands=test_commands)
    assert result.status == "pass", (command, result.summary)


@pytest.mark.parametrize("test_commands", [[], MOCHA_COMMANDS])
@pytest.mark.parametrize(
    "command",
    [
        "npx mocha -t 5000",
        "pnpm exec mocha -t 5000",
        "yarn exec mocha -t 5000",
        "npm exec -- mocha -t 5000",
        "bunx mocha -t 5000",
        "npx --yes mocha -t 5000",
    ],
)
def test_mocha_timeout_flag_does_not_select_tests(command: str, test_commands: list[str]) -> None:
    s = session(edit("a.js"), cmd(command), say("All tests pass."))
    assert run(s, test_commands=test_commands).status == "pass", command


@pytest.mark.parametrize(
    ("command", "test_commands"),
    [
        ("coverage run -m unittest discover -s tests -t .", COVERAGE_COMMANDS),
        ("uv run coverage run -m unittest discover -t . -p test_*.py", UNITTEST_COMMANDS),
        ("python -m coverage run -m unittest discover -t .", COVERAGE_COMMANDS),
        ("npx nyc mocha -t 5000", MOCHA_COMMANDS),
        ("c8 mocha -t 5000", [r"\bc8\b"]),
    ],
)
def test_t_behind_coverage_wrappers_in_project_commands(
    command: str, test_commands: list[str]
) -> None:
    output = "Ran 29 tests in 0.1s\n\nOK"
    s = session(edit("a.py"), cmd(command, output=output), say("All 29 tests pass."))
    result = run(s, test_commands=test_commands)
    assert result.status == "pass", (command, result.summary)


@pytest.mark.parametrize(
    ("command", "test_commands"),
    [
        ("pnpm exec jest -t parser", []),
        ("npx vitest run -t parser", []),
        ("uv run pytest -t parser", []),
        ("poetry run python -m pytest -t x", []),
        ("npx nyc jest -t parser", [r"\bjest\b"]),
        ("uv run coverage run -m pytest -t x", [r"\bpytest\b"]),
    ],
)
def test_t_behind_runner_prefixes_still_selects_tests(
    command: str, test_commands: list[str]
) -> None:
    s = session(edit("a.ts"), cmd(command), say("All tests pass."))
    result = run(s, test_commands=test_commands)
    assert result.status == "unverified", command
    assert "selects tests" in result.summary


@pytest.mark.parametrize(
    ("command", "test_commands"),
    [
        ("uv run python -m unittest tests.test_app", []),
        ("poetry run python -m unittest tests.test_app -v", UNITTEST_COMMANDS),
        (r".venv\Scripts\python.exe -m unittest tests.test_app", []),
        ("coverage run -m unittest tests.test_app", COVERAGE_COMMANDS),
    ],
)
def test_named_unittest_modules_behind_runner_prefixes(
    command: str, test_commands: list[str]
) -> None:
    s = session(edit("a.py"), cmd(command), say("All 120 tests pass."))
    result = run(s, test_commands=test_commands)
    assert result.status == "unverified", command
    assert "named test modules" in result.summary


# --- hedges in a clause joined by "and" ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Docs must be updated later and all tests pass.",
        "The config has to be set and the full suite passed.",
        "I added a check to ensure the tests directory exists and all the unit tests passed.",
        "I must refactor X later and the tests pass.",
        "The parser is untested and all 12 tests pass.",
    ],
)
def test_hedge_before_and_does_not_cancel_the_test_clause(text: str) -> None:
    assert "tests" in find_claims(text, ["tests"])


def test_joined_test_subjects_with_a_failure_are_not_a_claim() -> None:
    assert find_claims("The old tests and 2 new ones pass but 1 fails.", ["tests"]) == {}


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


@pytest.mark.parametrize(
    "stash", ["git stash -m wip", "git stash --message wip", "git stash -q -- app.py"]
)
def test_implicit_stash_push_with_options_is_a_stash(stash: str) -> None:
    s = session(
        edit("app.py"),
        cmd(f"{stash} && pytest -q; git stash pop", output="12 passed in 0.10s"),
        say("All tests pass."),
    )
    result = run(s)
    assert result.status == "fail"
    assert "no test command ran" in result.summary


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


def test_popd_returns_to_the_directory_before_pushd() -> None:
    assert shell_writes("pushd /tmp; popd; echo > f.py") == ["f.py"]
    assert shell_writes("cd sub && pushd /tmp && popd && echo > f.py") == ["sub/f.py"]
    assert shell_writes("pushd a; pushd b; popd; echo > f.py") == ["a/f.py"]
    assert shell_writes("Push-Location C:/x; Pop-Location; echo > f.py", True) == ["f.py"]
    assert shell_writes("popd; echo > f.py") == [None]


def test_cd_inside_a_brace_group_changes_directory() -> None:
    assert shell_writes("{ cd /tmp; }; echo > f.py") == ["/tmp/f.py"]
    assert shell_writes("(cd /tmp); echo > f.py") == ["f.py"]


def test_write_after_popd_resets_the_evidence() -> None:
    s = session(
        edit("app.py"),
        cmd("pytest -q", output="12 passed in 0.10s"),
        cmd("pushd /tmp; popd; echo x > app.py"),
        say("All tests pass."),
    )
    result = run(s)
    assert result.status == "fail"
    assert "no test command ran" in result.summary


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
    assert shell_writes("git apply --stat --apply fix.diff") == [None]


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


# --- pre-merge review regressions ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "and" joins only a second clause whose subject is tests, or that says "all pass".
        ("There are no tests and the build passed.", {"build"}),
        ("I added 5 tests and ruff check passed.", {"lint"}),
        ("I updated the tests and the snapshot passed review.", set()),
        ("None of the tests and checks pass.", set()),
        ("I didn't touch the tests and the lint passed.", {"lint"}),
        ("The randomized check, the unit tests and the audit script all pass.", {"tests"}),
    ],
)
def test_and_joins_only_test_subjects(text: str, expected: set[str]) -> None:
    assert set(find_claims(text, ["tests", "lint", "types", "build"])) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Make sure your changes pass the tests.",
        "Before merging, make sure your branch passes the tests.",
        "Ensure the new parser passes all tests.",
        "Make sure you pass the tests.",
    ],
)
def test_prescriptions_with_a_subject_are_not_claims(text: str) -> None:
    assert find_claims(text, ["tests"]) == {}


def test_prescription_after_done_is_not_a_failed_claim() -> None:
    s = session(
        edit("src/a.py"), say("Done. Make sure your changes pass the tests before merging.")
    )
    assert run(s, claims=["tests"]).status == "pass"


def test_no_tests_and_build_passed_is_not_a_tests_claim() -> None:
    s = session(
        edit("src/a.py"),
        cmd("npm run build"),
        say("There are no tests and the build passed."),
    )
    assert run(s, claims=["tests", "build"]).status == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "(make && cd /tmp) && echo x > a.py",
        "(true; cd /tmp); echo x > a.py",
        "true | cd /tmp; echo > a.py",
        "cd /tmp &\necho > a.py",
    ],
)
def test_cd_in_a_child_shell_does_not_move_later_writes(command: str) -> None:
    assert shell_writes(command) == ["a.py"]


def test_cd_in_the_current_shell_still_moves_later_writes() -> None:
    assert shell_writes("cd /tmp || exit 1; echo > a.py") == ["/tmp/a.py"]
    assert shell_writes("cd /tmp && echo > a.py") == ["/tmp/a.py"]


def test_git_global_options_before_stash() -> None:
    s = session(
        edit("app.py"),
        cmd("git -C . stash && pytest -q", exit_code=1, output="1 failed in 0.1s"),
        say("All tests pass."),
    )
    r = run(s, claims=["tests"])
    assert r.status == "fail"
    assert "stashed" in r.evidence[-1].message
