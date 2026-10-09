"""Helpers shared by the checks: regexes, edit-path mapping, command matching and outcomes."""

from __future__ import annotations

import functools
import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ruleproof import paths
from ruleproof.models import Context, Event, EventKind, Evidence

MAX_EVIDENCE = 50
EXCERPT_LIMIT = 200

Outcome = Literal["pass", "fail", "unverified"]


@functools.lru_cache(maxsize=512)
def compile_regex(
    pattern: str, ignore_case: bool = False, multiline: bool = False
) -> re.Pattern[str]:
    flags = (re.IGNORECASE if ignore_case else 0) | (re.MULTILINE if multiline else 0)
    return re.compile(pattern, flags)


_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-|sk_live_|sk_test_|rk_live_|gh[pousr]_|github_pat_|xox[abposr]-"
    r"|AKIA|ASIA|AIza|glpat-)[A-Za-z0-9_\-]{8,}"
)


def mask_tokens(text: str) -> str:
    """Hide all but the first 4 characters of strings that look like API tokens."""
    return _TOKEN.sub(lambda m: f"{m.group()[:4]}<redacted:{len(m.group()) - 4} chars>", text)


def clip(text: str, limit: int = EXCERPT_LIMIT) -> str:
    """``text`` on one line (whitespace collapsed, tokens masked), at most ``limit`` chars."""
    flat = mask_tokens(" ".join(text.split()))
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


# --- transcript events ------------------------------------------------------------------

_DENIED = re.compile(
    r"doesn't want to proceed|tool use was rejected|rejected by (?:the )?user"
    r"|requested permissions? to use|haven't granted it"
    r"|permission to use \w+ (?:has been|was) denied"
    r"|\bhook (?:error|blocked)|blocked by (?:a |the )?(?:\w+ )?hook|PreToolUse:\w+ hook"
    r"|user (?:denied|declined)|was (?:denied|cancelled|canceled) by the user",
    re.IGNORECASE,
)


def did_not_run(event: Event) -> bool:
    """True for a tool call that never took effect: denied, rejected, blocked by a hook.

    A failed edit (``is_error``: "String to replace not found", a blocked Write) changed
    nothing. A command counts as not run only when it has no exit code and its output says it
    was denied or blocked; a command that ran and failed still ran.
    """
    if event.is_error is not True:
        return False
    if event.kind is EventKind.EDIT:
        return True
    return event.exit_code is None and bool(_DENIED.search(event.output))


def commands_run(ctx: Context) -> list[Event]:
    """COMMAND events that actually ran."""
    assert ctx.session is not None
    return [ev for ev in ctx.session.commands() if not did_not_run(ev)]


def edits_made(ctx: Context) -> list[Event]:
    """EDIT events that took effect."""
    assert ctx.session is not None
    return [ev for ev in ctx.session.edits() if not did_not_run(ev)]


def _repo_root(ctx: Context) -> Path | str | None:
    cwd = ctx.session.cwd if ctx.session else None
    return ctx.repo if ctx.repo is not None else cwd


def relpath(path: str, ctx: Context) -> str | None:
    """Repo-relative form of a path recorded in the transcript; None outside the repo."""
    cwd = ctx.session.cwd if ctx.session else None
    return paths.relpath_in_repo(path, _repo_root(ctx), cwd) or None


def edit_relpath(event: Event, ctx: Context) -> str | None:
    """Repo-relative path of an EDIT event, or None when it lies outside the repo."""
    return relpath(event.path, ctx) if event.path else None


def is_windows_repo(ctx: Context) -> bool:
    root = _repo_root(ctx)
    return root is not None and bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/[A-Za-z]/)", str(root)))


def path_key(ctx: Context) -> Callable[[str], str]:
    """Key for comparing repo paths: case-insensitive for a repo on a Windows drive."""
    return str.lower if is_windows_repo(ctx) else str


_CODEX_SHELL_TOOLS = frozenset(
    ["exec_command", "exec", "shell", "shell_command", "local_shell", "container.exec"]
)


