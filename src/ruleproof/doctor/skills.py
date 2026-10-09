"""``doctor/skill``: agent skills that will not load, will not trigger, or have drifted."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ruleproof.doctor._common import Doc, Finding, RepoInfo, clip, ev, read_text
from ruleproof.models import Severity

SKILL_DIRS: tuple[str, ...] = (
    ".claude/skills",
    ".agents/skills",
    ".codex/skills",
    ".gemini/skills",
)
MAX_NAME = 64
MAX_DESCRIPTION = 1024

_NAME_OK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:(?:\s+(.*)|\s*)$")
_BLOCK = re.compile(r"^[|>][+-]?\d*$")
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?")
_SKILL_VARS = re.compile(r"^(?:\$\{?CLAUDE_SKILL_DIR\}?|\{baseDir\}|\$\{?SKILL_DIR\}?)/")
_RESOURCE_DIRS = ("scripts/", "references/", "reference/", "assets/", "templates/", "examples/")


@dataclass(slots=True)
class Frontmatter:
    fields: dict[str, str]
    lines: dict[str, int]  # key -> 1-based line
    end: int  # 0-based index of the closing ``---``


def parse_frontmatter(text: str) -> Frontmatter | None:
    """The simple ``key: value`` subset of YAML frontmatter, or None when absent/unterminated."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((j for j in range(1, len(lines)) if lines[j].strip() in ("---", "...")), None)
    if end is None:
        return None
    fields: dict[str, str] = {}
    where: dict[str, int] = {}
    i = 1
    while i < end:
        m = _KEY.match(lines[i])
        if not m:
            i += 1
            continue
        key, value = m.group(1), (m.group(2) or "").strip()
        k = i + 1
        while k < end and (lines[k][:1] in (" ", "\t") or not lines[k].strip()):
            k += 1
        block = [ln.strip() for ln in lines[i + 1 : k]]
        if _BLOCK.match(value):
            sep = "\n" if value[0] == "|" else " "
            value = sep.join(block).strip()
        elif value[:1] in ("'", '"') and value[-1:] == value[0] and len(value) > 1:
            q = value[0]
            value = value[1:-1].replace("''", "'") if q == "'" else value[1:-1].replace('\\"', '"')
        else:
            value = re.sub(r"\s+#.*$", "", value)
            value = " ".join([value, *[b for b in block if b]]).strip()
        fields[key] = value
        where[key] = i + 1
        i = k
    return Frontmatter(fields, where, end)


def check_skills(info: RepoInfo) -> list[Finding]:
    out: list[Finding] = []
    by_name: dict[str, list[tuple[Path, str]]] = {}
    for base in SKILL_DIRS:
        root = info.repo / base
        if not root.is_dir() or info.excluded(root):
            continue
        for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if info.excluded(skill_dir):
                continue
            skill_md = skill_dir / "SKILL.md"
            names = {p.name for p in skill_dir.iterdir()}
            if "SKILL.md" not in names:
                wrong = next((n for n in sorted(names) if n.lower() == "skill.md"), None)
                if wrong:
                    rel = info.rel(skill_dir / wrong)
                    out.append(
                        Finding(
                            "skill",
                            "error",
                            f"{rel} must be named SKILL.md; agents do not load {wrong}",
                            [ev("wrong file name", rel)],
                        )
                    )
                continue
            text = read_text(skill_md)
            if text is None:
                continue
            fm, findings = _check_one(info, skill_dir, skill_md, text)
            out.extend(findings)
            name = (fm.fields.get("name") if fm else None) or skill_dir.name
            normalised = "\n".join(ln.rstrip() for ln in text.strip().splitlines())
            by_name.setdefault(name, []).append((skill_md, normalised))
    for name, copies in sorted(by_name.items()):
        if len({c for _, c in copies}) > 1:
            rels = [info.rel(p) for p, _ in copies]
            out.append(
                Finding(
                    "skill",
                    "warning",
                    f"Skill '{name}' has copies with different content: {', '.join(rels)}",
                    [ev("one copy", r) for r in rels],
                )
            )
    return out


