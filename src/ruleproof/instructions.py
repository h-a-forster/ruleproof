"""Locate agent instruction files in a repository."""

from __future__ import annotations

import fnmatch
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

    In a git work tree the candidates come from ``git ls-files`` (tracked plus untracked files
    that are not ignored), so ignored directories are never walked. Elsewhere the tree is walked.
    ``exclude`` holds globs (see ``ruleproof.paths``) for files or directories to skip, such as
    example projects and test fixtures that carry their own instruction files.
    """
    fixed = set(ROOT_INSTRUCTION_PATHS)
    rels = _git_candidates(repo)
    if rels is None:
        rels = _walk_candidates(repo, max_depth)

    def wanted(rel: str) -> bool:
        if rel in fixed or any(fnmatch.fnmatchcase(rel, g) for g in GLOB_INSTRUCTION_PATHS):
            return True
        *dirs, name = rel.split("/")
        return (
            name in INSTRUCTION_NAMES
            and len(dirs) < max_depth
            and not any(d in SKIP_DIRS or d.startswith(".") for d in dirs)
        )

    found = {rel for rel in rels if wanted(rel)}
    if exclude:
        found = {rel for rel in found if not match_any(rel, exclude)}
    ordered = sorted(found, key=lambda rel: (rel.count("/"), rel))
    return [repo / rel for rel in ordered if (repo / rel).is_file()]


def _git_candidates(repo: Path) -> list[str] | None:
    """Repo-relative paths of possible instruction files per git, or None outside a work tree."""
    specs = [f":(glob)**/{name}" for name in sorted(INSTRUCTION_NAMES)]
    specs += [f":(glob){p}" for p in (*ROOT_INSTRUCTION_PATHS, *GLOB_INSTRUCTION_PATHS)]
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *specs],
            cwd=repo,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace")
    return sorted({p for p in out.split("\0") if p})


def _walk_candidates(repo: Path, max_depth: int) -> list[str]:
    rels: list[str] = []
    for rel in ROOT_INSTRUCTION_PATHS:
        if (repo / rel).is_file():
            rels.append(rel)
    for pattern in GLOB_INSTRUCTION_PATHS:
        rels.extend(p.relative_to(repo).as_posix() for p in repo.glob(pattern) if p.is_file())
    root_depth = len(repo.parts)
    for dirpath, dirnames, filenames in os.walk(repo):
        here = Path(dirpath)
        if len(here.parts) - root_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel_dir = here.relative_to(repo).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        rels.extend(prefix + name for name in filenames if name in INSTRUCTION_NAMES)
    return rels


def git_ignored(repo: Path, rel_paths: Iterable[str]) -> set[str]:
    """The subset of ``rel_paths`` that git ignores; empty when git or the repo is unavailable."""
    paths = list(rel_paths)
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
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
