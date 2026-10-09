"""Path normalisation and gitignore-style glob matching.

Glob rules (the same as ``.gitignore``, minus negation):

- Paths are posix and repo-relative (``src/app.py``), never starting with ``./`` or ``/``.
- A pattern matches a path or anything below it: ``api/gen`` matches ``api/gen`` and
  ``api/gen/x.go``; ``*.py`` matches ``a.py`` and everything inside a directory ``x.py/``.
- A pattern without a ``/`` (other than a trailing one) matches at any depth: ``*.bak``
  matches ``a/b/x.bak``, ``examples`` and ``examples/`` match ``pkg/examples/demo.md``.
- A pattern with a ``/`` at the start or in the middle is anchored at the repo root:
  ``src/*.py`` matches ``src/a.py`` but not ``lib/src/a.py``; ``/README.md`` only the root one.
- ``**`` matches zero or more whole directories; ``*`` and ``?`` never cross ``/``.
- ``[abc]``, ``[a-z]`` and ``[!abc]`` are character classes; a ``]`` right after ``[`` or
  ``[!`` is literal, and an unclosed ``[`` is an error. To match a literal ``*``, ``?`` or
  ``[``, put it in a class: ``[*]``, ``[?]``, ``[[]``.
  Backslash is not an escape character: it is read as a path separator (Windows paths).
- A trailing ``/`` is accepted and means the same as without it (directories and files are
  not distinguished, because diffs and transcripts only list files).
- Matching is case-sensitive.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath


class GlobError(ValueError):
    """An invalid glob pattern, e.g. an empty or reversed character class."""


@functools.lru_cache(maxsize=1024)
def compile_glob(pattern: str) -> re.Pattern[str]:
    """Regex for ``pattern`` (see the module docstring); raises ``GlobError`` if invalid."""
    pat = pattern.strip().replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    pat = pat.rstrip("/") if pat.strip("/") else pat
    anchored = "/" in pat
    pat = pat.lstrip("/")
    if not pat:
        raise GlobError(f"empty glob pattern {pattern!r}")
    if not anchored:
        pat = "**/" + pat

    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**", i):
                at_seg_start = i == 0 or pat[i - 1] == "/"
                at_seg_end = i + 2 == n or pat[i + 2] == "/"
                if at_seg_start and at_seg_end:
                    if i + 2 == n:
                        out.append(".*")  # trailing **: anything below
                        i += 2
                    else:
                        out.append("(?:[^/]*/)*")  # **/ : zero or more directories
                        i += 3
                    continue
                out.append("[^/]*")  # ** inside a segment acts like *
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            cls, i = _char_class(pat, i, pattern)
            out.append(cls)
            continue
        else:
            out.append(re.escape(c))
        i += 1
    try:
        return re.compile("".join(out) + r"(?:/.*)?\Z")  # also everything below a match
    except re.error as exc:
        raise GlobError(f"invalid glob pattern {pattern!r}: {exc.msg}") from None


def _char_class(pat: str, i: int, pattern: str) -> tuple[str, int]:
    """Translate the class starting at ``pat[i] == "["``; return (regex, next index)."""
    j = i + 1
    negate = j < len(pat) and pat[j] in "!^"
    if negate:
        j += 1
    start = j
    if j < len(pat) and pat[j] == "]":
        j += 1  # a leading ] is literal
    close = pat.find("]", j)
    if close == -1:
        raise GlobError(
            f"invalid glob pattern {pattern!r}: unclosed character class; write [[] to match "
            "a literal ["
        )
    body = pat[start:close]
    parts: list[str] = []
    k = 0
    while k < len(body):
        if k + 2 < len(body) and body[k + 1] == "-":
            lo, hi = body[k], body[k + 2]
            if lo > hi:
                raise GlobError(f"invalid glob pattern {pattern!r}: reversed range {lo}-{hi}")
            parts.append(f"{re.escape(lo)}-{re.escape(hi)}")
            k += 3
        else:
            parts.append(re.escape(body[k]))
            k += 1
    if "/" in body:
        raise GlobError(f"invalid glob pattern {pattern!r}: '/' inside a character class")
    return "[" + ("^/" if negate else "") + "".join(parts) + "]", close + 1


def glob_match(path: str, pattern: str) -> bool:
    return compile_glob(pattern).match(normalize(path)) is not None


def match_any(path: str, patterns: Iterable[str]) -> bool:
    p = normalize(path)
    return any(compile_glob(pat).match(p) is not None for pat in patterns)


def normalize(path: str) -> str:
    """Repo-relative posix form of an already-relative path."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def in_scope(path: str, scope: str | None) -> bool:
    """True when ``path`` is under the directory ``scope`` (or scope is None/empty)."""
    if not scope:
        return True
    s = normalize(scope).rstrip("/")
    p = normalize(path)
    return p == s or p.startswith(s + "/")