def is_powershell(event: Event, ctx: Context | None = None) -> bool:
    """A command run by PowerShell: a PowerShell tool, or a Codex shell call in a session on
    a Windows drive (Codex runs commands in PowerShell there without recording the shell)."""
    tool = (event.tool or "").lower()
    if tool in ("powershell", "pwsh"):
        return True
    session = ctx.session if ctx else None
    return (
        session is not None
        and session.agent == "codex"
        and tool in _CODEX_SHELL_TOOLS
        and bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", session.cwd or ""))
    )


def counted_edits(
    ctx: Context,
    scope: str | None,
    edit_paths: Sequence[str] | None,
    ignore_edit_paths: Sequence[str] | None,
) -> list[tuple[Event, str | None]]:
    """Edits that reset an "after the last edit" window, in order.

    File-tool edits that took effect inside the repo and the scope, filtered by
    ``edit_paths`` / ``ignore_edit_paths``, plus shell commands that obviously write files
    (see ``shell_writes``). A shell write whose target is unknown (``sed -i``, ``git apply``,
    ``--fix``) has path None and counts only for unscoped rules without ``edit_paths``.
    """
    if ctx.session is None:
        return []

    def keep(rel: str) -> bool:
        if not paths.in_scope(rel, scope):
            return False
        if edit_paths and not globs_match(rel, edit_paths, scope):
            return False
        return not globs_match(rel, ignore_edit_paths, scope)

    out: list[tuple[Event, str | None]] = []
    for ev in ctx.session.events:
        if did_not_run(ev):
            continue
        if ev.kind is EventKind.EDIT:
            rel = edit_relpath(ev, ctx)
            if rel is not None and keep(rel):
                out.append((ev, rel))
        elif ev.kind is EventKind.COMMAND:
            for target in shell_writes(ev.text, is_powershell(ev, ctx)):
                if target is None:
                    if not scope and not edit_paths:
                        out.append((ev, None))
                        break
                    continue
                rel = relpath(target, ctx)
                if rel is not None and keep(rel):
                    out.append((ev, rel))
                    break
    return out


def ran_after(command: Event, edit: Event) -> bool:
    """A command ran after an edit; a command that both writes and runs counts as after."""
    if command.index == edit.index:
        return edit.kind is EventKind.COMMAND
    return command.index > edit.index


# --- shell masking ----------------------------------------------------------------------

_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)\\?([A-Za-z_][\w.-]*)\1")
_PS_HINT = re.compile(
    r"\b(?:Get|Set|New|Remove|Write|Invoke|Start|Stop|Test|Select|Where|ForEach|Out|Copy|Move"
    r"|Add|Clear|Import|Export|ConvertTo|ConvertFrom|Join|Split|Resolve|Measure|Sort|Format"
    r"|Push|Pop|Rename|Expand|Compress|Update|Wait|Read)-[A-Z][A-Za-z]+\b|\$env:"
)
_WORD_START = " \t\r\n;&|(){}"


