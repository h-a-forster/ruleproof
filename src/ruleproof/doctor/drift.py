"""``doctor/drift``: CLAUDE.md / AGENTS.md / GEMINI.md side by side that have drifted apart."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ruleproof.doctor._common import Doc, Finding, RepoInfo, clip, ev, stem, words
from ruleproof.doctor.imports import find_imports, import_closure
from ruleproof.doctor.mentions import KNOWN_COMMANDS, iter_commands
from ruleproof.doctor.scan import Scan

SIMILAR_ENOUGH = 0.6
"""Line-set similarity at or above which two files are treated as in sync."""

_STOP = frozenset({"and", "or", "the", "a", "an", "of", "for", "to", "in", "on", "with", "how"})
_MARKUP = re.compile(r"^[\s>#*+\-\d.)]+|[*_`]")


@dataclass(slots=True)
class _Features:
    headings: list[tuple[frozenset[str], str, Doc, int]] = field(default_factory=list)
    commands: dict[str, tuple[Doc, int]] = field(default_factory=dict)
    section_command: dict[tuple[str, int], str] = field(default_factory=dict)
    lines: set[str] = field(default_factory=set)
    flat: str = ""


def check_drift(info: RepoInfo, scan: Scan) -> list[Finding]:
    out: list[Finding] = []
    names = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
    dirs = sorted({d.dir for d in scan.docs if d.path.name in names} | {info.repo})
    for d in dirs:
        files: dict[str, Path] = {}
        claude = d / "CLAUDE.md"
        if not info.is_file(claude) and d == info.repo:
            claude = d / ".claude" / "CLAUDE.md"
        if info.is_file(claude):
            files["claude"] = claude
        if info.is_file(d / "AGENTS.md"):
            files["codex"] = d / "AGENTS.md"
        if "GEMINI.md" in info.gemini_names and info.is_file(d / "GEMINI.md"):
            files["gemini"] = d / "GEMINI.md"
        agents = files.get("codex")
        for key in ("claude", "gemini"):
            if key in files and agents is not None:
                finding = _pointer_without_import(info, files[key], agents)
                if finding:
                    out.append(finding)
        pairs = [("claude", "codex"), ("claude", "gemini"), ("gemini", "codex")]
        for x, y in pairs:
            if x not in files or y not in files:
                continue
            if {x, y} == {"gemini", "codex"} and "AGENTS.md" in info.gemini_names:
                continue
            finding = _compare(info, files[x], files[y], x != "codex", y != "codex")
            if finding:
                out.append(finding)
    return out


def _pointer_without_import(info: RepoInfo, path: Path, agents: Path) -> Finding | None:
    doc = info.doc(path)
    if doc is None or len(doc.text) > 600 or "AGENTS.md" not in doc.text:
        return None
    if _symlink_standin(doc, agents):
        return None
    content = [
        (i, ln) for i, ln in enumerate(doc.prose) if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if len(content) > 3 or any(_same(imp.target, agents) for imp in find_imports(doc)):
        return None
    i, line = next(((i, ln) for i, ln in content if "AGENTS.md" in ln), content[0])
    agent = "Claude Code" if path.name == "CLAUDE.md" else "Gemini CLI"
    return Finding(
        "drift",
        "info",
        f"{doc.rel} points to AGENTS.md without importing it, so {agent} does not load it; "
        f"write `@{os.path.relpath(agents, path.parent).replace(os.sep, '/')}` on its own line",
        [ev("mentions AGENTS.md without an @import", doc.rel, i + 1, clip(line.strip()))],
    )


def _symlink_standin(doc: Doc, target: Path) -> bool:
    """A git symlink checked out as a plain file (``core.symlinks=false``) holds its target."""
    content = doc.text.strip()
    return bool(content) and "\n" not in content and _same(doc.dir / content, target)


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}\b", text) is not None


def _compare(info: RepoInfo, px: Path, py: Path, follow_x: bool, follow_y: bool) -> Finding | None:
    x_docs = import_closure(info, [px]) if follow_x else [d for d in [info.doc(px)] if d]
    y_docs = import_closure(info, [py]) if follow_y else [d for d in [info.doc(py)] if d]
    if not x_docs or not y_docs or _same(px, py):
        return None
    if _symlink_standin(x_docs[0], py) or _symlink_standin(y_docs[0], px):
        return None
    if any(_same(d.path, py) for d in x_docs) or any(_same(d.path, px) for d in y_docs):
        return None
    if _mentions(x_docs[0].text, py.name) or _mentions(y_docs[0].text, px.name):
        return None
    fx, fy = _features(x_docs), _features(y_docs)
    union = fx.lines | fy.lines
    if not union or len(fx.lines & fy.lines) / len(union) >= SIMILAR_ENOUGH:
        return None
    xname, yname = x_docs[0].rel, y_docs[0].rel
    parts: list[str] = []
    evidence = []
    for this, other, name, other_name in ((fx, fy, xname, yname), (fy, fx, yname, xname)):
        headings = _unique_headings(this, other)[:3]
        if headings:
            labels = []
            for _, title, doc, idx in headings:
                cmd = this.section_command.get((doc.rel, idx))
                labels.append(f"'{title}'" + (f" (`{clip(cmd, 40)}`)" if cmd else ""))
                evidence.append(ev(f"only in {name}", doc.rel, idx + 1, clip(doc.prose[idx])))
            parts.append(f"{name} has {', '.join(labels)}; {other_name} has no such section")
        covered = {c for (_, _, d, i) in headings for c in [this.section_command.get((d.rel, i))]}
        commands = [
            (c, loc)
            for c, loc in this.commands.items()
            if c not in covered and c not in other.commands and c not in other.flat
        ][:2]
        if commands:
            listed = ", ".join(f"`{clip(c, 40)}`" for c, _ in commands)
            parts.append(f"{name} runs {listed}; {other_name} never does")
            evidence.extend(ev(f"only in {name}", d.rel, i + 1, clip(c)) for c, (d, i) in commands)
    if not parts:
        return None
    return Finding(
        "drift",
        "warning",
        clip(
            f"{xname} and {yname} have drifted and neither imports the other: {'; '.join(parts)}",
            300,
        ),
        evidence,
    )


def _features(docs: list[Doc]) -> _Features:
    f = _Features()
    flat: list[str] = []
    for doc in docs:
        flat.append(" ".join(doc.text.split()))
        for idx, level, title in doc.headings:
            key = frozenset(stem(w) for w in words(title) if w not in _STOP)
            if level > 1 and key:
                f.headings.append((key, title.strip("*_` "), doc, idx))
        starts = [idx for idx, _, _ in doc.headings]
        for cmd in iter_commands(doc):
            if cmd.negative or cmd.tokens[0] not in KNOWN_COMMANDS:
                continue
            text = " ".join(cmd.text.split())
            f.commands.setdefault(text, (doc, cmd.index))
            owner = max((s for s in starts if s <= cmd.index), default=None)
            if owner is not None:
                f.section_command.setdefault((doc.rel, owner), text)
        for line in doc.lines:
            norm = " ".join(_MARKUP.sub("", line).lower().split())
            if len(norm) > 3 and not norm.startswith(("```", "~~~")):
                f.lines.add(norm)
    f.flat = "\n".join(flat)
    return f


def _unique_headings(
    this: _Features, other: _Features
) -> list[tuple[frozenset[str], str, Doc, int]]:
    out = []
    for h in this.headings:
        if not any(len(h[0] & o[0]) / len(h[0] | o[0]) >= 0.5 for o in other.headings):
            out.append(h)
    return out
