"""Shell commands and tool choices that instruction files tell the agent to use."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass

from ruleproof.doctor._common import Doc, RepoInfo, clause_before, clip, is_negated

SHELL_LANGS: frozenset[str] = frozenset(
    [
        "sh",
        "bash",
        "shell",
        "console",
        "zsh",
        "fish",
        "powershell",
        "ps",
        "ps1",
        "pwsh",
        "cmd",
        "bat",
        "batch",
        "terminal",
        "text",
        "txt",
        "shellsession",
        "sh-session",
    ]
) | {""}

KNOWN_COMMANDS: frozenset[str] = frozenset(
    [
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "npx",
        "pnpx",
        "bunx",
        "uv",
        "uvx",
        "pip",
        "pip3",
        "pipx",
        "python",
        "python3",
        "py",
        "poetry",
        "pdm",
        "pipenv",
        "hatch",
        "rye",
        "conda",
        "mamba",
        "micromamba",
        "pytest",
        "tox",
        "nox",
        "make",
        "just",
        "cargo",
        "go",
        "rustc",
        "ruff",
        "black",
        "isort",
        "flake8",
        "pylint",
        "mypy",
        "pyright",
        "eslint",
        "prettier",
        "biome",
        "tsc",
        "jest",
        "vitest",
        "mocha",
        "playwright",
        "node",
        "deno",
        "git",
        "gh",
        "docker",
        "docker-compose",
        "kubectl",
        "helm",
        "terraform",
        "gradle",
        "gradlew",
        "./gradlew",
        "mvn",
        "./mvnw",
        "dotnet",
        "bundle",
        "rake",
        "rails",
        "php",
        "composer",
        "mix",
        "swift",
        "xcodebuild",
        "pre-commit",
        "lefthook",
        "turbo",
        "nx",
        "./scripts",
    ]
)

JS_PMS = ("npm", "pnpm", "yarn", "bun")
PY_PMS = ("pip", "uv", "poetry", "pdm", "pipenv")
TOOLS = ("pytest", "unittest", "jest", "vitest", "mocha", "black", "prettier", "biome", "eslint")
"""Test runners, formatters and linters recognised in commands."""

_INSTALL_SUBCOMMANDS = frozenset({"install", "i", "ci", "add", "sync"})
_PROMPT = re.compile(r"^\s*(?:\$|>|%|PS[^>]{0,40}>)\s+")
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_PROSE_TOOL = re.compile(
    r"\b(?:use|uses|using|run|runs|running|prefer|via|with)\s+(?:the\s+)?[*_]{0,2}"
    r"(npm|pnpm|yarn|bun|pip|uv|poetry|pdm|pipenv|pytest|unittest|jest|vitest|mocha|black|"
    r"prettier|biome|eslint)[*_]{0,2}\b(?![\w/-]|\.\w)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Command:
    doc: Doc
    index: int  # 0-based line index
    text: str  # one command (pipeline segment), prompt stripped
    tokens: tuple[str, ...]  # tokens[0] is lowercased, without .exe/.cmd
    cwd: str | None  # directory from a preceding ``cd`` on the same line
    negative: bool  # the surrounding prose says not to run it
    in_span: bool  # from an inline code span rather than a fenced block

    @property
    def line(self) -> int:
        return self.index + 1


def iter_commands(doc: Doc) -> Iterator[Command]:
    block_cwd: list[str | None] = [None]  # a ``cd`` in a fenced block applies to later lines
    for i, prose in enumerate(doc.prose):
        lang = doc.fence[i]
        if lang is not None:
            if lang in SHELL_LANGS:
                yield from _segments(doc, i, doc.lines[i], False, False, block_cwd)
            continue
        block_cwd = [None]
        for start, end, content in doc.spans(i):
            negative = is_negated(prose, start, end)
            yield from _segments(doc, i, content, negative, True, [None])


def _segments(
    doc: Doc, i: int, text: str, negative: bool, in_span: bool, cwd: list[str | None]
) -> Iterator[Command]:
    text = _PROMPT.sub("", text)
    if text.lstrip().startswith(("#", "//", "REM ")):
        return
    text = re.sub(r"\s+#\s.*$", "", text)
    for segment in _SPLIT.split(text):
        tokens = tokenize(segment)
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) > 1:
            cwd[0] = tokens[1] if cwd[0] is None else f"{cwd[0]}/{tokens[1]}"
            continue
        yield Command(doc, i, segment.strip(), tokens, cwd[0], negative, in_span)


def tokenize(segment: str) -> tuple[str, ...]:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
    while tokens and (_ENV_ASSIGN.match(tokens[0]) or tokens[0] in ("sudo", "time", "env")):
        tokens = tokens[1:]
    if not tokens:
        return ()
    head = tokens[0]
    if not head.startswith(("./", "../")):
        head = head.replace("\\", "/").rsplit("/", 1)[-1]
    head = re.sub(r"\.(?:exe|cmd|bat|ps1)$", "", head.lower())
    return (head, *tokens[1:])


def _installs_project(args: list[str]) -> bool:
    return any(
        a in (".", "-e", "--editable", "-r", "--requirement")
        or a.startswith(("-e", "-r", ".[", "./", "requirements"))
        for a in args
    )


def unwrap(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Strip runner prefixes: ``uv run``, ``npx``, ``python -m``, ``pnpm exec`` ..."""
    for _ in range(4):
        if not tokens:
            break
        head, rest = tokens[0], tokens[1:]
        sub = rest[0] if rest else ""
        if head in ("uv", "poetry", "pdm", "pipenv", "hatch", "rye", "pipx") and sub == "run":
            tokens = rest[1:]
        elif head in ("npx", "pnpx", "bunx", "uvx"):
            tokens = rest
        elif head in ("pnpm", "yarn", "npm", "bun") and sub in ("exec", "dlx", "x"):
            tokens = rest[1:]
        elif head in ("pnpm", "yarn", "bun") and sub in TOOLS:
            tokens = rest
        elif head.startswith("python") or head == "py":
            if sub != "-m":
                break
            tokens = rest[1:]
        else:
            break
        while tokens and tokens[0].startswith("-"):
            tokens = tokens[1:]
    return tokens


