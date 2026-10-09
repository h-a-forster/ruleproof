"""Compile prose instruction files into a starter ``ruleproof.toml``.

The compiler is heuristic and deterministic. ``extract_directives`` finds imperative statements
in Markdown; a fixed set of recognisers turns the statements whose phrasing they understand into
rules; everything else is reported as uncovered, so the user sees what is *not* checked.
Precision beats recall: a recogniser only fires on phrasing it is confident about, and weaker
matches are emitted with ``severity = "warning"``.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ruleproof.errors import ConfigError
from ruleproof.instructions import SKIP_DIRS
from ruleproof.models import Rule, Severity

DirectiveKind = Literal["must", "never", "prefer", "other"]

MIN_CONFIDENCE = 0.5
"""Matches below this confidence are dropped."""
WARNING_BELOW = 0.8
"""Rules below this confidence are emitted as warnings."""


@dataclass(frozen=True, slots=True)
class Directive:
    file: str  # repo-relative posix path of the instruction file
    line: int  # 1-based line where the statement starts
    text: str  # the statement, Markdown emphasis and links removed, code spans kept
    kind: DirectiveKind
    lead: str = ""  # the heading or lead-in that changes its meaning ("## Never"), if any

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(slots=True)
class CompiledRule:
    rule: Rule
    directive: Directive
    confidence: float
    pattern: str  # name of the recogniser that produced the rule


@dataclass(slots=True)
class CompileResult:
    files: list[str] = field(default_factory=list)
    directives: list[Directive] = field(default_factory=list)
    rules: list[CompiledRule] = field(default_factory=list)
    uncovered: list[Directive] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Share of directives that produced at least one rule (0.0 when there are none)."""
        if not self.directives:
            return 0.0
        return (len(self.directives) - len(self.uncovered)) / len(self.directives)


# --------------------------------------------------------------------------- extraction

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_SETEXT = re.compile(r"^\s{0,3}(?:=+|-+)\s*$")
_HRULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?")
_TABLE = re.compile(r"^\s*\|")
_QUOTE = re.compile(r"^\s*(?:>\s?)+")
_SHELL_FENCES = frozenset(
    {"", "sh", "bash", "shell", "console", "zsh", "fish", "powershell", "pwsh", "ps1", "cmd"}
)

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "incl.")
_SENTENCE_END = re.compile(r"(?<=[.!?])(?:\*\*|__|\*|_)?\s+(?=[A-Z`*_\"'(\[])")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_MODAL = re.compile(
    r"\b(?:must|never|always|don'?t|do\s+not|avoid|should(?:n'?t)?|prefer(?:red|ably)?"
    r"|make\s+sure|ensure|required?|need\s+to|needs\s+to|have\s+to|has\s+to|mustn'?t"
    r"|cannot|can'?t|not\s+allowed|forbidden|prohibited|instead\s+of|rather\s+than"
    r"|only\s+use|be\s+sure|remember\s+to|important)\b",
    re.IGNORECASE,
)
_IMPERATIVE_VERBS = _words(
    """
    add annotate apply ask avoid build bump call check clean commit configure create declare
    define delete document do don't edit ensure execute explain export fix follow format generate
    give group handle import include install invoke keep label leave limit lint log make mark
    match mock move name never open pass pin place prefer prefix push put raise re-run read
    regenerate remove rename replace report request return reuse review run search separate set
    sort split start stick stop store test throw treat try update use validate verify wait wrap
    write always only
    """
)
_LABEL = re.compile(r"^(?:[\w`'/ .-]{1,40}?):\s+(?=\S)")
_NEVER_WORDS = re.compile(
    r"\b(?:never|don'?t|do\s+not|must\s+not|mustn'?t|should\s+not|shouldn'?t|avoid|cannot|"
    r"can'?t|no\s+longer|not\s+allowed|forbidden|prohibited)\b|^no\s",
    re.IGNORECASE,
)
_PREFER_WORDS = re.compile(
    r"\b(?:prefer(?:red|ably)?|should|recommended|instead\s+of|rather\s+than|favou?r|ideally)\b",
    re.IGNORECASE,
)
_NEGATIVE_MARKS = ("❌", "🚫", "⛔", "✗", "✘")


@dataclass(slots=True)
class _Block:
    parts: list[tuple[int, str]] = field(default_factory=list)
    is_item: bool = False
    lead: str = ""  # heading or "...:" lead-in that governs a list item
    fence_commands: list[str] = field(default_factory=list)


_NEGATIVE_HEADING = re.compile(
    r"(?:never|don'?ts?|do\s+not|avoid|forbidden|prohibited(?:\s+\w+)?|anti-?patterns|things\s+to"
    r"\s+avoid|what\s+not\s+to\s+do|never\s+do(?:\s+this)?|not\s+allowed)\W*"
)
_NEGATIVE_LEAD = re.compile(
    r"(?:you\s+)?(?:must\s+never|should\s+never|never|do\s+not|don'?t|avoid|must\s+not)\b"
    r"[^.:]{0,40}:"
)
_DONE_LEAD = re.compile(
    r"\bbefore\s+(?:you\s+)?(?:\w+\s+)?(?:commit|submit|push|finish|open|creat|mark|complet|send"
    r"|hand)\w*|\bdefinition\s+of\s+done\b|\bpre-?commit\s+checks?\b|\b(?:pr|pull\s+request"
    r"|submission|pre-?merge|completion)\s+checklist\b"
)


def _lead_kind(lead: str) -> str:
    low = lead.lower().strip()
    if not low or len(low) > 80:
        return ""
    if _NEGATIVE_HEADING.fullmatch(low) or _NEGATIVE_LEAD.fullmatch(low):
        return "never"
    if _DONE_LEAD.search(low):
        return "done"
    return ""


def _with_lead(text: str, lead: str) -> str:
    """The statement as it reads in context: "- `npm install`" under "## Never" is a ban."""
    kind = _lead_kind(lead)
    if not kind:
        return text
    lowered = text[:1].lower() + text[1:] if not text[1:2].isupper() else text
    if kind == "never":
        return text if _NEVER_WORDS.search(text) else "Never " + lowered
    if text.startswith("`"):
        return "Before finishing, run " + text
    first = re.match(r"[A-Za-z][\w'-]*", text)
    if first is None or first.group(0).lower() not in _IMPERATIVE_VERBS:
        return text  # a description under the heading, not a step
    return "Before finishing, " + lowered


def extract_directives(text: str, rel_path: str) -> list[Directive]:
    """Imperative statements in a Markdown instruction file, in document order.

    Headings, tables, code fences, HTML comments and front matter are skipped. Each list item
    and paragraph is split into sentences; a sentence is a directive when it contains a modal
    ("must", "never", "prefer", ...) or starts with an imperative verb. List items are read in
    the context of their heading or lead-in ("## Never", "Before committing:"), and a sentence
    ending in ``:`` directly followed by a shell code fence gets the fence's commands appended
    as code spans (``Before committing, run: `cargo fmt` `cargo test```).
    """
    out: list[Directive] = []
    for block in _blocks(text.splitlines()):
        for k, (line, sentence) in enumerate(_sentences(block)):
            clean = _clean(sentence)
            read = _with_lead(clean, block.lead) if block.is_item and k == 0 else clean
            if _is_directive(read) or (read != clean and "`" in clean):
                lead = block.lead if read != clean else ""
                out.append(Directive(rel_path, line, clean, _kind(read), lead))
    return out


def _blocks(lines: list[str]) -> list[_Block]:
    blocks: list[_Block] = []
    cur: _Block | None = None
    fence: str | None = None
    fence_owner: _Block | None = None
    in_comment = False
    heading = ""
    colon_lead = ""  # a paragraph ending in ":" right before a list
    item_leads: list[tuple[int, str]] = []  # (indent, text) of items ending in ":"
    start = 0
    if lines and lines[0].strip() == "---":  # YAML front matter (.mdc rules, skills)
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                start = j + 1
                break

    def close() -> None:
        nonlocal cur, colon_lead
        if cur is None:
            return
        blocks.append(cur)
        if not cur.is_item:
            text = " ".join(t for _, t in cur.parts).rstrip()
            colon_lead = _clean(text) if text.endswith(":") else ""
            item_leads.clear()
        cur = None

    for i in range(start, len(lines)):
        lineno, raw = i + 1, lines[i].rstrip()
        if fence is not None:
            if raw.strip().startswith(fence):
                fence = None
                fence_owner = None
            elif fence_owner is not None:
                cmd = _fence_command(raw)
                if cmd and len(fence_owner.fence_commands) < 6:
                    fence_owner.fence_commands.append(cmd)
            continue
        if in_comment:
            in_comment = "-->" not in raw
            continue
        m = _FENCE.match(raw)
        if m:
            close()
            fence = m.group(1)
            last = blocks[-1] if blocks else None
            if (
                last is not None
                and last.parts[-1][1].rstrip().endswith(":")
                and m.group(2).lower() in _SHELL_FENCES
            ):
                fence_owner = last
            colon_lead = ""
            continue
        stripped = raw.strip()
        if stripped.startswith("<!--"):
            in_comment = "-->" not in stripped
            continue
        if not stripped:
            close()
            continue
        if _HEADING.match(raw):
            close()
            heading = _clean(stripped.lstrip("#").strip())
            colon_lead = ""
            item_leads.clear()
            continue
        if _TABLE.match(raw) or _HRULE.match(raw):
            close()
            colon_lead = ""
            continue
        if _SETEXT.match(raw) and cur is not None and len(cur.parts) == 1 and not cur.is_item:
            heading = _clean(cur.parts[0][1])
            cur = None
            colon_lead = ""
            continue
        body = _QUOTE.sub("", raw)
        item = _LIST_ITEM.match(body)
        if item:
            close()
            indent = len(body) - len(body.lstrip())
            while item_leads and item_leads[-1][0] >= indent:
                item_leads.pop()
            lead = item_leads[-1][1] if item_leads else colon_lead or heading
            text = body[item.end() :]
            cur = _Block([(lineno, text)], is_item=True, lead=lead)
            if text.rstrip().endswith(":"):
                item_leads.append((indent, _clean(text)))
        elif cur is None:
            cur = _Block([(lineno, body.strip())])
        else:
            cur.parts.append((lineno, body.strip()))
    close()
    return blocks


def _fence_command(line: str) -> str:
    cmd = line.strip()
    if not cmd or cmd.startswith(("#", "//", "REM ")):
        return ""
    for prompt in ("$ ", "> ", "% ", "PS> "):
        if cmd.startswith(prompt):
            cmd = cmd[len(prompt) :]
    cmd = re.sub(r"\s+#.*$", "", cmd).strip()
    return "" if "`" in cmd else cmd