class _Masker:
    """Blank quoted prose, heredoc bodies and comments, keeping string length and offsets.

    Quoted strings that contain whitespace are blanked (a commit message is not a command);
    short quoted words are kept. Command substitutions (``$(...)``, POSIX backticks) are
    scanned as commands even inside double quotes. Unterminated quotes are literal text.
    """

    def __init__(self, s: str, ps: bool) -> None:
        self.s, self.ps, self.out = s, ps, list(s)

    def blank(self, start: int, end: int) -> None:
        for k in range(start, min(end, len(self.s))):
            if self.s[k] != "\n":
                self.out[k] = " "

    def sq_end(self, i: int) -> int:
        """Closing quote of the single-quoted string at ``i``; -1 when unterminated."""
        s, j = self.s, i + 1
        while True:
            j = s.find("'", j)
            if j == -1:
                return -1
            if s.startswith("''", j):
                j += 2
            elif 0 < j < len(s) - 1 and s[j - 1].isalnum() and s[j + 1].isalnum():
                j += 1  # an apostrophe inside a word: don't, it's
            else:
                return j

    def scan(self, i: int, close: str | None) -> int:
        """Mask from ``i``; with ``close`` (``)`` or a backtick) return after the closer."""
        s, n = self.s, len(self.s)
        depth = 0
        pending: list[str] = []
        while i < n:
            c = s[i]
            if c == "\n" and pending:
                i = self.heredocs(i + 1, pending)
                pending = []
                continue
            if c == "\\" and not self.ps:
                i += 2
                continue
            if c == "`":
                if self.ps:
                    i += 2
                elif close == "`":
                    return i + 1
                else:
                    i = self.scan(i + 1, "`")
                continue
            word_start = i == 0 or s[i - 1] in _WORD_START
            if c == "#" and word_start:
                end = s.find("\n", i)
                end = n if end == -1 else end
                if close == ")" and ")" in s[i:end]:
                    end = i + s[i:end].index(")")
                self.blank(i, end)
                i = end
                continue
            if self.ps and s.startswith("<#", i):
                end = s.find("#>", i + 2)
                end = n if end == -1 else end + 2
                self.blank(i, end)
                i = end
                continue
            if c == "@" and re.match(r"@['\"]\r?\n", s[i : i + 4]):
                closer = "\n" + s[i + 1] + "@"
                j = s.find(closer, i + 2)
                self.blank(i + 2, n if j == -1 else j)
                i = n if j == -1 else j + len(closer)
                continue
            if s.startswith("$((", i):
                i = self.arithmetic(i + 3)
                continue
            if c == "'":
                if i > 0 and s[i - 1] == "$" and not self.ps:
                    i = self.ansi_c(i)
                    continue
                if 0 < i < n - 1 and s[i - 1].isalnum() and s[i + 1].isalnum():
                    i += 1
                    continue
                j = self.sq_end(i)
                if j == -1:
                    i += 1
                    continue
                if any(ch.isspace() for ch in s[i + 1 : j]):
                    self.blank(i + 1, j)
                i = j + 1
                continue
            if c == '"':
                i = self.dq(i)
                continue
            if c == "<" and not s.startswith("<<<", i) and (i == 0 or s[i - 1] != "<"):
                m = _HEREDOC.match(s, i)
                if m:
                    pending.append(m.group(2))
                    i = m.end()
                    continue
            if close == ")":
                if c == "(":
                    depth += 1
                elif c == ")":
                    if depth == 0:
                        return i + 1
                    depth -= 1
            i += 1
        return n

    def arithmetic(self, i: int) -> int:
        depth = 0
        while i < len(self.s):
            if self.s[i] == "(":
                depth += 1
            elif self.s[i] == ")":
                if depth == 0:
                    return i + 2 if self.s.startswith("))", i) else i + 1
                depth -= 1
            i += 1
        return i

    def ansi_c(self, i: int) -> int:
        """``$'...'``: backslash escapes are allowed inside."""
        k = i + 1
        while k < len(self.s) and self.s[k] != "'":
            k += 2 if self.s[k] == "\\" else 1
        if k >= len(self.s):
            return i + 1
        if any(ch.isspace() for ch in self.s[i + 1 : k]):
            self.blank(i + 1, k)
        return k + 1

    def dq(self, start: int) -> int:
        s, n = self.s, len(self.s)
        escape = "`" if self.ps else "\\"
        literal: list[int] = []
        i = start + 1
        while i < n and s[i] != '"':
            if s[i] == escape and i + 1 < n:
                literal += [i, i + 1]
                i += 2
            elif s.startswith("$(", i):
                i = self.scan(i + 2, ")")
            elif s[i] == "`" and not self.ps:
                i = self.scan(i + 1, "`")
            else:
                literal.append(i)
                i += 1
        if i >= n:  # unterminated: undo and treat the quote as a literal character
            for k in range(start, n):
                self.out[k] = s[k]
            return start + 1
        if any(s[k].isspace() for k in literal):
            for k in literal:
                if s[k] != "\n":
                    self.out[k] = " "
        return i + 1

    def heredocs(self, i: int, delimiters: list[str]) -> int:
        s, n = self.s, len(self.s)
        for delim in delimiters:
            while i < n:
                j = s.find("\n", i)
                end = n if j == -1 else j
                line = s[i:end].strip()
                if line == delim:
                    i = end + 1
                    break
                if line.startswith(delim) and line[len(delim)] in " \t);&|":
                    # ``EOF && git push``: not valid POSIX, but agents write it; keep the rest
                    return i + s[i:end].index(delim) + len(delim)
                self.blank(i, end)
                i = end + 1
        return min(i, n)


