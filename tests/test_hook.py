from __future__ import annotations

import importlib
import io
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ruleproof import hook
from ruleproof.errors import GitError, TranscriptError
from ruleproof.models import Context, Diff, Event, EventKind, Evidence, Rule, RuleResult, Session


def _patch(monkeypatch: pytest.MonkeyPatch, module: str, **attrs: Any) -> None:
    """Set attributes on a module, creating a stand-in if it does not exist yet."""
    try:
        mod = importlib.import_module(module)
    except ImportError:
        mod = types.ModuleType(module)
        monkeypatch.setitem(sys.modules, module, mod)
    for name, value in attrs.items():
        monkeypatch.setattr(mod, name, value, raising=False)


RULE = Rule(
    id="tests-before-done",
    check="require-command",
    description="Run pytest before saying you are done",
    source="AGENTS.md:12",
)


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.results: list[RuleResult] = [
            RuleResult(
                RULE,
                "fail",
                "no successful pytest after the last edit",
                [Evidence("edit", event=5)],
            )
        ]
        self.rules = [RULE]
        self.contexts: list[Context] = []
        self.calls: dict[str, Any] = {}
        self.session = Session(
            agent="claude-code",
            id="s1",
            path="t.jsonl",
            events=[Event(0, EventKind.ASSISTANT, "working on it")],
        )
        self.repo = tmp_path

        def run_rules(rules: list[Rule], ctx: Context) -> list[RuleResult]:
            self.contexts.append(ctx)
            return self.results

        _patch(monkeypatch, "ruleproof.diff", repo_root=lambda p: self.repo_root(p))

        def from_git(repo: Path, base: str = "HEAD", include_untracked: bool = True) -> Diff:
            self.calls["from_git"] = (repo, base, include_untracked)
            return Diff(base=base)

        def load_rules(repo: Path, rules_file: Path | None = None, inline: bool = True) -> Any:
            self.calls["load_rules"] = (repo, rules_file)
            return self.rules

        _patch(monkeypatch, "ruleproof.diff", from_git=from_git)
        _patch(monkeypatch, "ruleproof.rules", load_rules=load_rules)
        _patch(monkeypatch, "ruleproof.transcripts", load_session=lambda *a, **k: self.load())
        _patch(monkeypatch, "ruleproof.engine", run_rules=run_rules)

    def repo_root(self, path: Path) -> Path:
        return self.repo

    def load(self) -> Session:
        return self.session

    def run(
        self, payload: Any, env: dict[str, str] | None = None, **kw: Any
    ) -> tuple[int, str, str]:
        stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        out, err = io.StringIO(), io.StringIO()
        code = hook.claude_stop(stdin, out, err, env=env or {}, **kw)
        return code, out.getvalue(), err.getvalue()


