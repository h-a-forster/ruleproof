"""Command-line interface.

Each command imports what it needs lazily, so ``ruleproof --help`` stays fast and a problem
in one subsystem does not break unrelated commands.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, TextIO, cast

from ruleproof import __version__
from ruleproof.errors import ConfigError, RuleproofError
from ruleproof.report import FORMATS, PROJECT_URL

if TYPE_CHECKING:
    from ruleproof.models import Report, Session, Severity

AGENTS = ("auto", "claude-code", "codex", "gemini-cli", "generic")
FAIL_ON = ("error", "warning", "info", "never")
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130

RULES_DOC_URL = f"{PROJECT_URL}/blob/main/docs/rules.md"
_INPUT_LABELS = {"diff": "diff", "session": "transcript"}
_MAX_SESSION_WARNINGS = 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version, or a usage error
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    _make_stdout_lossless()
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except RuleproofError as exc:
        print(f"ruleproof: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("ruleproof: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:
        debug = os.environ.get("RULEPROOF_DEBUG") == "1"
        if debug:
            traceback.print_exc()
        hint = "" if debug else " (set RULEPROOF_DEBUG=1 to see the traceback)"
        print(
            f"ruleproof: internal error: {type(exc).__name__}: {exc}\n"
            f"ruleproof: this is a bug; please report it at {PROJECT_URL}/issues{hint}",
            file=sys.stderr,
        )
        return EXIT_USAGE


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ruleproof",
        description="Check whether a coding agent followed its instruction files "
        "(AGENTS.md, CLAUDE.md, GEMINI.md).",
    )
    parser.add_argument("--version", action="version", version=f"ruleproof {__version__}")
    parser.add_argument("-q", "--quiet", action="store_true", help="less output")
    parser.add_argument("--no-color", action="store_true", help="never use ANSI colors")
    parser.set_defaults(handler=lambda _args: _print_help(parser))

    # Accept -q / --no-color after the subcommand too; SUPPRESS keeps the top-level value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="less output"
    )
    common.add_argument(
        "--no-color", action="store_true", default=argparse.SUPPRESS, help="never use ANSI colors"
    )

    sub = parser.add_subparsers(title="commands", metavar="COMMAND")

    def command(name: str, help_: str, handler: Callable[[argparse.Namespace], int]) -> Any:
        p = sub.add_parser(name, help=help_, description=help_, parents=[common])
        p.set_defaults(handler=handler)
        return p

    p = command("check", "check the diff and transcript against the rules", cmd_check)
    p.add_argument(
        "--rules",
        metavar="FILE",
        help="rules file; a relative path resolves against the current directory (default: "
        "ruleproof.toml, .ruleproof.toml or [tool.ruleproof] in pyproject.toml)",
    )
    p.add_argument("--repo", metavar="DIR", help="repository (default: current directory)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--base", metavar="REF", help="compare the working tree with REF (HEAD)")
    g.add_argument("--patch", metavar="FILE", help="read a unified diff from FILE ('-' = stdin)")
    g.add_argument("--no-diff", action="store_true", help="do not use a diff")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--transcript", metavar="FILE", help="agent transcript to check")
    g.add_argument("--session", metavar="latest|ID", help="session to check, found on disk")
    g.add_argument("--no-transcript", action="store_true", help="do not use a transcript")
    p.add_argument("--agent", choices=AGENTS, default="auto", help="transcript format")
    _add_output_args(p)
    p.add_argument("--strict", action="store_true", help="also fail on unverified results")

    p = command("compile", "turn instruction files into ruleproof.toml", cmd_compile)
    p.add_argument("files", nargs="*", metavar="FILES", help="instruction files (default: all)")
    p.add_argument(
        "--output", metavar="FILE", help="where to write ('-' = stdout; default: ruleproof.toml)"
    )
    p.add_argument("--force", action="store_true", help="overwrite an existing file")

    p = command("doctor", "lint instruction and agent config files", cmd_doctor)
    p.add_argument("--repo", metavar="DIR", help="repository (default: current directory)")
    _add_output_args(p)
    p.add_argument(
        "--max-tokens",
        type=int,
        default=2500,
        metavar="N",
        help="per-session token budget for instruction files (default: 2500)",
    )

    p = command("sessions", "list agent sessions for this repository", cmd_sessions)
    p.add_argument("--repo", metavar="DIR", help="repository (default: current directory)")
    p.add_argument("--agent", choices=AGENTS[1:], help="only this agent")
    p.add_argument("--all", action="store_true", help="sessions from every project")
    p.add_argument("--limit", type=int, metavar="N", help="show at most N sessions")
    p.add_argument("--json", action="store_true", help="print JSON")

    p = command("timeline", "print a session as a timeline", cmd_timeline)
    p.add_argument(
        "session", metavar="SESSION", help="transcript path, session id prefix, or latest"
    )
    p.add_argument(
        "--format",
        choices=("text", "json", "sqlite"),
        default="text",
        help="text (one line per event), json, or sqlite (default: text)",
    )
    p.add_argument("--output", metavar="FILE", help="write to FILE (required for sqlite)")

    command("checks", "list available checks and their parameters", cmd_checks)

    p_hook = sub.add_parser("hook", help="agent hook adapters", description="Agent hook adapters.")
    p_hook.set_defaults(handler=lambda _args: _print_help(p_hook))
    hook_sub = p_hook.add_subparsers(title="hooks", metavar="HOOK")
    h = hook_sub.add_parser(
        "claude-stop",
        help="Claude Code Stop hook: block finishing while rules fail",
        description="Claude Code Stop hook: reads the hook payload on stdin and blocks "
        "finishing while rules fail. Always exits 0.",
        parents=[common],
    )
    h.add_argument("--rules", metavar="FILE", help="rules file, relative to the hook payload's cwd")
    h.add_argument("--base", metavar="REF", default="HEAD", help="compare with REF (HEAD)")
    h.set_defaults(handler=cmd_hook_claude_stop)
    return parser


def _add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=FORMATS, default="text", help="report format")
    p.add_argument("--output", metavar="FILE", help="write the report to FILE")
    p.add_argument(
        "--fail-on",
        choices=FAIL_ON,
        default="error",
        help="lowest severity that fails the run (default: error)",
    )


def _print_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help(sys.stderr)
    return EXIT_USAGE


# --------------------------------------------------------------------------- commands


def cmd_check(args: argparse.Namespace) -> int:
    from ruleproof.engine import run_rules
    from ruleproof.models import Context, Report, SessionInfo
    from ruleproof.rules import load_rules

    no_git_ok = bool(args.patch or args.no_diff)
    repo = _resolve_repo(args.repo, allow_plain_dir=no_git_ok)
    rules = load_rules(repo, rules_file=_rules_path(args.rules))
    if not rules:
        raise ConfigError(
            f"no rules found in {repo}: run `ruleproof compile` to generate ruleproof.toml "
            f"from your instruction files, or write one (see {RULES_DOC_URL})"
        )

    diff = _load_diff(args, repo)
    session = _load_check_session(args, repo)
    if session is not None:
        _anchor_cwd(session, repo)
    results = run_rules(rules, Context(repo=repo, diff=diff, session=session))

    notes: list[str] = []
    if session is None:
        skipped = sum(1 for r in results if r.status == "skip" and "no transcript" in r.summary)
        if skipped:
            hint = (
                ""
                if args.no_transcript
                else " (use --session latest to check the latest agent session for this repo)"
            )
            noun = "rule" if skipped == 1 else "rules"
            notes.append(f"no transcript: {skipped} transcript {noun} skipped{hint}")
    else:
        notes += _session_warnings(session)

    report = Report(
        kind="check",
        tool_version=__version__,
        repo=str(repo),
        base=diff.base if diff else None,
        session=SessionInfo(session.agent, session.id, session.path) if session else None,
        results=results,
        notes=notes,
    )
    _emit_report(report, args)
    return _exit_code(report, args.fail_on, strict=args.strict)


def cmd_compile(args: argparse.Namespace) -> int:
    from ruleproof.compile import compile_files, render_summary, render_toml
    from ruleproof.instructions import find_instruction_files
    from ruleproof.rules import read_config

    repo = _resolve_repo(None, allow_plain_dir=True)
    if args.files:
        files = [Path(f) for f in args.files]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise ConfigError(f"no such file: {', '.join(missing)}")
    else:
        files = find_instruction_files(repo, exclude=read_config(repo).exclude)
        if not files:
            raise ConfigError(f"no instruction files (AGENTS.md, CLAUDE.md, ...) found in {repo}")

    result = compile_files(files, repo)
    toml = render_toml(result)
    if args.output == "-":
        _write_stdout(toml)
    else:
        target = Path(args.output) if args.output else repo / "ruleproof.toml"
        if target.exists() and not args.force:
            raise ConfigError(
                f"{target} already exists; use --force to overwrite it or --output - to print"
            )
        _write_file(target, toml)
        if not args.quiet:
            print(f"wrote {target}", file=sys.stderr)
    if not args.quiet:
        sys.stderr.write(render_summary(result).rstrip("\n") + "\n")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from ruleproof.doctor import run_doctor
    from ruleproof.models import Report
    from ruleproof.rules import read_config

    repo = _resolve_repo(args.repo, allow_plain_dir=True)
    config = read_config(repo)
    results = run_doctor(repo, max_tokens=args.max_tokens, exclude=config.exclude)
    report = Report(kind="doctor", tool_version=__version__, repo=str(repo), results=results)
    _emit_report(report, args)
    return _exit_code(report, args.fail_on, strict=False)


def cmd_sessions(args: argparse.Namespace) -> int:
    from ruleproof.transcripts.discover import find_sessions

    repo = _resolve_repo(args.repo, allow_plain_dir=True)
    refs = find_sessions(repo, agent=args.agent, all_projects=args.all, limit=args.limit)
    refs = sorted(refs, key=lambda r: _as_float(getattr(r, "mtime", None)), reverse=True)
    if args.limit is not None:
        refs = refs[: max(args.limit, 0)]

    if args.json:
        rows = [
            {
                "agent": r.agent,
                "id": r.id,
                "path": str(r.path),
                "cwd": None if r.cwd is None else str(r.cwd),
                "started_at": _iso(r.started_at),
                "mtime": _iso(r.mtime),
            }
            for r in refs
        ]
        _write_stdout(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
        return EXIT_OK

    if not refs:
        where = "any project" if args.all else str(repo)
        print(f"no agent sessions found for {where}", file=sys.stderr)
        return EXIT_OK
    table = [("AGENT", "ID", "STARTED", "CWD", "PATH")]
    table += [
        (
            r.agent,
            r.id[:8],
            _short_time(r.started_at if r.started_at else r.mtime),
            "" if r.cwd is None else str(r.cwd),
            str(r.path),
        )
        for r in refs
    ]
    _write_stdout(_format_table(table))
    return EXIT_OK


def cmd_timeline(args: argparse.Namespace) -> int:
    from ruleproof.transcripts.export import render_text, to_json, write_sqlite

    if args.format == "sqlite" and not args.output:
        raise ConfigError("--format sqlite needs --output FILE")
    session = _load_session_spec(args.session, agent="auto")
    if args.format == "text":
        _shorten_edit_paths(session)
    if args.format == "sqlite":
        write_sqlite([session], Path(args.output))
        if not args.quiet:
            print(f"wrote {args.output}", file=sys.stderr)
        return EXIT_OK
    text = render_text(session) if args.format == "text" else to_json(session)
    _emit(text if text.endswith("\n") else text + "\n", args.output)
    return EXIT_OK


def cmd_checks(args: argparse.Namespace) -> int:
    from ruleproof.checks import load_all
    from ruleproof.doctor import DOCTOR_CHECKS

    out: list[str] = ["Checks (use in ruleproof.toml or <!-- ruleproof: CHECK key=value -->):", ""]
    for name, spec in sorted(load_all().items()):
        joiner = " or " if spec.needs_any else " and "
        needs = joiner.join(sorted(_INPUT_LABELS.get(n, n) for n in spec.needs))
        out.append(f"{name}  (needs {needs})")
        if spec.doc and not args.quiet:
            out.append(f"    {' '.join(spec.doc.split())}")
        for pname, param in spec.params.items():
            if param.required:
                extra = "required"
            elif param.default is None:
                extra = "optional"
            else:
                extra = f"default: {_format_default(param.default)}"
            doc = f"  {param.doc}" if param.doc and not args.quiet else ""
            out.append(f"    {pname}: {param.type} ({extra}){doc}")
        out.append("")
    out.append("Doctor checks (ruleproof doctor):")
    out.append("")
    width = max((len(k) for k in DOCTOR_CHECKS), default=0)
    out += [f"{k.ljust(width)}  {v}" for k, v in sorted(DOCTOR_CHECKS.items())]
    _write_stdout("\n".join(out) + "\n")
    return EXIT_OK


def cmd_hook_claude_stop(args: argparse.Namespace) -> int:
    from ruleproof.hook import claude_stop

    return claude_stop(sys.stdin, sys.stdout, sys.stderr, rules_file=args.rules, base=args.base)


# --------------------------------------------------------------------------- inputs


def _resolve_repo(path: str | None, *, allow_plain_dir: bool) -> Path:
    from ruleproof.diff import repo_root
    from ruleproof.errors import GitError

    start = Path(path) if path else Path.cwd()
    if not start.is_dir():
        raise ConfigError(f"not a directory: {start}")
    try:
        return repo_root(start)
    except GitError:
        if allow_plain_dir:
            return start.resolve()
        raise


def _load_diff(args: argparse.Namespace, repo: Path) -> Any:
    from ruleproof.diff import from_git, from_patch

    if args.no_diff:
        return None
    if args.patch:
        if args.patch == "-":
            text = sys.stdin.read()
        else:
            try:
                with open(args.patch, encoding="utf-8", errors="replace", newline="") as f:
                    text = f.read()
            except OSError as exc:
                raise ConfigError(f"cannot read patch {args.patch}: {exc.strerror}") from None
        return from_patch(text)
    return from_git(repo, base=args.base or "HEAD", include_untracked=True)


def _load_check_session(args: argparse.Namespace, repo: Path) -> Session | None:
    if args.transcript:
        from ruleproof.transcripts import load_session

        path = Path(args.transcript)
        if not path.exists():
            raise ConfigError(f"no such transcript: {path}")
        return load_session(path, agent=args.agent)
    if args.session:
        return _load_session_spec(args.session, agent=args.agent, repo=repo)
    return None


def _load_session_spec(spec: str, *, agent: str, repo: Path | None = None) -> Session:
    """A session from a transcript path, ``latest``, or an id prefix."""
    from ruleproof.transcripts import load_session
    from ruleproof.transcripts.discover import resolve_session

    candidate = Path(spec)
    if spec != "latest" and candidate.exists():
        return load_session(candidate, agent=agent)
    if repo is None:
        repo = _resolve_repo(None, allow_plain_dir=True)
    ref = resolve_session(spec, repo, agent=None if agent == "auto" else agent)
    return load_session(Path(ref.path), agent=ref.agent)


def _rules_path(raw: str | None) -> Path | None:
    """``--rules`` as an absolute path; relative paths resolve against the shell's cwd."""
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        hint = (
            ""
            if Path(raw).is_absolute()
            else " (relative --rules paths resolve against the current directory, not --repo)"
        )
        raise ConfigError(f"rules file not found: {path}{hint}")
    return path


