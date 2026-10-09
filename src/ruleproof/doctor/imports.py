"""Claude Code / Gemini CLI ``@path`` imports, and the set of files each agent loads."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ruleproof.doctor._common import EXTENSIONS, Doc, RepoInfo

MAX_IMPORT_DEPTH = 5
"""Claude Code follows imports at most five hops deep."""

_IMPORT = re.compile(r"(?:(?<=\s)|^)@([^\s`'\"<>()\[\]{}]+)")


@dataclass(frozen=True, slots=True)
class Import:
    line: int  # 1-based
    raw: str  # as written after the @
    target: Path


def find_imports(doc: Doc) -> list[Import]:
    """``@path`` imports outside code. Home (``@~/``) and absolute imports are not returned."""
    found: list[Import] = []
    for i in range(len(doc.prose)):
        if "@" not in doc.prose[i]:
            continue
        for m in _IMPORT.finditer(doc.blanked(i)):
            raw = m.group(1).rstrip(".,;:!?")
            if not _looks_like_file(raw) or raw.startswith(("~", "/", "\\")):
                continue
            found.append(Import(i + 1, raw, Path(os.path.normpath(doc.dir / raw))))
    return found


def _looks_like_file(raw: str) -> bool:
    if raw.startswith(("./", "../", "~/")):
        return True
    name = raw.rsplit("/", 1)[-1]
    return "." in name[1:] and name.rsplit(".", 1)[1].lower() in EXTENSIONS


def import_closure(info: RepoInfo, roots: list[Path]) -> list[Doc]:
    """``roots`` plus everything they import, recursively (cycle-safe, depth-limited)."""
    seen: set[Path] = set()
    out: list[Doc] = []
    frontier = [(p, 0) for p in roots]
    while frontier:
        path, depth = frontier.pop(0)
        key = path.resolve()
        if key in seen:
            continue
        doc = info.doc(path)
        if doc is None:
            continue
        seen.add(key)
        out.append(doc)
        if depth < MAX_IMPORT_DEPTH:
            frontier.extend((imp.target, depth + 1) for imp in find_imports(doc))
    return out


def claude_roots(info: RepoInfo, directory: Path) -> list[Path]:
    names = ["CLAUDE.md", "CLAUDE.local.md"]
    paths = [directory / n for n in names]
    if directory == info.repo:
        paths.append(directory / ".claude" / "CLAUDE.md")
    return [p for p in paths if info.is_file(p)]


def gemini_roots(info: RepoInfo, directory: Path) -> list[Path]:
    return [directory / n for n in info.gemini_names if info.is_file(directory / n)]


def codex_roots(info: RepoInfo, directory: Path) -> list[Path]:
    """Codex reads AGENTS.override.md instead of AGENTS.md when both exist."""
    for name in ("AGENTS.override.md", "AGENTS.md"):
        if info.is_file(directory / name):
            return [directory / name]
    return []