def payload(**kw: Any) -> dict[str, Any]:
    base = {
        "session_id": "s1",
        "transcript_path": "~/.claude/projects/x/s1.jsonl",
        "cwd": ".",
        "permission_mode": "default",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    base.update(kw)
    return base


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    return Harness(monkeypatch, tmp_path)


def test_rules_file_and_base_options(h: Harness, tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    h.run(payload(cwd=str(cwd)))
    assert h.calls["load_rules"] == (h.repo, None)
    assert h.calls["from_git"] == (h.repo, "HEAD", True)

    h.run(payload(cwd=str(cwd)), rules_file="../rules/r.toml", base="main")
    assert h.calls["load_rules"] == (h.repo, cwd / "../rules/r.toml")
    assert h.calls["from_git"][1] == "main"

    absolute = tmp_path / "elsewhere.toml"
    h.run(payload(cwd=str(cwd)), rules_file=str(absolute))
    assert h.calls["load_rules"][1] == absolute


def test_blocks_on_error_failure(h: Harness) -> None:
    code, out, err = h.run(payload())
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    reason = decision["reason"]
    assert reason.startswith("ruleproof: 1 project rule from AGENTS.md is not satisfied yet.")
    assert "1. tests-before-done: no successful pytest after the last edit" in reason
    assert "rule (AGENTS.md:12): Run pytest before saying you are done" in reason
    assert "- #5  edit" in reason
    ctx = h.contexts[0]
    assert ctx.repo == h.repo and ctx.diff is not None and ctx.session is h.session


@pytest.mark.parametrize(
    ("status", "severity"), [("fail", "warning"), ("unverified", "error"), ("pass", "error")]
)
def test_does_not_block_on_warnings_or_unverified(h: Harness, status: str, severity: str) -> None:
    rule = Rule(id="r", check="claims", severity=severity)  # type: ignore[arg-type]
    h.results = [RuleResult(rule, status, "s")]  # type: ignore[arg-type]
    assert h.run(payload()) == (0, "", "")


def test_never_blocks_twice_in_a_row(h: Harness) -> None:
    code, out, _ = h.run(payload(stop_hook_active=True))
    assert (code, out) == (0, "")
    assert h.contexts == []  # did not even run


def test_disabled_by_env(h: Harness) -> None:
    assert h.run(payload(), env={"RULEPROOF_HOOK_DISABLE": "1"}) == (0, "", "")
    assert h.contexts == []


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]"])
def test_bad_stdin_is_silent(h: Harness, stdin: str) -> None:
    code, out, err = h.run(stdin)
    assert (code, out) == (0, "")
    assert err.startswith("ruleproof hook: ") and err.count("\n") == 1


def test_missing_cwd_is_silent(h: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(path: Path) -> Path:
        raise GitError("not a directory")

    monkeypatch.setattr(h, "repo_root", boom)
    missing = tmp_path / "gone"
    code, out, err = h.run(payload(cwd=str(missing)))
    assert (code, out) == (0, "")
    assert err == f"ruleproof hook: cwd {missing} does not exist; nothing checked\n"


def test_not_a_git_repo_still_runs_transcript_rules(
    h: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(path: Path) -> Path:
        raise GitError("not a git repository")

    monkeypatch.setattr(h, "repo_root", boom)
    code, out, err = h.run(payload(cwd=str(tmp_path)))
    assert code == 0 and json.loads(out)["decision"] == "block"
    ctx = h.contexts[0]
    assert ctx.diff is None and ctx.session is h.session and ctx.repo == tmp_path.resolve()
    assert "from_git" not in h.calls
    assert "not inside a git repository; diff rules skipped" in err
    assert "--repo" not in err and "--patch" not in err


def test_no_rules_is_silent(h: Harness) -> None:
    h.rules = []
    code, out, err = h.run(payload())
    assert (code, out) == (0, "")
    assert "no rules found" in err


def test_internal_error_is_silent(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(rules: list[Rule], ctx: Context) -> list[RuleResult]:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("ruleproof.engine.run_rules", crash)
    code, out, err = h.run(payload())
    assert (code, out) == (0, "")
    assert err == "ruleproof hook: internal error: RuntimeError: kaboom\n"


def test_other_events_are_ignored(h: Harness) -> None:
    code, out, err = h.run(payload(hook_event_name="PreToolUse"))
    assert (code, out) == (0, "")
    assert "unsupported hook event" in err


def test_transcript_error_still_checks_diff(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def bad() -> Session:
        raise TranscriptError("unrecognised transcript")

    monkeypatch.setattr(h, "load", bad)
    code, out, err = h.run(payload())
    assert json.loads(out)["decision"] == "block"
    assert h.contexts[0].session is None
    assert "transcript rules skipped: unrecognised transcript" in err


def test_appends_last_assistant_message(h: Harness) -> None:
    h.run(payload(last_assistant_message="All tests pass."))
    final = h.contexts[0].session.final_message()  # type: ignore[union-attr]
    assert final is not None and final.text == "All tests pass." and final.index == 1


def test_does_not_duplicate_last_assistant_message(h: Harness) -> None:
    h.run(payload(last_assistant_message="working on it"))
    assert len(h.contexts[0].session.events) == 1  # type: ignore[union-attr]


def test_block_reason_is_bounded() -> None:
    many = [
        RuleResult(
            Rule(id=f"r{i}", check="c", source=f"CLAUDE.md:{i + 1}"),
            "fail",
            "x" * 500,
            [Evidence("e" * 300, path="a.py", line=1)] * 5,
        )
        for i in range(20)
    ]
    reason = hook.block_reason(many)
    assert len(reason) <= 3000
    assert reason.startswith("ruleproof: 20 project rules from CLAUDE.md are not satisfied yet.")


# --------------------------------------------------------------------------- end to end


def _real_modules_or_skip() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    try:
        from ruleproof.diff import from_git, repo_root  # noqa: F401
        from ruleproof.rules import load_rules  # noqa: F401
        from ruleproof.transcripts import load_session  # noqa: F401
    except (ImportError, AttributeError):
        pytest.skip("ruleproof modules not available yet")


def make_git_repo(root: Path, rules_toml: str) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (root / "AGENTS.md").write_text("# Rules\n\n- Never create .bak files.\n", encoding="utf-8")
    (root / "ruleproof.toml").write_text(rules_toml, encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    return root


BAK_RULES = """version = 1

[[rule]]
id = "no-backup-files"
description = "Never create .bak files"
source = "AGENTS.md:3"
check = "forbid-change"
paths = ["*.bak"]
"""


def test_end_to_end_blocks_on_real_repo(tmp_path: Path) -> None:
    _real_modules_or_skip()
    repo = make_git_repo(tmp_path, BAK_RULES)
    (repo / "notes.bak").write_text("old\n", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    stdin = io.StringIO(
        json.dumps(payload(cwd=str(repo), transcript_path=str(repo / "missing.jsonl")))
    )
    assert hook.claude_stop(stdin, out, err, env={}) == 0
    decision = json.loads(out.getvalue())
    assert decision["decision"] == "block"
    assert "no-backup-files" in decision["reason"] and "notes.bak" in decision["reason"]

    (repo / "notes.bak").unlink()
    out = io.StringIO()
    stdin = io.StringIO(
        json.dumps(payload(cwd=str(repo), transcript_path=str(repo / "missing.jsonl")))
    )
    assert hook.claude_stop(stdin, out, io.StringIO(), env={}) == 0
    assert out.getvalue() == ""


# --------------------------------------------------------------------------- PreToolUse

PRETOOL_RULES = r"""version = 1

[[rule]]
id = "no-commit"
description = "Never commit; leave committing to the user."
source = "AGENTS.md:7"
check = "forbid-command"
command = '\bgit\s+commit\b'

[[rule]]
id = "no-webfetch"
check = "forbid-tool"
tool = '^WebFetch$'

[[rule]]
id = "no-generated"
description = "Do not edit generated code in api/gen/; change the schema instead."
source = "AGENTS.md:9"
check = "forbid-edit"
paths = ["api/gen/"]

[[rule]]
id = "stay-inside"
check = "forbid-edit"
outside_repo = true

[[rule]]
id = "no-new-bak"
check = "forbid-change"
paths = ["*.bak"]
actions = ["add"]

[[rule]]
id = "warn-only"
severity = "warning"
check = "forbid-command"
command = 'rm -rf'

[[rule]]
id = "not-a-pretool-check"
check = "require-command"
command = 'pytest'
"""


@pytest.fixture
def pre_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    pytest.importorskip("ruleproof.rules")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)  # found on disk; the hook does not spawn git
    (repo / "ruleproof.toml").write_text(PRETOOL_RULES, encoding="utf-8")
    return repo


def pretool(
    repo: Path, tool: str, tool_input: Any, env: dict[str, str] | None = None, **kw: Any
) -> tuple[str | None, str]:
    """Run the PreToolUse hook; return (deny reason or None, stderr)."""
    data = {
        "session_id": "s1",
        "transcript_path": str(repo / "t.jsonl"),
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "toolu_1",
    }
    data.update(kw)
    out, err = io.StringIO(), io.StringIO()
    assert hook.claude_pretool(io.StringIO(json.dumps(data)), out, err, env=env or {}) == 0
    if not out.getvalue():
        return None, err.getvalue()
    decision = json.loads(out.getvalue())["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    return decision["permissionDecisionReason"], err.getvalue()


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_pretool_forbid_command(pre_repo: Path, tool: str) -> None:
    reason, _ = pretool(pre_repo, tool, {"command": "git commit -m 'wip'"})
    assert reason is not None
    assert reason.startswith("ruleproof: this tool call would break 1 project rule")
    assert "1. no-commit:" in reason
    assert "rule (AGENTS.md:7): Never commit; leave committing to the user." in reason
    assert pretool(pre_repo, tool, {"command": "git status"}) == (None, "")


def test_pretool_unwraps_shell_wrappers(pre_repo: Path) -> None:
    reason, _ = pretool(pre_repo, "Bash", {"command": "bash -lc 'git commit -am x'"})
    assert reason is not None and "no-commit" in reason


def test_pretool_ignores_warnings_and_other_checks(pre_repo: Path) -> None:
    assert pretool(pre_repo, "Bash", {"command": "rm -rf build"}) == (None, "")
    assert pretool(pre_repo, "Bash", {"command": "echo done"}) == (None, "")


def test_pretool_forbid_tool(pre_repo: Path) -> None:
    reason, _ = pretool(pre_repo, "WebFetch", {"url": "https://example.com", "prompt": "x"})
    assert reason is not None and "no-webfetch" in reason
    assert pretool(pre_repo, "Read", {"file_path": str(pre_repo / "a.py")}) == (None, "")


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_pretool_forbid_edit(pre_repo: Path, tool: str) -> None:
    target = pre_repo / "api" / "gen" / "client.py"
    reason, _ = pretool(pre_repo, tool, {"file_path": str(target), "content": "x"})
    assert reason is not None
    assert "no-generated" in reason and "api/gen/client.py" in reason
    assert "change the schema instead" in reason
    ok = pre_repo / "src" / "app.py"
    assert pretool(pre_repo, tool, {"file_path": str(ok), "content": "x"}) == (None, "")


def test_pretool_notebook_edit(pre_repo: Path) -> None:
    nb = pre_repo / "api" / "gen" / "n.ipynb"
    reason, _ = pretool(pre_repo, "NotebookEdit", {"notebook_path": str(nb), "new_source": "x"})
    assert reason is not None and "no-generated" in reason


def test_pretool_relative_path(pre_repo: Path) -> None:
    reason, _ = pretool(pre_repo, "Edit", {"file_path": "api/gen/client.py"})
    assert reason is not None and "no-generated" in reason


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive paths")
def test_pretool_windows_paths(pre_repo: Path) -> None:
    backslashed = str(pre_repo).replace("/", "\\") + "\\api\\gen\\client.py"
    lower_drive = backslashed[0].lower() + backslashed[1:]
    reason, _ = pretool(pre_repo, "Write", {"file_path": lower_drive, "content": "x"})
    assert reason is not None and "no-generated" in reason
    forward = str(pre_repo).replace("\\", "/") + "/api/gen/client.py"
    reason, _ = pretool(pre_repo, "Write", {"file_path": forward, "content": "x"})
    assert reason is not None and "no-generated" in reason
    reason, _ = pretool(pre_repo, "Edit", {"file_path": "api\\gen\\client.py"})
    assert reason is not None and "no-generated" in reason


def test_pretool_outside_repo_edit(pre_repo: Path) -> None:
    outside = pre_repo.parent / "elsewhere" / "notes.txt"
    reason, _ = pretool(pre_repo, "Write", {"file_path": str(outside), "content": "x"})
    assert reason is not None and "stay-inside" in reason and "outside the repo" in reason


def test_pretool_forbid_change_new_file_only(pre_repo: Path) -> None:
    reason, _ = pretool(pre_repo, "Write", {"file_path": str(pre_repo / "x.bak"), "content": ""})
    assert reason is not None and "no-new-bak" in reason
    (pre_repo / "old.bak").write_text("", encoding="utf-8")
    assert pretool(pre_repo, "Edit", {"file_path": str(pre_repo / "old.bak")}) == (None, "")


def test_pretool_finds_repo_root_from_subdir(pre_repo: Path) -> None:
    sub = pre_repo / "api"
    sub.mkdir()
    reason, _ = pretool(pre_repo, "Edit", {"file_path": "gen/client.py"}, cwd=str(sub))
    assert reason is not None and "api/gen/client.py" in reason


def test_pretool_outside_git_uses_cwd(pre_repo: Path) -> None:
    (pre_repo / ".git").rmdir()
    reason, err = pretool(pre_repo, "Bash", {"command": "git commit"})
    assert reason is not None and err == ""


@pytest.mark.parametrize("stdin", ["", "nope", "[]", '{"cwd": "."}'])
def test_pretool_malformed_payload_allows(pre_repo: Path, stdin: str) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert hook.claude_pretool(io.StringIO(stdin), out, err, env={}) == 0
    assert out.getvalue() == ""
    assert err.getvalue().startswith("ruleproof hook: ") and err.getvalue().count("\n") == 1


def test_pretool_odd_tool_input_does_not_crash(pre_repo: Path) -> None:
    assert pretool(pre_repo, "Bash", "not a dict")[0] is None
    assert pretool(pre_repo, "Write", {"file_path": 3})[0] is None


def test_pretool_disabled(pre_repo: Path) -> None:
    env = {"RULEPROOF_HOOK_DISABLE": "1"}
    assert pretool(pre_repo, "Bash", {"command": "git commit"}, env=env) == (None, "")


def test_pretool_no_rules_allows(pre_repo: Path) -> None:
    (pre_repo / "ruleproof.toml").unlink()
    reason, err = pretool(pre_repo, "Bash", {"command": "git commit"})
    assert reason is None and "no rules found" in err


def test_pretool_wrong_event(pre_repo: Path) -> None:
    reason, err = pretool(pre_repo, "Bash", {"command": "git commit"}, hook_event_name="Stop")
    assert reason is None and "unsupported hook event" in err


def test_pretool_rules_option(pre_repo: Path, tmp_path: Path) -> None:
    (tmp_path / "personal.toml").write_text(
        'version = 1\n[[rule]]\nid = "no-push"\ncheck = "forbid-command"\ncommand = "git push"\n',
        encoding="utf-8",
    )
    out = io.StringIO()
    data = {
        "hook_event_name": "PreToolUse",
        "cwd": str(pre_repo),
        "tool_name": "Bash",
        "tool_input": {"command": "git push"},
    }
    hook.claude_pretool(
        io.StringIO(json.dumps(data)), out, io.StringIO(), env={}, rules_file="../personal.toml"
    )
    decision = json.loads(out.getvalue())["hookSpecificOutput"]
    assert "no-push" in decision["permissionDecisionReason"]