def tools_in(tokens: tuple[str, ...]) -> list[str]:
    """Package managers, test runners, formatters and linters a command invokes."""
    out: list[str] = []
    head, args = tokens[0], list(tokens[1:])
    sub = args[0] if args else ""
    if head in ("npm", "pnpm", "yarn"):
        if not {"-g", "--global"} & set(args) and sub != "global":
            out.append(head)
    elif head == "bun":
        out.append("bun")
    elif head in ("pip", "pip3") or (head.startswith("python") and args[:2] == ["-m", "pip"]):
        pip_args = args[2:] if head.startswith("python") else args
        if pip_args[:1] == ["install"] and _installs_project(pip_args[1:]):
            out.append("pip")
    elif head in PY_PMS:
        out.append(head)
    if sub in _INSTALL_SUBCOMMANDS or (head == "pip" and sub == "install"):
        return out
    inner = unwrap(tokens)
    if inner:
        name = "pytest" if inner[0] == "py.test" else inner[0]
        if name in TOOLS:
            out.append(name)
        elif name == "ruff" and inner[1:2] == ("format",):
            out.append("ruff-format")
    return out


@dataclass(frozen=True, slots=True)
class Mention:
    tool: str
    doc: Doc
    index: int
    excerpt: str
    hint: str | None  # subproject directory the mention is scoped to, if any

    @property
    def line(self) -> int:
        return self.index + 1

    @property
    def where(self) -> str:
        return f"{self.doc.rel}:{self.line}"


def tool_mentions(info: RepoInfo, doc: Doc) -> list[Mention]:
    """Positive (not negated) tool choices in commands and in prose like "use pnpm"."""
    out: list[Mention] = []
    for cmd in iter_commands(doc):
        if cmd.negative:
            continue
        for tool in tools_in(cmd.tokens):
            hint = scope_hint(info, doc, cmd.index, cmd.text, cmd.cwd)
            out.append(Mention(tool, doc, cmd.index, clip(cmd.text, 80), hint))
    for i, line in enumerate(doc.prose):
        if not line.strip() or doc.fence[i] is not None:
            continue
        blanked = doc.blanked(i)
        for m in _PROSE_TOOL.finditer(blanked):
            tool = m.group(1).lower()
            if is_negated(blanked, m.start(1), m.end(1)):
                continue
            if tool == "black" and "format" not in clause_before(blanked, m.start(1)).lower():
                continue
            excerpt = clip(line.strip().lstrip("-*+ ").strip(), 80)
            out.append(Mention(tool, doc, i, excerpt, scope_hint(info, doc, i, m.group(0), None)))
    return out


def scope_hint(info: RepoInfo, doc: Doc, i: int, own: str, cwd: str | None) -> str | None:
    """The subproject a mention is about, from a ``cd`` or a subproject named nearby."""
    base = info.rel(doc.dir)
    base = "" if base in (".", "") or base.startswith("/") else base
    if cwd:
        target = "/".join(p for p in (base, cwd.strip("./").rstrip("/")) if p)
        return target or None
    text = doc.heading_at(i) + " " + doc.prose[i].replace(own, " ")
    for sp in info.subprojects:
        name = sp[len(base) + 1 :] if base and sp.startswith(base + "/") else sp
        if base and not sp.startswith(base + "/"):
            continue
        if re.search(rf"(?<![\w/.-]){re.escape(name)}/?(?![\w-])", text):
            return sp
    return None
