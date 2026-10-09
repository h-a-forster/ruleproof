"""``doctor/conflict``: instructions loaded together that contradict each other."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ruleproof.doctor._common import (
    NEGATION,
    Doc,
    Finding,
    RepoInfo,
    clip,
    ev,
    is_negated,
    stem,
    words,
)
from ruleproof.doctor.mentions import Mention, tool_mentions
from ruleproof.doctor.scan import Scan

CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("JavaScript package manager", ("npm", "pnpm", "yarn", "bun")),
    ("Python package manager", ("pip", "uv", "poetry", "pdm", "pipenv")),
    ("Python test runner", ("pytest", "unittest")),
    ("JavaScript test runner", ("jest", "vitest", "mocha")),
    ("Python formatter", ("black", "ruff-format")),
    ("JavaScript formatter", ("prettier", "biome")),
    ("JavaScript linter", ("eslint", "biome")),
)


def display(tool: str) -> str:
    return "ruff format" if tool == "ruff-format" else tool


def check_conflicts(info: RepoInfo, scan: Scan) -> list[Finding]:
    out: list[Finding] = []
    for group, docs in scan.groups().items():
        mentions = [
            m for d in docs for m in tool_mentions(info, d) if m.hint is None or m.hint == group
        ]
        out.extend(_tool_conflicts(mentions))
        out.extend(_indent_conflicts(docs))
        out.extend(_polarity_conflicts(docs))
    return out


def _tool_conflicts(mentions: list[Mention]) -> list[Finding]:
    out: list[Finding] = []
    for job, tools in CATEGORIES:
        relevant = [m for m in mentions if m.tool in tools]
        per_line: dict[tuple[str, int], set[str]] = {}
        for m in relevant:
            per_line.setdefault((m.doc.rel, m.index), set()).add(m.tool)
        by_tool: dict[str, list[Mention]] = {}
        for m in relevant:
            if len(per_line[(m.doc.rel, m.index)]) == 1:  # "npm or pnpm" lists alternatives
                by_tool.setdefault(m.tool, []).append(m)
        if len(by_tool) < 2:
            continue
        parts = [f"{display(t)} ({ms[0].where})" for t, ms in by_tool.items()]
        evidence = [
            ev(f"uses {display(t)}", m.doc.rel, m.line, m.excerpt)
            for t, ms in by_tool.items()
            for m in ms[:2]
        ]
        out.append(
            Finding("conflict", "warning", f"Conflicting {job}: {' vs '.join(parts)}", evidence)
        )
    return out


# --- Indentation ----------------------------------------------------------------------------

_SPACES = re.compile(r"\b([1-8])[\s-]*spaces?\b", re.IGNORECASE)
_TABS = re.compile(
    r"\btabs\b|\btab\s+(?:characters?|indentation)\b|\bhard\s+tabs?\b", re.IGNORECASE
)
_LANG_ALIASES: dict[str, str] = {
    "python": "python",
    "py": "python",
    "javascript": "js",
    "js": "js",
    "jsx": "js",
    "typescript": "ts",
    "ts": "ts",
    "tsx": "ts",
    "go": "go",
    "golang": "go",
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "markdown": "markdown",
    "md": "markdown",
    "makefile": "make",
    "rust": "rust",
    "java": "java",
    "html": "html",
    "css": "css",
    "scss": "css",
    "shell": "shell",
    "bash": "shell",
    "toml": "toml",
    "ruby": "ruby",
    "php": "php",
    "kotlin": "kotlin",
    "swift": "swift",
    "lua": "lua",
    "sql": "sql",
    "c": "c",
    "c++": "cpp",
    "cpp": "cpp",
    "csharp": "csharp",
    "c#": "csharp",
}


@dataclass(frozen=True, slots=True)
class _Indent:
    doc: Doc
    index: int
    value: str
    langs: frozenset[str]


def _langs(text: str) -> frozenset[str]:
    return frozenset(_LANG_ALIASES[w] for w in words(text) if w in _LANG_ALIASES)


def _indent_statements(doc: Doc) -> list[_Indent]:
    out: list[_Indent] = []
    for i, line in enumerate(doc.prose):
        heading = doc.heading_at(i)
        if "indent" not in line.lower() and "indent" not in heading.lower():
            continue
        values = {
            f"{m.group(1)} spaces"
            for m in _SPACES.finditer(line)
            if not is_negated(line, m.start(), m.end())
        } | {"tabs" for m in _TABS.finditer(line) if not is_negated(line, m.start(), m.end())}
        if len(values) != 1:
            continue
        langs = _langs(line) or _langs(heading)
        out.append(_Indent(doc, i, values.pop(), langs))
    return out


def _indent_conflicts(docs: list[Doc]) -> list[Finding]:
    by_langs: dict[frozenset[str], dict[str, _Indent]] = {}
    for doc in docs:
        for st in _indent_statements(doc):
            by_langs.setdefault(st.langs, {}).setdefault(st.value, st)
    out: list[Finding] = []
    for langs, values in by_langs.items():
        if len(values) < 2:
            continue
        scope = f" for {', '.join(sorted(langs))}" if langs else ""
        parts = [f"{v} ({s.doc.rel}:{s.index + 1})" for v, s in values.items()]
        evidence = [
            ev(f"indent with {v}", s.doc.rel, s.index + 1, clip(s.doc.prose[s.index]))
            for v, s in values.items()
        ]
        out.append(
            Finding(
                "conflict",
                "warning",
                f"Conflicting indentation{scope}: {' vs '.join(parts)}",
                evidence,
            )
        )
    return out


# --- Always X / never X ---------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_LABEL = re.compile(r"^(?:important|note|warning|critical|rule|tip)\s*[:!-]\s*", re.IGNORECASE)
_NEG_START = re.compile(
    r"^(?:you\s+)?(?:never|don'?t|do\s+not|must\s+not|mustn'?t|should\s+not|shouldn'?t|"
    r"avoid|cannot|can'?t)\s+(.+)$",
    re.IGNORECASE,
)
_POS_START = re.compile(
    r"^(?:always|you\s+must|must|you\s+should|make\s+sure\s+(?:to|you)|be\s+sure\s+to|"
    r"remember\s+to|do)\s+(.+)$",
    re.IGNORECASE,
)
_NEG_CONTEXT = re.compile(
    r"\b(?:don'?t|do\s+not|never|avoid|forbidden|prohibited|not\s+allowed|must\s+not|"
    r"don'?ts|anti-?patterns?)\b",
    re.IGNORECASE,
)
_ARTICLES = frozenset({"a", "an", "the", "please", "always", "ever"})


@dataclass(frozen=True, slots=True)
class _Statement:
    doc: Doc
    index: int
    negative: bool
    text: str
    key: frozenset[str]

    @property
    def where(self) -> str:
        return f"{self.doc.rel}:{self.index + 1}"


def _key(obj: str) -> frozenset[str]:
    obj = re.sub(r"\([^)]*\)", " ", obj)
    return frozenset(stem(w) for w in words(obj) if w not in _ARTICLES)


def _statements(doc: Doc) -> list[_Statement]:
    out: list[_Statement] = []
    context_negative = False
    for i, raw in enumerate(doc.prose):
        if not raw.strip():
            continue
        text = raw.replace("`", "").replace("**", "").replace("__", "").strip()
        bullet = _BULLET.match(text)
        if text.startswith("#"):
            context_negative = bool(_NEG_CONTEXT.search(text))
            continue
        if not bullet:
            if text.endswith(":"):
                context_negative = bool(_NEG_CONTEXT.search(text))
                continue
            context_negative = False
        body = _LABEL.sub("", bullet.group(1) if bullet else text)
        clauses = [c.strip() for c in re.split(r"(?<=[.!?])\s+|;\s*", body) if c.strip()]
        for n, clause in enumerate(clauses):
            clause = clause.rstrip(".!?")
            neg = _NEG_START.match(clause)
            pos = _POS_START.match(clause)
            if neg:
                out.append(_Statement(doc, i, True, clause, _key(neg.group(1))))
            elif pos and not NEGATION.match(pos.group(1)):
                out.append(_Statement(doc, i, False, clause, _key(pos.group(1))))
            elif bullet and n == 0:
                if context_negative:
                    out.append(_Statement(doc, i, True, clause, _key(clause)))
                elif not NEGATION.search(clause):
                    out.append(_Statement(doc, i, False, clause, _key(clause)))
    return out


def _polarity_conflicts(docs: list[Doc]) -> list[Finding]:
    statements = [s for d in docs for s in _statements(d) if len(s.key) >= 2]
    negatives = [s for s in statements if s.negative]
    positives = [s for s in statements if not s.negative]
    out: list[Finding] = []
    seen: set[frozenset[str]] = set()
    for neg in negatives:
        for pos in positives:
            if neg.doc is pos.doc and neg.index == pos.index:
                continue
            overlap = len(neg.key & pos.key) / len(neg.key | pos.key)
            if overlap < 0.8 or neg.key in seen:
                continue
            seen.add(neg.key)
            out.append(
                Finding(
                    "conflict",
                    "warning",
                    f'Contradictory instructions: {pos.where} says "{clip(pos.text, 60)}" but '
                    f'{neg.where} says "{clip(neg.text, 60)}"',
                    [
                        ev("says to", pos.doc.rel, pos.index + 1, clip(pos.text)),
                        ev("says not to", neg.doc.rel, neg.index + 1, clip(neg.text)),
                    ],
                )
            )
    return out
