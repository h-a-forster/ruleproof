"""Shared plumbing for the doctor checks: parsed Markdown documents, repo facts, findings."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruleproof.instructions import SKIP_DIRS
from ruleproof.models import SEVERITIES, Evidence, Rule, RuleResult, Severity
from ruleproof.paths import match_any

DOCTOR_CHECKS: dict[str, str] = {
    "drift": (
        "Instruction files for different agents in the same directory (CLAUDE.md, AGENTS.md, "
        "GEMINI.md) that differ in content and neither imports nor references the other"
    ),
    "conflict": (
        "Contradictory instructions: different package managers, test runners, formatters or "
        "indentation named for the same job, or 'always X' next to 'never X'"
    ),
    "repo-mismatch": (
        "Instructions naming a package manager, test runner or Python/Node version that the "
        "repo's lockfiles and config contradict"
    ),
    "dead-reference": (
        "@imports, links and file paths that do not exist, and npm/pnpm/yarn/bun scripts, "
        "make targets and just recipes that are not defined"
    ),
    "size": "Estimated instruction tokens each agent loads at the start of every session",
    "skill": (
        "Agent skills with missing or invalid frontmatter, a name that does not match the "
        "directory, an oversized description, missing referenced files, or copies that drifted"
    ),
    "mcp-drift": (
        "The same MCP server configured differently across agents' config files, unreadable "
        "MCP config files, and literal secrets in MCP config"
    ),
}

EXTENSIONS: frozenset[str] = frozenset(
    [
        "md",
        "mdc",
        "mdx",
        "txt",
        "rst",
        "adoc",
        "json",
        "jsonc",
        "json5",
        "toml",
        "yaml",
        "yml",
        "ini",
        "cfg",
        "conf",
        "py",
        "pyi",
        "ipynb",
        "ts",
        "tsx",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "vue",
        "svelte",
        "astro",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "bat",
        "cmd",
        "go",
        "rs",
        "java",
        "kt",
        "kts",
        "scala",
        "rb",
        "php",
        "c",
        "h",
        "cc",
        "cpp",
        "hpp",
        "cs",
        "fs",
        "swift",
        "m",
        "mm",
        "lua",
        "sql",
        "html",
        "htm",
        "css",
        "scss",
        "sass",
        "less",
        "xml",
        "proto",
        "graphql",
        "gql",
        "tf",
        "hcl",
        "lock",
        "gradle",
        "csv",
        "tsv",
        "env",
        "example",
        "dockerfile",
        "mk",
    ]
)
"""File extensions that make a token look like a file path rather than a word."""

GENERATED_DIRS: frozenset[str] = SKIP_DIRS | {
    "out",
    "coverage",
    "htmlcov",
    "tmp",
    "temp",
    "logs",
    ".cache",
    ".git",
}
"""Directories whose contents are generated or local, so references into them are not checked."""

SUBPROJECT_MANIFESTS: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
)

_INDEX_LIMIT = 200_000
_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass(slots=True)
class Finding:
    check: str
    severity: Severity
    summary: str
    evidence: list[Evidence] = field(default_factory=list)

    def sort_key(self) -> tuple[int, str, int, str, str]:
        first = self.evidence[0] if self.evidence else None
        path = first.path if first and first.path else ""
        line = first.line if first and first.line else 0
        return (_RANK[self.severity], path, line, self.check, self.summary)

    def result(self) -> RuleResult:
        rule = Rule(
            id=f"doctor/{self.check}",
            check="doctor",
            description=DOCTOR_CHECKS[self.check],
            severity=self.severity,
            source=None,
            origin="doctor",
        )
        return RuleResult(rule=rule, status="fail", summary=self.summary, evidence=self.evidence)


def ev(
    message: str, path: str | None, line: int | None = None, excerpt: str | None = None
) -> Evidence:
    return Evidence(message=message, path=path, line=line, excerpt=excerpt)


def read_text(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return fh.read().removeprefix("﻿")
    except OSError:
        return None


def clip(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- Markdown -------------------------------------------------------------------------------

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`{]*)")
_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


class Doc:
    """A Markdown instruction file split into prose and fenced code.

    ``prose[i]`` is line ``i`` with HTML comments removed, or ``""`` for code lines.
    ``fence[i]`` is the fence language for lines inside a fenced block, else None.
    """

    __slots__ = ("fence", "headings", "lines", "path", "prose", "rel", "text")

    def __init__(self, path: Path, rel: str, text: str) -> None:
        self.path = path
        self.rel = rel
        self.text = text
        self.lines = text.splitlines()
        self.prose: list[str] = []
        self.fence: list[str | None] = []
        self.headings: list[tuple[int, int, str]] = []  # (index, level, title)
        marker: str | None = None
        lang = ""
        in_comment = False
        for i, line in enumerate(self.lines):
            if marker is not None:
                closing = re.match(rf"^\s{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$", line)
                if closing:
                    marker = None
                    self.fence.append(None)
                else:
                    self.fence.append(lang)
                self.prose.append("")
                continue
            m = _FENCE.match(line)
            if m and not in_comment:
                marker, lang = m.group(1), m.group(2).lower()
                self.prose.append("")
                self.fence.append(None)
                continue
            text_line, in_comment = _strip_comments(line, in_comment)
            self.prose.append(text_line)
            self.fence.append(None)
            h = _HEADING.match(text_line)
            if h:
                self.headings.append((i, len(h.group(1)), h.group(2).strip()))

    @property
    def dir(self) -> Path:
        return self.path.parent

    def spans(self, i: int) -> list[tuple[int, int, str]]:
        """Inline code spans on prose line ``i`` as (start, end, content)."""
        return [(m.start(), m.end(), m.group(2).strip()) for m in _SPAN.finditer(self.prose[i])]

    def blanked(self, i: int) -> str:
        """Prose line ``i`` with code spans replaced by spaces (positions are preserved)."""
        return _SPAN.sub(lambda m: " " * len(m.group(0)), self.prose[i])

    def heading_at(self, i: int) -> str:
        title = ""
        for idx, _, text in self.headings:
            if idx > i:
                break
            title = text
        return title


def _strip_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    out: list[str] = []
    rest = line
    while True:
        if in_comment:
            end = rest.find("-->")
            if end < 0:
                return "".join(out), True
            out.append(" " * (end + 3))
            rest = rest[end + 3 :]
            in_comment = False
        start = rest.find("<!--")
        if start < 0:
            out.append(rest)
            return "".join(out), False
        out.append(rest[:start])
        out.append("    ")
        rest = rest[start + 4 :]
        in_comment = True


# --- Clauses and negation -------------------------------------------------------------------

_BOUNDARY = re.compile(
    r"[.!?](?=\s|$)|[;,()\[\]]|\s[-–—]\s|—|\b(?:but|then|and\s+use|in\s+favou?r\s+of|"
    r"replaced\s+by|superseded\s+by)\b",
    re.IGNORECASE,
)
NEGATION = re.compile(
    r"\b(?:not|never|don'?t|doesn'?t|do\s+not|cannot|can'?t|avoid|instead\s+of|rather\s+than|"
    r"(?:migrat|mov|switch)(?:e|ed|es|ing)?\s+(?:away\s+)?from|replac(?:e|es|ed|ing)|no\s+longer|"
    r"unlike|without|forbidden|prohibited|deprecated|disallowed|banned|over|legacy|old)\b",
    re.IGNORECASE,
)
_LIST_ITEM = re.compile(r"[\s,*_'\"]*(?:(?:or|and|nor)\b[\s*_'\"]*)?", re.IGNORECASE)
_NEGATION_AFTER = re.compile(
    r"^[\s`*_'\"]*(?:(?:is|was|are|were|has\s+been|have\s+been|should\s+be)\s+)?"
    r"(?:not\b|no\s+longer|deprecated|replaced|removed|forbidden|banned|disallowed|unsupported|"
    r"discouraged)",
    re.IGNORECASE,
)


def clause_before(line: str, start: int) -> str:
    """The clause leading up to ``start``.

    A list continues its clause: in "don't use `pip`, `poetry`, or `conda`" the clause before
    `conda` is "don't use `pip`, `poetry`, or ".
    """
    prefix = line[:start]
    cuts = [0, *(m.end() for m in _BOUNDARY.finditer(prefix))]
    k = len(cuts) - 1
    while k > 0 and _LIST_ITEM.fullmatch(_SPAN.sub("", prefix[cuts[k] :])):
        k -= 1
    return prefix[cuts[k] :]


def clause_after(line: str, end: int) -> str:
    suffix = line[end:]
    m = _BOUNDARY.search(suffix)
    return suffix[: m.start()] if m else suffix


def is_negated(line: str, start: int, end: int) -> bool:
    """True when the text at ``line[start:end]`` sits in a negated clause."""
    if NEGATION.search(clause_before(line, start)):
        return True
    return bool(_NEGATION_AFTER.match(clause_after(line, end)))


_WORD = re.compile(r"[a-z0-9][a-z0-9._/+-]*")


def stem(word: str) -> str:
    w = word.lower().strip("._/-")
    for suffix in ("ing", "ed", "es", "s"):
        if len(w) > len(suffix) + 2 and w.endswith(suffix) and not w.endswith("ss"):
            w = w[: -len(suffix)]
            break
    if len(w) > 3 and w[-1] == w[-2]:
        w = w[:-1]
    return w.rstrip("e") or w


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


# --- Repository facts -----------------------------------------------------------------------


class RepoInfo:
    """Lazily computed facts about the repository, shared by all doctor checks."""

    def __init__(self, repo: Path, exclude: Sequence[str] = ()) -> None:
        self.repo = repo
        self.exclude = tuple(exclude)
        self._docs: dict[Path, Doc | None] = {}
        self._json: dict[Path, Any] = {}
        self._index: set[str] | None = None
        self._basenames: set[str] = set()
        self.index_complete = True
        self._ignore: list[str] | None = None
        self._gemini: tuple[str, ...] | None = None
        self._subprojects: list[str] | None = None
        self._listings: dict[Path, set[str]] = {}
        self._package_names: dict[str, set[str]] = {}

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.repo).as_posix()
        except ValueError:
            return path.as_posix()

    def excluded(self, path: Path | str) -> bool:
        """True when ``path`` (absolute, or repo-relative posix) matches an ``exclude`` glob."""
        if not self.exclude:
            return False
        rel = self.rel(path) if isinstance(path, Path) else path
        parts = rel.split("/")
        return any(
            match_any("/".join(parts[: k + 1]), self.exclude)
            or match_any("/".join(parts[: k + 1]) + "/", self.exclude)
            for k in range(len(parts))
        )

    def is_file(self, path: Path) -> bool:
        """An existing, non-excluded file whose name matches exactly, even on Windows/macOS."""
        return (
            path.name in self._listing(path.parent) and path.is_file() and not self.excluded(path)
        )

    def package_json_names(self, key: str) -> set[str]:
        """Union of the ``key`` mapping's names over every package.json in the repo."""
        if key not in self._package_names:
            names: set[str] = set()
            for rel in self.index:
                if rel.rsplit("/", 1)[-1] == "package.json" and not self.ignored(rel):
                    data = self.json(self.repo / rel)
                    value = data.get(key) if isinstance(data, dict) else None
                    if isinstance(value, dict):
                        names |= set(value)
            self._package_names[key] = names
        return self._package_names[key]

    def inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.repo.resolve())
        except (ValueError, OSError):
            return False
        return True

    def doc(self, path: Path) -> Doc | None:
        if path not in self._docs:
            text = read_text(path) if self.is_file(path) else None
            self._docs[path] = None if text is None else Doc(path, self.rel(path), text)
        return self._docs[path]

    def json(self, path: Path) -> Any:
        """Parsed JSON at ``path``, or None when missing or invalid."""
        if path not in self._json:
            text = read_text(path) if self.is_file(path) else None
            try:
                self._json[path] = None if text is None else json.loads(text)
            except ValueError:
                self._json[path] = None
        return self._json[path]

    def _walk(self) -> None:
        self._index = set()
        for dirpath, dirnames, filenames in os.walk(self.repo):
            base = self.rel(Path(dirpath))
            prefix = "" if base in ("", ".") else base + "/"
            dirnames[:] = sorted(
                d for d in dirnames if d not in SKIP_DIRS and not self.excluded(prefix + d)
            )
            filenames = [f for f in filenames if not self.excluded(prefix + f)]
            for name in (*dirnames, *filenames):
                self._index.add(prefix + name)
                self._basenames.add(name)
            if len(self._index) > _INDEX_LIMIT:
                self.index_complete = False
                return

    @property
    def index(self) -> set[str]:
        """Repo-relative posix paths of all files and directories outside dependency dirs."""
        if self._index is None:
            self._walk()
        assert self._index is not None
        return self._index

    @property
    def basenames(self) -> set[str]:
        _ = self.index
        return self._basenames

    def has_suffix_path(self, rel: str) -> bool:
        """True when some indexed path equals ``rel`` or ends with ``/rel``."""
        tail = "/" + rel
        return rel in self.index or any(p.endswith(tail) for p in self.index)

    def ignored(self, rel: str) -> bool:
        """True for paths in generated/dependency dirs or matched by the root .gitignore."""
        parts = rel.strip("/").split("/")
        if any(p in GENERATED_DIRS for p in parts[:-1]) or parts[0] in GENERATED_DIRS:
            return True
        if self._ignore is None:
            text = read_text(self.repo / ".gitignore") or ""
            self._ignore = [
                ln.strip()
                for ln in text.splitlines()
                if ln.strip() and not ln.lstrip().startswith(("#", "!"))
            ]
        candidates = ["/".join(parts[: k + 1]) for k in range(len(parts))]
        return any(
            match_any(c, self._ignore) or match_any(c + "/", self._ignore) for c in candidates
        )

    @property
    def gemini_names(self) -> tuple[str, ...]:
        """Context file names Gemini CLI loads (``context.fileName`` in .gemini/settings.json)."""
        if self._gemini is None:
            names: tuple[str, ...] = ("GEMINI.md",)
            data = load_jsonc(self.repo / ".gemini" / "settings.json")
            if isinstance(data, dict):
                ctx = data.get("context")
                value = ctx.get("fileName") if isinstance(ctx, dict) else None
                if value is None:
                    value = data.get("contextFileName")
                if isinstance(value, str):
                    names = (value,)
                elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                    names = tuple(value) or names
            self._gemini = names
        return self._gemini

    @property
    def subprojects(self) -> list[str]:
        """Directories below the root that have their own package manifest."""
        if self._subprojects is None:
            found = {
                p.rsplit("/", 1)[0]
                for p in self.index
                if "/" in p and p.rsplit("/", 1)[1] in SUBPROJECT_MANIFESTS
            }
            self._subprojects = sorted(d for d in found if not self.ignored(d + "/x"))
        return self._subprojects

    def _listing(self, directory: Path) -> set[str]:
        """Exact (case-sensitive) entry names, so lookups behave the same on every OS."""
        if directory not in self._listings:
            try:
                self._listings[directory] = set(os.listdir(directory))
            except OSError:
                self._listings[directory] = set()
        return self._listings[directory]

    def nearest(self, start: Path, names: tuple[str, ...]) -> Path | None:
        """First existing ``start/<name>``, walking up to the repo root."""
        here = start
        while True:
            listing = self._listing(here)
            for name in names:
                candidate = here / name
                if name in listing and self.is_file(candidate):
                    return candidate
            if here == self.repo or self.repo not in here.parents:
                return None
            here = here.parent


def load_jsonc(path: Path) -> Any:
    """JSON with comments and trailing commas, or None when missing or invalid."""
    text = read_text(path) if path.is_file() else None
    if text is None:
        return None
    try:
        return json.loads(strip_jsonc(text))
    except ValueError:
        return None


def strip_jsonc(text: str) -> str:
    """Blank out ``//`` and ``/* */`` comments and trailing commas, keeping line/column."""
    out = list(text)
    i, n = 0, len(text)
    last_comma: int | None = None
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            last_comma = None
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue
        elif c == ",":
            last_comma = i
        elif c in "}]" and last_comma is not None:
            out[last_comma] = " "
            last_comma = None
        elif not c.isspace():
            last_comma = None
        i += 1
    return "".join(out)


def shannon_entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(value)
    return -sum(c / total * math.log2(c / total) for c in counts.values())
