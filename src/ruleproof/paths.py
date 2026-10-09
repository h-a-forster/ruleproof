"""Path normalisation and gitignore-style glob matching.

Glob rules:

- Paths are posix and repo-relative (``src/app.py``), never starting with ``./`` or ``/``.
- A pattern without ``/`` matches the basename at any depth: ``*.bak`` matches ``a/b/x.bak``.
- A pattern with ``/`` is anchored at the repo root: ``src/*.py`` matches ``src/a.py`` only.
- ``**`` matches zero or more whole directories; ``*`` and ``?`` never cross ``/``.
- ``[abc]`` / ``[!abc]`` are character classes.
- A trailing ``/`` matches everything under that directory: ``vendor/`` == ``vendor/**``.
- Matching is case-sensitive.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath


@functools.lru_cache(maxsize=1024)
def compile_glob(pattern: str) -> re.Pattern[str]:
    pat = pattern.strip().replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    if pat.endswith("/"):
        pat += "**"
    anchored = "/" in pat.rstrip("/")
    pat = pat.lstrip("/")
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
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


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
    case-insensitive for Windows paths and understands MSYS ``/c/...`` paths.
    """
    if repo is None:
        return normalize(path) if not _looks_absolute(path) else None
    repo_s = str(repo)
    windows = _is_windows_abs(repo_s) or _is_windows_abs(path) or os.name == "nt"
    raw = path.strip().strip('"').strip("'")
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
