"""Unwrap shell invocations (``bash -lc '...'``, ``pwsh -Command ...``, ``cmd /c ...``).

Agents often record ``["bash", "-lc", "pytest -q"]`` or ``powershell.exe -NoProfile -Command
"pytest -q"`` where the interesting part is the inner script. ``unwrap`` returns that script,
or the command unchanged when it is not a recognised wrapper.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable, Sequence

from ruleproof.models import OUTPUT_LIMIT

_POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "ash"}
_POWERSHELLS = {"powershell", "pwsh"}
_Rest = Callable[[int], str]  # raw remainder of the command line from token i

_PS_VALUE_FLAGS = {
    "executionpolicy",
    "ep",
    "ex",
    "windowstyle",
    "w",
    "workingdirectory",
    "wd",
    "inputformat",
    "outputformat",
    "of",
    "configurationname",
    "version",
    "psconsolefile",
    "custompipename",
    "settingsfile",
}


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """Cap ``text`` at ``limit`` characters, keeping its head and tail."""
    if len(text) <= limit:
        return text
    marker = f"\n… [{len(text) - limit} chars truncated] …\n"
    keep = max(limit - len(marker), 0)
    head = keep * 2 // 3
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def unwrap(command: str | Sequence[str]) -> str:
    """The inner script of a wrapped shell command (applied repeatedly for nested wrappers)."""
    if isinstance(command, str):
        text = command.strip()
    else:
        argv = [str(a) for a in command]
        inner = _unwrap_argv(argv)
        text = inner if inner is not None else _join(argv)
    for _ in range(3):
        inner = _unwrap_string(text)
        if inner is None or inner == text:
            break
        text = inner.strip()
    return text


def _exe_name(token: str) -> str:
    name = re.split(r"[\\/]", token)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _join(argv: list[str]) -> str:
    return " ".join(f'"{a}"' if (not a or any(c.isspace() for c in a)) else a for a in argv)


def _unwrap_argv(argv: list[str]) -> str | None:
    tokens = [(a, 0, 0) for a in argv]
    return _unwrap_tokens(tokens, lambda i: " ".join(argv[i:]))


def _unwrap_string(text: str) -> str | None:
    tokens = _tokenize(text)
    if not tokens:
        return None
    return _unwrap_tokens(tokens, lambda i: text[tokens[i][1] :])


def _unwrap_tokens(tokens: list[tuple[str, int, int]], rest: _Rest) -> str | None:
    exe = _exe_name(tokens[0][0])
    if exe in _POSIX_SHELLS:
        return _posix(tokens)
    if exe in _POWERSHELLS:
        return _powershell(tokens, rest, positional_is_command=exe == "powershell")
    if exe == "cmd":
        return _cmd(tokens, rest)
    return None


def _posix(tokens: list[tuple[str, int, int]]) -> str | None:
    i, seen_c = 1, False
    while i < len(tokens):
        tok = tokens[i][0]
        if tok in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if tok.startswith("--") and len(tok) > 2:
            i += 1
            continue
        if tok.startswith(("-", "+")) and len(tok) > 1 and tok[1:].isalpha():
            seen_c = seen_c or "c" in tok[1:]
            i += 1
            continue
        return tok if seen_c else None
    return None


def _powershell(
    tokens: list[tuple[str, int, int]], rest: _Rest, *, positional_is_command: bool
) -> str | None:
    i = 1
    while i < len(tokens):
        tok = tokens[i][0]
        if not tok.startswith(("-", "/")) or len(tok) < 2:
            return _strip_outer_quotes(rest(i)) if positional_is_command else None
        name = tok.lstrip("-/").lower()
        if name in ("c", "command") or (len(name) >= 3 and "command".startswith(name)):
            return _strip_outer_quotes(rest(i + 1)) if i + 1 < len(tokens) else None
        if name in ("e", "ec") or (len(name) >= 2 and "encodedcommand".startswith(name)):
            return _decode_ps(tokens[i + 1][0]) if i + 1 < len(tokens) else None
        if name in ("f", "file"):
            return None
        i += 2 if name in _PS_VALUE_FLAGS else 1
    return None


def _cmd(tokens: list[tuple[str, int, int]], rest: _Rest) -> str | None:
    for i, (tok, _, _) in enumerate(tokens[1:], 1):
        low = tok.lower()
        if low in ("/c", "/k", "/s/c", "/d/c"):
            return _strip_outer_quotes(rest(i + 1)) if i + 1 < len(tokens) else None
        if not low.startswith("/"):
            return None
    return None


def _strip_outer_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        inner = s[1:-1]
        if s[0] not in inner.replace('\\"', "").replace('""', ""):
            return inner.replace('\\"', '"') if s[0] == '"' else inner
    return s


def _decode_ps(value: str) -> str | None:
    try:
        return base64.b64decode(value, validate=True).decode("utf-16-le")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _tokenize(text: str) -> list[tuple[str, int, int]]:
    """Split a command line into (unquoted token, start, end), POSIX-style quoting."""
    tokens: list[tuple[str, int, int]] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        start, buf = i, []
        while i < n and not text[i].isspace():
            c = text[i]
            if c == "'":
                j = text.find("'", i + 1)
                if j == -1:
                    j = n
                buf.append(text[i + 1 : j])
                i = j + 1
            elif c == '"':
                i += 1
                while i < n and text[i] != '"':
                    if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
                        i += 1
                    buf.append(text[i])
                    i += 1
                i += 1
            else:
                buf.append(c)
                i += 1
        tokens.append(("".join(buf), start, min(i, n)))
    return tokens