def _mask(command: str, ps: bool) -> str:
    m = _Masker(command, ps)
    m.scan(0, None)
    return "".join(m.out)


# --- nested commands --------------------------------------------------------------------

_POSIX_C = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:\S*[\\/])?(?:sh|bash|zsh|dash|ksh)(?:\.exe)?"
    r"(?:\s+(?:[-+]o\s+[\w-]+|--[\w-]+|-[abd-zA-Z]*o\s+[\w-]+|-[abd-zA-Z]+))*"
    r"\s+-[A-Za-z]*c[A-Za-z]*(?:\s+--)?\s+"
)
_PS_C = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:\S*[\\/])?(?:pwsh|powershell)(?:\.exe)?"
    r"(?:\s+-(?!c\b|command\b)[\w-]+(?:\s+(?:bypass|unrestricted|remotesigned|allsigned"
    r"|restricted|default|undefined|hidden|normal|minimized|maximized)\b)?)*"
    r"\s+-(?:c|command)\s+",
    re.IGNORECASE,
)
_CMD_C = re.compile(r"(?:^|(?<=[\s;&|(]))(?:\S*[\\/])?cmd(?:\.exe)?\s+/[cCkK]\s+")
_EVAL = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:eval|iex|Invoke-Expression|ssh(?:\s+-\w+(?:\s+[^\s\"'-]\S*)?)*"
    r"\s+[^\s\"'-]\S*)\s+",
    re.IGNORECASE,
)
_PIPE_SHELL = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:echo|printf)\s+([^|\n]*?)\|\s*(?:sudo\s+)?"
    r"(sh|bash|zsh|dash|pwsh|powershell)\b"
)
_SCRIPT_E = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:\S*[\\/])?(?:python[\d.]*|py|node|ruby|perl|php)(?:\.exe)?"
    r"(?:\s+-\w+)*?\s+-[ce]\s+"
)
_QUOTE_CHARS = re.compile(r"[\"']")
_MAX_NESTING = 4


@dataclass(frozen=True, slots=True)
class _View:
    masked: str  # same length as the source text; quote characters kept
    ps: bool
    raw: bool = False  # interpreter code (python -c): matched as written, never segmented


def _read_word(s: str, i: int, ps: bool) -> str:
    """The shell word at ``i``: a quoted string (unescaped) or the rest of the line."""
    if i >= len(s):
        return ""
    if s[i] == "'":
        j = s.find("'", i + 1)
        while j != -1 and s.startswith("''", j):
            j = s.find("'", j + 2)
        return s[i + 1 : len(s) if j == -1 else j].replace("''", "'")
    if s[i] == '"':
        buf: list[str] = []
        escape = "`" if ps else "\\"
        k = i + 1
        while k < len(s) and s[k] != '"':
            if s[k] == escape and k + 1 < len(s):
                buf.append(s[k + 1])
                k += 2
                continue
            buf.append(s[k])
            k += 1
        return "".join(buf)
    end = s.find("\n", i)
    return s[i:] if end == -1 else s[i:end]


def _views(command: str, ps: bool, depth: int = 0) -> list[_View]:
    ps = ps or bool(_PS_HINT.search(command))
    masked = _mask(command, ps)
    out = [_View(masked, ps)]
    if depth >= _MAX_NESTING:
        return out
    nested: list[tuple[str, bool]] = []
    for m in _POSIX_C.finditer(masked):
        nested.append((_read_word(command, m.end(), False), False))
    for m in _PS_C.finditer(masked):
        nested.append((_read_word(command, m.end(), True), True))
    for m in _CMD_C.finditer(masked):
        nested.append((_read_word(command, m.end(), False), False))
    for m in _EVAL.finditer(masked):
        nested.append((_read_word(command, m.end(), ps), ps))
    for m in _PIPE_SHELL.finditer(masked):
        start = m.start(1)
        word = _read_word(command, start, ps) if command[start] in "'\"" else m.group(1)
        nested.append((word, m.group(2).lower() in ("pwsh", "powershell")))
    for text, nested_ps in nested:
        if text.strip():
            out.extend(_views(text, nested_ps, depth + 1))
    for m in _SCRIPT_E.finditer(masked):
        code = _read_word(command, m.end(), ps)
        if code.strip():
            out.append(_View(code, ps, raw=True))
    return out