def _check_one(
    info: RepoInfo, skill_dir: Path, skill_md: Path, text: str
) -> tuple[Frontmatter | None, list[Finding]]:
    rel = info.rel(skill_md)
    fm = parse_frontmatter(text)
    if fm is None:
        why = (
            "unterminated YAML frontmatter"
            if text.lstrip().startswith("---")
            else "no YAML frontmatter"
        )
        return None, [
            Finding(
                "skill",
                "error",
                f"{rel} has {why}; agents need `name` and `description` to discover the skill",
                [ev("frontmatter", rel, 1, clip(text.splitlines()[0]) if text.strip() else None)],
            )
        ]
    out: list[Finding] = []

    lines = text.splitlines()

    def add(severity: Severity, summary: str, key: str | None) -> None:
        line = fm.lines.get(key, 1) if key else 1
        evidence = ev(key or "frontmatter", rel, line, clip(lines[line - 1]))
        out.append(Finding("skill", severity, summary, [evidence]))

    name = fm.fields.get("name", "")
    description = fm.fields.get("description", "")
    if not description:
        add(
            "error",
            f"{rel} has no `description`; agents use it to decide when to load the skill",
            None,
        )
    elif len(description) > MAX_DESCRIPTION:
        add(
            "warning",
            f"{rel} description is {len(description):,} characters (limit {MAX_DESCRIPTION:,})",
            "description",
        )
    if not name:
        if not rel.startswith(".claude/"):  # Claude Code falls back to the directory name
            add(
                "warning",
                f"{rel} has no `name` (required by Codex and the Agent Skills spec)",
                None,
            )
    else:
        if len(name) > MAX_NAME or not _NAME_OK.match(name):
            add(
                "warning",
                f"{rel} name '{clip(name, 70)}' must be lowercase letters, digits and hyphens, "
                f"at most {MAX_NAME} characters",
                "name",
            )
        if name != skill_dir.name:
            add(
                "warning",
                f"{rel} name '{clip(name, 70)}' does not match its directory '{skill_dir.name}'",
                "name",
            )
    doc = Doc(skill_md, rel, text)
    for i, target, is_link in _references(doc, fm.end):
        if (skill_dir / target).exists() or info.excluded(skill_dir / target):
            continue
        if not is_link and (info.repo / target).exists():  # commands run from the repo root
            continue
        out.append(
            Finding(
                "skill",
                "warning",
                f"{rel}:{i + 1} refers to {target}, which is missing from the skill directory",
                [ev("missing file", rel, i + 1, clip(doc.lines[i].strip()))],
            )
        )
    return fm, out


def _references(doc: Doc, start: int) -> list[tuple[int, str, bool]]:
    """Relative links, and ``scripts/...``-style paths in code, that point into the skill."""
    found: list[tuple[int, str, bool]] = []
    seen: set[str] = set()
    for i in range(start + 1, len(doc.prose)):
        links = [m.group(1) for m in _LINK.finditer(doc.blanked(i))]
        if doc.fence[i] is not None:
            code = [doc.lines[i]]
        else:
            code = [content for _, _, content in doc.spans(i)]
        tokens = [t.strip("'\"") for c in code for t in c.split()]
        for raw, is_link in [(t, True) for t in links] + [(t, False) for t in tokens]:
            target = _SKILL_VARS.sub("", raw.split("#", 1)[0])
            if raw in seen or not target or re.search(r"[<>{}$*?]|://|^[A-Za-z]+:", target):
                continue
            if target.startswith(("#", "/", "~", "../")):
                continue
            if is_link and not re.search(r"\.\w+$|/$", target):
                continue
            if not is_link and not target.startswith((*_RESOURCE_DIRS, "./")):
                continue
            seen.add(raw)
            found.append((i, target, is_link))
    return found
