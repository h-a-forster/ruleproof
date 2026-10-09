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
        "cwd": "/somewhere",
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
