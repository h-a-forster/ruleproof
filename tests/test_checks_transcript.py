"""Tests for the transcript checks and the shared command-matching helpers."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from ruleproof.checks import load_all
from ruleproof.checks._common import (
    command_matches,
    command_texts,
    invokes,
    output_verdict,
    shell_writes,
)
from ruleproof.models import (
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


def run(rule: Rule, session: Session, diff: Diff | None = None) -> RuleResult:
    return load_all()[rule.check].fn(rule, Context(REPO, diff, session))


def cmd(
    text: str,
    exit_code: int | None = 0,
    output: str = "",
    is_error: bool | None = None,
    actor: str = "main",
) -> Event:
    return Event(
        0, EventKind.COMMAND, text, tool="Bash", exit_code=exit_code, output=output,
        is_error=is_error, actor=actor,
    )  # fmt: skip


def edit(
    path: str, action: EditAction = "modify", tool: str = "Edit", actor: str = "main"
) -> Event:
    return Event(0, EventKind.EDIT, path=path, action=action, tool=tool, actor=actor)


def say(text: str, actor: str = "main") -> Event:
    return Event(0, EventKind.ASSISTANT, text, actor=actor)


def tool(name: str, text: str = "") -> Event:
    return Event(0, EventKind.TOOL, text, tool=name)


def session(*events: Event, cwd: str = "C:/work/repo") -> Session:
    for i, ev in enumerate(events):
        ev.index = i
    return Session("generic", "s", "t.jsonl", cwd=cwd, events=list(events))


# --- command matching -------------------------------------------------------------------

PUSH = re.compile(r"git\s+push")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push", True),
        ("git push origin main", True),
        ('git commit -m "x" && git push', True),
        ('git "push"', True),  # a short quoted word is still an argument
        ('git commit -m "never git push"', False),
        ("git commit -m 'git push later'", False),
        ("git commit -m 'it''s fine; git push'", False),  # PowerShell '' escape
        ('git commit -m "it\'s fine; git push"', False),
        ('git commit -m "say \\"git push\\" ok"', False),  # POSIX escaped quote
        ("git commit -m \"$(cat <<'EOF'\nDon't git push\nEOF\n)\" && git status", False),
        ("git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\" && git push", True),
        ("cat <<EOF > notes.txt\ngit push\nEOF\ngit status", False),
        ("cat <<-EOF > notes.txt\n\tgit push\n\tEOF\ngit push", True),
        ("$m = @'\ngit push\n'@\ngit commit -m $m", False),  # PowerShell here-string
        ('$m = @"\nit\'s git push\n"@\ngit status', False),
        ('bash -lc "git push origin main"', True),
        ("bash -c 'echo \"git push\"'", False),  # payload is masked again
        ("/usr/bin/bash -c 'git push'", True),
        ("sh -e -c 'git push'", True),
        ('pwsh -NoProfile -Command "git push"', True),
        ('powershell.exe -ExecutionPolicy Bypass -Command "git push"', True),
        ("cmd /c git push", True),
        ('cmd.exe /C "git push"', True),
        ('grep -c "git push" log.txt', False),  # -c of grep is not a shell
        ("echo \"bash -c 'git push'\"", False),
        ('echo "unterminated git push', True),  # an unterminated quote is literal text
        ("echo don't && git push", True),
        ("echo it's && git push && echo 'x y'", True),
        ("git commit -m 'don\\'t' && git push", True),
        ("echo $'don\\'t' && git push", True),  # ANSI-C quoting
        ("# Let's push the branch now\ngit push origin main", True),
        ("cd repo  # don't forget\ngit push", True),
        ('git commit -m "a b" # it\'s && git push', False),  # comment
        ("echo $((1<<y))\ngit push", True),  # arithmetic, not a heredoc
        ("git commit -F- <<EOF\nmsg\nEOF && git push", True),
        ("bash -euo pipefail -c 'git push x'", True),
        ("bash -ce 'git push x'", True),
        ("bash -xc 'git push'", True),
        ("bash -c -- 'git push'", True),
        ("bash --login -c 'git push'", True),
        ("bash -o pipefail -c 'git push x'", True),
        ("sudo -u me bash -c 'git push x'", True),
        ("pwsh -NoProfile -c 'git push'", True),
        ('echo "`git push origin`"', True),  # POSIX backticks run even inside quotes
        ('eval "git push origin"', True),
        ("Invoke-Expression 'git push origin'", True),
        ('ssh host "git push"', True),
        ('echo "git push" | bash', True),
        ("python -c \"import os; os.system('git push')\"", True),
        ("bash -c \"git commit -m 'never git push'\"", False),
        ("echo $(git push)", True),
        ('echo "$(git push)"', True),  # command substitution runs even inside quotes
    ],
)
def test_command_matches_ignores_quoted_text(command: str, expected: bool) -> None:
    assert command_matches(PUSH, command) is expected


def test_match_quoted_uses_raw_command() -> None:
    assert command_matches(PUSH, 'git commit -m "never git push"', match_quoted=True)
    assert command_texts("a 'b c'", match_quoted=True) == ["a 'b c'"]


def test_command_texts_short_quoted_words_unquoted() -> None:
    assert command_texts("rm -rf \"/\" 'x'") == ["rm -rf / x"]


# --- output_verdict ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "verdict"),
    [
        ("", None),
        ("hello world", None),
        ("===== 12 passed in 0.31s =====", True),
        ("12 passed, 1 skipped in 0.31s", True),
        ("===== 1 failed, 11 passed in 0.31s =====", False),
        ("===== 2 errors in 0.31s =====", False),
        ("collected 0 items\n===== no tests ran in 0.01s =====", False),
        ("0 passed in 0.01s", None),
        ("3 passed in 0.1s\nFAIL Required test coverage of 80% not reached.", False),
        ("tests/test_a.py::test_x FAILED\n3 passed in 0.1s", False),
        ("FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed in 0.1s", False),
        ("Would reformat: a.py\n1 file would be reformatted", False),
        ("Finished `dev` profile [unoptimized] target(s) in 1.0s", True),
        ("Ran 5 tests in 0.002s\n\nOK", True),
        ("Ran 5 tests in 0.002s\n\nOK (skipped=1)", True),
        ("Ran 5 tests in 0.002s\n\nFAILED (failures=1)", False),
        ("Tests:       1 failed, 4 passed, 5 total", False),
        ("Tests:       5 passed, 5 total", True),
        (" Test Files  1 passed (1)\n      Tests  3 passed (3)", True),
        (" Test Files  1 failed (1)\n      Tests  1 failed | 2 passed (3)", False),
        ("ok  \texample.com/pkg\t0.012s", True),
        ("ok  \texample.com/pkg\t(cached)", True),
        ("--- FAIL: TestX (0.00s)\nFAIL\nFAIL\texample.com/pkg\t0.01s", False),
        ("test result: ok. 3 passed; 0 failed", True),
        ("test result: FAILED. 2 passed; 1 failed", False),
        ("error[E0308]: mismatched types", False),
        ("All checks passed!", True),
        ("Found 3 errors.", False),
        ("Found 2 errors (2 fixed, 0 remaining).", True),
        ("Found 3 errors (1 fixed, 2 remaining).", False),
        ("Success: no issues found in 12 source files", True),
        ("Found 2 errors in 1 file (checked 12 source files)", False),
        ("src/a.ts(3,1): error TS2322: Type 'x' is not assignable", False),
        ("\u2716 3 problems (2 errors, 1 warning)", False),
        ("\u2716 1 problem (0 errors, 1 warning)", None),
        ("BUILD SUCCESSFUL in 3s", True),
        ("BUILD FAILED in 3s", False),
        ("[INFO] BUILD FAILURE", False),
        ("Build succeeded.\n    0 Warning(s)", True),
        ("Passed!  - Failed:     0, Passed:    12", True),
        ("Failed!  - Failed:     1, Passed:    11", False),
        ("npm ERR! Test failed.", False),
        ("ok then, all good", None),
        ("retrying after 1 error\n===== 3 passed in 1.0s =====", True),
    ],
)
def test_output_verdict(output: str, verdict: bool | None) -> None:
    assert output_verdict(output) is verdict


# --- forbid-command ---------------------------------------------------------------------


def test_forbid_command() -> None:
    s = session(
        cmd('git commit -m "do not git push yet"'),
        cmd("git push origin main", actor="sub-1"),
        cmd("GIT PUSH"),
    )
    r = run(make_rule("forbid-command", command=r"git\s+push"), s)
    assert (r.status, r.summary) == ("fail", 'ran "git push origin main"')
    assert r.evidence[0].event == 1
    assert "subagent sub-1" in r.evidence[0].message
    r = run(make_rule("forbid-command", command=r"git\s+push", ignore_case=True), s)
    assert r.summary == '2 commands match "git\\s+push"'
    r = run(make_rule("forbid-command", command=r"git\s+push", match_quoted=True), s)
    assert [e.event for e in r.evidence] == [0, 1]


def test_forbid_command_pass() -> None:
    r = run(make_rule("forbid-command", command="rm -rf"), session(cmd("ls")))
    assert (r.status, r.summary) == ("pass", 'no command matches "rm -rf"')


# --- require-command --------------------------------------------------------------------

E = "C:/work/repo/src/app.py"


@pytest.mark.parametrize(
    ("events", "status", "summary"),
    [
        (
            [edit(E), cmd("uv run pytest")],
            "pass",
            'ran "uv run pytest" after the last edit (#0) (exit 0)',
        ),
        (
            [edit(E), cmd("uv run pytest", 1)],
            "fail",
            'ran "uv run pytest" after the last edit (#0) but it failed (exit 1)',
        ),
        (
            [edit(E), cmd("uv run pytest", None, is_error=True)],
            "fail",
            'ran "uv run pytest" after the last edit (#0) but it failed (reported as failed)',
        ),
        (
            [edit(E), cmd("uv run pytest", None)],
            "unverified",
            'ran "uv run pytest" after the last edit (#0) (exit code unknown)',
        ),
        (
            [edit(E), cmd("uv run pytest", None, output="==== 3 passed in 0.1s ====")],
            "pass",
            'ran "uv run pytest" after the last edit (#0) '
            "(exit code unknown, output shows success)",
        ),
        (
            [edit(E), cmd("uv run pytest", None, output="==== 1 failed in 0.1s ====")],
            "fail",
            'ran "uv run pytest" after the last edit (#0) but it failed '
            "(exit code unknown, output shows failures)",
        ),
        (
            [cmd("uv run pytest"), edit(E)],
            "fail",
            '"uv run pytest" ran before the last edit, not after',
        ),
        ([edit(E), cmd("ls")], "fail", 'no command matching "pytest" ran after the last edit (#0)'),
        ([cmd("ls")], "pass", "not required: no edits"),
        ([cmd("pytest", 1)], "pass", "not required: no edits"),
        # the last run decides
        ([edit(E), cmd("pytest", 1), cmd("pytest", 0)], "pass", None),
        ([edit(E), cmd("pytest", 0), cmd("pytest -x", 2)], "fail", None),
        # edits outside the repo never count
        ([cmd("pytest"), edit("C:/Users/me/.config/x.toml")], "pass", None),
        ([cmd("pytest"), edit("/c/work/repo/src/app.py")], "fail", None),  # MSYS path
        ([cmd("pytest"), edit(r"C:\WORK\Repo\src\app.py")], "fail", None),  # Windows case
        ([cmd("pytest"), edit("src/app.py")], "fail", None),  # relative to cwd
        # quoted mentions are not runs
        ([edit(E), cmd('git commit -m "ran pytest twice"')], "fail", None),
        ([edit(E), cmd('bash -lc "uv run pytest -q"')], "pass", None),
    ],
)
def test_require_command(events: list[Event], status: str, summary: str | None) -> None:
    r = run(make_rule("require-command", command="pytest"), session(*events))
    assert r.status == status, r.summary
    if summary is not None:
        assert r.summary == summary


def test_require_command_without_edits_but_with_diff_changes() -> None:
    d = Diff("HEAD", [FileChange("src/x.py", "modified")])
    rule = make_rule("require-command", command="pytest")
    assert run(rule, session(cmd("ls")), d).summary == 'no command matching "pytest" ran'
    assert run(rule, session(cmd("pytest")), d).summary == 'ran "pytest" (exit 0)'


def test_require_command_before_edit_evidence() -> None:
    s = session(cmd("pytest"), edit(E))
    r = run(make_rule("require-command", command="pytest"), s)
    assert r.evidence[0].message == "last run was before edit #1"
    assert r.evidence[0].event == 0
    assert (r.evidence[1].path, r.evidence[1].event) == ("src/app.py", 1)


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"after_last_edit": False}, "pass"),
        ({"ignore_edit_paths": ["*.md"]}, "pass"),
        ({"edit_paths": ["src/**"]}, "pass"),
        ({"edit_paths": ["docs/**"]}, "fail"),
        ({}, "fail"),
    ],
)
def test_require_command_edit_filters(params: dict[str, Any], status: str) -> None:
    s = session(edit(E), cmd("pytest"), edit("C:/work/repo/docs/README.md"))
    assert run(make_rule("require-command", command="pytest", **params), s).status == status


def test_require_command_must_succeed_false() -> None:
    s = session(edit(E), cmd("pytest", 1))
    r = run(make_rule("require-command", command="pytest", must_succeed=False), s)
    assert (r.status, r.summary) == ("pass", 'ran "pytest" after the last edit (#0)')


@pytest.mark.parametrize(
    ("events", "diff", "status"),
    [
        ([edit("C:/work/repo/README.md")], None, "pass"),  # not required
        ([edit(E)], None, "fail"),
        ([], Diff("HEAD", [FileChange("src/x.py", "modified")]), "fail"),
        ([], Diff("HEAD", [FileChange("docs/x.md", "renamed", "src/x.py")]), "fail"),
        ([edit("C:/other/src/app.py")], None, "pass"),  # outside the repo
    ],
)
def test_require_command_when_paths(events: list[Event], diff: Diff | None, status: str) -> None:
    rule = make_rule("require-command", command="pytest", when_paths=["src/**"])
    r = run(rule, session(*events), diff)
    assert r.status == status
    if status == "pass":
        assert r.summary == "not required: no matching changes"


def test_require_command_scoped() -> None:
    rule = make_rule("require-command", scope="pkg", command="pytest")
    other = session(edit(E))
    assert run(rule, other).summary == "not required: no matching changes"
    s = session(edit("C:/work/repo/pkg/a.py"), cmd("pytest"), edit(E))
    assert run(rule, s).status == "pass"  # the later edit is outside the scope


def test_require_command_match_quoted() -> None:
    s = session(edit(E), cmd('echo "run pytest now"'))
    rule = make_rule("require-command", command="pytest", match_quoted=True)
    assert run(rule, s).status == "pass"


# --- forbid-edit ------------------------------------------------------------------------


def test_forbid_edit_paths_and_outside_repo() -> None:
    s = session(
        edit("C:/work/repo/.env"),
        edit("C:/work/repo/.env", "delete"),
        edit(r"C:\Users\me\.bashrc", tool="Write"),
        edit("C:/work/repo/src/a.py"),
        Event(0, EventKind.EDIT, tool="Write"),  # no path recorded
    )
    r = run(make_rule("forbid-edit", paths=[".env"]), s)
    assert (r.status, r.summary) == ("fail", "edited 1 forbidden file: .env")
    assert (r.evidence[0].path, r.evidence[0].event) == (".env", 0)
    r = run(make_rule("forbid-edit", outside_repo=True), s)
    assert r.summary == r"edited 1 forbidden file: C:\Users\me\.bashrc"
    assert r.evidence[0].path is None
    assert r.evidence[0].excerpt == "Write"
    r = run(make_rule("forbid-edit", paths=[".env"], outside_repo=True), s)
    assert len(r.evidence) == 2


def test_forbid_edit_needs_a_target() -> None:
    r = run(make_rule("forbid-edit"), session())
    assert r.status == "skip"
    assert "paths or outside_repo" in r.summary


def test_forbid_edit_scope_and_pass() -> None:
    s = session(edit("C:/work/repo/pkg/gen/a.py"), edit("C:/work/repo/gen/b.py"))
    r = run(make_rule("forbid-edit", scope="pkg", paths=["gen/"]), s)
    assert [e.path for e in r.evidence] == ["pkg/gen/a.py"]
    assert run(make_rule("forbid-edit", paths=["*.lock"]), s).summary == "no forbidden file edited"


def test_forbid_edit_relative_to_session_cwd() -> None:
    s = session(edit("../outside.txt"), edit("inside.txt"), cwd="C:/work/repo")
    r = run(make_rule("forbid-edit", outside_repo=True), s)
    assert [e.message for e in r.evidence] == ["edited ../outside.txt outside the repo"]


# --- forbid-tool ------------------------------------------------------------------------


def test_forbid_tool() -> None:
    s = session(tool("WebFetch", "https://example.com"), cmd("ls"), tool("WebFetch"), say("hi"))
    r = run(make_rule("forbid-tool", tool="^Web"), s)
    assert (r.status, r.summary) == ("fail", "called WebFetch 2 times")
    assert r.evidence[0].excerpt == "https://example.com"
    r = run(make_rule("forbid-tool", tool="^Bash$"), s)
    assert r.summary == "called Bash once"
    assert run(make_rule("forbid-tool", tool="webfetch"), s).status == "pass"


# --- forbid-message ---------------------------------------------------------------------


def test_forbid_message() -> None:
    s = session(
        say("Sure.\nYou're absolutely right! Fixing it.\nDone."),
        say("you're absolutely right", actor="sub-1"),
    )
    r = run(make_rule("forbid-message", pattern="absolutely right"), s)
    assert r.status == "fail"
    assert r.summary == '1 assistant message matches "absolutely right"'
    assert r.evidence[0].excerpt == "You're absolutely right! Fixing it."
    r = run(make_rule("forbid-message", pattern="absolutely right", subagents=True), s)
    assert len(r.evidence) == 2
    r = run(make_rule("forbid-message", pattern="ABSOLUTELY", ignore_case=True), s)
    assert len(r.evidence) == 1
    assert run(make_rule("forbid-message", pattern="nope"), s).status == "pass"


# --- regressions from review ------------------------------------------------------------

DENIED = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if "
    "it was a file edit, the new_string was NOT written to the file)."
)
BLOCKED = "PreToolUse:Bash hook error: [guard]: git push is blocked"


@pytest.mark.parametrize("output", [DENIED, BLOCKED])
def test_denied_or_blocked_commands_did_not_run(output: str) -> None:
    s = session(cmd("git push origin main", None, output, is_error=True))
    assert run(make_rule("forbid-command", command=r"git\s+push"), s).status == "pass"
    s = session(edit(E), cmd("uv run pytest", None, output, is_error=True))
    assert run(make_rule("require-command", command="pytest"), s).summary == (
        'no command matching "pytest" ran after the last edit (#0)'
    )


def test_failed_command_still_ran() -> None:
    s = session(cmd("git push origin main", 1, "rejected: non-fast-forward", is_error=True))
    assert run(make_rule("forbid-command", command=r"git\s+push"), s).status == "fail"


def test_failed_edits_do_not_count() -> None:
    bad = edit(E)
    bad.is_error = True
    s = session(cmd("pytest"), bad)
    assert run(make_rule("require-command", command="pytest"), s).summary == (
        "not required: no edits"
    )
    blocked = edit("C:/work/repo/.env", tool="Write")
    blocked.is_error = True
    assert run(make_rule("forbid-edit", paths=[".env"]), session(blocked)).status == "pass"


def test_exit_zero_with_failing_output_fails() -> None:
    out = "FAILED tests/test_a.py::test_x\n1 failed, 41 passed in 0.5s"
    s = session(edit(E), cmd("uv run pytest -q 2>&1 | tail -5", 0, out))
    r = run(make_rule("require-command", command="pytest"), s)
    assert r.status == "fail"
    assert r.summary.endswith("but it failed (exit 0 but output shows failures)")


def test_powershell_quoting() -> None:
    bs = "\\"
    assert command_matches(PUSH, 'Set-Location "C:' + bs + "Program Files" + bs + '"; git push')
    assert command_matches(PUSH, 'Write-Host "a `"b c" ; git push')
    assert not command_matches(PUSH, 'git commit -m "say `"git push`" ok"', powershell=True)
    assert not command_matches(PUSH, "<# git push #>\ngit status", powershell=True)
    assert not command_matches(PUSH, "git status # git push", powershell=True)


def test_require_command_uses_powershell_quoting_from_tool() -> None:
    bs = "\\"
    text = 'cd "C:' + bs + "repo" + bs + '"; uv run pytest -q'
    ev = cmd(text)
    ev.tool = "PowerShell"
    assert run(make_rule("require-command", command="pytest"), session(edit(E), ev)).status == (
        "pass"
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("uv run pytest -q", True),
        ("cd repo && uv run pytest", True),
        ("FOO=1 python -m pytest", True),
        ("timeout 60 npx vitest run", True),
        ("poetry run pytest", True),
        ("pnpm exec jest", True),
        (".venv/bin/pytest", True),
        ("make test", True),
        ("bash -lc 'uv run pytest'", True),
        ("cat pytest.ini", False),
        ("grep -n pytest pyproject.toml", False),
        ("uv add --dev pytest", False),
        ("pip show pytest", False),
        ("uv run pytest --version", False),
        ("uv run pytest --collect-only -q", False),
        ("pytest --co", False),
        ("pytest -h", False),
        ("echo done > /tmp/pytest.log", False),
        ('git commit -m "run pytest"', False),
    ],
)
def test_invokes_requires_command_position(command: str, expected: bool) -> None:
    assert invokes(re.compile(r"\b(?:pytest|jest|vitest)\b|make\s+test"), command) is expected


@pytest.mark.parametrize(
    ("command", "writes"),
    [
        ("echo x > notes.txt", ["notes.txt"]),
        ("cat >> CHANGELOG.md <<EOF\nentry\nEOF", ["CHANGELOG.md"]),
        ("pytest 2>&1 | tee out.log", ["out.log"]),
        ("sed -i 's/a b/c/' src/a.py", ["src/a.py"]),
        ("sed -i.bak -e s/a/b/ x.py y.py", ["x.py", "y.py"]),
        ("git apply fix.patch", [None]),
        ("ruff check --fix .", [None]),
        ("npx prettier --write src", [None]),
        ("uv run ruff format .", [None]),
        ("cargo fmt", [None]),
        ("ruff format --check .", []),
        ("echo x > /dev/null", []),
        ("pytest 2>&1", []),
        ('echo "a > b"', []),
        ("ls", []),
    ],
)
def test_shell_writes(command: str, writes: list[str | None]) -> None:
    assert shell_writes(command) == writes


def test_shell_writes_count_as_edits() -> None:
    s = session(edit(E), cmd("pytest"), cmd("sed -i s/a/b/ src/app.py"))
    assert run(make_rule("require-command", command="pytest"), s).status == "fail"
    s = session(edit(E), cmd("pytest"), cmd("echo note >> README.md"))
    rule = make_rule("require-command", command="pytest", ignore_edit_paths=["*.md"])
    assert run(rule, s).status == "pass"
    s = session(edit(E), cmd("pytest"), cmd("echo x > /tmp/out.txt"))
    assert run(make_rule("require-command", command="pytest"), s).status == "pass"
    s = session(edit(E), cmd("ruff check --fix . && pytest"))  # writes, then runs
    assert run(make_rule("require-command", command="pytest"), s).status == "pass"


def test_tokens_masked_in_excerpts() -> None:
    s = session(cmd("curl -H 'Authorization: token ghp_abcdefghijklmnopqrstuvwxyz0123' x"))
    r = run(make_rule("forbid-command", command="curl"), s)
    assert "ghp_abcdef" not in r.summary
    assert r.evidence[0].excerpt is not None and "ghp_<redacted:" in r.evidence[0].excerpt