def _is_absolute(path: str) -> bool:
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _anchor_cwd(session: Session, repo: Path) -> None:
    """A relative ``session.cwd`` (e.g. ``"."`` in a generic transcript) means the repo."""
    if session.cwd and not _is_absolute(session.cwd):
        session.cwd = str((repo / session.cwd).resolve())


def _shorten_edit_paths(session: Session) -> None:
    """Show edit paths relative to the session's repo when they lie inside it."""
    from ruleproof.diff import repo_root
    from ruleproof.errors import GitError
    from ruleproof.paths import relpath_in_repo

    if not session.cwd or not _is_absolute(session.cwd) or not Path(session.cwd).is_dir():
        return
    try:
        repo = repo_root(Path(session.cwd))
    except GitError:
        repo = Path(session.cwd)
    for ev in session.edits():
        if ev.path:
            rel = relpath_in_repo(ev.path, repo, session.cwd)
            if rel:
                ev.path = rel


def _session_warnings(session: Session) -> list[str]:
    w = session.warnings
    if not w:
        return []
    notes = [f"transcript: {msg}" for msg in w[:_MAX_SESSION_WARNINGS]]
    if len(w) > _MAX_SESSION_WARNINGS:
        notes.append(f"transcript: {len(w) - _MAX_SESSION_WARNINGS} more warnings")
    return notes


