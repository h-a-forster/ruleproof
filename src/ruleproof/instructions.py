"""Locate agent instruction files in a repository."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from ruleproof.paths import match_any

INSTRUCTION_NAMES: frozenset[str] = frozenset(
    {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md"}
)
"""Files read by an agent at any depth (nested ones apply to their subtree)."""

ROOT_INSTRUCTION_PATHS: tuple[str, ...] = (
    ".github/copilot-instructions.md",
    ".claude/CLAUDE.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
)
"""Single-location instruction files, relative to the repo root."""

GLOB_INSTRUCTION_PATHS: tuple[str, ...] = (
    ".cursor/rules/*.mdc",
    ".cursor/rules/*.md",
    ".github/instructions/*.instructions.md",
)

SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".turbo",
        "vendor",
        "third_party",
    }
)


def find_instruction_files(
    repo: Path, max_depth: int = 8, exclude: Sequence[str] = ()
) -> list[Path]:
    """Instruction files under ``repo``, sorted by depth then repo-relative path.

    ``exclude`` holds globs (see ``ruleproof.paths``) for files or directories to skip, such as
    example projects and test fixtures that carry their own instruction files.
    """
    found: set[Path] = set()
    for rel in ROOT_INSTRUCTION_PATHS:
        p = repo / rel
        if p.is_file():
            found.add(p)
    for pattern in GLOB_INSTRUCTION_PATHS:
        found.update(p for p in repo.glob(pattern) if p.is_file())

    root_depth = len(repo.parts)
    for dirpath, dirnames, filenames in os.walk(repo):
        here = Path(dirpath)
        if len(here.parts) - root_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if exclude:
            rel_dir = here.relative_to(repo).as_posix()
            prefix = "" if rel_dir == "." else rel_dir + "/"
            dirnames[:] = [d for d in dirnames if not match_any(f"{prefix}{d}/", exclude)]
        for name in filenames:
            if name in INSTRUCTION_NAMES:
                found.add(here / name)

    def key(p: Path) -> tuple[int, str]:
        rel = p.relative_to(repo).as_posix()
        return (rel.count("/"), rel)

    if exclude:
        found = {p for p in found if not match_any(p.relative_to(repo).as_posix(), exclude)}
    ignored = git_ignored(repo, (p.relative_to(repo).as_posix() for p in found))
    return sorted((p for p in found if p.relative_to(repo).as_posix() not in ignored), key=key)


def git_ignored(repo: Path, rel_paths: Iterable[str]) -> set[str]:
    """The subset of ``rel_paths`` that git ignores; empty when git or the repo is unavailable."""
    paths = list(rel_paths)
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--no-index", "-z", "--stdin"],
            cwd=repo,
            input="\0".join(paths).encode("utf-8"),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode not in (0, 1):  # 1 = nothing ignored; 128 = not a repository
        return set()
    return {p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p}