def command_texts(command: str, match_quoted: bool = False, powershell: bool = False) -> list[str]:
    """The texts a user's command pattern is searched in.

    With ``match_quoted`` the raw command line. Otherwise the command with comments, heredoc
    bodies, here-strings and quoted strings that contain whitespace blanked out (so a commit
    message mentioning ``git push`` is not a push), short quoted words unquoted (``"push"``
    -> ``push``), plus, recursively, the scripts run by ``sh/bash -c``, ``pwsh -Command``,
    ``cmd /c``, ``eval``, ``Invoke-Expression``, ``ssh host "..."`` and ``echo ... | sh``,
    and the code passed to ``python -c`` / ``node -e`` as written. PowerShell quoting
    (backtick escapes, ``''``, ``<# #>``) applies with ``powershell`` or when the command
    uses cmdlets.
    """
    if match_quoted:
        return [command]
    views = _views(command, powershell)
    return [v.masked if v.raw else _QUOTE_CHARS.sub("", v.masked) for v in views]


def command_matches(
    rx: re.Pattern[str], command: str, match_quoted: bool = False, powershell: bool = False
) -> bool:
    return any(rx.search(t) for t in command_texts(command, match_quoted, powershell))


# --- simple commands --------------------------------------------------------------------

_SEPARATOR = re.compile(r"&&|\|\||[;\n|(){}`]|(?<![<>&])&(?![>&])|\$\(")
_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
_PREFIX_1 = frozenset(
    ["npx", "bunx", "pnpx", "uvx", "sudo", "doas", "env", "time", "nice", "nohup", "command"]
    + ["exec", "xvfb-run", "stdbuf", "timeout", "pnpm", "yarn", "bun"]
)
_PREFIX_2 = frozenset(
    tuple(p.split())
    for p in (
        "uv run",
        "poetry run",
        "pdm run",
        "hatch run",
        "rye run",
        "pipenv run",
        "conda run",
        "pnpm exec",
        "pnpm dlx",
        "yarn dlx",
        "yarn exec",
        "npm exec",
        "bun x",
        "bundle exec",
    )
)
_PREFIX_VALUE_OPTIONS = frozenset(
    # Runner options that take a separate value: `uv run --project DIR pytest`.
    ["--project", "--directory", "--python", "-p", "--package", "--env-file", "--extra"]
    + ["--group", "--only-group", "--with", "--with-editable", "--with-requirements", "--index"]
    + ["--index-url", "--extra-index-url", "--config-file", "-C", "-P", "--filter", "-F"]
    + ["--dir", "--cwd", "--workspace", "-n", "--name", "--prefix", "-u", "--unset"]
    + ["--chdir", "-s", "--signal", "-k", "--kill-after"]
)
_PYTHON = re.compile(r"^(?:python[\d.]*|py)$")
_PYTHON_VALUE_OPTIONS = frozenset(["-X", "-W", "--check-hash-based-pycs"])
_NOT_A_RUN = frozenset(["--version", "-V", "--help", "-h", "--collect-only", "--co"])


# A backslash between path characters (``.venv\Scripts\pytest.exe``, ``C:\repo``) is a
# Windows path separator, not a POSIX escape: Codex runs Windows commands in PowerShell
# without saying so, and Git Bash keeps such paths working.
_WINDOWS_PATH = re.compile(r"(?:^|[\s=])(?:[A-Za-z]:|\.{1,2}|[\w.-]+)\\[\w.-]")


def _tokens(segment: str, ps: bool) -> list[str]:
    try:
        lex = shlex.shlex(segment, posix=True)
        lex.whitespace_split = True
        lex.commenters = ""
        if ps or _WINDOWS_PATH.search(segment):
            lex.escape = ""
        return list(lex)
    except ValueError:
        return segment.split()