# --------------------------------------------------------------------------- output


def _emit_report(report: Report, args: argparse.Namespace) -> None:
    from ruleproof.report import render
    from ruleproof.report.text import SYMBOL_CHARS

    to_stdout = not args.output or args.output == "-"
    color = to_stdout and use_color(sys.stdout, no_color=args.no_color)
    unicode = not to_stdout or _can_encode(sys.stdout, SYMBOL_CHARS)
    fail_on = getattr(args, "fail_on", None)
    text = render(report, args.format, color, quiet=args.quiet, unicode=unicode, fail_on=fail_on)
    _emit(text, args.output)


def _exit_code(report: Report, fail_on: str, *, strict: bool) -> int:
    if fail_on == "never":
        return EXIT_OK
    failed = report.failed(at_least=cast("Severity", fail_on), strict=strict)
    return EXIT_FAILED if failed else EXIT_OK


def _emit(text: str, output: str | None) -> None:
    if output and output != "-":
        _write_file(Path(output), text)
    else:
        _write_stdout(text)


def _write_file(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc.strerror}") from None


def _write_stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _make_stdout_lossless() -> None:
    """Never crash on characters the console encoding (e.g. cp1252) cannot represent."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and stream.errors == "strict":
            stream.reconfigure(errors="replace")


def _can_encode(stream: TextIO, chars: str) -> bool:
    try:
        chars.encode(getattr(stream, "encoding", None) or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def use_color(stream: TextIO, *, no_color: bool = False) -> bool:
    """ANSI colors only on a terminal, without ``--no-color`` or ``NO_COLOR``."""
    if no_color or os.environ.get("NO_COLOR"):
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    return _enable_windows_vt()


def _enable_windows_vt() -> bool:
    """Turn on ANSI escape processing in the Windows console; True if colors will work."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(mode.value & enable_vt) or bool(
            kernel32.SetConsoleMode(handle, mode.value | enable_vt)
        )
    except (OSError, AttributeError):
        return False


def _format_table(rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    lines = [
        "  ".join([cell.ljust(w) for cell, w in zip(row, widths, strict=False)] + [row[-1]])
        for row in rows
    ]
    return "\n".join(line.rstrip() for line in lines) + "\n"


def _as_float(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, int | float):
        moment = datetime.fromtimestamp(value, tz=UTC)
        with contextlib.suppress(OSError, OverflowError, ValueError):  # Windows: early timestamps
            moment = moment.astimezone()
        return moment.isoformat(timespec="seconds")
    return str(value)


def _short_time(value: object) -> str:
    """``2026-10-09 14:03`` from an ISO string, datetime or epoch seconds."""
    iso = _iso(value)
    if not iso:
        return ""
    return iso.replace("T", " ")[:16]


def _format_default(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)
