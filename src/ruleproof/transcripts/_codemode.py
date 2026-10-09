"""Extract tool calls from Codex "code mode" cells (JavaScript calling ``tools.X(...)``).

Current Codex runs commands through a JavaScript cell such as::

    const r = await tools.exec_command({cmd: "pytest -q", workdir: "C:/repo"});
    await tools.apply_patch("*** Begin Patch\\n*** Update File: a.py\\n...");

This module does not run JavaScript. It masks string literals and comments, finds
``tools.<name>(`` calls in the remaining code, and statically evaluates the top-level ``cmd`` /
``workdir`` of an ``exec_command`` object argument and the patch passed to ``apply_patch``:
string literals, ``+`` concatenations of them, ``[...].join(sep)``, or a variable initialised
with one of those. Anything else is left unresolved (None) rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ruleproof.models import EditAction

_CALL_RE = re.compile(r"(?<![\w$.])tools\s*\.\s*([A-Za-z_$][\w$]*)\s*\(")
_PATCH_HEADER_RE = re.compile(
    r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.MULTILINE
)
_HEADER_ACTIONS: dict[str, EditAction] = {"Add": "add", "Update": "modify", "Delete": "delete"}
_JOIN_RE = re.compile(r"\s*\.\s*join\s*\(\s*")
_CLOSE_RE = re.compile(r"\s*\)")
_COLON_RE = re.compile(r"\s*:\s*")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*(?![\w$.(\[])")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


@dataclass(slots=True)
class Literal:
    start: int  # offset of the opening quote
    end: int  # offset just past the closing quote
    value: str  # decoded value (template ``${...}`` parts kept verbatim)


@dataclass(slots=True)
class Call:
    tool: str
    args: str  # source of the argument list, literals included
    command: str | None = None  # exec_command: the ``cmd`` value, if statically known
    workdir: str | None = None
    patch: str | None = None  # apply_patch: the patch text, if statically known


@dataclass(slots=True)
class Cell:
    calls: list[Call] = field(default_factory=list)


def scan(source: str) -> tuple[str, list[Literal]]:
    """Return ``source`` with literal bodies and comments blanked, plus the literals."""
    out = list(source)
    literals: list[Literal] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c in "\"'`":
            j, value = _read_string(source, i)
            literals.append(Literal(i, j, value))
            for k in range(i + 1, max(j - 1, i + 1)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif source.startswith("//", i):
            j = source.find("\n", i)
            j = n if j == -1 else j
            out[i:j] = " " * (j - i)
            i = j
        elif source.startswith("/*", i):
            j = source.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in source[i:j]]
            i = j
        else:
            i += 1
    return "".join(out), literals


def _read_string(source: str, i: int) -> tuple[int, str]:
    quote, n = source[i], len(source)
    buf: list[str] = []
    j = i + 1
    while j < n:
        c = source[j]
        if c == quote:
            return j + 1, "".join(buf)
        if c == "\\" and j + 1 < n:
            j, decoded = _escape(source, j + 1)
            buf.append(decoded)
            continue
        if c == "\n" and quote != "`":
            return j, "".join(buf)  # unterminated: stop at the line end
        buf.append(c)
        j += 1
    return n, "".join(buf)


def _escape(source: str, j: int) -> tuple[int, str]:
    c = source[j]
    if c in _ESCAPES and not (c == "0" and j + 1 < len(source) and source[j + 1].isdigit()):
        return j + 1, _ESCAPES[c]
    if c == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", source[j + 1 : j + 3]):
        return j + 3, chr(int(source[j + 1 : j + 3], 16))
    if c == "u":
        m = re.match(r"\{([0-9A-Fa-f]{1,6})\}|([0-9A-Fa-f]{4})", source[j + 1 :])
        if m:
            code = int(m.group(1) or m.group(2), 16)
            return j + 1 + m.end(), chr(code) if code <= 0x10FFFF else ""
    if c == "\r" and source.startswith("\r\n", j):
        return j + 2, ""
    if c == "\n":
        return j + 1, ""
    return j + 1, c


def _matching(masked: str, open_at: int, end: int | None = None) -> int:
    """Offset of the bracket closing the one at ``open_at`` (or ``end`` if unbalanced)."""
    stop = len(masked) if end is None else end
    depth = 0
    for k in range(open_at, stop):
        ch = masked[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return k
    return stop


class _Resolver:
    """Static evaluation of the few expression shapes agents use for commands and patches."""

    def __init__(self, masked: str, literals: list[Literal]) -> None:
        self.masked = masked
        self.literals = {lit.start: lit for lit in literals}

    def split(self, start: int, end: int) -> list[tuple[int, int]]:
        """Spans of the comma-separated items between ``start`` and ``end`` (top level only)."""
        spans: list[tuple[int, int]] = []
        depth, item = 0, start
        for k in range(start, end):
            ch = self.masked[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                spans.append((item, k))
                item = k + 1
        spans.append((item, end))
        return [(s, e) for s, e in (self.trim(s, e) for s, e in spans) if s < e]

    def trim(self, start: int, end: int) -> tuple[int, int]:
        while start < end and self.masked[start].isspace():
            start += 1
        while end > start and self.masked[end - 1].isspace():
            end -= 1
        return start, end

    def props(self, start: int, end: int) -> dict[str, tuple[int, int]]:
        """Top-level properties of the object literal spanning ``start``..``end`` (braces incl.)."""
        out: dict[str, tuple[int, int]] = {}
        for s, e in self.split(start + 1, end - 1):
            lit = self.literals.get(s)
            if lit is not None:
                key, after = lit.value, lit.end
            else:
                m = _IDENT_RE.match(self.masked, s, e)
                if m is None:
                    continue  # spread, computed key, method
                key, after = m.group(0), m.end()
            colon = _COLON_RE.match(self.masked, after, e)
            if colon:
                out.setdefault(key, self.trim(colon.end(), e))
            elif after == e and lit is None:
                out.setdefault(key, (s, e))  # shorthand {cmd}
        return out

    def value(self, start: int, end: int, depth: int = 0) -> str | None:
        """The string an expression evaluates to, or None when it cannot be known statically.

        Understood: a literal, literals joined with ``+``, ``[...].join(sep)``, and a variable
        declared earlier with one of those.
        """
        start, end = self.trim(start, end)
        if depth > 3 or start >= end:
            return None
        if self.masked[start] == "[":
            return self._joined(start, end, depth)
        parts: list[str] = []
        pos = start
        while True:
            lit = self.literals.get(pos)
            if lit is not None:
                if self.masked[pos] == "`" and "${" in lit.value:
                    return None  # template with interpolation
                parts.append(lit.value)
                pos = lit.end
            else:
                m = _IDENT_RE.match(self.masked, pos, end)
                part = self.variable(m.group(0), start, depth + 1) if m else None
                if m is None or part is None:
                    return None
                parts.append(part)
                pos = m.end()
            pos, _ = self.trim(pos, end)
            if pos == end:
                return "".join(parts)
            if self.masked[pos] != "+":
                return None
            pos, _ = self.trim(pos + 1, end)

    def _joined(self, start: int, end: int, depth: int) -> str | None:
        close = _matching(self.masked, start, end)
        m = _JOIN_RE.match(self.masked, close + 1, end)
        sep = self.literals.get(m.end()) if m else None
        tail = _CLOSE_RE.match(self.masked, sep.end, end) if sep else None
        if sep is None or tail is None or tail.end() != end:
            return None
        items = [self.value(s, e, depth + 1) for s, e in self.split(start + 1, close)]
        if any(item is None for item in items):
            return None
        return sep.value.join(item for item in items if item is not None)

    def variable(self, name: str, before: int, depth: int) -> str | None:
        decl_re = re.compile(rf"(?<![\w$.])(?:const|let|var)\s+{re.escape(name)}\s*=\s*")
        decls = list(decl_re.finditer(self.masked, 0, before))
        if not decls:
            return None
        start = decls[-1].end()
        end = start
        depth_ = 0
        while end < len(self.masked):
            ch = self.masked[end]
            if ch in "([{":
                depth_ += 1
            elif ch in ")]}":
                if depth_ == 0:
                    break
                depth_ -= 1
            elif ch in ";\n" and depth_ == 0:
                break
            end += 1
        return self.value(start, end, depth)

    def argument(
        self, open_at: int, close: int, keys: tuple[str, ...], *, plain: bool = False
    ) -> str | None:
        """The first call argument's string value: the first of ``keys`` when it is an object
        literal, or the argument itself when ``plain``."""
        spans = self.split(open_at + 1, close)
        if not spans:
            return None
        s, e = spans[0]
        if self.masked[s] == "{":
            props = self.props(s, _matching(self.masked, s, e) + 1)
            span = next((props[k] for k in keys if k in props), None)
            return self.value(*span) if span else None
        return self.value(s, e) if plain else None


def parse_cell(source: str) -> Cell:
    masked, literals = scan(source)
    resolver = _Resolver(masked, literals)
    cell = Cell()
    for m in _CALL_RE.finditer(masked):
        open_at = m.end() - 1
        close = _matching(masked, open_at)
        call = Call(tool=m.group(1), args=source[open_at + 1 : close])
        if call.tool == "exec_command":
            call.command = resolver.argument(open_at, close, ("cmd",))
            call.workdir = resolver.argument(open_at, close, ("workdir",))
        elif call.tool == "apply_patch":
            call.patch = resolver.argument(open_at, close, ("patch", "input"), plain=True)
        cell.calls.append(call)
    return cell


def patch_files(patch: str) -> list[tuple[EditAction, str]]:
    """``(action, path)`` for each file header of an apply_patch text.

    ``action`` is ``add``, ``modify`` or ``delete``; a ``*** Move to:`` line turns the preceding
    update into a delete of the old path plus an add of the new one.
    """
    out: list[tuple[EditAction, str]] = []
    for m in _PATCH_HEADER_RE.finditer(patch.replace("\r\n", "\n")):
        if m.group(3):
            if out and out[-1][0] == "modify":
                out[-1] = ("delete", out[-1][1])
            out.append(("add", m.group(3)))
            continue
        out.append((_HEADER_ACTIONS[m.group(1)], m.group(2)))
    return out