def _sentences(block: _Block) -> Iterator[tuple[int, str]]:
    joined = ""
    starts: list[tuple[int, int]] = []  # (offset in joined, line number)
    for lineno, text in block.parts:
        if joined:
            joined += " "
        starts.append((len(joined), lineno))
        joined += text.strip()
    masked = _CODE_SPAN.sub(lambda m: "`" + "x" * (len(m.group(0)) - 2) + "`", joined)
    masked = re.sub(r"\]\([^)]*\)", lambda m: "]" + "x" * (len(m.group(0)) - 1), masked)

    cuts = [0]
    for m in _SENTENCE_END.finditer(masked):
        before = masked[: m.start()].lower()
        if not before.endswith(_ABBREVIATIONS):
            cuts.append(m.end())
    cuts.append(len(joined))

    pieces = [(cuts[k], joined[cuts[k] : cuts[k + 1]].strip()) for k in range(len(cuts) - 1)]
    pieces = [(off, s) for off, s in pieces if s]
    if block.fence_commands and pieces and pieces[-1][1].endswith(":"):
        off, s = pieces[-1]
        pieces[-1] = (off, s + " " + " ".join(f"`{c}`" for c in block.fence_commands))
    for off, s in pieces:
        line = starts[0][1]
        for start, lineno in starts:
            if start <= off:
                line = lineno
        yield line, s


def _clean(text: str) -> str:
    parts = re.split(r"(`[^`\n]+`)", text)
    for k in range(0, len(parts), 2):  # outside code spans only
        p = parts[k]
        p = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", p)
        p = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", p)
        p = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", p)
        p = re.sub(r"</?[a-zA-Z][^>]*>", "", p)
        p = re.sub(r"(\*\*|__)(.+?)\1", r"\2", p)
        p = re.sub(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", r"\1", p)
        p = re.sub(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])", r"\1", p)
        p = p.replace("**", "")
        parts[k] = p
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _is_directive(clean: str) -> bool:
    if len(clean) < 4 or (clean.endswith(":") and len(clean.split()) <= 4):
        return False  # too short, or a label introducing a list
    if _MODAL.search(clean) or clean.startswith(_NEGATIVE_MARKS):
        return True
    if "`" in clean and _DONE_LEAD.search(clean.lower()):
        return True
    if re.match(r"(?i)no\s+(?:\w+\s+)?`", clean):  # "No `print()` calls."
        return True
    body = _LABEL.sub("", clean, count=1)
    m = re.match(r"[A-Za-z][\w'-]*", body)
    return m is not None and m.group(0).lower() in _IMPERATIVE_VERBS


def _kind(clean: str) -> DirectiveKind:
    if _NEVER_WORDS.search(clean) or clean.startswith(_NEGATIVE_MARKS):
        return "never"
    if _PREFER_WORDS.search(clean):
        return "prefer"
    if _MODAL.search(clean) or _DONE_LEAD.search(clean.lower()):
        return "must"
    body = _LABEL.sub("", clean, count=1)
    m = re.match(r"[A-Za-z][\w'-]*", body)
    if m is not None and m.group(0).lower() in _IMPERATIVE_VERBS:
        return "must"
    return "other"


# --------------------------------------------------------------------------- sentence model


@dataclass(frozen=True, slots=True)
class _Span:
    text: str
    start: int  # offset of the opening backtick in the sentence
    end: int  # offset just past the closing backtick


@dataclass(slots=True)
class _Sentence:
    text: str
    low: str
    spans: list[_Span]

    @classmethod
    def of(cls, d: Directive) -> _Sentence:
        text = _with_lead(d.text, d.lead)
        for mark in _NEGATIVE_MARKS:
            if text.startswith(mark):
                text = "Never use " + text[len(mark) :].lstrip(" :-")
                break
        spans = [_Span(m.group(1).strip(), m.start(), m.end()) for m in _CODE_SPAN.finditer(text)]
        low = "".join(c.lower() if len(c.lower()) == 1 else c for c in text)  # keep offsets
        return cls(text, low, spans)

    def chains(self) -> list[list[_Span]]:
        """Runs of code spans joined only by commas, slashes, "and", "or" or "nor"."""
        out: list[list[_Span]] = []
        for span in self.spans:
            if out:
                between = self.low[out[-1][-1].end : span.start]
                if _CHAIN_JOINER.fullmatch(between):
                    out[-1].append(span)
                    continue
            out.append([span])
        return out

    def before(self, span: _Span) -> str:
        return self.low[: span.start]

    def after(self, span: _Span) -> str:
        return self.low[span.end :]


_CHAIN_JOINER = re.compile(r"[\s,/]*(?:(?:and/or|or|and|nor|then|&&|;)\s*)?[\s,]*")


@dataclass(slots=True)
class _Emit:
    check: str
    params: dict[str, Any]
    confidence: float
    pattern: str
    label: str  # short human key, used for the rule id
    description: str
    severity: Severity | None = None  # None: derived from confidence


@dataclass(slots=True)
class _Repo:
    root: Path
    dirs: frozenset[str]
    files: frozenset[str]
    extensions: frozenset[str]

    @classmethod
    def scan(cls, root: Path, limit: int = 5000) -> _Repo:
        dirs: set[str] = set()
        files: set[str] = set()
        exts: set[str] = set()
        seen = 0
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                )
                rel = Path(dirpath).relative_to(root).as_posix()
                if rel == ".":
                    dirs.update(dirnames)
                    files.update(filenames)
                for name in filenames:
                    exts.add(Path(name).suffix.lower())
                seen += len(filenames)
                if seen > limit:
                    break
            dirs.update(d for d in (".changeset",) if (root / d).is_dir())
        return cls(root, frozenset(dirs), frozenset(files), frozenset(exts))

    def source_globs(self) -> tuple[list[str], bool]:
        """Globs for "the code" and whether they came from the layout (False: a generic guess)."""
        for top in ("src", "lib"):
            if top in self.dirs:
                return [f"{top}/**"], True
        tops = [d for d in ("packages", "crates", "apps", "libs") if d in self.dirs]
        if tops:
            return [f"{d}/**" for d in tops], True
        present = [g for g in _CODE_GLOBS if Path(g).suffix in self.extensions]
        return (present, True) if present else (list(_CODE_GLOBS), False)

    def changelog(self) -> str | None:
        for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "CHANGELOG.rst", "CHANGES.rst"):
            if name in self.files:
                return name
        if "changelog" in self.dirs:
            return "changelog/"
        return None

    def code_globs(self) -> list[str]:
        present = [g for g in _CODE_GLOBS if Path(g).suffix in self.extensions]
        return present or list(_CODE_GLOBS)

    def manifests(self) -> list[str]:
        found = [m for m in _MANIFESTS if m in self.files]
        return found or list(_MANIFESTS)


_CODE_GLOBS = (
    "*.py",
    "*.ts",
    "*.tsx",
    "*.js",
    "*.jsx",
    "*.rs",
    "*.go",
    "*.java",
    "*.kt",
    "*.c",
    "*.cc",
    "*.cpp",
    "*.h",
    "*.cs",
    "*.rb",
    "*.swift",
    "*.php",
)
_TEST_GLOBS = (
    "**/tests/**",
    "**/test/**",
    "**/__tests__/**",
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.*",
    "*.spec.*",
    "tests.rs",
)
_DOC_GLOBS = ("*.md", "*.rst", "docs/**")
_MANIFESTS = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "requirements-dev.txt",
    "Gemfile",
    "composer.json",
)
_PY = ["*.py", "*.pyi"]
_JS = ["*.js", "*.jsx", "*.mjs", "*.cjs", "*.ts", "*.tsx", "*.mts", "*.cts", "*.vue", "*.svelte"]
_TS = ["*.ts", "*.tsx", "*.mts", "*.cts"]
_RS = ["*.rs"]
_LANGUAGE_GLOBS: tuple[tuple[re.Pattern[str], list[str]], ...] = (
    (re.compile(r"\bpython\b"), _PY),
    (re.compile(r"\brust\b"), _RS),
    (re.compile(r"\btypescript\b|\bts\b"), _TS),
    (re.compile(r"\bjavascript\b|\bjs\b"), _JS),
    (re.compile(r"\bgo\s+(?:code|files?|packages?|sources?)\b|\bgolang\b"), ["*.go"]),
)

# --------------------------------------------------------------------------- commands

_KNOWN_TOOLS = _words(
    """
    cargo rustc rustfmt rustup go gofmt goimports golangci-lint npm npx pnpm pnpx yarn bun bunx
    deno node tsc eslint prettier biome jest vitest playwright turbo nx lerna tsx ts-node python
    python3 py pip pip3 pipx uv uvx poetry pdm hatch rye pipenv conda mamba pytest tox nox ruff
    black isort flake8 pylint mypy pyright basedpyright ty pre-commit prek make just cmake ctest
    ninja bazel bazelisk buck2 gradle ./gradlew gradlew mvn ./mvnw dotnet git gh docker
    docker-compose podman kubectl helm terraform sudo rm curl wget brew mix rebar3 bundle rake
    rails ruby rspec rubocop composer phpunit swift xcodebuild flutter dart cabal stack zig meson
    mise nix sh bash pwsh shellcheck shfmt codespell typos taplo actionlint markdownlint
    hadolint buf protoc alembic maturin vite webpack rollup esbuild changeset wrangler prisma
    drizzle-kit flox hogli x.py ./x.py ./x django-admin ./manage.py manage.py coverage sphinx-build
    mkdocs pnpm-workspace uvicorn gunicorn yamllint
    """
)
_RUNNERS: tuple[tuple[str, ...], ...] = (
    ("uv", "run"),
    ("uvx",),
    ("npx",),
    ("pnpm", "exec"),
    ("pnpm", "dlx"),
    ("pnpx",),
    ("bunx",),
    ("bun", "x"),
    ("yarn", "dlx"),
    ("npm", "exec"),
    ("python", "-m"),
    ("python3", "-m"),
    ("py", "-m"),
    ("poetry", "run"),
    ("hatch", "run"),
    ("pdm", "run"),
    ("rye", "run"),
    ("pipenv", "run"),
)
_JS_PMS = frozenset({"npm", "pnpm", "yarn", "bun"})
_INTERPRETERS = frozenset({"python", "python3", "py", "node", "bash", "sh", "pwsh", "deno"})
_PLACEHOLDER = re.compile(r"[<>{}]|\.\.\.(?!/)|…|\$\{|\bfoo\b|\bxxx\b", re.IGNORECASE)
_FILE_EXT = re.compile(
    r"\.(?:md|mdx|mdc|rst|txt|json|jsonc|ya?ml|toml|lock|ini|cfg|env|py|pyi|rs|ts|tsx|js|jsx|"
    r"mjs|cjs|mts|cts|go|sum|mod|sh|ps1|bat|snap|html|css|scss|sql|proto|c|cc|cpp|h|hpp|java|kt|"
    r"swift|rb|php|xml|csv|gradle|bzl|bazel|vue|svelte|ipynb|svg|png)$",
    re.IGNORECASE,
)
_CHECK_WORDS = re.compile(
    r"test|lint|check|fmt|format|clippy|typecheck|type-check|tsc|mypy|pyright|ruff|eslint|"
    r"prettier|biome|build|verify|validate|pre-commit|prek|precommit|\bvet\b|golangci|black|"
    r"isort|flake8|pytest|jest|vitest|\btox\b|\bnox\b|\bci\b|compile|tidy|spell|codespell|"
    r"nextest|snapshot|preflight|\bty\b|coverage|audit",
    re.IGNORECASE,
)
_NOT_A_CHECK = re.compile(
    r"^(?:cd|export|source|set|echo|cat|ls|git|gh|curl|wget|open|code)\b"
    r"|\b(?:install|sync|add|dev|start|serve|watch|up|init|login|clone)\b",
    re.IGNORECASE,
)
_SPECIAL = set(".^$*+?{}[]|()\\")