def _is_windows_abs(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")


def _msys_to_windows(path: str) -> str:
    """``/c/Users/x`` (Git Bash / MSYS) -> ``C:/Users/x``."""
    m = re.match(r"^/([A-Za-z])(/.*)?$", path)
    if m:
        return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return path


def relpath_in_repo(path: str, repo: Path | str | None, cwd: str | None = None) -> str | None:
    """Map a path recorded in a transcript to a repo-relative posix path.

    Relative paths are resolved against ``cwd`` (the agent's working directory) when given,
    else against ``repo``. Returns None when the path lies outside the repo. Comparison is
    case-insensitive for Windows paths and understands MSYS ``/c/...`` paths. ``~/...`` is
    expanded with ``HOME`` (or ``USERPROFILE``) from the environment, never the filesystem.
    """
    if repo is None:
        return normalize(path) if not _looks_absolute(path) else None
    repo_s = str(repo)
    windows = _is_windows_abs(repo_s) or _is_windows_abs(path) or os.name == "nt"
    raw = path.strip().strip('"').strip("'")
    if raw.startswith("~"):
        expanded = _expand_home(raw)
        if expanded is None:
            return None
        raw = expanded
    if windows:
        raw = _msys_to_windows(raw)
        repo_s = _msys_to_windows(repo_s.replace("\\", "/"))
        base = _msys_to_windows(cwd.replace("\\", "/")) if cwd else repo_s
        pw = PureWindowsPath(raw)
        if not pw.is_absolute():
            pw = PureWindowsPath(base) / pw
        full = _resolve_dots(str(pw).replace("\\", "/"))
        root = _resolve_dots(str(PureWindowsPath(repo_s)).replace("\\", "/")).rstrip("/")
        if full.lower() == root.lower():
            return ""
        if full.lower().startswith(root.lower() + "/"):
            return full[len(root) + 1 :]
        return None
    pp = PurePosixPath(raw)
    if not pp.is_absolute():
        pp = PurePosixPath(cwd or repo_s) / pp
    full = _resolve_dots(str(pp))
    root = _resolve_dots(str(PurePosixPath(repo_s))).rstrip("/")
    if full == root:
        return ""
    if full.startswith(root + "/"):
        return full[len(root) + 1 :]
    return None


def _expand_home(path: str) -> str | None:
    """``~/x`` -> ``$HOME/x`` from the environment only; None for ``~user`` or no home."""
    if not re.match(r"~(?:[/\\]|$)", path):
        return None
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not home:
        return None
    return home.rstrip("/\\") + path[1:]


def _looks_absolute(path: str) -> bool:
    return path.startswith(("/", "\\", "~")) or _is_windows_abs(path)


def _resolve_dots(path: str) -> str:
    """Collapse ``.`` and ``..`` segments without touching the filesystem."""
    prefix = ""
    m = re.match(r"^([A-Za-z]:)?/", path)
    if m:
        prefix = m.group(0)
        path = path[len(prefix) :]
    parts: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return prefix + "/".join(parts)