def _program(token: str) -> str:
    name = re.split(r"[\\/]", token)[-1]
    return re.sub(r"\.(?:exe|cmd|bat|ps1)$", "", name, flags=re.IGNORECASE)


def simple_commands(command: str, powershell: bool = False) -> list[list[str]]:
    """The simple commands in a command line (and nested scripts), as word lists.

    Separators are ``&&``, ``||``, ``;``, ``|``, ``&``, newlines, parentheses, braces and
    command substitutions; quoted prose, comments and heredoc bodies are blanked first.
    """
    out: list[list[str]] = []
    for view in _views(command, powershell):
        if view.raw:
            continue
        for seg in _SEPARATOR.split(view.masked):
            words = [w for w in _tokens(seg, view.ps) if w]
            if any(w.strip() for w in words):
                out.append(words)
    return out


def _candidates(words: list[str]) -> Iterable[list[str]]:
    """``words`` and what remains after each runner prefix (``uv run``, ``npx``, ``env X=1``)."""
    pos = 0
    while pos < len(words):
        while pos < len(words) and _ASSIGNMENT.match(words[pos]):
            pos += 1
        if pos >= len(words):
            return
        yield [_program(words[pos]), *words[pos + 1 :]]
        prog = _program(words[pos]).lower()
        nxt = words[pos + 1] if pos + 1 < len(words) else ""
        if (prog, nxt) in _PREFIX_2:
            pos += 2
        elif _PYTHON.match(prog):
            # `python -X utf8 -m unittest`, `py -3 -m pytest`, `python manage.py test`
            j = pos + 1
            while j < len(words) and words[j].startswith("-") and words[j] not in ("-m", "-c"):
                j += 2 if words[j] in _PYTHON_VALUE_OPTIONS else 1
            if j < len(words) and words[j] == "-m":
                pos = j + 1
            elif j < len(words) and words[j].lower().endswith(".py"):
                pos = j
                yield [_program(words[pos]), *words[pos + 1 :]]
                return
            else:
                return
        elif prog in _PREFIX_1:
            pos += 1
        else:
            return
        while pos < len(words) and (words[pos].startswith("-") or words[pos][:1].isdigit()):
            pos += 1
            if words[pos - 1] in _PREFIX_VALUE_OPTIONS:
                pos += 1  # the option's value, never the tool
            if pos < len(words):
                yield [_program(words[pos]), *words[pos + 1 :]]


def invokes(rx: re.Pattern[str], command: str, powershell: bool = False) -> bool:
    """True when a simple command in ``command`` starts with a tool matching ``rx``.

    The tool must be in command position (optionally after env assignments and runner
    prefixes such as ``uv run``, ``python -m``, ``npx``, ``pnpm exec``, ``bundle exec``), so
    ``cat pytest.ini`` or ``uv add pytest`` do not count. Invocations that only print
    information (``--version``, ``--help``, ``--collect-only``) do not count either.
    """
    return bool(split_invocations(rx, command, powershell)[0])


def split_invocations(
    rx: re.Pattern[str], command: str, powershell: bool = False
) -> tuple[list[list[str]], list[list[str]]]:
    """(the invocations of the tool ``rx`` names, from the tool on; the other simple
    commands) in ``command``, with the matching rules of ``invokes``."""
    hits: list[list[str]] = []
    others: list[list[str]] = []
    for words in simple_commands(command, powershell):
        hit = next(
            (
                cand
                for cand in _invocations(words)
                if rx.match(" ".join(cand)) and not _NOT_A_RUN.intersection(cand[1:])
            ),
            None,
        )
        if hit is None:
            others.append(words)
        else:
            hits.append(hit)
    return hits, others


def _invocations(words: list[str]) -> Iterable[list[str]]:
    """``_candidates``, plus the command with a script path kept whole (``./scripts/test.sh``,
    ``.\\test.ps1``), so a pattern can name a script without matching the ``test`` builtin."""
    yield from _candidates(words)
    head = next((k for k, w in enumerate(words) if not _ASSIGNMENT.match(w)), None)
    if head is not None and re.search(r"[\\/]", words[head]):
        yield [words[head].replace("\\", "/"), *words[head + 1 :]]


# --- shell writes -----------------------------------------------------------------------

