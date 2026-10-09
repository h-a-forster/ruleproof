"""``doctor/repo-mismatch``: instructions that the repo's lockfiles and config contradict."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ruleproof.doctor._common import Doc, Finding, RepoInfo, clip, ev, is_negated, read_text
from ruleproof.doctor.conflict import display
from ruleproof.doctor.mentions import JS_PMS, PY_PMS, Mention, tool_mentions
from ruleproof.doctor.scan import Scan

JS_LOCKFILES: dict[str, str] = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "bun.lock": "bun",
    "bun.lockb": "bun",
}
PY_LOCKFILES: dict[str, str] = {
    "uv.lock": "uv",
    "poetry.lock": "poetry",
    "pdm.lock": "pdm",
    "Pipfile.lock": "pipenv",
}
JS_TEST_CONFIGS: dict[str, str] = {"jest.config": "jest", "vitest.config": "vitest"}
Version = tuple[int, ...]


def check_mismatch(info: RepoInfo, scan: Scan) -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for doc in scan.docs:
        for m in tool_mentions(info, doc):
            start = info.repo / m.hint if m.hint else doc.dir
            found = _tool_mismatch(info, m, start)
            if found is None or (doc.rel, m.tool) in seen:
                continue
            seen.add((doc.rel, m.tool))
            expected, source, path = found
            out.append(
                Finding(
                    "repo-mismatch",
                    "warning",
                    f"{m.where} uses {display(m.tool)} but the repo uses {expected} ({source})",
                    [
                        ev(f"uses {display(m.tool)}", doc.rel, m.line, m.excerpt),
                        ev(f"the repo uses {expected}", path),
                    ],
                )
            )
        out.extend(_version_mismatches(info, doc))
    return out


Fact = tuple[str, str, str]
"""(tool, what shows it, repo-relative path)."""


def _tool_mismatch(info: RepoInfo, m: Mention, start: Path) -> Fact | None:
    if m.tool in JS_PMS:
        if m.tool == "bun" and not re.match(
            r"bun\s+(?:install|i|add|remove|rm|update)\b", m.excerpt
        ):
            return None
        expected = _js_package_manager(info, start)
    elif m.tool in PY_PMS:
        expected = _py_package_manager(info, start)
    elif m.tool in ("jest", "vitest", "mocha"):
        runners = _js_test_runners(info, start)
        if runners is None or m.tool in runners[0]:
            return None
        return " / ".join(sorted(runners[0])), runners[1], runners[1]
    elif m.tool == "unittest":
        expected = _pytest_config(info, start)
    else:
        return None
    if expected is None or expected[0] == m.tool:
        return None
    return expected


def _walk_up(info: RepoInfo, start: Path) -> Iterator[Path]:
    here = start
    while True:
        yield here
        if here == info.repo or info.repo not in here.parents:
            return
        here = here.parent


def _js_package_manager(info: RepoInfo, start: Path) -> Fact | None:
    for here in _walk_up(info, start):
        found = {
            tool: (info.rel(here / name), info.rel(here / name))
            for name, tool in JS_LOCKFILES.items()
            if info.is_file(here / name)
        }
        pkg = info.json(here / "package.json")
        declared = pkg.get("packageManager") if isinstance(pkg, dict) else None
        if isinstance(declared, str) and declared.split("@")[0] in JS_PMS:
            rel = info.rel(here / "package.json")
            found.setdefault(declared.split("@")[0], (f"{rel} packageManager", rel))
        if found:
            if len(found) != 1:
                return None
            tool, (source, path) = next(iter(found.items()))
            return tool, source, path
    return None


def _py_package_manager(info: RepoInfo, start: Path) -> Fact | None:
    for here in _walk_up(info, start):
        found = {tool: name for name, tool in PY_LOCKFILES.items() if info.is_file(here / name)}
        if not found:
            text = (
                read_text(here / "pyproject.toml")
                if info.is_file(here / "pyproject.toml")
                else None
            )
            if text and re.search(r"^\[tool\.poetry\]", text, re.MULTILINE):
                found["poetry"] = "pyproject.toml"
        if found:
            if len(found) != 1:
                return None
            tool, name = next(iter(found.items()))
            return tool, info.rel(here / name), info.rel(here / name)
    return None


def _js_test_runners(info: RepoInfo, start: Path) -> tuple[set[str], str] | None:
    pkg_path = info.nearest(start, ("package.json",))
    if pkg_path is None:
        return None
    pkg = info.json(pkg_path)
    if not isinstance(pkg, dict):
        return None
    present: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        deps = pkg.get(key)
        if isinstance(deps, dict):
            present |= {t for t in ("jest", "vitest", "mocha") if t in deps}
    present |= {t for t in ("jest", "mocha") if t in pkg}
    for child in pkg_path.parent.iterdir():
        stem = child.name.split(".")
        if len(stem) >= 3 and ".".join(stem[:2]) in JS_TEST_CONFIGS:
            present.add(JS_TEST_CONFIGS[".".join(stem[:2])])
        elif child.name.startswith(".mocharc"):
            present.add("mocha")
    return (present, info.rel(pkg_path)) if present else None


def _pytest_config(info: RepoInfo, start: Path) -> Fact | None:
    for here in _walk_up(info, start):
        for name, marker in (
            ("pytest.ini", None),
            ("pyproject.toml", r"^\[tool\.pytest\.ini_options\]"),
            ("setup.cfg", r"^\[tool:pytest\]"),
            ("tox.ini", r"^\[pytest\]"),
            ("conftest.py", None),
        ):
            path = here / name
            if not info.is_file(path):
                continue
            if marker is None or re.search(marker, read_text(path) or "", re.MULTILINE):
                return "pytest", f"{info.rel(path)} configures pytest", info.rel(path)
    return None


# --- Versions -------------------------------------------------------------------------------

_NOT_RANGE = r"(?!\.?\d|\s*(?:[-–]|to|through|and)\s*v?\d)"
_PY_STATED = re.compile(
    r"\bpython\s*(?:version\s*)?(>=|≥|\^|~=|==)?\s*(3\.\d{1,2})(?:\.\d+)?"
    r"(\+|\s+or\s+(?:newer|later|higher|above))?" + _NOT_RANGE,
    re.IGNORECASE,
)
_NODE_STATED = re.compile(
    r"\bnode(?:\.?js)?\s*(?:version\s*)?(>=|≥|\^|~)?\s*v?(\d{1,2})(?:\.\d+){0,2}"
    r"(\+|\.x|\s+or\s+(?:newer|later|higher|above))?" + _NOT_RANGE,
    re.IGNORECASE,
)


_REQUIREMENT = re.compile(
    r"\b(?:requir\w*|support\w*|target\w*|use|uses|using|install\w*|need\w*|minimum|min|"
    r"version|compatib\w*|run|runs|running|develop\w*|pinned|must)\b",
    re.IGNORECASE,
)


def _ver(text: str) -> Version:
    return tuple(int(p) for p in text.split("."))


def _vtext(v: Version) -> str:
    return ".".join(str(p) for p in v)


VersionFact = tuple[Version, str, str]
"""(version, what states it, repo-relative path)."""


def _python_facts(info: RepoInfo, start: Path) -> tuple[VersionFact | None, VersionFact | None]:
    pinned: VersionFact | None = None
    minimum: VersionFact | None = None
    pv = info.nearest(start, (".python-version",))
    if pv is not None:
        m = re.search(r"^\s*(?:cpython-|python)?(3\.\d+)", read_text(pv) or "", re.MULTILINE)
        if m:
            pinned = (_ver(m.group(1)), info.rel(pv), info.rel(pv))
    pp = info.nearest(start, ("pyproject.toml",))
    if pp is not None:
        text = read_text(pp) or ""
        req = re.search(r"^\s*requires-python\s*=\s*[\"']([^\"']+)", text, re.MULTILINE)
        low = re.search(r"(?:>=|~=|==|\^)\s*(3\.\d+)", req.group(1)) if req else None
        if req and low:
            what = f'{info.rel(pp)} requires-python "{req.group(1)}"'
            minimum = (_ver(low.group(1)), what, info.rel(pp))
    return pinned, minimum


def _node_facts(info: RepoInfo, start: Path) -> tuple[VersionFact | None, VersionFact | None]:
    pinned: VersionFact | None = None
    minimum: VersionFact | None = None
    nv = info.nearest(start, (".nvmrc", ".node-version"))
    if nv is not None:
        m = re.match(r"\s*v?(\d+)", read_text(nv) or "")
        if m:
            pinned = (_ver(m.group(1)), info.rel(nv), info.rel(nv))
    pkg_path = info.nearest(start, ("package.json",))
    pkg = info.json(pkg_path) if pkg_path else None
    engines = pkg.get("engines") if isinstance(pkg, dict) else None
    node = engines.get("node") if isinstance(engines, dict) else None
    if pkg_path and isinstance(node, str) and "||" not in node:
        low = re.match(r"\s*(?:>=|\^|~|=)?\s*v?(\d+)", node)
        if low:
            what = f'{info.rel(pkg_path)} engines.node "{node}"'
            minimum = (_ver(low.group(1)), what, info.rel(pkg_path))
    return pinned, minimum


def _version_mismatches(info: RepoInfo, doc: Doc) -> list[Finding]:
    out: list[Finding] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc.prose):
        if not raw.strip():
            continue
        line = raw.replace("`", " ")
        if not _REQUIREMENT.search(line):
            continue
        for lang, pattern, facts, digits in (
            ("Python", _PY_STATED, _python_facts, 2),
            ("Node", _NODE_STATED, _node_facts, 1),
        ):
            for m in pattern.finditer(line):
                if is_negated(line, m.start(), m.end()):
                    continue
                stated = _ver(m.group(2))[:digits]
                at_least = bool(m.group(1) and m.group(1) != "==") or bool(m.group(3))
                problem = _compare(stated, at_least, *facts(info, doc.dir))
                if problem is None or problem[0] in seen:
                    continue
                seen.add(problem[0])
                said = f"{lang} {_vtext(stated)}{'+' if at_least else ''}"
                out.append(
                    Finding(
                        "repo-mismatch",
                        "warning",
                        f"{doc.rel}:{i + 1} says {said} but {problem[0]}",
                        [
                            ev(f"says {said}", doc.rel, i + 1, clip(raw.strip())),
                            ev(problem[0], problem[1]),
                        ],
                    )
                )
    return out


def _compare(
    stated: Version, at_least: bool, pinned: VersionFact | None, minimum: VersionFact | None
) -> tuple[str, str] | None:
    """(what disagrees, its path), or None when the statement agrees with the repo."""
    if at_least:
        if minimum and minimum[0] != stated:
            return f"{minimum[1]} sets the minimum to {_vtext(minimum[0])}", minimum[2]
        return None
    if pinned and pinned[0][: len(stated)] != stated:
        return f"{pinned[1]} pins {_vtext(pinned[0])}", pinned[2]
    if minimum and stated < minimum[0]:
        return f"{minimum[1]} sets the minimum to {_vtext(minimum[0])}", minimum[2]
    return None