def _esc(token: str) -> str:
    """``re.escape`` that leaves ``-``, ``/``, ``:`` and friends readable."""
    return "".join("\\" + c if c in _SPECIAL else c for c in token)


def _anchor(token: str) -> str:
    return r"\b" if token[:1].isalnum() or token[:1] == "_" else ""


def _end(token: str) -> str:
    return r"\b" if token[-1:].isalnum() or token[-1:] == "_" else ""


def _segments(cmd: str) -> list[list[str]]:
    """Shell command split on ``&&``, ``||``, ``;`` and ``|``, as token lists (``cd`` dropped)."""
    out: list[list[str]] = []
    for seg in re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd.strip()):
        toks = seg.split()
        while toks and toks[0] in ("$", ">", "%"):
            toks = toks[1:]
        while toks and re.fullmatch(r"[A-Z_][A-Z0-9_]*=\S*", toks[0]):
            toks = toks[1:]
        if toks and toks[0] not in ("cd", "export", "source", "set", "pushd"):
            out.append(toks)
    return out


def _strip_runner(toks: list[str]) -> list[str]:
    changed = True
    while changed:
        changed = False
        for runner in _RUNNERS:
            n = len(runner)
            if tuple(toks[:n]) == runner and len(toks) > n:
                toks = toks[n:]
                while len(toks) > 1 and toks[0].startswith("-"):
                    takes_value = toks[0] in _VALUE_FLAGS
                    toks = toks[2:] if takes_value and len(toks) > 2 else toks[1:]
                changed = True
    return toks


_VALUE_FLAGS = _words(
    "--group --only-group --with --with-requirements --extra --package --python -p --directory "
    "--project --env-file --index -C --filter -F --dir --workspace -w"
)
_SINGLE_PURPOSE = _words(
    "pytest mypy pyright basedpyright black isort flake8 pylint tsc eslint jest shellcheck "
    "codespell typos"
)
"""Tools whose arguments are targets, not subcommands: ``mypy src`` is just ``mypy``."""


def _is_command(span: str) -> bool:
    segs = _segments(span)
    if not segs:
        return False
    first = segs[0][0]
    return first in _KNOWN_TOOLS or first.startswith("./") or bool(_SCRIPT.fullmatch(first))


_SCRIPT = re.compile(
    r"(?:[\w.-]+/)*[\w.-]+\.(?:sh|py|js|mjs|ts|ps1)|(?:scripts|bin|tools)/[\w./-]+"
)


def _looks_like_path_token(tok: str) -> bool:
    return "/" in tok or "*" in tok or "=" in tok or bool(_FILE_EXT.search(tok)) or tok == "."


def _require_regex(cmd: str) -> list[tuple[str, str]]:
    """(regex, label) for each checkable segment of a command a rule says to run."""
    out: list[tuple[str, str]] = []
    for seg in _segments(cmd):
        toks = _strip_runner(seg)
        keep: list[str] = []
        for i, tok in enumerate(toks):
            if _PLACEHOLDER.search(tok):
                break
            if i > 0 and tok.startswith("+"):
                continue
            if i > 0 and (tok.startswith("-") or _looks_like_path_token(tok)):
                if keep and keep[0] in _INTERPRETERS and len(keep) == 1 and "/" in tok:
                    keep.append(tok)
                break
            keep.append(tok)
            if len(keep) == 3 or keep[0] in _SINGLE_PURPOSE:
                break
        if not keep:
            continue
        head = keep[0]
        if head in _JS_PMS:
            rest = keep[1:]
            if rest and rest[0] == "run":
                rest = rest[1:]
            if not rest:
                continue
            body = r"\s+".join(_esc(t) for t in rest)
            regex = rf"\b{head}\s+(?:run\s+)?{body}(?![\w:-])"
        else:
            if len(keep) == 1 and head in _MULTI_COMMAND:
                continue  # a bare multi-command tool says nothing about what to run
            regex = _anchor(head) + r"\s+".join(_esc(t) for t in keep) + _end(keep[-1])
        out.append((regex, " ".join(keep)))
    return out


def _forbid_regex(cmd: str) -> tuple[str, str] | None:
    """(regex, label) for a command a rule forbids; flags are kept because they matter."""
    segs = _segments(cmd)
    if not segs:
        return None
    toks = _strip_runner(segs[0])
    if len(toks) == 1 and toks[0] in _FAMILY_FORBID:
        return _FAMILY_FORBID[toks[0]], toks[0]
    if len(toks) == 2 and toks[0] in ("pip", "pip3") and toks[1] == "install":
        return _FAMILY_FORBID["pip"], "pip install"
    keep: list[str] = []
    for tok in toks:
        if _PLACEHOLDER.search(tok):
            break
        keep.append(tok)
    if not keep or (len(keep) == 1 and (len(toks) > 1 or keep[0] in _MULTI_COMMAND)):
        return None  # "never run `bun <file>`": what is left is too broad
    regex = _anchor(keep[0]) + r"\s+".join(_esc(t) for t in keep) + _end(keep[-1])
    return regex, " ".join(keep)


_MULTI_COMMAND = _words(
    "cargo go uv git gh dotnet poetry npm pnpm yarn bun deno make just docker kubectl hatch pdm "
    "rye nx turbo gradle mvn bazel"
)
_CMD_START = r"(?:^|[;&|(]\s*)"
_FAMILY_FORBID: dict[str, str] = {
    "pip": r"(?<!uv )\bpip3?\s+install\b",
    "pip3": r"(?<!uv )\bpip3?\s+install\b",
    "npm": r"\bnpm\s+(?:i|install|ci|add|run|exec|test|start|uninstall|remove|rm|update|up)\b",
    "npx": _CMD_START + r"npx\s",
    "yarn": _CMD_START + r"yarn(?:\s|$)",
    "pnpm": _CMD_START + r"pnpm(?:\s|$)",
    "bun": _CMD_START + r"bunx?(?:\s|$)",
    "poetry": _CMD_START + r"poetry\s+(?:add|install|lock|run|update|remove)\b",
    "pipenv": _CMD_START + r"pipenv\s",
    "conda": _CMD_START + r"conda\s+install\b",
}
_TOOL_WORDS = _words(
    "uv pip pip3 poetry pipenv conda pdm rye hatch npm npx pnpm yarn bun deno jest vitest mocha "
    "black ruff isort flake8 pylint autopep8 yapf prettier eslint biome mypy pyright ty "
    "unittest pytest nose"
)
_TOOL_ALT = "|".join(sorted(map(re.escape, _TOOL_WORDS), key=len, reverse=True))


def _tool_forbid(tool: str) -> str:
    if tool in _FAMILY_FORBID:
        return _FAMILY_FORBID[tool]
    return _CMD_START + rf"(?:\S*/)?{_esc(tool)}(?:\s|$)"


# --------------------------------------------------------------------------- phrase patterns

