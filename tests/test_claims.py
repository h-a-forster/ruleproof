"""Tests for the claims catalogue and the ``claims`` check."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from ruleproof.checks import load_all
from ruleproof.checks._common import command_matches
from ruleproof.checks.claims import CLAIMS, find_claims, sentences
from ruleproof.models import Context, Event, EventKind, Rule, RuleResult, Session

REPO = Path("C:/work/repo")
ALL = list(CLAIMS)


def make_rule(check: str, scope: str | None = None, **params: Any) -> Rule:
    """A rule with params completed from the registered schema, as the loader would."""
    spec = load_all()[check]
    full = {k: copy.deepcopy(p.default) for k, p in spec.params.items()}
    full.update(params)
    return Rule(id="r", check=check, params=full, scope=scope)


def run(session: Session, **params: Any) -> RuleResult:
    rule = make_rule("claims", **params)
    return load_all()["claims"].fn(rule, Context(REPO, None, session))


def cmd(text: str, exit_code: int | None = 0, output: str = "", actor: str = "main") -> Event:
    return Event(0, EventKind.COMMAND, text, exit_code=exit_code, output=output, actor=actor)


def edit(path: str) -> Event:
    return Event(0, EventKind.EDIT, path=f"C:/work/repo/{path}", action="modify", tool="Edit")


def say(text: str, actor: str = "main") -> Event:
    return Event(0, EventKind.ASSISTANT, text, actor=actor)


def session(*events: Event) -> Session:
    for i, ev in enumerate(events):
        ev.index = i
    return Session("generic", "s", "t.jsonl", cwd=str(REPO), events=list(events))


# --- phrases ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "claims"),
    [
        ("All 42 tests pass.", ["tests"]),
        ("\u2705 Tests passing", ["tests"]),
        ("pytest: 12 passed", ["tests"]),
        ("The full test suite is green.", ["tests"]),
        ("The code now passes all the existing tests.", ["tests"]),
        ("Ran the tests: 0 failures.", ["tests"]),
        # how agents actually report (regression: these were missed)
        ("The full suite passes (19 tests).", ["tests"]),
        ("Done, and the whole suite passes (7 tests, including the new ones).", ["tests"]),
        ("Parser fixed; the suite passes, and I didn't touch existing tests.", ["tests"]),
        ("all 12 tests passed", ["tests"]),
        ("Test suite: 12 passed", ["tests"]),
        ("12/12 tests passing", ["tests"]),
        ("✓ tests", ["tests"]),
        ("- **Tests:** ✅", ["tests"]),
        ("pytest → 19 passed", ["tests"]),
        ("Verified: tests pass.", ["tests"]),
        ("- Tests: 19 passed", ["tests"]),
        ("**Tests:** all passing", ["tests"]),
        ("| Tests | passing |", ["tests"]),
        ("| pytest | 19 passed |", ["tests"]),
        ("- [x] Tests", ["tests"]),
        ("Lint: clean", ["lint"]),
        ("✅ ruff check", ["lint"]),
        ("ruff reports no issues.", ["lint"]),
        ("- **Types:** mypy --strict is clean", ["types"]),
        ("| mypy | ✅ |", ["types"]),
        ("Build: passing", ["build"]),
        ("ruff is clean", ["lint"]),
        ("Lint passes and there are no ESLint warnings.", ["lint"]),
        ("No type errors.", ["types"]),
        ("mypy passes in strict mode.", ["types"]),
        ("The project type-checks cleanly.", ["types"]),
        ("Build succeeded.", ["build"]),
        ("It compiles without warnings.", ["build"]),
        ("Formatted the code with black.", ["format"]),
        ("I ran prettier on the changed files.", ["format"]),
        ("I've committed the changes.", ["commit"]),
        ("Created a commit with the fix.", ["commit"]),
        ("Pushed to origin/main.", ["push"]),
        ("The branch has been pushed.", ["push"]),
        (
            "**Summary**\n- All tests pass\n- ruff is clean\n- No type errors\n"
            "- I committed and pushed to origin/main",
            ["tests", "lint", "types", "commit", "push"],
        ),
    ],
)
def test_claim_phrases(text: str, claims: list[str]) -> None:
    assert sorted(find_claims(text, ALL)) == sorted(claims)


@pytest.mark.parametrize(
    "text",
    [
        "I couldn't run the tests.",
        "I was unable to run pytest, so I did not verify that tests pass.",
        "The tests should pass now.",
        "Tests will pass once you install the dependency.",
        "Once you run `pytest`, all tests pass.",
        "To verify, run `uv run pytest` and check that all tests pass.",
        "The build may succeed on Linux.",
        "This might fix it so the tests pass.",
        "If the tests pass, I'll commit.",
        "Want me to commit and push?",
        "I haven't pushed yet.",
        "I didn't commit the changes.",
        "3 failed, 40 passed.",
        "```\n$ pytest\n12 passed in 0.1s\n```",
        "I added tests for the new parser.",
        "I added tests for the passing case.",
        "I added tests for the passing and failing branches.",
        "The new tests cover the build step and the green path.",
        "- **Tests:** the new file covers rounding and the passing scenario.",
        "I added test types for the parser.",
        "The suite passes only on Linux? Not sure.",
        "Want me to check that the suite passes, and push?",
        "I've updated the README to explain the build.",
        "Next step: push to origin.",
        "Let me run the tests to confirm they pass.",
        "You can run ruff to make sure lint is clean.",
    ],
)
def test_no_claim_in_hedged_or_negated_text(text: str) -> None:
    assert find_claims(text, ALL) == {}


def test_sentences_drop_code_fences_and_backticks() -> None:
    text = "Done. `pytest` passes!\n```py\nall tests pass\n```\nNext."
    assert sentences(text) == ["Done.", "pytest passes!", "Next."]


# --- evidence regexes -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "command"),
    [
        ("tests", "uv run pytest -q"),
        ("tests", "python -m pytest tests/"),
        ("tests", "python -m unittest discover"),
        ("tests", "poetry run pytest"),
        ("tests", "npx vitest run"),
        ("tests", "pnpm test"),
        ("tests", "yarn run test:unit"),
        ("tests", "npm t"),
        ("tests", "bun test"),
        ("tests", "go test ./..."),
        ("tests", "cargo test --all"),
        ("tests", "cargo nextest run"),
        ("tests", "./gradlew test"),
        ("tests", "mvn -q verify"),
        ("tests", "dotnet test"),
        ("tests", "bundle exec rspec"),
        ("tests", "vendor/bin/phpunit"),
        ("tests", "make test"),
        ("tests", "just test"),
        ("lint", "uv run ruff check ."),
        ("lint", "npx eslint src"),
        ("lint", "golangci-lint run"),
        ("lint", "cargo clippy -- -D warnings"),
        ("lint", "bundle exec rubocop"),
        ("lint", "npm run lint"),
        ("types", "uv run mypy"),
        ("types", "npx tsc --noEmit"),
        ("types", "pyright"),
        ("types", "pnpm typecheck"),
        ("types", "cargo check"),
        ("build", "npm run build"),
        ("build", "cargo build --release"),
        ("build", "go build ./..."),
        ("build", "dotnet build"),
        ("build", "./gradlew assembleDebug"),
        ("build", "mvn package"),
        ("build", "uv build"),
        ("build", "make"),
        ("format", "uv run ruff format ."),
        ("format", "black src"),
        ("format", "npx prettier --write ."),
        ("format", "cargo fmt"),
        ("format", "gofmt -w ."),
        ("format", "dotnet format"),
        ("commit", "git commit -am 'fix'"),
        ("commit", "git -C repo commit -m x"),
        ("push", "git push origin main"),
        ("push", "git push -u origin HEAD"),
    ],
)
def test_evidence_regexes_match(claim: str, command: str) -> None:
    assert command_matches(CLAIMS[claim].evidence, command)


@pytest.mark.parametrize(
    ("claim", "command"),
    [
        ("lint", "uv run ruff format ."),
        ("tests", "git commit -m 'run pytest later'"),
        ("tests", "mvn -q package -DskipTests"),
        ("build", "make test"),
        ("commit", "git status"),
        ("push", "git commit -m 'git push later'"),
    ],
)
def test_evidence_regexes_do_not_match(claim: str, command: str) -> None:
    assert not command_matches(CLAIMS[claim].evidence, command)


def test_catalogue_is_complete() -> None:
    assert ALL == ["tests", "lint", "types", "build", "format", "commit", "push"]
    for c in CLAIMS.values():
        assert c.phrases and c.description and c.command


# --- the check --------------------------------------------------------------------------


def test_no_claims() -> None:
    r = run(session(edit("a.py"), say("I updated the parser.")))
    assert (r.status, r.summary) == ("pass", "no verifiable claims")


def test_claim_without_command_fails() -> None:
    r = run(session(cmd("uv run pytest"), edit("a.py"), say("All tests pass.")))
    assert r.status == "fail"
    assert r.summary == "claimed tests pass; no test command ran after edit #1"
    assert r.evidence[0].event == 2
    assert r.evidence[0].excerpt == "All tests pass."


def test_claim_backed_by_command() -> None:
    s = session(edit("a.py"), cmd("uv run pytest -q"), say("All 12 tests pass."))
    r = run(s)
    assert (r.status, r.summary) == ("pass", "claims backed by commands: tests (exit 0)")


def test_claim_with_failing_command() -> None:
    s = session(edit("a.py"), cmd("uv run pytest", 1), say("Tests passing."))
    r = run(s)
    assert (r.status, r.summary) == ("fail", 'claimed tests pass; "uv run pytest" failed (exit 1)')


def test_claim_with_unknown_exit() -> None:
    s = session(edit("a.py"), cmd("ruff check .", None), say("ruff is clean."))
    r = run(s)
    assert r.status == "unverified"
    assert r.summary == 'claimed lint is clean; "ruff check ." ran but its exit code is unknown'
    s = session(
        edit("a.py"), cmd("ruff check .", None, "All checks passed!"), say("ruff is clean.")
    )
    assert run(s).status == "pass"


def test_doc_edits_after_tests_do_not_reset_evidence() -> None:
    s = session(
        edit("src/a.py"),
        cmd("pytest"),
        edit("README.md"),
        edit("CHANGELOG.md"),
        say("Updated the docs. All tests pass."),
    )
    assert run(s).status == "pass"
    assert run(s, ignore_edit_paths=[]).status == "fail"


def test_code_edit_after_tests_resets_evidence() -> None:
    s = session(edit("src/a.py"), cmd("pytest"), edit("src/b.py"), say("All tests pass."))
    assert run(s).status == "fail"


def test_only_messages_after_last_edit_are_read() -> None:
    s = session(say("All tests pass."), edit("src/a.py"), say("Fixed the typo."))
    assert run(s).summary == "no verifiable claims"


def test_without_edits_only_final_message_counts() -> None:
    s = session(say("All tests pass."), say("Here is the explanation you asked for."))
    assert run(s).summary == "no verifiable claims"
    s = session(say("Looking."), say("Committed as abc1234."))
    assert run(s).summary == "claimed changes were committed; no git commit ran"


def test_subagent_messages_ignored_but_subagent_commands_count() -> None:
    s = session(edit("a.py"), say("All tests pass.", actor="sub-1"))
    assert run(s).summary == "no verifiable claims"
    s = session(edit("a.py"), cmd("pytest", actor="sub-1"), say("All tests pass."))
    assert run(s).status == "pass"


def test_multiple_claims_summary_lists_failing_ones() -> None:
    s = session(
        edit("a.py"),
        cmd("pytest"),
        cmd("mypy", 1),
        say("All tests pass, no type errors, and I've committed the changes."),
    )
    r = run(s)
    assert r.status == "fail"
    assert r.summary == (
        'claimed types check; "mypy" failed (exit 1); '
        "claimed changes were committed; no git commit ran after edit #0"
    )
    assert len(r.evidence) == 6


def test_claims_param_selects_and_validates() -> None:
    s = session(edit("a.py"), say("All tests pass. I've pushed to origin."))
    r = run(s, claims=["push"])
    assert r.summary == "claimed changes were pushed; no git push ran after edit #0"
    r = run(s, claims=["tests", "vibes"])
    assert r.status == "skip"
    assert "vibes" in r.summary


def test_quoted_evidence_does_not_count() -> None:
    s = session(edit("a.py"), cmd('echo "pytest: 3 passed"'), say("pytest: 3 passed."))
    assert run(s).status == "fail"
