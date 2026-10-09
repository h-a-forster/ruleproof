from __future__ import annotations

import importlib
import io
import json
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ruleproof import __version__, cli
from ruleproof.checks import CheckSpec, Param
from ruleproof.errors import GitError
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


def result(rid: str, status: str, severity: str = "error", summary: str = "s") -> RuleResult:
    rule = Rule(id=rid, check="forbid-change", severity=severity, source="AGENTS.md:3")  # type: ignore[arg-type]
    return RuleResult(rule, status, summary, [Evidence("e", path="a.bak", line=1)])  # type: ignore[arg-type]


class Fakes:
    """Stand-ins for the modules the CLI drives; records what it was called with."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.repo = tmp_path
        self.rules = [Rule(id="r", check="forbid-change")]
        self.results = [result("r", "pass")]
        self.calls: dict[str, Any] = {}
        self.session = Session(agent="claude-code", id="sess-1", path="t.jsonl")

        def from_git(repo: Path, base: str = "HEAD", include_untracked: bool = True) -> Diff:
            self.calls["from_git"] = (repo, base, include_untracked)
            return Diff(base=base)

        def from_patch(text: str, base: str | None = None) -> Diff:
            self.calls["from_patch"] = text
            return Diff(base=None)

        def load_rules(repo: Path, rules_file: Path | None = None, inline: bool = True) -> Any:
            self.calls["load_rules"] = (repo, rules_file)
            return self.rules

        def run_rules(rules: list[Rule], ctx: Context) -> list[RuleResult]:
            self.calls["ctx"] = ctx
            return self.results

        def load_session(path: Path, agent: str = "auto", include_subagents: bool = True) -> Any:
            self.calls["load_session"] = (Path(path), agent)
            return self.session

        def resolve_session(spec: str, repo: Path, agent: str | None = None) -> Any:
            self.calls["resolve_session"] = (spec, repo, agent)
            return SessionRef("codex", "abc", str(tmp_path / "x.jsonl"), None, None, 0.0)

        _patch(monkeypatch, "ruleproof.diff", repo_root=lambda p: self.repo_root(p))
        _patch(monkeypatch, "ruleproof.diff", from_git=from_git, from_patch=from_patch)
        _patch(monkeypatch, "ruleproof.rules", load_rules=load_rules)
        _patch(monkeypatch, "ruleproof.engine", run_rules=run_rules)
        _patch(monkeypatch, "ruleproof.transcripts", load_session=load_session)
        _patch(monkeypatch, "ruleproof.transcripts.discover", resolve_session=resolve_session)
        monkeypatch.chdir(tmp_path)

    def repo_root(self, path: Path) -> Path:
        return self.repo


@dataclass
class SessionRef:
    agent: str
    id: str
    path: str
    cwd: str | None
    started_at: str | None
    mtime: float


@pytest.fixture
def fk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Fakes:
    monkeypatch.delenv("RULEPROOF_DEBUG", raising=False)
    return Fakes(monkeypatch, tmp_path)


# --------------------------------------------------------------------------- basics


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"ruleproof {__version__}"


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage: ruleproof" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["check", "--bogus"],
        ["check", "--base", "main", "--no-diff"],
        ["check", "--session", "latest", "--no-transcript"],
        ["check", "--format", "xml"],
        ["check", "--fail-on", "sometimes"],
        ["nope"],
    ],
)
def test_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(argv) == 2
    assert "usage:" in capsys.readouterr().err


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", "--help"]) == 0
    out = capsys.readouterr().out
    for flag in ("--rules", "--patch", "--session", "--agent", "--fail-on", "--strict"):
        assert flag in out


# --------------------------------------------------------------------------- check


def test_check_pass_defaults(fk: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check"]) == 0
    assert fk.calls["from_git"] == (fk.repo, "HEAD", True)
    assert fk.calls["load_rules"] == (fk.repo, None)
    assert fk.calls["ctx"].session is None
    out = capsys.readouterr().out
    assert "1 rule: 0 failed, 0 unverified, 0 skipped, 1 passed" in out
    assert "\x1b" not in out  # captured stdout is not a TTY


@pytest.mark.parametrize(
    ("results", "argv", "code"),
    [
        ([result("a", "fail")], [], 1),
        ([result("a", "fail")], ["--fail-on", "never"], 0),
        ([result("a", "fail", "warning")], [], 0),
        ([result("a", "fail", "warning")], ["--fail-on", "warning"], 1),
        ([result("a", "fail", "info")], ["--fail-on", "warning"], 0),
        ([result("a", "fail", "info")], ["--fail-on", "info"], 1),
        ([result("a", "unverified")], [], 0),
        ([result("a", "unverified")], ["--strict"], 1),
        ([result("a", "skip")], ["--strict"], 0),
    ],
)
def test_check_exit_codes(fk: Fakes, results: list[RuleResult], argv: list[str], code: int) -> None:
    fk.results = results
    assert cli.main(["check", "-q", *argv]) == code


def test_check_no_rules_is_config_error(fk: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    fk.rules = []
    assert cli.main(["check"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("ruleproof: error: no rules found")
    assert "ruleproof compile" in err
    assert "/blob/main/docs/rules.md" in err and "architecture" not in err


def test_check_missing_rules_file_shows_absolute_path(
    fk: Fakes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check", "--repo", str(tmp_path), "--rules", "nope.toml"]) == 2
    err = capsys.readouterr().err
    assert f"rules file not found: {tmp_path.resolve() / 'nope.toml'}" in err
    assert "resolve against the current directory" in err
    (tmp_path / "r.toml").write_text("", encoding="utf-8")
    assert cli.main(["check", "--rules", "r.toml"]) == 0
    assert fk.calls["load_rules"][1] == (tmp_path / "r.toml").resolve()


def test_check_split_failed_count(fk: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    fk.results = [result("a", "fail"), result("b", "fail", "warning")]
    assert cli.main(["check"]) == 1
    assert "2 rules: 2 failed (1 at or above error)," in capsys.readouterr().out
    assert cli.main(["check", "--format", "markdown"]) == 1
    assert "### ruleproof: 2 failed (1 at or above error)\n" in capsys.readouterr().out
    assert cli.main(["check", "--format", "markdown", "--fail-on", "warning"]) == 1
    assert "### ruleproof: 2 failed\n" in capsys.readouterr().out


def test_check_anchors_relative_session_cwd(fk: Fakes, tmp_path: Path) -> None:
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n", encoding="utf-8")
    fk.session.cwd = "."
    assert cli.main(["check", "--transcript", str(t)]) == 0
    assert fk.calls["ctx"].session.cwd == str(tmp_path.resolve())
    fk.session.cwd = "/abs/elsewhere"
    cli.main(["check", "--transcript", str(t)])
    assert fk.calls["ctx"].session.cwd == "/abs/elsewhere"


def test_subcommand_help_documents_common_flags(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["timeline", "--help"]) == 0
    out = capsys.readouterr().out
    assert "less output" in out and "never use ANSI colors" in out and "sqlite" in out
    assert cli.main(["hook", "claude-stop", "--help"]) == 0
    assert "hook payload's cwd" in " ".join(capsys.readouterr().out.split())


def test_check_notes_skipped_transcript_rules(
    fk: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    fk.results = [result("a", "skip", summary="no transcript"), result("b", "pass")]
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "note: no transcript: 1 transcript rule skipped" in out
    assert "--session latest" in out
    fk.results = [result("a", "skip", summary="no transcript")]
    cli.main(["check", "--no-transcript"])
    assert "--session latest" not in capsys.readouterr().out


def test_check_with_transcript_and_agent(fk: Fakes, tmp_path: Path) -> None:
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n", encoding="utf-8")
    fk.session.warnings = [f"line {i}: bad json" for i in range(5)]
    assert cli.main(["check", "--transcript", str(t), "--agent", "codex", "--format", "json"]) == 0
    assert fk.calls["load_session"] == (t, "codex")
    assert fk.calls["ctx"].session is fk.session


def test_check_session_spec(fk: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", "--session", "latest", "--format", "json"]) == 0
    assert fk.calls["resolve_session"] == ("latest", fk.repo, None)
    assert fk.calls["load_session"][1] == "codex"  # the ref's agent
    data = json.loads(capsys.readouterr().out)
    assert data["session"] == {"agent": "claude-code", "id": "sess-1", "path": "t.jsonl"}


def test_check_missing_transcript(fk: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", "--transcript", "nope.jsonl"]) == 2
    assert "no such transcript" in capsys.readouterr().err


def test_check_patch_from_stdin_outside_git(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def not_git(path: Path) -> Path:
        raise GitError("not a git repository")

    monkeypatch.setattr(fk, "repo_root", not_git)
    monkeypatch.setattr("sys.stdin", io.StringIO("--- a/x\n+++ b/x\n"))
    assert cli.main(["check", "--patch", "-", "--no-color"]) == 0
    assert fk.calls["from_patch"].startswith("--- a/x")
    assert fk.calls["load_rules"][0] == tmp_path.resolve()


def test_check_outside_git_without_patch_fails(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def not_git(path: Path) -> Path:
        raise GitError("not a git repository: here")

    monkeypatch.setattr(fk, "repo_root", not_git)
    assert cli.main(["check"]) == 2
    assert capsys.readouterr().err == "ruleproof: error: not a git repository: here\n"


def test_check_output_file(fk: Fakes, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fk.results = [result("a", "fail")]
    target = tmp_path / "out" / "report.sarif"
    assert cli.main(["check", "--format", "sarif", "--output", str(target)]) == 1
    assert capsys.readouterr().out == ""
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    assert b"\r\n" not in target.read_bytes()


def test_check_no_diff_and_base(fk: Fakes) -> None:
    cli.main(["check", "--no-diff"])
    assert "from_git" not in fk.calls and fk.calls["ctx"].diff is None
    cli.main(["check", "--base", "main"])
    assert fk.calls["from_git"][1] == "main"


# --------------------------------------------------------------------------- errors


def test_unexpected_error(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(*a: Any, **k: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr("ruleproof.rules.load_rules", crash)
    assert cli.main(["check"]) == 2
    err = capsys.readouterr().err
    assert "internal error: ValueError: boom" in err and "please report" in err
    assert "Traceback" not in err

    monkeypatch.setenv("RULEPROOF_DEBUG", "1")
    assert cli.main(["check"]) == 2
    assert "Traceback" in capsys.readouterr().err


def test_keyboard_interrupt(fk: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(*a: Any, **k: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr("ruleproof.rules.load_rules", interrupt)
    assert cli.main(["check"]) == 130


# --------------------------------------------------------------------------- color


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_use_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli, "_enable_windows_vt", lambda: True)
    assert cli.use_color(io.StringIO()) is False
    assert cli.use_color(FakeTTY()) is True
    assert cli.use_color(FakeTTY(), no_color=True) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli.use_color(FakeTTY()) is False


def test_color_on_tty(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli, "_enable_windows_vt", lambda: True)
    tty = FakeTTY()
    monkeypatch.setattr("sys.stdout", tty)
    cli.main(["check"])
    assert "\x1b[" in tty.getvalue()
    tty.seek(0)
    tty.truncate()
    cli.main(["check", "--no-color"])
    assert "\x1b[" not in tty.getvalue()


def test_ascii_fallback_when_stdout_cannot_encode(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Console(io.TextIOWrapper):  # a terminal whose code page lacks the symbols
        def isatty(self) -> bool:
            return True

    buf = io.BytesIO()
    stream = Console(buf, encoding="ascii")
    monkeypatch.setattr("sys.stdout", stream)
    fk.results = [result("a", "fail"), result("b", "pass")]
    assert cli.main(["check"]) == 1
    stream.flush()
    out = buf.getvalue().decode("ascii")
    assert "x a  s" in out and "+ b  s" in out


def test_redirected_output_is_utf8(fk: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1252")
    monkeypatch.setattr("sys.stdout", stream)
    fk.results = [result("a", "fail")]
    assert cli.main(["check"]) == 1
    stream.flush()
    buf.getvalue().decode("utf-8")


# --------------------------------------------------------------------------- compile


def test_compile(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "AGENTS.md").write_text("- Never do X.\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    def compile_files(paths: list[Path], repo: Path) -> str:
        seen["paths"] = [p.name for p in paths]
        return "RESULT"

    _patch(
        monkeypatch,
        "ruleproof.compile",
        compile_files=compile_files,
        render_toml=lambda r: "version = 1\n",
        render_summary=lambda r: "1 rule compiled",
    )
    assert cli.main(["compile"]) == 0
    assert seen["paths"] == ["AGENTS.md"]
    assert (tmp_path / "ruleproof.toml").read_text(encoding="utf-8") == "version = 1\n"
    assert "1 rule compiled" in capsys.readouterr().err

    assert cli.main(["compile"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["compile", "--force"]) == 0

    capsys.readouterr()
    assert cli.main(["compile", "--output", "-", "-q"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "version = 1\n" and captured.err == ""


def test_compile_without_instruction_files(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, "ruleproof.compile", compile_files=None, render_toml=None)
    _patch(monkeypatch, "ruleproof.compile", render_summary=None)
    assert cli.main(["compile"]) == 2
    assert "no instruction files" in capsys.readouterr().err


# --------------------------------------------------------------------------- doctor


@pytest.mark.parametrize(
    ("severity", "argv", "code"),
    [("warning", [], 0), ("error", [], 1), ("warning", ["--fail-on", "warning"], 1)],
)
def test_doctor(
    fk: Fakes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    severity: str,
    argv: list[str],
    code: int,
) -> None:
    seen: dict[str, Any] = {}

    def run_doctor(repo: Path, *, max_tokens: int = 2500, exclude: Any = ()) -> list[RuleResult]:
        seen["max_tokens"] = max_tokens
        seen["exclude"] = exclude
        return [result("doctor/size", "fail", severity)]

    @dataclass
    class Config:
        rules: list[Rule]
        exclude: list[str]
        origin: str | None

    def read_config(repo: Path, rules_file: Path | None = None) -> Config:
        seen["config_repo"] = repo
        return Config([], ["vendor/**"], "ruleproof.toml")

    _patch(monkeypatch, "ruleproof.doctor", run_doctor=run_doctor, DOCTOR_CHECKS={})
    _patch(monkeypatch, "ruleproof.rules", read_config=read_config)
    assert cli.main(["doctor", "--max-tokens", "100", "--format", "json", *argv]) == code
    assert seen["max_tokens"] == 100
    assert seen["exclude"] == ["vendor/**"] and seen["config_repo"] == fk.repo
    assert json.loads(capsys.readouterr().out)["kind"] == "doctor"


# --------------------------------------------------------------------------- sessions


def _refs(tmp_path: Path) -> list[SessionRef]:
    return [
        SessionRef("codex", "older-session-id", str(tmp_path / "a.jsonl"), "/w", None, 100.0),
        SessionRef(
            "claude-code",
            "0123456789abcdef",
            str(tmp_path / "b.jsonl"),
            "/w",
            "2026-10-09T14:03:22.123Z",
            200.0,
        ),
    ]


def test_sessions_table(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def find_sessions(repo: Path, agent: Any = None, all_projects: bool = False, limit: Any = None):  # type: ignore[no-untyped-def]
        seen.update(repo=repo, agent=agent, all_projects=all_projects, limit=limit)
        return _refs(tmp_path)

    _patch(monkeypatch, "ruleproof.transcripts.discover", find_sessions=find_sessions)
    assert cli.main(["sessions", "--agent", "codex", "--all", "--limit", "5"]) == 0
    assert seen == {"repo": fk.repo, "agent": "codex", "all_projects": True, "limit": 5}
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["AGENT", "ID", "STARTED", "CWD", "PATH"]
    assert lines[1].startswith("claude-code  01234567  2026-10-09 14:03")  # newest first
    assert lines[2].startswith("codex")

    assert cli.main(["sessions", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["0123456789abcdef", "older-session-id"]
    assert set(rows[0]) == {"agent", "id", "path", "cwd", "started_at", "mtime"}


def test_sessions_none(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, "ruleproof.transcripts.discover", find_sessions=lambda *a, **k: [])
    assert cli.main(["sessions"]) == 0
    assert "no agent sessions found" in capsys.readouterr().err


# --------------------------------------------------------------------------- timeline


def test_timeline(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    written: dict[str, Any] = {}
    _patch(
        monkeypatch,
        "ruleproof.transcripts.export",
        render_text=lambda s: f"timeline of {s.id}",
        to_json=lambda s: '{"id": "x"}',
        write_sqlite=lambda sessions, path: written.update(path=path, n=len(sessions)),
    )
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n", encoding="utf-8")
    assert cli.main(["timeline", str(t)]) == 0
    assert capsys.readouterr().out == "timeline of sess-1\n"
    assert fk.calls["load_session"] == (t, "auto")

    def not_git(path: Path) -> Path:
        raise GitError("not a git repository")

    monkeypatch.setattr(fk, "repo_root", not_git)
    fk.session.cwd = str(tmp_path)
    fk.session.events = [
        Event(0, EventKind.EDIT, path=str(tmp_path / "src" / "deep" / "module.py")),
        Event(1, EventKind.EDIT, path="/outside/the/repo.py"),
    ]
    _patch(
        monkeypatch,
        "ruleproof.transcripts.export",
        render_text=lambda s: "\n".join(e.path or "" for e in s.events),
    )
    assert cli.main(["timeline", str(t)]) == 0
    assert capsys.readouterr().out.splitlines() == ["src/deep/module.py", "/outside/the/repo.py"]

    assert cli.main(["timeline", "latest", "--format", "json"]) == 0
    assert fk.calls["resolve_session"][0] == "latest"
    assert json.loads(capsys.readouterr().out) == {"id": "x"}

    assert cli.main(["timeline", "abc", "--format", "sqlite"]) == 2
    assert "needs --output" in capsys.readouterr().err
    assert cli.main(["timeline", "abc", "--format", "sqlite", "--output", "t.db"]) == 0
    assert written == {"path": Path("t.db"), "n": 1}


# --------------------------------------------------------------------------- checks


def test_checks_lists_params_and_doctor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = CheckSpec(
        name="forbid-thing",
        fn=lambda rule, ctx: RuleResult(rule, "pass", ""),
        needs=frozenset({"diff", "session"}),
        params={
            "paths": Param("glob_list", required=True, doc="files to protect"),
            "ignore_case": Param("bool", default=False),
            "limit": Param("int"),
        },
        doc="Fails when the thing happens.",
        needs_any=True,
    )
    monkeypatch.setattr("ruleproof.checks.load_all", lambda: {"forbid-thing": spec})
    _patch(monkeypatch, "ruleproof.doctor", DOCTOR_CHECKS={"doctor/size": "large files"})
    assert cli.main(["checks"]) == 0
    out = capsys.readouterr().out
    assert "forbid-thing  (needs diff or transcript)" in out
    assert "Fails when the thing happens." in out
    assert "paths: glob_list (required)  files to protect" in out
    assert "ignore_case: bool (default: false)" in out
    assert "limit: int (optional)" in out
    assert "doctor/size  large files" in out


# --------------------------------------------------------------------------- hook


def test_hook_command_wires_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def claude_stop(stdin: Any, stdout: Any, stderr: Any, **kw: Any) -> int:
        seen["stdin"] = stdin.read()
        seen.update(kw)
        return 0

    monkeypatch.setattr("ruleproof.hook.claude_stop", claude_stop)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"x": 1}'))
    assert cli.main(["hook", "claude-stop"]) == 0
    assert seen == {"stdin": '{"x": 1}', "rules_file": None, "base": "HEAD"}
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["hook", "claude-stop", "--rules", "../r.toml", "--base", "main"]) == 0
    assert seen["rules_file"] == "../r.toml" and seen["base"] == "main"


def test_hook_pretool_command_wires_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def claude_pretool(stdin: Any, stdout: Any, stderr: Any, **kw: Any) -> int:
        seen["stdin"] = stdin.read()
        seen.update(kw)
        return 0

    monkeypatch.setattr("ruleproof.hook.claude_pretool", claude_pretool)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["hook", "claude-pretool", "--rules", "r.toml"]) == 0
    assert seen == {"stdin": "{}", "rules_file": "r.toml", "base": "HEAD"}
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["hook", "claude-pretool", "--base", "origin/main"]) == 0
    assert seen["base"] == "origin/main"


def test_hook_without_name_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["hook"]) == 2
    err = capsys.readouterr().err
    assert "claude-stop" in err and "claude-pretool" in err


# --------------------------------------------------------------------------- end to end


def test_end_to_end_check_on_real_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    try:
        from ruleproof.diff import from_git, repo_root  # noqa: F401
        from ruleproof.rules import load_rules  # noqa: F401
    except (ImportError, AttributeError):
        pytest.skip("ruleproof modules not available yet")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "AGENTS.md").write_text("# Rules\n\n- Never create .bak files.\n", "utf-8")
    (tmp_path / "ruleproof.toml").write_text(
        'version = 1\n\n[[rule]]\nid = "no-bak"\nsource = "AGENTS.md:3"\n'
        'check = "forbid-change"\npaths = ["*.bak"]\n',
        "utf-8",
    )
    git("add", ".")
    git("commit", "-q", "-m", "init")
    (tmp_path / "x.bak").write_text("old\n", "utf-8")
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert cli.main(["check", "--repo", str(tmp_path), "--format", "json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["failed"] == 1
    assert data["results"][0]["evidence"][0]["path"] == "x.bak"

    (tmp_path / "x.bak").unlink()
    assert cli.main(["check", "--repo", str(tmp_path)]) == 0


def test_compile_honours_exclude(
    fk: Fakes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "AGENTS.md").write_text("- Do X.\n", encoding="utf-8")
    (tmp_path / "examples" / "demo").mkdir(parents=True)
    (tmp_path / "examples" / "demo" / "AGENTS.md").write_text("- Do Y.\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    @dataclass
    class Config:
        exclude: list[str]

    def compile_files(paths: list[Path], repo: Path) -> str:
        seen["paths"] = [p.relative_to(tmp_path).as_posix() for p in paths]
        return "RESULT"

    _patch(
        monkeypatch,
        "ruleproof.rules",
        read_config=lambda repo, rules_file=None: Config(["examples/"]),
    )
    _patch(
        monkeypatch,
        "ruleproof.compile",
        compile_files=compile_files,
        render_toml=lambda r: "",
        render_summary=lambda r: "",
    )
    assert cli.main(["compile", "--output", "-"]) == 0
    assert seen["paths"] == ["AGENTS.md"]