_NEG = (
    r"(?:\bnever|\bdon'?t|\bdo\s+not|\bmust\s+not|\bmustn'?t|\bshould\s+not|\bshouldn'?t"
    r"|\bavoid|\bplease\s+don'?t|\bnot\s+allowed\s+to|\bforbidden\s+to|\bcannot|\bcan'?t|\bno)"
)
_SOFT_NEG = re.compile(r"\b(?:avoid|should\s+not|shouldn'?t|try\s+not|prefer\s+not)\b")
_NEG_BEFORE_COMMAND = re.compile(
    _NEG + r"\s+(?:\w+ly\s+)?(?:ever\s+)?(?:(?:run|use|call|invoke|execute|type|running|using"
    r"|calling|invoking|executing)\s+)?(?:the\s+|a\s+|any\s+)?(?:command\s+)?:?\s*$"
    r"|" + _NEG + r"\s+(?:run|invoke|execute)\s+(?:[\w-]+\s+){1,3}?(?:with|using|via)\s+$"
)
_NEG_RUN_UNKNOWN = re.compile(_NEG + r"\s+(?:\w+ly\s+)?(?:ever\s+)?(?:run|invoke|execute)\s+$")
_UNKNOWN_COMMAND = re.compile(r"[a-z][\w.-]*(?:\s+[\w./:=-]+){0,3}")
_FORBIDDEN_AFTER = re.compile(
    r"^\s*(?:commands?\s+)?(?:is|are)\s+(?:strictly\s+)?(?:forbidden|prohibited|not\s+allowed"
    r"|banned|disallowed|never\s+allowed)\b"
)
_RUN_BEFORE = re.compile(
    r"(?:\b(?:run|execute|invoke|running|re-?run|always)(?:\s+(?:the|both|all|these|following))?"
    r"(?:\s+commands?)?\s*:?\s*(?:\(\s*)?$"
    r"|\b(?:run|execute)\s+(?:the\s+|all\s+)?(?:[\w-]+\s+){0,3}?(?:\(|:|with\s+|via\s+|using\s+)"
    r"\s*(?:e\.g\.,?\s*)?$"
    r"|\b(?:without|after)\s+(?:first\s+)?(?:running|executing)\s+$"
    r"|\b(?:must|should|need\s+to|needs\s+to|have\s+to)\s+(?:all\s+)?pass\s*(?:\(|:|-|—|with"
    r"|using|via)?\s*(?:e\.g\.,?\s*)?$"
    r"|\b(?:always|must)\b[^:]*:\s*$)"
)
_PASSES_AFTER = re.compile(
    r"^\s*(?:\)\s*)?(?:must\s+|should\s+|needs?\s+to\s+|has\s+to\s+|have\s+to\s+)?(?:all\s+)?"
    r"(?:pass(?:es)?|succeeds?|is\s+clean|are\s+clean|is\s+green|reports?\s+no|runs?\s+clean"
    r"|exits?\s+(?:with\s+)?0|before\s+(?:you\s+)?(?:commit|push|submit|finish|open|creat|mark"
    r"|complet|declar|hand|send))"
)
_OBLIGATION = re.compile(
    r"\bbefore\s+(?:you\s+)?(?:\w+\s+)?(?:commit|committing|finish|finishing|submit|submitting"
    r"|open|opening|creat|push|pushing|mark|marking|complet|declar|send|sending|hand|consider"
    r"|calling|saying|report|ending|wrap|merg|request|any\s+commit|a\s+pr|pr\b|the\s+pr|yield"
    r"|stopping|returning|claiming)"
    r"|\b(?:always|must|should|need\s+to|needs\s+to|have\s+to|has\s+to|make\s+sure\s+(?:to|you)"
    r"|be\s+sure\s+to|remember\s+to|don'?t\s+forget\s+to|ensure\s+(?:you|to))\s+(?:\w+\s+){0,2}?"
    r"(?:run|execute|re-?run|invoke)\b"
    r"|\b(?:prior\s+to\s+(?:commit|push|submit|finish)\w*|at\s+the\s+end\s+of\s+(?:a|the|each|"
    r"every|your)\s+(?:task|session|turn|change)|mandatory)"
    r"|\b(?:make\s+sure|ensure|verify)\b(?=.*\bpass(?:es)?\b)"
    r"|\bafter\s+(?:making\s+|any\s+|every\s+|each\s+|all\s+|your\s+|code\s+)+(?:changes?|edits?"
    r"|modifications?)\b|\bafter\s+(?:changing|modifying|editing|writing)\b"
    r"|\bwhen\s+(?:you(?:'re|\s+are)\s+)?(?:done|finished)\b|\bonce\s+(?:you(?:'re|\s+are)\s+)?"
    r"(?:done|finished)\b|\bwithout\s+(?:first\s+)?(?:running|executing)\b"
    r"|\b(?:must|should|need\s+to)\s+(?:all\s+)?pass\b"
    r"|\bonly\s+(?:commit|push|submit|open)\w*\b.*\bafter\b"
)
_NOT_OBLIGED = re.compile(
    r"\b(?:no\s+need\s+to|don'?t\s+need\s+to|do\s+not\s+need\s+to|doesn'?t\s+need|not\s+necessary"
    r"|unnecessary|optional(?:ly)?|if\s+you\s+want|you\s+can|you\s+may|can\s+be\s+used"
    r"|not\s+required|if\s+needed|if\s+necessary|if\s+applicable|for\s+example\s+only)\b"
)
_GLOBAL_CONDITION = re.compile(
    r"\b(?:after|when|whenever|once|before)\s+(?:you(?:'ve|\s+have|'re|\s+are)?\s+)?(?:making\s+|"
    r"any\s+|every\s+|each\s+|all\s+|your\s+|code\s+)*(?:changes?|done|finished|edits?|"
    r"modifications?|commit\w*|push\w*|submit\w*|finish\w*|open\w*|creat\w*)\b"
    r"|\b(?:if|when|after)\s+(?:you(?:'ve|\s+have)?\s+)?(?:changed|modified|edited|touched)\s+"
    r"(?:any\s+)?(?:files?|code)(?:\s+in\s+(?:the|this)\s+(?:repo|repository|project))?\b"
)
_CONDITION = re.compile(
    r"\b(?:if|when|whenever|after|once)\b[^.;]*?\b(?:chang|modif|edit|touch|updat|add|work"
    r"|writ|alter|introduc)\w*"
)
_QUALIFIED_AFTER = re.compile(
    r"^\s*(?:\w+ly\s+)?(?:to|for|when|whenever|while|in|on|before|after|unless|if|during|inside"
    r"|within|against|with|without|from|until)\b"
)
_CONDITIONAL_TAIL = re.compile(r"\b(?:unless|except|only\s+(?:if|when)|if\s+you)\b")


