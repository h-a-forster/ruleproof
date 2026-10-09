"""Helpers shared by the checks: regexes, edit-path mapping, command matching and outcomes."""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from ruleproof import paths
from ruleproof.models import Context, Event, Evidence

MAX_EVIDENCE = 50
EXCERPT_LIMIT = 200

Outcome = Literal["pass", "fail", "unverified"]


@functools.lru_cache(maxsize=512)
def compile_regex(
    pattern: str, ignore_case: bool = False, multiline: bool = False
) -> re.Pattern[str]:
    flags = (re.IGNORECASE if ignore_case else 0) | (re.MULTILINE if multiline else 0)
    return re.compile(pattern, flags)


def clip(text: str, limit: int = EXCERPT_LIMIT) -> str:
    """``text`` on one line (whitespace collapsed), at most ``limit`` characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def quoted(text: str, limit: int = 60) -> str:
    return '"' + clip(text, limit) + '"'


def plural(n: int, noun: str, many: str | None = None) -> str:
    return f"{n} {noun if n == 1 else (many or noun + 's')}"


def preview(items: Sequence[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown} and {len(items) - limit} more"


def cap(evidence: list[Evidence], limit: int = MAX_EVIDENCE) -> list[Evidence]:
    """Keep the first ``limit`` items and note how many were dropped."""
    if len(evidence) <= limit:
        return evidence
    return [*evidence[:limit], Evidence(f"... and {len(evidence) - limit} more")]


def globs_match(path: str, patterns: Iterable[str] | None, scope: str | None = None) -> bool:
    """Match repo-relative ``path`` against globs.

    For a scoped rule (from ``pkg/AGENTS.md``) a pattern may also be written relative to the
    scope directory: ``src/**`` then matches ``pkg/src/x.py`` as well as ``src/x.py``.
    """
    pats = list(patterns or ())
    if not pats:
        return False
    if paths.match_any(path, pats):
        return True
    if scope:
        prefix = paths.normalize(scope).rstrip("/") + "/"
        norm = paths.normalize(path)
        if norm.startswith(prefix):
            return paths.match_any(norm[len(prefix) :], pats)
    return False


def edit_relpath(event: Event, ctx: Context) -> str | None:
    """Repo-relative path of an EDIT event, or None when it lies outside the repo."""
    if not event.path:
        return None
    cwd = ctx.session.cwd if ctx.session else None
    repo: Path | str | None = ctx.repo if ctx.repo is not None else cwd
    rel = paths.relpath_in_repo(event.path, repo, cwd)
    return rel or None


def counted_edits(
    ctx: Context,
    scope: str | None,
    edit_paths: Sequence[str] | None,
    ignore_edit_paths: Sequence[str] | None,
) -> list[tuple[Event, str]]:
    """In-repo, in-scope edits that pass the ``edit_paths`` / ``ignore_edit_paths`` filters."""
    if ctx.session is None:
        return []
    out: list[tuple[Event, str]] = []
    for ev in ctx.session.edits():
        rel = edit_relpath(ev, ctx)
        if rel is None or not paths.in_scope(rel, scope):
            continue
        if edit_paths and not globs_match(rel, edit_paths, scope):
            continue
        if globs_match(rel, ignore_edit_paths, scope):
            continue
        out.append((ev, rel))
    return out


# --- command matching -------------------------------------------------------------------

_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)\\?([A-Za-z_][\w.-]*)\1")
_SHELL_C = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:\S*[\\/])?"
    r"(?:"
    r"(?:sh|bash|zsh|dash|pwsh|powershell)(?:\.exe)?"
    r"(?:\s+(?:-(?i:executionpolicy|ep)\s+\S+|-(?![A-Za-z]*c\b|(?i:command)\b)[\w-]+))*"
    r"\s+-(?:[A-Za-z]*c|(?i:command))"
    r"|cmd(?:\.exe)?\s+/[cCkK]"
    r")\s+"
)
_QUOTE_CHARS = re.compile(r"[\"']")
_MAX_NESTING = 4


def command_texts(command: str, match_quoted: bool = False) -> list[str]:
    """The texts a command pattern is matched against.

    With ``match_quoted`` the raw command line. Otherwise the command with quoted strings that
    contain whitespace, heredoc bodies and PowerShell here-strings blanked out (so a commit
    message mentioning ``git push`` is not a push), short quoted words unquoted (``"push"`` ->
    ``push``), plus, recursively, the script passed to ``sh/bash/zsh/dash -c``,
    ``pwsh/powershell -Command`` and ``cmd /c``.
    """
    if match_quoted:
        return [command]
    return _texts(command, 0)


def _texts(command: str, depth: int) -> list[str]:
    masked = _mask(command)
    out = [_QUOTE_CHARS.sub("", masked)]
    if depth < _MAX_NESTING:
        for m in _SHELL_C.finditer(masked):
            payload = _read_word(command, m.end())
            if payload.strip():
                out.extend(_texts(payload, depth + 1))
    return out


def command_matches(rx: re.Pattern[str], command: str, match_quoted: bool = False) -> bool:
    return any(rx.search(t) for t in command_texts(command, match_quoted))


def _mask(s: str) -> str:
    out = list(s)
    _scan(s, 0, out, in_sub=False)
    return "".join(out)


def _blank(out: list[str], start: int, end: int) -> None:
    for k in range(start, min(end, len(out))):
        if out[k] != "\n":
            out[k] = " "


def _sq_end(s: str, i: int) -> int:
    """Index of the quote closing the single-quoted string opened at ``i`` (``''`` escapes)."""
    j = i + 1
    while True:
        j = s.find("'", j)
        if j == -1:
            return len(s)
        if s.startswith("''", j):
            j += 2
            continue
        return j


def _scan(s: str, i: int, out: list[str], in_sub: bool) -> int:
    """Mask from ``i``; inside ``$(...)`` (``in_sub``) return the index after the ``)``."""
    n = len(s)
    depth = 0
    pending: list[str] = []
    while i < n:
        c = s[i]
        if c == "\n" and pending:
            i = _skip_heredocs(s, i + 1, out, pending)
            pending = []
            continue
        if c == "\\":
            i += 2
            continue
        if c == "@" and re.match(r"@['\"]\r?\n", s[i : i + 4]):
            close = "\n" + s[i + 1] + "@"
            j = s.find(close, i + 2)
            end = n if j == -1 else j
            _blank(out, i + 2, end)
            i = n if j == -1 else j + len(close)
            continue
        if c == "'":
            j = _sq_end(s, i)
            if any(ch.isspace() for ch in s[i + 1 : j]):
                _blank(out, i + 1, j)
            i = j + 1
            continue
        if c == '"':
            i = _scan_dq(s, i + 1, out)
            continue
        if c == "<" and not s.startswith("<<<", i):
            m = _HEREDOC.match(s, i)
            if m:
                pending.append(m.group(2))
                i = m.end()
                continue
        if in_sub:
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    return i + 1
                depth -= 1
        i += 1
    return n


def _scan_dq(s: str, i: int, out: list[str]) -> int:
    """Mask a double-quoted string starting after its opening quote; keep ``$(...)``."""
    n = len(s)
    literal: list[int] = []
    while i < n and s[i] != '"':
        if s[i] in "\\`" and i + 1 < n:
            literal += [i, i + 1]
            i += 2
        elif s.startswith("$(", i):
            i = _scan(s, i + 2, out, in_sub=True)
        else:
            literal.append(i)
            i += 1
    if any(s[k].isspace() for k in literal):
        for k in literal:
            if out[k] != "\n":
                out[k] = " "
    return i + 1


def _skip_heredocs(s: str, i: int, out: list[str], delimiters: list[str]) -> int:
    n = len(s)
    for delim in delimiters:
        while i < n:
            j = s.find("\n", i)
            end = n if j == -1 else j
            if s[i:end].strip() == delim:
                i = end + 1
                break
            _blank(out, i, end)
            i = end + 1
    return min(i, n)


def _read_word(s: str, i: int) -> str:
    """The shell word at ``i``: a quoted string (unescaped) or the rest of the line."""
    if i >= len(s):
        return ""
    if s[i] == "'":
        return s[i + 1 : _sq_end(s, i)].replace("''", "'")
    if s[i] == '"':
        buf: list[str] = []
        k = i + 1
        while k < len(s) and s[k] != '"':
            if s[k] in "\\`" and k + 1 < len(s) and s[k + 1] in '"\\`$':
                buf.append(s[k + 1])
                k += 2
                continue
            buf.append(s[k])
            k += 1
        return "".join(buf)
    end = s.find("\n", i)
    return s[i:] if end == -1 else s[i:end]


# --- outcomes ---------------------------------------------------------------------------


def command_outcome(event: Event) -> tuple[Outcome, str]:
    """Pass/fail/unverified for a command event, with a short reason such as ``exit 0``."""
    if event.exit_code == 0:
        return "pass", "exit 0"
    if event.exit_code is not None:
        return "fail", f"exit {event.exit_code}"
    if event.is_error:
        return "fail", "reported as failed"
    verdict = output_verdict(event.output)
    if verdict is True:
        return "pass", "exit code unknown, output shows success"
    if verdict is False:
        return "fail", "exit code unknown, output shows failures"
    return "unverified", "exit code unknown"


_M = re.MULTILINE
_FAILURE = [
    re.compile(r"^.*\b[1-9]\d* (?:failed|errors?)\b.* in [\d.]+s\b", _M),  # pytest summary
    re.compile(r"^FAILED \((?:failures|errors)=", _M),  # unittest
    re.compile(r"^\s*Tests?:?\s+.*\b[1-9]\d* failed\b", _M),  # jest / vitest
    re.compile(r"^\s*Test Files\s+.*\b[1-9]\d* failed\b", _M),  # vitest
    re.compile(r"^(?:FAIL\b|--- FAIL:)", _M),  # go test
    re.compile(r"test result: FAILED\b"),  # cargo test
    re.compile(r"^error(?:\[E\d{4}\])?: (?:could not compile|aborting)|^error\[E\d{4}\]", _M),
    re.compile(r"^Found [1-9]\d* errors? in \d+ files?", _M),  # mypy
    re.compile(r"\berror TS\d+:"),  # tsc
    re.compile(r"\b\d+ problems? \([1-9]\d* errors?", _M),  # eslint
    re.compile(r"\bBUILD FAIL(?:ED|URE)\b|^Build FAILED\.", _M),  # gradle, maven, dotnet
    re.compile(r"^Failed!\s+-\s+Failed:\s+[1-9]", _M),  # dotnet test
    re.compile(r"^npm (?:ERR!|error) ", _M),
]
_RUFF_FOUND = re.compile(r"^Found (\d+) errors?(?: \((\d+) fixed, (\d+) remaining\))?", _M)
_SUCCESS = [
    re.compile(r"^.*\b\d+ passed\b.* in [\d.]+s\b", _M),  # pytest summary
    re.compile(r"^\s*Tests?:?\s+.*\b\d+ passed\b", _M),  # jest / vitest
    re.compile(r"^ok\s+\S+\s+(?:[\d.]+s|\(cached\))", _M),  # go test
    re.compile(r"test result: ok\."),  # cargo test
    re.compile(r"^All checks passed!", _M),  # ruff
    re.compile(r"^Success: no issues found", _M),  # mypy
    re.compile(r"\bBUILD SUCCESS(?:FUL)?\b|^Build succeeded\.", _M),
    re.compile(r"^Passed!\s+-\s+Failed:\s+0\b", _M),  # dotnet test
]
_UNITTEST_OK = re.compile(r"^Ran \d+ tests? in [\d.]+s\s*\n+\s*OK\b", _M)


def output_verdict(output: str) -> bool | None:
    """True/False when command output unambiguously shows success/failure, else None.

    Recognises the summary lines of pytest, unittest, jest, vitest, go test, cargo, ruff,
    mypy, tsc, eslint, gradle, maven and dotnet. Any failure signal wins over success.
    """
    if not output:
        return None
    if any(rx.search(output) for rx in _FAILURE):
        return False
    for m in _RUFF_FOUND.finditer(output):
        remaining = m.group(3)
        if remaining is None or int(remaining) > 0:
            return False
    if _UNITTEST_OK.search(output) or any(rx.search(output) for rx in _SUCCESS):
        return True
    if any(m.group(3) == "0" for m in _RUFF_FOUND.finditer(output)):
        return True
    return None