_REDIRECT = re.compile(r"(?<![<>&\d])\d?>>?\s*(?!&)([^\s;&|<>()]+)")
_NULL_TARGETS = frozenset(["/dev/null", "nul", "$null", "/dev/stdout", "/dev/stderr"])
_FORMAT_WRITES = re.compile(
    r"^(?:ruff format|black|isort|cargo fmt|rustfmt|gofmt -w|goimports -w|shfmt -w"
    r"|dotnet format|mix format|deno fmt)\b"
)


def shell_writes(command: str, powershell: bool = False) -> list[str | None]:
    """Files a shell command obviously writes; None for a write whose target is unknown.

    Conservative: output redirects (``>``, ``>>``) and ``tee`` to files, ``sed -i`` /
    ``perl -i``, ``git apply`` / ``git am`` / ``patch``, ``git checkout -- ...`` /
    ``git restore``, ``--fix`` / ``--write`` flags and formatters that rewrite files by
    default (``ruff format``, ``black``, ``cargo fmt``, ``gofmt -w``) unless run with
    ``--check`` or ``--diff``. Writes by scripts or other programs are not seen.
    """
    out: list[str | None] = []
    for view in _views(command, powershell):
        if view.raw:
            continue
        text = _QUOTE_CHARS.sub("", view.masked)
        for m in _REDIRECT.finditer(text):
            target = m.group(1)
            if target.lower() not in _NULL_TARGETS and not target.startswith("$"):
                out.append(target)
    for words in simple_commands(command, powershell):
        for cand in _candidates(words):
            prog, args = cand[0].lower(), cand[1:]
            line = " ".join(cand)
            if prog == "tee":
                out.extend(a for a in args if not a.startswith("-"))
            elif prog in ("sed", "perl") and any(re.match(r"^-[a-zA-Z]*i", a) for a in args):
                operands: list[str] = []
                script_flag = skip = False
                for a in args:
                    if skip:
                        skip = False
                    elif a in ("-e", "-f", "--expression", "--file"):
                        script_flag = skip = True
                    elif not a.startswith("-"):
                        operands.append(a)
                files = operands if script_flag else operands[1:]
                out.extend([f for f in files if f.strip()] or [None])
            elif re.match(r"^(?:git (?:apply|am|restore|checkout --)|patch)\b", line):
                out.append(None)
            elif "--check" in args or "--diff" in args:
                continue
            elif "--fix" in args or "--write" in args or _FORMAT_WRITES.match(line):
                out.append(None)
            else:
                continue
            break
    return out


# --- outcomes ---------------------------------------------------------------------------


def command_outcome(event: Event) -> tuple[Outcome, str]:
    """Pass/fail/unverified for a command event, with a short reason such as ``exit 0``.

    Output that shows failures wins over exit 0 (``pytest | tail`` exits 0).
    """
    verdict = output_verdict(event.output)
    if event.exit_code == 0:
        if verdict is False:
            return "fail", "exit 0 but output shows failures"
        return "pass", "exit 0"
    if event.exit_code is not None:
        return "fail", f"exit {event.exit_code}"
    if event.is_error:
        return "fail", "reported as failed"
    if verdict is True:
        return "pass", "exit code unknown, output shows success"
    if verdict is False:
        return "fail", "exit code unknown, output shows failures"
    return "unverified", "exit code unknown"


