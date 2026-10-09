"""The set of Markdown files the doctor reads: instruction files plus what they import."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ruleproof.doctor._common import Doc, RepoInfo
from ruleproof.doctor.imports import claude_roots, gemini_roots, import_closure
from ruleproof.instructions import find_instruction_files


@dataclass(slots=True)
class Scan:
    docs: list[Doc] = field(default_factory=list)
    group: dict[Path, str] = field(default_factory=dict)
    """Directory (repo-relative, "" for the root) whose agents load the doc."""
    importing: set[Path] = field(default_factory=set)
    """Docs loaded by Claude Code or Gemini CLI, whose ``@imports`` are resolved."""

    def add(self, doc: Doc, group: str) -> None:
        if doc.path not in self.group:
            self.docs.append(doc)
            self.group[doc.path] = group

    def groups(self) -> dict[str, list[Doc]]:
        out: dict[str, list[Doc]] = {}
        for doc in self.docs:
            out.setdefault(self.group[doc.path], []).append(doc)
        return out


def build_scan(info: RepoInfo) -> Scan:
    scan = Scan()
    dirs: set[Path] = {info.repo}
    for path in find_instruction_files(info.repo, exclude=info.exclude):
        doc = info.doc(path)
        if doc is None:
            continue
        rel = doc.rel
        scan.add(doc, "" if rel.startswith(".") or "/" not in rel else rel.rsplit("/", 1)[0])
        if not rel.startswith("."):
            dirs.add(path.parent)
    for directory in sorted(dirs):
        group = info.rel(directory) if directory != info.repo else ""
        roots = claude_roots(info, directory) + gemini_roots(info, directory)
        for doc in import_closure(info, roots):
            scan.importing.add(doc.path)
            if info.inside(doc.path) and not info.excluded(doc.path):
                scan.add(doc, group)
    return scan