def _cut(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()  # TOML comments allow no controls
    return text if len(text) <= 100 else text[:97].rstrip() + "..."


def _quote(d: Directive) -> str:
    """The source sentence for a comment, with the lead-in it was read under."""
    return _cut(d.text) + (f'  (under "{_cut(d.lead)}")' if d.lead else "")


def _code(text: str) -> str:
    return f"`{text}`"


# --------------------------------------------------------------------------- recognisers


def _rec_commands(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``never run `X` `` → forbid-command; ``run `X` before committing`` → require-command."""
    out: list[_Emit] = []
    obliged = bool(_OBLIGATION.search(s.low)) and not _NOT_OBLIGED.search(s.low)
    for chain in s.chains():
        cmds = [sp for sp in chain if _is_command(sp.text)]
        before, after = s.before(chain[0]), s.after(chain[-1])
        if not cmds and _NEG_RUN_UNKNOWN.search(before):  # "never run `nvm`": a command for sure
            cmds = [sp for sp in chain if _UNKNOWN_COMMAND.fullmatch(sp.text)]
        if not cmds:
            continue
        if re.search(r"\b(?:instead\s+of|rather\s+than|over|not|vs\.?)\s*\(?$", before):
            continue  # the substitution recogniser owns "use X instead of `Y`"
        if _NEG_BEFORE_COMMAND.search(before) or _FORBIDDEN_AFTER.match(after):
            if _QUALIFIED_AFTER.match(after):
                continue  # "never use `curl` to test HMR": the situation is not checkable
            others = [sp.text for sp in s.spans if sp not in chain]
            soft = bool(_SOFT_NEG.search(before[-40:]))
            conditional = bool(_CONDITIONAL_TAIL.search(s.low))
            for sp in cmds:
                fr = _forbid_regex(sp.text)
                if fr is None or sp.text.startswith("-") or _FORCE_PUSH_CMD.search(sp.text):
                    continue  # flags and force pushes have dedicated recognisers
                regex, label = fr
                if any(re.search(regex, o) for o in others):
                    continue  # the sentence also recommends something this would match
                conf = 0.9 - (0.25 if soft else 0) - (0.25 if conditional else 0)
                out.append(
                    _Emit(
                        "forbid-command",
                        {"command": regex},
                        conf,
                        "never-run",
                        label,
                        f"Do not run {_code(label)}",
                    )
                )
            continue
        positive = bool(_RUN_BEFORE.search(before)) or bool(_PASSES_AFTER.match(after))
        if not (positive and obliged):
            continue
        cond = _condition(s, chain[0])
        for sp in cmds:
            for regex, label in _require_regex(sp.text):
                if _NOT_A_CHECK.search(label):
                    continue
                conf = 0.9 if _CHECK_WORDS.search(label) else 0.65
                params: dict[str, Any] = {
                    "command": regex,
                    "must_succeed": True,
                    "after_last_edit": True,
                }
                if cond is not None:
                    if not cond:
                        continue  # "when adding a rule, run X": the trigger is not checkable
                    params["when_paths"] = cond
                elif _CONDITIONAL_TAIL.search(s.low):
                    conf = min(conf, 0.6)
                out.append(
                    _Emit(
                        "require-command",
                        params,
                        conf,
                        "run-before-done",
                        label,
                        f"Run {_code(label)} after the last edit",
                    )
                )
    return out


def _condition(s: _Sentence, first_cmd: _Span) -> list[str] | None:
    """None: unconditional. []: a condition we cannot map to paths. Else when_paths globs."""
    head = s.low[: first_cmd.start]
    tail = s.low[first_cmd.end :]
    region = head if _CONDITION.search(head) else tail if _CONDITION.search(tail) else ""
    if not region:
        return None
    cond = _CONDITION.search(region)
    assert cond is not None
    if _GLOBAL_CONDITION.search(region[cond.start() :]) and not _language_globs(region):
        return None
    globs = _language_globs(region)
    for sp in s.spans:
        if sp.start >= cond.start() and _is_path(sp.text) and sp.end <= first_cmd.start:
            globs.append(_path_glob(sp.text, s.before(sp)))
    return globs


def _language_globs(text: str) -> list[str]:
    out: list[str] = []
    for rx, globs in _LANGUAGE_GLOBS:
        m = rx.search(text)
        if m and not re.search(r"\bnon-?\s*$|\bnot\s+$", text[: m.start()]):
            out.extend(g for g in globs if g not in out)
    return out


_FORCE_PUSH = re.compile(
    r"force[- ]?push|push\s+(?:--force|-f)\b|`--force`|`-f`|push\s+with\s+`?--force"
)
_FORCE_PUSH_CMD = re.compile(r"\bgit\s+push\b.*\s(?:--force|-f)\b")
_NO_VERIFY = re.compile(
    r"--no-verify|(?:skip|bypass|disable)(?:ping|ing)?\s+(?:the\s+|any\s+|git\s+)?"
    r"(?:git\s+|pre-commit\s+|commit\s+|pre-push\s+|husky\s+)*hooks?"
)
_NEG_ANYWHERE = re.compile(_NEG + r"\b")


def _rec_git_safety(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """Force pushes and hook bypasses."""
    out: list[_Emit] = []
    neg = _NEG_ANYWHERE.search(s.low)
    if neg is None:
        return out
    fp = _FORCE_PUSH.search(s.low)
    scoped = fp is not None and re.match(
        r"\s*(?:(?:a|the|any)\s+)?(?:branch(?:es)?\s+)?(?:that|which|when|while|if|during|in|unless)\b",
        s.low[fp.end() :],
    )
    if fp and re.search(r"\bpush", s.low) and not scoped:  # "a branch that is in the queue"
        conf = 0.7 if "unless" in s.low or _SOFT_NEG.search(s.low) else 0.9
        out.append(
            _Emit(
                "forbid-command",
                {"command": r"\bgit\s+push\b[^;&|\n]*\s(?:--force(?!-with-lease)|-f)\b"},
                conf,
                "no-force-push",
                "force-push",
                "Do not force-push",
            )
        )
    m = _NO_VERIFY.search(s.low)
    if m and neg.start() < m.start():
        out.append(
            _Emit(
                "forbid-command",
                {"command": r"\bgit\s+(?:commit|push|merge|rebase)\b[^;&|\n]*\s--no-verify\b"},
                0.9,
                "no-verify",
                "no-verify",
                "Do not bypass git hooks with --no-verify",
            )
        )
    return out


_STRONG_NEG = (
    r"(?:\bnever|\bdon'?t|\bdo\s+not|\bmust\s+not|\bmustn'?t|\bshould\s+not|\bshouldn'?t"
    r"|\bplease\s+don'?t|\bnot\s+allowed\s+to)"
)
_COMMIT_PUSH = re.compile(
    _STRONG_NEG
    + r"\s+(?:\w+ly\s+)?(?:ever\s+)?(?:(?:create|make|run)\s+(?:a\s+|any\s+)?)?(?:git\s+)?"
    r"(commit|push)(?:es|s|ing)?\b(?=\s*(?:$|[.,;:)!—-]|\s+(?:or|and|/)\s+(?:git\s+)?"
    r"(?:commit|push)|\s+(?:your\s+|the\s+|any\s+|these\s+)?(?:changes|code|work|anything)\b"
    r"|\s+(?:unless|until|without|on\s+(?:your|my|the\s+user'?s)\s+own|automatically|yourself"
    r"|on\s+behalf|for\s+the\s+user|to\s+(?:the\s+)?(?:repo|repository|remote|git|origin)\b)))"
)
_COMMIT_CONDITION = re.compile(
    r"\b(?:unless|until|without\s+(?:being\s+|first\s+|explicit\w*\s+|the\s+user\W*s?\s+)?"
    r"(?:asked|told|requested|instructed|permission|approval|confirm\w*|explicit\w*|consent)"
    r"|explicitly\s+(?:asked|requested|told)|on\s+behalf)"
)
_PUSH_MAIN = re.compile(
    _STRONG_NEG + r"\s+(?:\w+ly\s+)?push\s+(?:directly\s+)?to\s+`?(main|master)`?\b"
)


def _rec_commit_push(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``never commit``, ``don't push unless asked`` → forbid-command on git commit / push."""
    out: list[_Emit] = []
    for m in _COMMIT_PUSH.finditer(s.low):
        tail = s.low[m.end() : m.end() + 60]
        if re.match(r"\s+without\s+(?:first\s+)?(?:running|testing|checking|verify)", tail):
            continue  # "never commit without running X" is a require-command
        verbs = {m.group(1)}
        if re.match(r"\s*(?:or|and|/)\s*(?:git\s+)?(commit|push)", tail):
            verbs.add(re.match(r"\s*(?:or|and|/)\s*(?:git\s+)?(commit|push)", tail).group(1))  # type: ignore[union-attr]
        conditional = bool(_COMMIT_CONDITION.search(s.low))
        for verb in sorted(verbs):
            out.append(
                _Emit(
                    "forbid-command",
                    {"command": rf"\bgit\s+{verb}\b"},
                    0.65 if conditional else 0.9,
                    "never-commit",
                    f"git-{verb}",
                    f"Do not run git {verb}" + (" unless asked" if conditional else ""),
                )
            )
    m2 = _PUSH_MAIN.search(s.low)
    if m2:
        branch = m2.group(1)
        out.append(
            _Emit(
                "forbid-command",
                {"command": rf"\bgit\s+push\b[^;&|\n]*[\s:]{branch}(?:\s|$)"},
                0.7,
                "never-commit",
                f"push-{branch}",
                f"Do not push to {branch}",
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class _Mention:
    tool: str
    text: str  # the code span, or the bare word
    start: int
    end: int


_TOOL_MENTION = re.compile(rf"`([^`]+)`|(?<![\w`.@/-])({_TOOL_ALT})(?![\w`/-]|\.\w)")
_AMBIGUOUS_WORDS = _words("black ty nose rye hatch deno jest stack")
_TOOL_NOUN_AFTER = re.compile(
    r"\s+(?:patterns?|style|conventions?|apis?|config\w*|syntax|types?|rules?|tests?|fixtures?"
    r"|assertions?|mocks?|plugins?|options?|settings?|docs?|documentation|format|output|cache"
    r"|workspaces?|features?|version|projects?|scripts?|lock\w*|environments?)\b"
)
_LIST_JOIN = re.compile(r"\s*(?:,|/|or|nor|,\s*or|,\s*nor)\s*")
_POSITIVE_BEFORE = re.compile(
    r"(?:\b(?:use|uses|using|run|prefer|via|with)\s+(?:only\s+)?|^|:\s*|^\w+(?:\s+\w+)?:\s*)$"
)
_INSTEAD = re.compile(
    r"\s*(?:,\s*|\(\s*)?(?:and\s+|but\s+)?(?:not|never|instead\s+of|rather\s+than|over|\(not)\s+"
    r"(?:use\s+|using\s+)?"
)
_NEG_BEFORE_TOOL = re.compile(
    _NEG + r"\s+(?:\w+ly\s+)?(?:ever\s+)?(?:use|run|using|invoke|call)\s+(?:the\s+)?$"
)


def _mentions(s: _Sentence) -> list[_Mention]:
    out: list[_Mention] = []
    for m in _TOOL_MENTION.finditer(s.low):
        if m.group(1) is not None:
            segs = _segments(m.group(1))
            toks = _strip_runner(segs[0]) if segs else []
            tool = toks[0] if toks else ""
            if tool not in _TOOL_WORDS:
                continue
            if len(toks) > 1 and toks[1] not in ("install", "i", "add", "ci"):
                continue  # `bun test` is one command of the tool, not the tool
            out.append(_Mention(tool, m.group(1), m.start(), m.end()))
        else:
            word = m.group(2)
            if word in _AMBIGUOUS_WORDS or _TOOL_NOUN_AFTER.match(s.low, m.end()):
                continue
            out.append(_Mention(word, word, m.start(), m.end()))
    return out


def _rec_substitution(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``use uv, not pip`` / ``prefer pnpm over npm`` → forbid-command on the replaced tool."""
    out: list[_Emit] = []
    ms = _mentions(s)

    def listed(j: int) -> list[_Mention]:
        group = [ms[j]]
        while j + 1 < len(ms) and _LIST_JOIN.fullmatch(s.low[ms[j].end : ms[j + 1].start]):
            j += 1
            group.append(ms[j])
        return group

    for i, a in enumerate(ms):
        # "use A, not B or C" / "A instead of B" / "prefer A over B"
        if i + 1 < len(ms):
            between = s.low[a.end : ms[i + 1].start]
            prefix = s.low[max(0, a.start - 30) : a.start]
            plain_instead = re.search(r"instead\s+of|rather\s+than|\(not", between)
            if _INSTEAD.fullmatch(between) and (_POSITIVE_BEFORE.search(prefix) or plain_instead):
                soft = "prefer" in prefix or "over" in between
                for b in listed(i + 1):
                    out.extend(_ban(b, a, 0.7 if soft else 0.9))
        # "never use B[, C]; use A"
        if _NEG_BEFORE_TOOL.search(s.low[: a.start]) and (i == 0 or ms[i - 1].end < a.start):
            group = listed(i)
            rest = s.low[group[-1].end :]
            if _QUALIFIED_AFTER.match(rest):
                continue
            nxt = ms[i + len(group)] if i + len(group) < len(ms) else None
            soft = bool(_SOFT_NEG.search(s.low[max(0, a.start - 20) : a.start]))
            for b in group:
                out.extend(_ban(b, nxt, 0.7 if soft else 0.9))
    out.extend(_runner_substitution(s))
    return out


def _ban(banned: _Mention, allowed: _Mention | None, conf: float) -> list[_Emit]:
    if allowed is not None and banned.tool == allowed.tool:
        return []
    regex = _tool_forbid(banned.tool)
    if allowed is not None and (
        re.search(regex, allowed.text) or re.search(regex, f"{allowed.tool} install x")
    ):
        return []
    if banned.tool not in _FAMILY_FORBID:
        conf -= 0.15
    why = f"Use {allowed.tool}, not {banned.tool}" if allowed else f"Do not use {banned.tool}"
    return [_Emit("forbid-command", {"command": regex}, conf, "use-x-not-y", banned.tool, why)]


def _runner_substitution(s: _Sentence) -> list[_Emit]:
    """``use `uv run pytest` instead of `pytest` `` → forbid the bare command at a command start."""
    out: list[_Emit] = []
    for a, b in zip(s.spans, s.spans[1:], strict=False):
        between = s.low[a.end : b.start]
        if not re.fullmatch(
            r"\s*(?:,\s*)?(?:not|instead\s+of|rather\s+than|\(not|over)\s*", between
        ):
            continue
        at, bt = a.text.split(), b.text.split()
        if (
            len(at) <= len(bt)
            or at[-len(bt) :] != bt
            or tuple(at[: len(at) - len(bt)]) not in _RUNNERS
        ):
            continue
        regex = _CMD_START + r"\s+".join(_esc(t) for t in bt) + _end(bt[-1])
        if re.search(regex, a.text):
            continue
        out.append(
            _Emit(
                "forbid-command",
                {"command": regex},
                0.7,
                "use-x-not-y",
                f"bare-{b.text}",
                f"Run {_code(b.text)} through {_code(a.text)}",
            )
        )
    return out


_EDIT_VERBS = (
    r"(?:edit|modify|change|touch|update|alter|rewrite|write\s+to|hand-edit|overwrite|reformat"
    r"|delete|remove|commit)"
)
_EDIT_NEG = re.compile(
    _NEG
    + r"\s+(?:\w+ly\s+)?(?:ever\s+)?(?:manually\s+|directly\s+|by\s+hand\s+)?("
    + _EDIT_VERBS
    + r")(?:\s+(?:or|and|/)\s+"
    + _EDIT_VERBS
    + r")?"
    r"(?:(?:\s+(?:any\s+)?(?:of\s+)?(?:the\s+)?(?:files?|code|anything|contents?|generated\s+\w+))?"
    r"(?:\s+(?:in|under|inside|within|of|from|at))?"
    r"|(?:\s+[\w-]+){1,3}?\s+(?:in|under|inside|within))(?:\s+the)?\s*:?\s*(?:\(\s*)?$"
)
_NOT_EDITED_AFTER = re.compile(
    r"^\s*(?:files?\s+|directory\s+|folder\s+)?(?:is|are|must|should)\s+(?:never|not)\s+"
    r"(?:be\s+)?(?:manually\s+|directly\s+|hand-)?(?:edited|modified|changed|touched|committed)"
    r"|^\s*(?:files?\s+|directory\s+)?(?:is|are)\s+read-only"
)
_GENERATED_AFTER = re.compile(
    r"^\s*(?:files?\s+|directory\s+|folder\s+|code\s+)?(?:is|are)\s+(?:all\s+)?"
    r"(?:auto-?)?generated\b"
)
_FILES_IN = re.compile(r"\b(?:files?|code|anything)\s+(?:in|under|inside|within)\s+(?:the\s+)?$")
_NOT_A_FILE_AFTER = re.compile(
    r"^\s*(?:branch|branches|tags?|refs?|remotes?|packages?|modules?|crates?|namespaces?|urls?"
    r"|endpoints?|routes?|api|apis|tables?|columns?|keys?|fields?|env\w*\s+var\w*)\b"
)
_MANUAL = re.compile(r"\b(?:manually|by\s+hand|hand-edit|directly|generated|regenerat\w*)\b")
_EXISTING_TESTS = re.compile(
    _NEG + r"\s+(?:\w+ly\s+)?(?:modify|change|edit|delete|remove|weaken|alter|rewrite|disable"
    r"|skip)\s+(?:the\s+|any\s+)?(?:existing|current|passing)\s+tests?\b"
)
_LOCKFILE = re.compile(
    _NEG + r"\s+(?:\w+ly\s+)?(?:manually\s+|directly\s+|by\s+hand\s+)?(?:edit|modify|change|touch)"
    r"\s+(?:the\s+|any\s+)?lock\s?files?\b"
)
_LOCKFILES = [
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
]


def _is_path(span: str) -> bool:
    if not span or any(c.isspace() for c in span) or _PLACEHOLDER.search(span):
        return False
    if span.startswith(("-", "@", "$", "http:", "https:", "#")) or "://" in span:
        return False
    if "(" in span or "=" in span or span.endswith((":", ";")):
        return False
    if span.startswith(".") and "/" not in span and not _FILE_EXT.search(span):
        return span in (".env",) or span.startswith(".env.")
    return "/" in span or "*" in span or bool(_FILE_EXT.search(span))


def _path_glob(span: str, before: str) -> str:
    p = span.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    last = p.rstrip("/").rsplit("/", 1)[-1]
    is_dir = p.endswith("/") or ("." not in last and "*" not in last)
    if is_dir and not p.endswith("/"):
        p += "/"
    return p


def _rec_paths(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``never edit `api/gen/` `` / ``` `dist/` is generated``` → forbid-change / forbid-edit."""
    out: list[_Emit] = []
    if _EXISTING_TESTS.search(s.low):
        out.append(
            _Emit(
                "forbid-change",
                {"paths": list(_TEST_GLOBS), "actions": ["modify", "delete"]},
                0.65,
                "never-edit-path",
                "existing-tests",
                "Do not modify or delete existing tests",
            )
        )
    if _LOCKFILE.search(s.low):
        out.append(
            _Emit(
                "forbid-edit",
                {"paths": _LOCKFILES},
                0.85,
                "never-edit-path",
                "lockfiles",
                "Do not edit lockfiles by hand",
            )
        )
    negated_sentence = _NEG_ANYWHERE.search(s.low) is not None
    if _CONDITIONAL_TAIL.search(s.low) and "unless" in s.low:
        return out
    for chain in s.chains():
        before, after = s.before(chain[0]), s.after(chain[-1])
        in_dir = bool(_FILES_IN.search(before))  # "files in `migrations`": a bare word is a dir
        paths = [
            sp
            for sp in chain
            if _is_path(sp.text) or (in_dir and re.fullmatch(r"[\w][\w.-]*", sp.text))
        ]
        if not paths:
            continue
        m = _EDIT_NEG.search(before)
        generated = bool(_GENERATED_AFTER.match(after)) and negated_sentence
        if not (m or _NOT_EDITED_AFTER.match(after) or generated):
            continue
        verb = m.group(1) if m else ""
        if verb == "commit":
            continue  # "never commit `.env`" is about git, not about the working tree
        if _NOT_A_FILE_AFTER.match(after):
            continue  # "never modify the `gh/user/N` branches"
        manual = generated or bool(_MANUAL.search(s.low))
        globs = [_path_glob(sp.text, s.before(sp)) for sp in paths]
        params: dict[str, Any] = {"paths": globs}
        if verb in ("delete", "remove"):
            params["actions"] = ["delete"]
        soft = bool(_SOFT_NEG.search(before[-40:]))
        label = "-".join(globs[:2])
        if manual:
            out.append(
                _Emit(
                    "forbid-edit",
                    params,
                    0.75 if soft else 0.9,
                    "never-edit-path",
                    label,
                    "Do not edit " + ", ".join(_code(g) for g in globs) + " by hand",
                )
            )
        else:
            out.append(
                _Emit(
                    "forbid-change",
                    params,
                    0.7 if soft else 0.85,
                    "never-edit-path",
                    label,
                    "Do not change " + ", ".join(_code(g) for g in globs),
                )
            )
    return out


_BACKUP = re.compile(r"\b(?:backups?|back-ups?)\s*(?:copies|copy|files?)?\b|\.bak\b|\.orig\b")


def _rec_backup_files(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``don't create .bak / .orig copies`` → forbid-change actions=["add"]."""
    m = _BACKUP.search(s.low)
    neg = _NEG_ANYWHERE.search(s.low)
    if m is None or neg is None or neg.start() > m.start():
        return []
    if not re.search(r"\b(?:create|make|leave|add|keep|commit|write|backups?|copies)\b", s.low):
        return []
    globs = ["*.bak", "*.orig"]
    for sp in s.spans:
        t = sp.text.strip()
        if re.fullmatch(r"\*?\.[\w*-]+|[\w-]*\*[\w.*-]*", t):
            g = t if t.startswith("*") else "*" + t
            if g not in globs:
                globs.append(g)
    return [
        _Emit(
            "forbid-change",
            {"paths": globs, "actions": ["add"]},
            0.85,
            "no-backup-files",
            "backup-files",
            "Do not create backup copies; use version control",
        )
    ]


_TEST_ADJ = (
    r"(?:(?:unit|regression|new|corresponding|appropriate|relevant|matching|integration)\s+)*"
)
_ADD_TESTS = re.compile(
    r"\b(?:always|must|should)\s+(?:add|write|include|create)\s+" + _TEST_ADJ + r"tests?\b"
    r"|\b(?:add|write|include|create)\s+" + _TEST_ADJ + r"tests?\s+(?:for|covering|that\s+"
    r"(?:cover|exercise)|alongside|when|before|with)\b"
    r"|\b(?:new|changed)\s+(?:code|features?|functionality|behaviou?r)\s+(?:must|should)\s+"
    r"(?:have|include|be\s+covered\s+by|come\s+with)\s+(?:\w+\s+)?tests?\b"
    r"|\b(?:bug\s?fix(?:es)?|fixes|changes|prs?|pull\s+requests)\s+(?:must|should)\s+(?:include|"
    r"have|come\s+with|add)\s+(?:a\s+|an\s+)?(?:\w+\s+)?tests?\b"
)
_CHANGELOG = re.compile(
    r"\b(?:update|add\s+(?:an?\s+)?(?:entry|note|line)\s+(?:to|in)|add\s+to|edit|record\s+\w+\s+in"
    r"|document\s+\w+(?:\s+\w+)?\s+in)\s+(?:the\s+)?`?(changelog(?:\.md)?|changes\.md)`?"
    r"|\badd\s+(?:an?\s+)?(?:\w+\s+)?`?changelog`?\s+entry\b"
)
_CHANGESET = re.compile(r"\b(?:add|create|include|run)\s+(?:an?\s+)?(?:new\s+)?changesets?\b")
_DOCS = re.compile(
    r"\b(?:update|keep|add\s+to)\s+(?:the\s+)?(?:relevant\s+|corresponding\s+|related\s+)?"
    r"(?:docs|documentation|readme)\b"
)


def _rec_require_change(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """Changelog / changeset / tests / docs that must accompany a code change."""
    out: list[_Emit] = []
    if _NEG_ANYWHERE.search(s.low) and not re.search(r"\bdon'?t\s+forget\b", s.low):
        return out
    if _NOT_OBLIGED.search(s.low):
        return out
    src, from_layout = repo.source_globs()
    trigger_conf = 0.7 if from_layout else 0.6
    if _CHANGELOG.search(s.low):
        target = repo.changelog() or "CHANGELOG.md"
        for sp in s.spans:
            if re.fullmatch(r"(?i)[\w./-]*change(?:log|s)[\w.-]*", sp.text) and _is_path(sp.text):
                target = _path_glob(sp.text, "")
        out.append(
            _Emit(
                "require-change",
                {"if_changed": src, "then_changed": [target], "except": list(_DOC_GLOBS)},
                trigger_conf,
                "update-changelog",
                Path(target.rstrip("/")).stem.lower(),
                f"Update {_code(target)} when the code changes",
            )
        )
    if _CHANGESET.search(s.low):
        out.append(
            _Emit(
                "require-change",
                {"if_changed": src, "then_changed": [".changeset/*.md"]},
                trigger_conf,
                "update-changelog",
                "changeset",
                "Add a changeset when the code changes",
            )
        )
    if _ADD_TESTS.search(s.low) and re.search(
        r"\b(?:always|must|should|every|each|all|any|new|bug|fix|changes?|feature|code"
        r"|functionality|regression|behaviou?r|prs?|pull)\b",
        s.low,
    ):
        tests = list(_TEST_GLOBS)
        out.append(
            _Emit(
                "require-change",
                {"if_changed": src, "then_changed": tests, "except": [*tests, *_DOC_GLOBS]},
                min(trigger_conf, 0.65),
                "add-tests",
                "tests",
                "Add or update tests when the code changes",
            )
        )
    doc_target = [
        _path_glob(sp.text, "")
        for sp in s.spans
        if _is_path(sp.text) and re.search(r"(?i)\.(?:md|mdx|rst)$|^docs?/", sp.text)
    ]
    if _DOCS.search(s.low) and _CONDITION.search(s.low) and doc_target:
        out.append(
            _Emit(
                "require-change",
                {"if_changed": src, "then_changed": doc_target},
                0.55,
                "update-docs",
                "docs",
                "Update the docs when the code changes",
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class _Marker:
    name: str
    mention: re.Pattern[str]
    pattern: str
    paths: list[str]
    confidence: float
    case_sensitive: bool = False
    specific: bool = False  # a code span naming a rule code narrows the pattern


_MARKERS: tuple[_Marker, ...] = (
    _Marker(
        "console-log",
        re.compile(r"console\.log|console\s+statements"),
        r"\bconsole\.log\(",
        _JS,
        0.9,
    ),
    _Marker(
        "debugger",
        re.compile(r"`debugger;?`|\bdebugger\s+statements?"),
        r"^\s*debugger\b",
        _JS,
        0.9,
    ),
    _Marker(
        "print",
        re.compile(r"`print(?:\(\))?`|\bprint\(\)|\bprint\s+statements?"),
        r"^\s*print\(",
        _PY,
        0.7,
    ),
    _Marker(
        "breakpoint", re.compile(r"breakpoint\(\)|`breakpoint`"), r"\bbreakpoint\(\)", _PY, 0.9
    ),
    _Marker(
        "pdb",
        re.compile(r"\bi?pdb\b|set_trace"),
        r"\bimport\s+i?pdb\b|\bi?pdb\.set_trace\(",
        _PY,
        0.9,
    ),
    _Marker("dbg", re.compile(r"dbg!"), r"\bdbg!\(", _RS, 0.9),
    _Marker(
        "todo",
        re.compile(r"`TODO`|\bTODO\s+comments?\b|`FIXME`|\bTODOs\b"),
        r"\b(?:TODO|FIXME)\b",
        list(_CODE_GLOBS),
        0.65,
        case_sensitive=True,
    ),
    _Marker(
        "type-ignore",
        re.compile(r"type:\s*ignore"),
        r"#\s*type:\s*ignore",
        _PY,
        0.85,
        specific=True,
    ),
    _Marker("noqa", re.compile(r"\bnoqa\b"), r"#\s*noqa\b", _PY, 0.7, specific=True),
    _Marker("ts-ignore", re.compile(r"@ts-ignore"), r"@ts-ignore\b", _JS, 0.9),
    _Marker("ts-nocheck", re.compile(r"@ts-nocheck"), r"@ts-nocheck\b", _JS, 0.9),
    _Marker(
        "eslint-disable", re.compile(r"eslint-disable"), r"eslint-disable", _JS, 0.7, specific=True
    ),
    _Marker(
        "any-type",
        re.compile(r"`any`(?:\s+types?)?|\bany\s+type\b|`as\s+any`|`:\s*any`"),
        r"(?::\s*any\b|\bas\s+any\b|<any>)",
        _TS,
        0.75,
        case_sensitive=True,
    ),
    _Marker("unwrap", re.compile(r"\.?unwrap\(\)|`unwrap`"), r"\.unwrap\(\)", _RS, 0.6),
    _Marker(
        "test-only", re.compile(r"\.only\b|`only`"), r"\b(?:it|test|describe)\.only\(", _JS, 0.85
    ),
    _Marker("bare-except", re.compile(r"bare\s+`?except|`except:`"), r"^\s*except\s*:", _PY, 0.8),
    _Marker(
        "star-import",
        re.compile(r"(?:wildcard|star)\s+imports?|`from\s+\S+\s+import\s+\*`"),
        r"^\s*from\s+\S+\s+import\s+\*",
        _PY,
        0.75,
    ),
    _Marker(
        "default-export",
        re.compile(r"default\s+exports?|`export\s+default`"),
        r"^\s*export\s+default\b",
        _JS,
        0.65,
    ),
)
_MARKER_NEG = re.compile(
    _NEG + r"\b|\b(?:remove|delete|strip|leave\s+out|get\s+rid\s+of|instead\s+of|rather\s+than"
    r"|never\s+leave|clean\s+up)\b"
)


def _rec_forbid_text(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``don't leave `console.log` `` / ``no `# type: ignore` `` → forbid-text by language."""
    out: list[_Emit] = []
    for marker in _MARKERS:
        m = marker.mention.search(s.text if marker.case_sensitive else s.low)
        if m is None:
            continue
        negs = list(_MARKER_NEG.finditer(s.low[: m.start()]))
        if not negs or re.match(r"(?:if|when)\b", s.low):
            continue
        neg = negs[-1]
        clause = s.low[neg.end() : m.start()]
        reach = 25 if neg.group(0).startswith(("instead", "rather")) else 60
        if len(clause) > reach or re.search(r"[.;]|\bbut\b|\binstead\b|\bunless\b", clause):
            continue
        variants = {
            sp.text.strip()
            for sp in s.spans
            if marker.mention.search(sp.text if marker.case_sensitive else sp.text.lower())
        }
        if len(variants) > 1:
            continue  # "`# type: ignore[code]`, not bare `# type: ignore`": too fine for a regex
        pattern = marker.pattern
        if marker.specific and variants:
            span = variants.pop()
            detail = marker.mention.search(span.lower())
            if detail is not None and re.search(r"\w", span[detail.end() :]):
                pattern = r"\s*".join(_esc(part) for part in span.split())
        conf = marker.confidence
        if _SOFT_NEG.search(s.low) or re.search(
            r"\b(?:prefer|instead\s+of|rather\s+than)\b", s.low
        ):
            conf -= 0.15
        if re.search(r"\b(?:except|unless|outside\s+of|other\s+than)\b", s.low):
            conf -= 0.2
        paths = list(marker.paths)
        out.append(
            _Emit(
                "forbid-text",
                {"pattern": pattern, "paths": paths},
                conf,
                "no-debug-code",
                marker.name,
                f"Do not add {marker.name.replace('-', ' ')} to the code",
            )
        )
    return out


_OUTSIDE_REPO = re.compile(
    r"\b(?:outside|beyond)\s+(?:of\s+)?(?:the\s+|this\s+|your\s+)?(?:current\s+)?(?:git\s+)?"
    r"(?:repo|repository|project|working\s+(?:directory|tree|dir)|workspace|worktree|codebase"
    r"|project\s+(?:root|directory))\b"
)
_ONLY_INSIDE = re.compile(
    r"\bonly\s+(?:edit|modify|touch|change|write)\s+(?:files?\s+)?(?:in|inside|within|under)\s+"
    r"(?:the\s+|this\s+)?(?:repo|repository|project|workspace|working\s+directory)\b"
)


def _rec_outside_repo(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``don't touch files outside the repo`` → forbid-edit outside_repo=true."""
    m = _OUTSIDE_REPO.search(s.low)
    neg = _NEG_ANYWHERE.search(s.low)
    edits = re.search(
        r"\b(?:edit|modify|touch|change|write|create|delete|files?)\b",
        s.low[: m.start()] if m else "",
    )
    if (m and neg and neg.start() < m.start() and edits) or _ONLY_INSIDE.search(s.low):
        return [
            _Emit(
                "forbid-edit",
                {"outside_repo": True},
                0.9,
                "outside-repo",
                "outside-repo",
                "Do not edit files outside the repository",
            )
        ]
    return []


_DEPENDENCIES = re.compile(
    r"\b(?:add|adding|introduce|introducing|install|installing|pull\s+in)\s+(?:any\s+|new\s+|"
    r"additional\s+|extra\s+|third[- ]party\s+|external\s+|runtime\s+)*(?:dependenc(?:y|ies)|"
    r"packages|libraries|deps|crates)\b"
    r"|\b(?:new\s+)?dependencies\s+(?:must|should|need\s+to)\s+(?:be\s+)?(?:approved|discussed)"
)
_ASK_FIRST = re.compile(
    r"\bwithout\s+(?:first\s+)?(?:asking|approval|permission|discuss\w*|confirm\w*|check\w*|"
    r"consult\w*|justification)|\bask\s+(?:me\s+|the\s+user\s+)?(?:first|before)|\bapproved\b"
    r"|\bdiscussed\b"
)


def _rec_dependencies(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``never add dependencies without asking`` → forbid-change on manifests (warning)."""
    m = _DEPENDENCIES.search(s.low)
    if m is None:
        return []
    neg = _NEG_ANYWHERE.search(s.low[: m.start()])
    if neg is None and not _ASK_FIRST.search(s.low):
        return []
    if re.search(r"\b(?:to|in)\s+`?[\w-]+/", s.low[m.end() : m.end() + 20]):
        return []  # scoped to one package of a monorepo
    return [
        _Emit(
            "forbid-change",
            {"paths": repo.manifests(), "actions": ["modify"]},
            0.6,
            "no-new-dependencies",
            "dependencies",
            "Do not add dependencies without asking (review manifest changes)",
        )
    ]


_CLAIMS = re.compile(
    _NEG + r"\s+(?:\w+ly\s+)?(?:claim|say|report|state|assert|pretend)\s+(?:that\s+)?(?:\w+\s+)"
    r"{0,4}?(?:pass(?:es|ed|ing)?|works?|fixed|succeed\w*|is\s+green|are\s+green)\b"
    r"|\bverify\b[^.]*\bbefore\s+(?:claiming|reporting|saying|declaring)\b"
)


def _rec_claims(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``never claim tests pass without running them`` → claims."""
    if _CLAIMS.search(s.low) is None:
        return []
    return [
        _Emit(
            "claims",
            {},
            0.85,
            "no-false-claims",
            "unverified-claims",
            "Do not claim results that no successful command backs",
        )
    ]


_LICENSE_HEADER = re.compile(
    r"\b(?:license|licence|copyright|spdx)\s+(?:header|notice|identifier)s?\b"
)


def _rec_license_header(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``new files must start with the license header`` → require-text on new files."""
    if _LICENSE_HEADER.search(s.low) is None or _NEG_ANYWHERE.search(s.low):
        return []
    if not re.search(r"\b(?:new|every|each|all|any)\s+(?:source\s+)?files?\b|\bmust\b", s.low):
        return []
    pattern = "SPDX-License-Identifier" if "spdx" in s.low else "(?i)(?:copyright|license)"
    src, _ = repo.source_globs()
    return [
        _Emit(
            "require-text",
            {"pattern": pattern, "paths": src, "new_files_only": True},
            0.6,
            "license-header",
            "license-header",
            "New source files carry the license header",
        )
    ]


_SECRET_WORDS = re.compile(
    r"\b(?:secrets?|credentials?|api\s+keys?|private\s+keys?|access\s+tokens?|tokens?|passwords?"
    r"|\.env)\b|`\.env`"
)
_SECRET_VERB = re.compile(
    _STRONG_NEG + r"\s+(?:\w+ly\s+)?(?:ever\s+)?(?:commit|add|check\s+in|push|include|hard-?code"
    r"|store)\b"
)


def _rec_secrets(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``never commit secrets / `.env` files`` → forbid-change on env and key files, and on
    private key blocks in added lines."""
    verb = _SECRET_VERB.search(s.low)
    if verb is None:
        return []
    obj = _SECRET_WORDS.search(s.low, verb.end())
    if obj is None or obj.start() - verb.end() > 40:
        return []
    out = [
        _Emit(
            "forbid-text",
            {"pattern": r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"},
            0.9,
            "no-secrets",
            "private-keys",
            "Do not commit private keys",
        )
    ]
    if verb.group(0).split()[-1] in ("commit", "add", "in", "push"):
        out.append(
            _Emit(
                "forbid-change",
                {
                    "paths": [".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519"],
                    "except": [".env.example", ".env.sample", ".env.template"],
                    "actions": ["add"],
                },
                0.8,
                "no-secrets",
                "secret-files",
                "Do not add env or key files",
            )
        )
    return out


_CODE_TOKEN = re.compile(r"@?[A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)*(?:\(\)|!)?")
_NEG_BEFORE_CODE = re.compile(
    "(?:"
    + _STRONG_NEG
    + r"|\bavoid)\s+(?:\w+ly\s+)?(?:ever\s+)?(?:(?:use|call|add|import|write|introduce|leave"
    r"|rely\s+on|instantiate|using|calling|adding|importing)\s+)?(?:new\s+|any\s+|the\s+|a\s+)?$"
)


def _code_token_regex(token: str) -> str | None:
    """A regex for an identifier-like token, or None when it is too plain to search for."""
    if not _CODE_TOKEN.fullmatch(token) or _is_command(token):
        return None
    call, macro = token.endswith("()"), token.endswith("!")
    name = token[:-2] if call else token[:-1] if macro else token
    distinctive = (
        call
        or macro
        or name.startswith("@")
        or "." in name
        or "::" in name
        or re.search(r"[a-z][A-Z]", name)
        or ("_" in name.strip("_") and len(name) > 4)
    )
    if not distinctive:
        return None
    head = r"(?<![\w.])" if call and "." not in name and "::" not in name else _anchor(name)
    tail = r"\(" if call else "!" if macro else r"\b"
    return head + _esc(name) + tail


def _rec_forbid_code(d: Directive, s: _Sentence, repo: _Repo) -> list[_Emit]:
    """``do not use `datetime.now()` `` → forbid-text on that token in code (warning)."""
    out: list[_Emit] = []
    if re.search(r"\b(?:except|unless|outside|other\s+than|only|instead\s+of)\b", s.low):
        return out
    if re.match(r"(?:in|for|when|if|inside|within)\b", s.low):
        return out  # "In `airflow-core`, ... must not call X": the scope is not checkable
    for chain in s.chains():
        before, after = s.before(chain[0]), s.after(chain[-1])
        if not _NEG_BEFORE_CODE.search(before) or _QUALIFIED_AFTER.match(after):
            continue
        if re.match(r"^\s*(?:\w+\s+)?(?:files?|directory|folder|command|flag|option)\b", after):
            continue
        for sp in chain:
            if any(mk.mention.search(sp.text.lower()) for mk in _MARKERS):
                continue  # the marker table has a better pattern
            regex = _code_token_regex(sp.text)
            if regex is None:
                continue
            soft = bool(_SOFT_NEG.search(before[-40:]))
            out.append(
                _Emit(
                    "forbid-text",
                    {"pattern": regex, "paths": repo.code_globs()},
                    0.55 if soft else 0.65,
                    "never-use-code",
                    sp.text.strip("@!()"),
                    f"Do not add new uses of {_code(sp.text)}",
                )
            )
    return out


_RECOGNISERS: tuple[Callable[[Directive, _Sentence, _Repo], list[_Emit]], ...] = (
    _rec_commands,
    _rec_git_safety,
    _rec_commit_push,
    _rec_substitution,
    _rec_paths,
    _rec_backup_files,
    _rec_require_change,
    _rec_forbid_text,
    _rec_forbid_code,
    _rec_secrets,
    _rec_outside_repo,
    _rec_dependencies,
    _rec_claims,
    _rec_license_header,
)


# --------------------------------------------------------------------------- compile


def _recognise(d: Directive, repo: _Repo) -> list[tuple[CompiledRule, str]]:
    s = _Sentence.of(d)
    out: list[tuple[CompiledRule, str]] = []
    seen: set[str] = set()
    for rec in _RECOGNISERS:
        for e in rec(d, s, repo):
            if e.confidence < MIN_CONFIDENCE:
                continue
            key = _params_key(e.check, e.params)
            if key in seen:
                continue
            seen.add(key)
            severity: Severity = e.severity or (
                "warning" if e.confidence < WARNING_BELOW else "error"
            )
            rule = Rule(
                id="",
                check=e.check,
                params=e.params,
                description=e.description,
                severity=severity,
                source=d.location,
                scope=_scope(d.file),
            )
            out.append((CompiledRule(rule, d, round(e.confidence, 2), e.pattern), e.label))
    return out


def _params_key(check: str, params: dict[str, Any]) -> str:
    return check + repr(sorted((k, repr(v)) for k, v in params.items()))


def _scope(rel_path: str) -> str | None:
    parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    if not parent or parent.startswith(".") or parent.split("/")[0] in (".github", ".cursor"):
        return None
    return parent


def compile_files(paths: list[Path], repo: Path) -> CompileResult:
    """Extract directives from ``paths`` and compile the ones a recogniser understands.

    Identical rules from several directives (AGENTS.md and CLAUDE.md saying the same thing) are
    emitted once; every directive they came from counts as covered.
    """
    info = _Repo.scan(repo)
    result = CompileResult()
    seen: dict[str, CompiledRule] = {}
    ids: set[str] = set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConfigError(f"cannot read instruction file {path}: {exc.strerror}") from exc
        rel = _relative(path, repo)
        result.files.append(rel)
        for d in extract_directives(text, rel):
            result.directives.append(d)
            compiled = _recognise(d, info)
            if not compiled:
                result.uncovered.append(d)
                continue
            for cr, label in compiled:
                key = _params_key(cr.rule.check, cr.rule.params) + repr(cr.rule.scope)
                if key in seen:
                    continue
                cr.rule.id = _unique_id(_base_id(cr.rule, label), ids)
                seen[key] = cr
                result.rules.append(cr)
    return result


def _relative(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.name


_ID_PREFIX = {
    "require-command": "run-",
    "forbid-command": "no-",
    "forbid-change": "no-change-",
    "forbid-edit": "no-edit-",
    "forbid-text": "no-",
    "require-change": "update-",
    "require-text": "require-",
    "claims": "",
}


def _base_id(rule: Rule, label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40].strip("-") or "rule"
    prefix = _ID_PREFIX.get(rule.check, "")
    if rule.check == "forbid-change" and rule.params.get("actions") == ["add"]:
        prefix = "no-"
    if rule.check == "forbid-change" and slug == "dependencies":
        prefix = "no-new-"
    if rule.check == "require-change" and slug == "tests":
        prefix = "add-"
    if slug.startswith(prefix.rstrip("-") + "-") or slug == prefix.rstrip("-"):
        prefix = ""
    return prefix + slug


def _unique_id(base: str, taken: set[str]) -> str:
    rid, n = base, 2
    while rid in taken:
        rid, n = f"{base}-{n}", n + 1
    taken.add(rid)
    return rid


# --------------------------------------------------------------------------- rendering

_PARAM_ORDER = (
    "command",
    "pattern",
    "tool",
    "paths",
    "except",
    "actions",
    "if_changed",
    "then_changed",
    "must_succeed",
    "after_last_edit",
    "when_paths",
    "edit_paths",
    "ignore_edit_paths",
    "outside_repo",
    "ignore_case",
    "new_files_only",
    "claims",
    "max_files",
    "max_lines",
)


def render_toml(result: CompileResult) -> str:
    """A reviewable ``ruleproof.toml``: one commented rule per compiled directive, then the
    directives that were not compiled, as comments."""
    lines = [
        "# Generated by `ruleproof compile` from " + (", ".join(result.files) or "no files") + ".",
        "# The rules were inferred heuristically from prose: review each one before relying on",
        "# it, and delete or adjust anything that does not match what the instructions mean.",
        f"# {len(result.rules)} rules from {len(result.directives)} directives "
        f"({result.coverage:.0%} of directives compiled).",
        "",
        "version = 1",
    ]
    for cr in result.rules:
        r = cr.rule
        lines += ["", f"# {cr.directive.location}  {_quote(cr.directive)}", "[[rule]]"]
        lines.append(f"id = {_toml_str(r.id)}")
        lines.append(f"description = {_toml_str(r.description)}")
        lines.append(f"source = {_toml_str(r.source or cr.directive.location)}")
        lines.append(f"check = {_toml_str(r.check)}")
        if r.severity != "error":
            lines.append(f"severity = {_toml_str(r.severity)}")
        if r.scope:
            lines.append(f"scope = {_toml_str(r.scope)}")
        for key in sorted(r.params, key=lambda k: (_order(k), k)):
            lines.append(f"{key} = {_toml_value(r.params[key])}")
    if result.uncovered:
        lines += [
            "",
            "# ---------------------------------------------------------------------------",
            f"# Not compiled: {len(result.uncovered)} directives. These are NOT checked.",
            "# Add rules by hand or as inline <!-- ruleproof: ... --> annotations.",
            "#",
        ]
        lines += [f"# {d.location}  {_quote(d)}" for d in result.uncovered]
    return "\n".join(lines) + "\n"


def _order(key: str) -> int:
    return _PARAM_ORDER.index(key) if key in _PARAM_ORDER else len(_PARAM_ORDER)


def _toml_str(value: str) -> str:
    """Literal ``'...'`` when the value allows it (keeps regexes readable), else basic."""
    if "'" not in value and not any(ord(c) < 32 or ord(c) == 127 for c in value):
        return f"'{value}'" if "\\" in value else _basic(value)
    return _basic(value)


def _basic(value: str) -> str:
    out = []
    for c in value:
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 32 or ord(c) == 127:
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot render {type(value).__name__} as TOML")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def render_summary(result: CompileResult) -> str:
    """A few lines for the terminal: counts, rules per check, coverage, top uncovered."""
    by_check = Counter(cr.rule.check for cr in result.rules)
    covered = len(result.directives) - len(result.uncovered)
    lines = [
        f"{_plural(len(result.directives), 'directive')} in {_plural(len(result.files), 'file')}"
        f"; {covered} compiled into {_plural(len(result.rules), 'rule')}"
        f" ({result.coverage:.0%} coverage).",
    ]
    if by_check:
        lines.append("Rules: " + ", ".join(f"{c} {n}" for c, n in sorted(by_check.items())))
    warnings = sum(1 for cr in result.rules if cr.rule.severity == "warning")
    if warnings:
        verb = "is a warning" if warnings == 1 else "are warnings"
        lines.append(f"{_plural(warnings, 'rule')} {verb} (lower confidence); review them first.")
    top = _top_uncovered(result.uncovered, 5)
    if top:
        lines.append("Not compiled, for example:")
        lines += [f"  {d.location}  {_quote(d)}" for d in top]
    return "\n".join(lines)


def _top_uncovered(directives: Iterable[Directive], n: int) -> list[Directive]:
    rank = {"never": 0, "must": 1, "prefer": 2, "other": 3}
    return sorted(directives, key=lambda d: (rank[d.kind], d.file, d.line))[:n]