_M = re.MULTILINE
# Each signal names the kind of tool that prints it, so the output of a chained command
# (``ruff check . && ruff format --check .``) can be attributed (see ``output_verdict``).
_FAILURE: list[tuple[str, re.Pattern[str]]] = [
    ("tests", re.compile(r"^.*\b[1-9]\d* (?:failed|errors?)\b.* in [\d.]+s\b", _M)),  # pytest
    ("tests", re.compile(r"\bno tests ran\b")),  # pytest: nothing collected
    ("tests", re.compile(r"^(?:FAILED|ERROR)\b", _M)),  # pytest short summary, unittest
    ("tests", re.compile(r"^\S+::\S+ (?:FAILED|ERROR)\b", _M)),  # pytest -v
    ("tests", re.compile(r"Required test coverage of .* not reached|\bfail-under=", _M)),
    ("tests", re.compile(r"^\s*Tests?:?\s+.*\b[1-9]\d* failed\b", _M)),  # jest / vitest
    ("tests", re.compile(r"^\s*Test Files\s+.*\b[1-9]\d* failed\b", _M)),  # vitest
    ("tests", re.compile(r"^(?:FAIL\b|--- FAIL:)", _M)),  # go test
    ("tests", re.compile(r"test result: FAILED\b")),  # cargo test
    (
        "build",
        re.compile(r"^error(?:\[E\d{4}\])?: (?:could not compile|aborting)|^error\[E\d{4}\]", _M),
    ),
    ("types", re.compile(r"^Found [1-9]\d* errors? in \d+ files?", _M)),  # mypy
    ("format", re.compile(r"^\d+ files? would be reformatted|^Would reformat:", _M)),  # ruff/black
    ("types", re.compile(r"\berror TS\d+:")),  # tsc
    ("lint", re.compile(r"\b\d+ problems? \([1-9]\d* errors?", _M)),  # eslint
    ("build", re.compile(r"\bBUILD FAIL(?:ED|URE)\b|^Build FAILED\.", _M)),  # gradle, maven, dotnet
    ("tests", re.compile(r"^Failed!\s+-\s+Failed:\s+[1-9]", _M)),  # dotnet test
    ("", re.compile(r"^npm (?:ERR!|error) ", _M)),
]
_RUFF_FOUND = re.compile(r"^Found (\d+) errors?(?: \((\d+) fixed, (\d+) remaining\))?", _M)
_SUCCESS: list[tuple[str, re.Pattern[str]]] = [
    ("tests", re.compile(r"^.*\b[1-9]\d* passed\b.* in [\d.]+s\b", _M)),  # pytest summary
    ("tests", re.compile(r"^\s*Tests?:?\s+.*\b[1-9]\d* passed\b", _M)),  # jest / vitest
    ("tests", re.compile(r"^ok\s+\S+\s+(?:[\d.]+s|\(cached\))", _M)),  # go test
    ("tests", re.compile(r"test result: ok\. [1-9]")),  # cargo test
    ("lint", re.compile(r"^All checks passed!", _M)),  # ruff
    ("types", re.compile(r"^Success: no issues found", _M)),  # mypy
    ("build", re.compile(r"\bBUILD SUCCESS(?:FUL)?\b|^Build succeeded\.", _M)),
    ("build", re.compile(r"^\s*Finished\b.*\btarget\(s\) in\b", _M)),  # cargo build
    ("tests", re.compile(r"^Passed!\s+-\s+Failed:\s+0\b", _M)),  # dotnet test
    ("format", re.compile(r"^\d+ files? (?:already formatted|(?:would be )?left unchanged)", _M)),
]
_UNITTEST_OK = re.compile(r"^Ran [1-9]\d* tests? in [\d.]+s\s*\n+\s*OK\b", _M)


def output_verdict(output: str, kind: str | None = None) -> bool | None:
    """True/False when command output unambiguously shows success/failure, else None.

    Recognises the summary lines of pytest (and pytest-cov), unittest, jest, vitest, go
    test, cargo, ruff, black, mypy, tsc, eslint, gradle, maven and dotnet. Any failure
    signal wins over success; "no tests ran" is a failure and "0 passed" is not a success.
    With ``kind`` (``tests``, ``lint``, ``types``, ``format``, ``build``) only the signals
    printed by that kind of tool are read: for one command of a chain.
    """
    if not output:
        return None

    def mine(tag: str) -> bool:
        return kind is None or tag == kind

    if any(mine(tag) and rx.search(output) for tag, rx in _FAILURE):
        return False
    ruff = list(_RUFF_FOUND.finditer(output)) if mine("lint") else []
    for m in ruff:
        remaining = m.group(3)
        if remaining is None or int(remaining) > 0:
            return False
    if mine("tests") and _UNITTEST_OK.search(output):
        return True
    if any(mine(tag) and rx.search(output) for tag, rx in _SUCCESS):
        return True
    if any(m.group(3) == "0" for m in ruff):
        return True
    return None
