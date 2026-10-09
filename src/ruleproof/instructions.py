"""Locate agent instruction files in a repository."""

from __future__ import annotations

import os
from pathlib import Path

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


def find_instruction_files(repo: Path, max_depth: int = 8) -> list[Path]:
    """Instruction files under ``repo``, sorted by repo-relative path (root first)."""
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
        for name in filenames:
            if name in INSTRUCTION_NAMES:
                found.add(here / name)

    def key(p: Path) -> tuple[int, str]:
        rel = p.relative_to(repo).as_posix()
        return (rel.count("/"), rel)

    return sorted(found, key=key)
