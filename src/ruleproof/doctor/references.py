"""``doctor/dead-reference``: imports, links, paths, scripts and targets that do not exist."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

from ruleproof.doctor._common import (
    EXTENSIONS,
    Doc,
    Finding,
    RepoInfo,
    clip,
    ev,
    read_text,
)
from ruleproof.doctor.imports import find_imports
from ruleproof.doctor.mentions import Command, iter_commands
from ruleproof.doctor.scan import Scan

_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'(][^)]*)?\)")
_REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_PLACEHOLDER = re.compile(
    r"[<>{}$*?\[\]|]|\.\.\.|path/to|(?:^|/)(?:foo|bar|baz|xxx|your[-_]\w*|my[-_]\w*)(?:/|\.|$)"
)
_SKIP_LINE = re.compile(
    r"\b(?:never|don'?t|do\s+not|avoid|creat\w*|generat\w*|will\s+be|output\w*|produc\w*|"
    r"writ\w*|add\w*|new|e\.g|example|such\s+as|like|rename\w*|mov\w*|delet\w*|remov\w*)\b",
    re.IGNORECASE,
)
_COMMITTED_DOT_DIRS = frozenset(
    {".github", ".gitlab", ".circleci", ".husky", ".devcontainer", ".changeset", ".storybook"}
)
_FRAMEWORKS = frozenset(
    {
        "node.js",
        "next.js",
        "nuxt.js",
        "vue.js",
        "react.js",
        "express.js",
        "three.js",
        "d3.js",
        "chart.js",
        "nest.js",
        "ember.js",
        "deno.json",
    }
)

NPM_BUILTINS = frozenset(
    [
        "access",
        "adduser",
        "audit",
        "bugs",
        "cache",
        "ci",
        "completion",
        "config",
        "dedupe",
        "deprecate",
        "diff",
        "dist-tag",
        "docs",
        "doctor",
        "edit",
        "exec",
        "explain",
        "explore",
        "find-dupes",
        "fund",
        "help",
        "hook",
        "init",
        "install",
        "install-ci-test",
        "install-test",
        "i",
        "link",
        "ll",
        "login",
        "logout",
        "ls",
        "org",
        "outdated",
        "owner",
        "pack",
        "ping",
        "pkg",
        "prefix",
        "profile",
        "prune",
        "publish",
        "query",
        "rebuild",
        "repo",
        "restart",
        "root",
        "search",
        "set",
        "shrinkwrap",
        "star",
        "stars",
        "stop",
        "team",
        "token",
        "uninstall",
        "unpublish",
        "unstar",
        "update",
        "version",
        "view",
        "whoami",
        "x",
        "add",
        "rm",
        "remove",
        "up",
        "create",
    ]
)
PNPM_BUILTINS = NPM_BUILTINS | frozenset(
    [
        "dlx",
        "env",
        "fetch",
        "import",
        "install-test",
        "licenses",
        "list",
        "patch",
        "patch-commit",
        "patch-remove",
        "server",
        "setup",
        "store",
        "unlink",
        "why",
        "deploy",
        "approve-builds",
        "self-update",
        "cat-file",
        "cat-index",
        "find-hash",
        "workspace",
        "recursive",
        "outdated",
    ]
)
YARN_BUILTINS = NPM_BUILTINS | frozenset(
    [
        "bin",
        "constraints",
        "dlx",
        "global",
        "info",
        "node",
        "npm",
        "plugin",
        "set",
        "unplug",
        "upgrade",
        "upgrade-interactive",
        "workspace",
        "workspaces",
        "why",
        "generate-lock-entry",
        "check",
        "autoclean",
        "licenses",
        "policies",
    ]
)
_WORKSPACE_FLAGS = frozenset(
    {
        "-w",
        "--workspace",
        "--workspaces",
        "-ws",
        "--prefix",
        "-C",
        "--dir",
        "--filter",
        "-F",
        "-r",
        "--recursive",
        "--cwd",
        "--if-present",
        "-W",
        "--workspace-root",
    }
)


def check_references(info: RepoInfo, scan: Scan) -> list[Finding]:
    out: list[Finding] = []
    for doc in scan.docs:
        out.extend(_imports(info, doc, doc.path in scan.importing))
        out.extend(_links(info, doc))
        out.extend(_backticked_paths(info, doc))
        out.extend(_scripts(info, doc))
    return out


def _imports(info: RepoInfo, doc: Doc, resolved: bool) -> list[Finding]:
    out: list[Finding] = []
    for imp in find_imports(doc):
        if imp.target.exists() or info.excluded(imp.target):
            continue
        if resolved:
            agent = "Claude Code" if "CLAUDE" in doc.path.name.upper() else "the agent"
            summary = (
                f"{doc.rel}:{imp.line} imports @{imp.raw}, which does not exist; "
                f"{agent} skips it silently"
            )
        else:
            summary = f"{doc.rel}:{imp.line} refers to @{imp.raw}, which does not exist"
        out.append(
            Finding(
                "dead-reference",
                "error" if resolved else "warning",
                summary,
                [ev("missing import", doc.rel, imp.line, clip(doc.prose[imp.line - 1].strip()))],
            )
        )
    return out


def _link_targets(doc: Doc) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for i in range(len(doc.prose)):
        line = doc.blanked(i)
        if "](" in line:
            found.extend((i, m.group(1)) for m in _LINK.finditer(line))
        m = _REF_DEF.match(line)
        if m:
            found.append((i, m.group(1)))
    return found


def _links(info: RepoInfo, doc: Doc) -> list[Finding]:
    out: list[Finding] = []
    for i, raw in _link_targets(doc):
        target = unquote(raw.strip("<>").split("#", 1)[0].split("?", 1)[0])
        if (
            not target
            or _SCHEME.match(target)
            or target.startswith("//")
            or _PLACEHOLDER.search(target)
        ):
            continue
        path = info.repo / target.lstrip("/") if target.startswith("/") else doc.dir / target
        path = Path(os.path.normpath(path))
        if (
            path.exists()
            or info.excluded(path)
            or (info.inside(path) and info.ignored(info.rel(path)))
        ):
            continue
        out.append(
            Finding(
                "dead-reference",
                "warning",
                f"{doc.rel}:{i + 1} links to {raw.strip('<>')}, which does not exist",
                [ev("broken link", doc.rel, i + 1, clip(doc.prose[i].strip()))],
            )
        )
    return out


def path_candidate(token: str) -> bool:
    """True when a backticked token is clearly meant as a file or directory path."""
    if (
        not token
        or any(c in token for c in " \t\"'=,;()@~:\\#%&!^")
        or _PLACEHOLDER.search(token)
        or token.lower() in _FRAMEWORKS
        or re.match(r"^v?\d", token)
    ):
        return False
    if token.startswith("/") or token.startswith("-"):
        return False
    if token.startswith((".", "_")) and "/" not in token and not token.startswith(("./", "../")):
        return False
    name = token.rstrip("/").rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[1].lower() if "." in name[1:] else ""
    if "/" not in token.rstrip("/"):
        return False  # a bare file name is often a runtime or user file, not a repo path
    return ext in EXTENSIONS or token.startswith(("./", "../")) or token.endswith("/")


def _backticked_paths(info: RepoInfo, doc: Doc) -> list[Finding]:
    out: list[Finding] = []
    seen: set[str] = set()
    for i, line in enumerate(doc.prose):
        if "`" not in line or _SKIP_LINE.search(line) or _SKIP_LINE.search(doc.heading_at(i)):
            continue
        for _, _, token in doc.spans(i):
            if token in seen or not path_candidate(token):
                continue
            seen.add(token)
            if _path_exists(info, doc, token):
                continue
            out.append(
                Finding(
                    "dead-reference",
                    "warning",
                    f"{doc.rel}:{i + 1} mentions `{token}`, which does not exist in the repo",
                    [ev("missing path", doc.rel, i + 1, clip(line.strip()))],
                )
            )
    return out


def _path_exists(info: RepoInfo, doc: Doc, token: str) -> bool:
    rel = token.rstrip("/")
    for base in (doc.dir, info.repo):
        candidate = Path(os.path.normpath(base / rel))
        if candidate.exists():
            return True
    norm = rel.removeprefix("./")
    first = norm.split("/", 1)[0]
    if first.startswith(".") and first not in ("", ".", "..") and first not in _COMMITTED_DOT_DIRS:
        return True  # e.g. `.claude/settings.local.json`: tool config that is often local
    if (
        not info.index_complete
        or info.ignored(norm)
        or info.excluded(norm)
        or rel.startswith("../")
    ):
        return True
    if "/" not in norm:
        return norm in info.basenames
    return info.has_suffix_path(norm)


# --- Scripts, make targets, just recipes ----------------------------------------------------


def _scripts(info: RepoInfo, doc: Doc) -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for cmd in iter_commands(doc):
        if cmd.negative:
            continue
        for kind, name, problem in _missing(info, cmd):
            if (kind, name) in seen:
                continue
            seen.add((kind, name))
            out.append(
                Finding(
                    "dead-reference",
                    "warning",
                    f"{doc.rel}:{cmd.line} runs `{clip(cmd.text, 60)}` but {problem}",
                    [ev(f"undefined {kind}", doc.rel, cmd.line, clip(cmd.text))],
                )
            )
    return out


def _start_dir(info: RepoInfo, cmd: Command, extra: str | None = None) -> Path:
    here = cmd.doc.dir
    for sub in (cmd.cwd, extra):
        if sub and not re.search(r"[$<{~]", sub):
            here = Path(os.path.normpath(here / sub))
    return here


def _missing(info: RepoInfo, cmd: Command) -> list[tuple[str, str, str]]:
    head, args = cmd.tokens[0], list(cmd.tokens[1:])
    if head in ("npm", "pnpm", "yarn", "bun"):
        name = _script_name(head, args)
        if name is None or set(args) & _WORKSPACE_FLAGS:
            return []
        return _check_script(info, cmd, name)
    if head == "make":
        return _check_make(info, cmd, args)
    if head == "just":
        return _check_just(info, cmd, args)
    return []


def _script_name(head: str, args: list[str]) -> str | None:
    if not args or args[0].startswith("-"):
        return None
    sub, rest = args[0], args[1:]
    if sub in ("run", "run-script", "rum", "urn"):
        name = rest[0] if rest and not rest[0].startswith("-") else None
    elif head == "npm":
        name = "test" if sub in ("test", "t", "tst") else None
    elif head == "pnpm":
        name = None if sub in PNPM_BUILTINS else sub
    elif head == "yarn":
        name = None if sub in YARN_BUILTINS else sub
    else:
        name = None
    if (
        name is None
        or _PLACEHOLDER.search(name)
        or (head == "bun" and ("." in name or "/" in name))
    ):
        return None
    return name


def _check_script(info: RepoInfo, cmd: Command, name: str) -> list[tuple[str, str, str]]:
    pkg_path = info.nearest(_start_dir(info, cmd), ("package.json",))
    if pkg_path is None:
        return []
    pkg = info.json(pkg_path)
    if not isinstance(pkg, dict):
        return []
    scripts = pkg.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    if name in scripts or name in info.package_json_names("scripts"):
        return []
    shorthand = cmd.tokens[1] == name and cmd.tokens[0] in ("pnpm", "yarn")
    if shorthand:
        deps: set[str] = set()
        for key in ("dependencies", "devDependencies"):
            value = pkg.get(key)
            if isinstance(value, dict):
                deps |= set(value)
        bin_dir = pkg_path.parent / "node_modules" / ".bin"
        deps |= info.package_json_names("dependencies")
        deps |= info.package_json_names("devDependencies")
        if name in deps or (bin_dir / name).exists() or not deps and not scripts:
            return []
        return [("script", name, f'{info.rel(pkg_path)} has no script or dependency "{name}"')]
    return [("script", name, f'{info.rel(pkg_path)} has no "{name}" script')]


def _make_targets(path: Path, depth: int = 0) -> set[str] | None:
    """Targets defined in a Makefile, or None when they cannot be known statically."""
    text = read_text(path)
    if text is None or depth > 3:
        return None
    targets: set[str] = set()
    in_define = False
    for line in text.splitlines():
        if line.startswith("\t"):
            continue
        stripped = line.strip()
        if stripped.startswith("define "):
            in_define = True
        elif stripped.startswith("endef"):
            in_define = False
        if in_define or not stripped or stripped.startswith("#"):
            continue
        inc = re.match(r"^-?(?:include|sinclude)\s+(.+)$", stripped)
        if inc:
            for name in inc.group(1).split():
                if "$" in name or "*" in name:
                    return None
                sub = _make_targets(path.parent / name, depth + 1)
                if sub is None:
                    return None
                targets |= sub
            continue
        if re.match(r"^[\w.\-/ ]*?\s*(?::{1,3}|\?|\+|!)?=", stripped) or stripped.startswith(
            ("export ", "override ", "ifeq", "ifneq", "ifdef", "ifndef", "else", "endif")
        ):
            continue
        m = re.match(r"^([^:#=\t][^:#=]*?)\s*(?<!:):{1,2}(?![:=])(.*)$", stripped)
        if not m:
            continue
        names, prereqs = m.group(1).split(), m.group(2).split(";")[0].split()
        if names == [".PHONY"]:
            targets.update(p for p in prereqs if "$" not in p)
            continue
        for name in names:
            if name == "%":
                return None
            if "$" not in name and "%" not in name:
                targets.add(name)
    return targets


def _check_make(info: RepoInfo, cmd: Command, args: list[str]) -> list[tuple[str, str, str]]:
    directory: str | None = None
    names: list[str] = []
    skip_next = False
    for k, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg in ("-f", "--file", "--makefile"):
            return []
        if arg in ("-C", "--directory"):
            directory = args[k + 1] if k + 1 < len(args) else None
            skip_next = True
        elif arg.startswith("--directory="):
            directory = arg.split("=", 1)[1]
        elif arg in ("-j", "-l", "-o", "-W", "--jobs"):
            skip_next = True
        elif not arg.startswith("-") and "=" not in arg and not arg.isdigit():
            names.append(arg)
    names = [n for n in names if not _PLACEHOLDER.search(n)]
    if not names:
        return []
    start = _start_dir(info, cmd, directory)
    makefile = info.nearest(start, ("GNUmakefile", "makefile", "Makefile"))
    if makefile is None:
        return []
    targets = _make_targets(makefile)
    if targets is None:
        return []
    return [
        ("make target", n, f'{info.rel(makefile)} has no "{n}" target')
        for n in names
        if n not in targets and not (makefile.parent / n).exists()
    ]


def _just_recipes(path: Path) -> set[str] | None:
    text = read_text(path)
    if text is None:
        return None
    recipes: set[str] = set()
    for line in text.splitlines():
        if not line or line[0] in " \t#[":
            continue
        if re.match(r"^(?:import|mod)\b", line):
            return None
        alias = re.match(r"^alias\s+([\w-]+)\s*:=", line)
        if alias:
            recipes.add(alias.group(1))
            continue
        if re.match(r"^(?:set|export)\s", line) or re.match(r"^[\w-]+\s*:=", line):
            continue
        m = re.match(r"^@?([A-Za-z_][\w-]*)(?:\s[^:]*)?:(?!=)", line)
        if m:
            recipes.add(m.group(1))
    return recipes


def _check_just(info: RepoInfo, cmd: Command, args: list[str]) -> list[tuple[str, str, str]]:
    if any(a.startswith(("-f", "--justfile", "-d", "--working-directory")) for a in args):
        return []
    names = [a for a in args if not a.startswith("-")]
    if not names or any(a.startswith("-") for a in args[: args.index(names[0])]):
        return []
    name = names[0]
    if _PLACEHOLDER.search(name) or "=" in name:
        return []
    justfile = info.nearest(
        _start_dir(info, cmd), ("justfile", "Justfile", ".justfile", "JUSTFILE")
    )
    if justfile is None:
        return []
    recipes = _just_recipes(justfile)
    if recipes is None or name in recipes:
        return []
    return [("just recipe", name, f'{info.rel(justfile)} has no "{name}" recipe')]
