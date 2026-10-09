"""Extract tool calls from Codex "code mode" cells (JavaScript calling ``tools.X(...)``).

Current Codex runs commands through a JavaScript cell such as::

    const r = await tools.exec_command({cmd: "pytest -q", workdir: "C:/repo"});
    await tools.apply_patch("*** Begin Patch\\n*** Update File: a.py\\n...");

This module does not evaluate JavaScript. It masks string literals and comments, finds
``tools.<name>(`` calls in the remaining code, reads ``cmd:`` / ``workdir:`` string literals
inside each call, and collects ``*** Add/Update/Delete File:`` headers from every string literal
of a cell that calls ``apply_patch``.
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
    command: str | None = None  # exec_command: the decoded ``cmd`` string literal
    workdir: str | None = None


@dataclass(slots=True)
class Cell:
    calls: list[Call] = field(default_factory=list)
    patches: list[tuple[EditAction, str]] = field(default_factory=list)  # (action, path)


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


def _matching_paren(masked: str, open_at: int) -> int:
    depth = 0
    for k in range(open_at, len(masked)):
        ch = masked[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return k
    return len(masked)


def _literal_after(
    masked: str, literals: list[Literal], key: str, start: int, end: int
) -> str | None:
    """The string value of ``key`` in an object literal between ``start`` and ``end``.

    Handles ``{cmd: "..."}``, ``{"cmd": "..."}``, ``{cmd: name}`` and ``{cmd}`` where ``name``
    (or ``cmd``) is a ``const``/``let``/``var`` initialised with a string literal earlier on.
    """
    value_at: int | None = None
    m = re.compile(rf"(?<![\w$.]){key}\s*(?::\s*|(?=[,}}]))").search(masked, start, end)
    if m and not m.group(0).rstrip().endswith(":"):
        return _variable(masked, literals, key, start)
    if m:
        value_at = m.end()
    else:  # quoted key: {"cmd": ...}
        for lit in literals:
            if start <= lit.start < end and lit.value == key:
                sep = _COLON_RE.match(masked, lit.end)
                if sep:
                    value_at = sep.end()
                    break
    if value_at is None:
        return None
    literal = next((lit.value for lit in literals if lit.start == value_at), None)
    if literal is not None:
        return literal
    ident = _IDENT_RE.match(masked, value_at)
    return _variable(masked, literals, ident.group(0), start) if ident else None


def _variable(masked: str, literals: list[Literal], name: str, before: int) -> str | None:
    decls = list(re.finditer(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*", masked[:before]))
    if not decls:
        return None
    value_at = decls[-1].end()
    return next((lit.value for lit in literals if lit.start == value_at), None)


def parse_cell(source: str) -> Cell:
    masked, literals = scan(source)
    cell = Cell()
    for m in _CALL_RE.finditer(masked):
        open_at = m.end() - 1
        close = _matching_paren(masked, open_at)
        call = Call(tool=m.group(1), args=source[open_at + 1 : close])
        if call.tool == "exec_command":
            call.command = _literal_after(masked, literals, "cmd", open_at, close)
            call.workdir = _literal_after(masked, literals, "workdir", open_at, close)
        cell.calls.append(call)
    if any(c.tool == "apply_patch" for c in cell.calls):
        cell.patches = patch_files("\n".join(lit.value for lit in literals))
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
