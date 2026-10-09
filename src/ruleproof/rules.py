"""Load rules from TOML and inline instruction-file annotations, and validate their params.

Sources, in order: an explicit rules file, else the first of ``ruleproof.toml``,
``.ruleproof.toml`` and ``pyproject.toml`` (only if it has a ``[tool.ruleproof]`` table; files
are never merged), plus ``<!-- ruleproof: ... -->`` annotations in instruction files.
"""

from __future__ import annotations

import dataclasses
import difflib
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from ruleproof.checks import CheckSpec, Param, load_all
from ruleproof.errors import ConfigError
from ruleproof.instructions import (
    INSTRUCTION_NAMES,
    ROOT_INSTRUCTION_PATHS,
    find_instruction_files,
)
from ruleproof.models import SEVERITIES, Rule

RULES_FILES: tuple[str, ...] = ("ruleproof.toml", ".ruleproof.toml", "pyproject.toml")
SUPPORTED_VERSION = 1
RULE_FIELDS: tuple[str, ...] = ("id", "check", "description", "severity", "source", "scope")

_ID_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*")
_RULE_HEADER_RE = re.compile(r"^\s*\[\[\s*rule\s*\]\]")
_PYPROJECT_HEADER_RE = re.compile(
    r"^\s*\[\[\s*tool\s*\.\s*ruleproof\s*\.\s*rule\s*\]\]"
    r'|^\s*\[\[\s*"?tool"?\s*\.\s*"?ruleproof"?\s*\.\s*"?rule"?\s*\]\]'
)
_TOML_LINE_RE = re.compile(r"at line (\d+)")
_LIST_TYPES = frozenset({"str_list", "glob_list", "regex_list"})
_TYPE_EXPECTED = {
    "str": "a string",
    "int": "an integer",
    "bool": "a boolean (true or false)",
    "str_list": "a list of strings",
    "glob_list": "a list of glob patterns",
    "regex": "a regular expression string",
    "regex_list": "a list of regular expression strings",
}


class _Raw(str):
    """An annotation value as written; coerced to the parameter's type by ``validate``."""

    __slots__ = ()


# ------------------------------------------------------------------------------ loading


@dataclasses.dataclass(slots=True)
class Config:
    """The rules file's contents before validation."""

    rules: list[Rule] = dataclasses.field(default_factory=list)
    exclude: list[str] = dataclasses.field(default_factory=list)  # globs skipped when scanning
    origin: str | None = None  # display path of the rules file, if any


def read_config(repo: Path, rules_file: Path | None = None) -> Config:
    """Read the explicit rules file, or the first one found in ``repo``; no validation."""
    if rules_file is not None:
        if not rules_file.is_file():
            raise ConfigError(f"rules file not found: {rules_file}")
        origin = _display(rules_file, repo)
        text = _read(rules_file)
        if rules_file.name == "pyproject.toml":
            found = _pyproject_config(text, origin)
            if found is None:
                raise ConfigError(f"{origin}: no [tool.ruleproof] table; add one with rules")
            return found
        return _config_from_table(_load_toml(text, origin), text, origin, _RULE_HEADER_RE, "")
    for name in RULES_FILES:
        path = repo / name
        if not path.is_file():
            continue
        text = _read(path)
        if name == "pyproject.toml":
            if "ruleproof" not in text:
                continue
            found = _pyproject_config(text, name)
            if found is None:
                continue
            return found
        return _config_from_table(_load_toml(text, name), text, name, _RULE_HEADER_RE, "")
    return Config()


def load_rules(repo: Path, rules_file: Path | None = None, inline: bool = True) -> list[Rule]:
    """All rules for ``repo``, validated against the check registry.

    Raises ``ConfigError`` for a missing explicit rules file, invalid TOML or annotations,
    invalid parameters, and rule ids defined more than once.
    """
    config = read_config(repo, rules_file)
    rules = list(config.rules)
    if inline:
        for path in find_instruction_files(repo, exclude=config.exclude):
            rules.extend(parse_inline(_read(path), path.relative_to(repo).as_posix()))

    validated = [validate(rule) for rule in rules]
    _check_unique(validated)
    return validated


def _check_unique(rules: list[Rule]) -> None:
    seen: dict[str, Rule] = {}
    for rule in rules:
        first = seen.setdefault(rule.id, rule)
        if first is not rule:
            raise ConfigError(
                f"{rule.origin}: duplicate rule id {rule.id!r}, already defined at "
                f"{first.origin}; rule ids must be unique, rename one of them"
            )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _display(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------------- TOML


def parse_rules_toml(text: str, origin: str) -> list[Rule]:
    """Rules from a ``ruleproof.toml`` document; ``origin`` is the path shown in messages."""
    data = _load_toml(text, origin)
    return _config_from_table(data, text, origin, _RULE_HEADER_RE, "").rules


def parse_pyproject_toml(text: str, origin: str) -> list[Rule] | None:
    """Rules from ``[tool.ruleproof]`` in a ``pyproject.toml``; None when the table is absent."""
    config = _pyproject_config(text, origin)
    return None if config is None else config.rules


def _pyproject_config(text: str, origin: str) -> Config | None:
    data = _load_toml(text, origin)
    tool = data.get("tool")
    if not isinstance(tool, dict) or "ruleproof" not in tool:
        return None
    table = tool["ruleproof"]
    if not isinstance(table, dict):
        line = _key_line(text, "ruleproof")
        raise ConfigError(f"{_at(origin, line)}: tool.ruleproof must be a table")
    return _config_from_table(table, text, origin, _PYPROJECT_HEADER_RE, "tool.ruleproof.")


def _load_toml(text: str, origin: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = str(exc)
        m = _TOML_LINE_RE.search(msg)
        where = _at(origin, int(m.group(1)) if m else None)
        raise ConfigError(f"{where}: invalid TOML: {msg}") from None


def _config_from_table(
    table: dict[str, Any], text: str, origin: str, header_re: re.Pattern[str], prefix: str
) -> Config:
    version = table.get("version", SUPPORTED_VERSION)
    if type(version) is not int or version != SUPPORTED_VERSION:
        raise ConfigError(
            f"{_at(origin, _key_line(text, 'version'))}: unsupported {prefix}version = "
            f"{_toml_repr(version)}; this ruleproof reads version = {SUPPORTED_VERSION}"
        )
    for key in table:
        if key not in ("version", "exclude", "rule"):
            hint = " (the array is [[rule]], singular)" if key == "rules" else ""
            raise ConfigError(
                f"{_at(origin, _key_line(text, key))}: unknown key {prefix}{key}{hint}; "
                f"expected {prefix}version, {prefix}exclude and [[{prefix}rule]] tables"
            )
    exclude = table.get("exclude", [])
    if isinstance(exclude, str):
        exclude = [exclude]
    if not isinstance(exclude, list) or not all(isinstance(e, str) for e in exclude):
        raise ConfigError(
            f"{_at(origin, _key_line(text, 'exclude'))}: {prefix}exclude must be a list of "
            f'glob patterns, e.g. exclude = ["examples/", "tests/fixtures/"]'
        )
    entries = table.get("rule", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ConfigError(
            f"{_at(origin, _key_line(text, 'rule'))}: {prefix}rule must be an array of tables; "
            f"write each rule under its own [[{prefix}rule]] header"
        )

    header_lines = [n for n, line in enumerate(text.splitlines(), 1) if header_re.match(line)]
    if len(header_lines) != len(entries):  # e.g. an inline array: no header line per rule
        header_lines = []
    rules: list[Rule] = []
    for idx, entry in enumerate(entries):
        line = header_lines[idx] if header_lines else None
        rules.append(_rule_from_entry(entry, _at(origin, line), idx + 1, prefix))
    return Config(rules=rules, exclude=list(exclude), origin=origin)


def _rule_from_entry(entry: dict[str, Any], where: str, number: int, prefix: str) -> Rule:
    label = f"[[{prefix}rule]] #{number}"
    for name in ("id", "check"):
        if name not in entry:
            raise ConfigError(f'{where}: {label} has no {name}; add {name} = "..."')
    for name in RULE_FIELDS:
        if name in entry and not isinstance(entry[name], str):
            raise ConfigError(
                f"{where}: {label}: {name} must be a string, found "
                f"{_toml_type(entry[name])} {_toml_repr(entry[name])}"
            )
    params = {k: v for k, v in entry.items() if k not in RULE_FIELDS}
    return Rule(
        id=entry["id"],
        check=entry["check"],
        params=params,
        description=entry.get("description", ""),
        severity=entry.get("severity", "error"),
        source=entry.get("source"),
        origin=where,
        scope=entry.get("scope"),
    )


def _key_line(text: str, key: str) -> int | None:
    pattern = re.compile(
        rf"^\s*\"?{re.escape(key)}\"?\s*(=|\.)|^\s*\[+\s*(\w+\.)*{re.escape(key)}\s*\]"
    )
    for n, line in enumerate(text.splitlines(), 1):
        if pattern.match(line):
            return n
    return None


def _at(origin: str, line: int | None) -> str:
    return f"{origin}:{line}" if line is not None else origin


# ------------------------------------------------------------------------------- inline

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
_ANNOTATION_RE = re.compile(r"<!--\s*ruleproof\b")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
_EMPHASIS_RES = (
    (re.compile(r"(\*\*|__|~~)(.+?)\1"), r"\2"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"\1"),
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"), r"\1"),
)


def parse_inline(text: str, rel_path: str) -> list[Rule]:
    """Rules from ``<!-- ruleproof: <check> key=value ... -->`` annotations in Markdown.

    Annotations inside fenced code blocks and inline code spans are ignored. Values are kept
    as written; ``validate`` converts them to each parameter's type (comma-separated lists,
    ``true``/``false``/``yes``/``no``/``1``/``0`` booleans, decimal integers).
    """
    masked = _mask_code(text)
    original_lines = text.split("\n")
    masked_lines = masked.split("\n")
    comment_lines: set[int] = set()  # 0-based lines touched by an HTML comment
    for c in _COMMENT_RE.finditer(masked):
        first = masked.count("\n", 0, c.start())
        comment_lines.update(range(first, first + c.group(0).count("\n") + 1))
    rules: list[Rule] = []
    for m in _ANNOTATION_RE.finditer(masked):
        line = masked.count("\n", 0, m.start()) + 1
        where = f"{rel_path}:{line}"
        end = masked.find("-->", m.end())
        if end == -1:
            raise ConfigError(f"{where}: unterminated ruleproof annotation; close it with -->")
        body = masked[m.end() : end]
        if not body.lstrip().startswith(":"):
            raise ConfigError(
                f"{where}: expected 'ruleproof:' followed by a check name, as in "
                "<!-- ruleproof: forbid-change paths=dist/ -->"
            )
        check, values = _parse_annotation_body(body.lstrip()[1:], where)
        line_start = masked.rfind("\n", 0, m.start()) + 1
        same_line = original_lines[line - 1][: m.start() - line_start]
        description = _prose(same_line) or _preceding_prose(
            original_lines, masked_lines, comment_lines, line
        )
        fields: dict[str, Any] = {
            "id": _default_id(rel_path, line),
            "description": description,
            "severity": "error",
            "source": where,
            "scope": _default_scope(rel_path),
        }
        for name in RULE_FIELDS:
            if name in values:
                fields[name] = str(values.pop(name))
        rules.append(Rule(check=check, params=dict(values), origin=where, **fields))
    return rules


def _mask_code(text: str) -> str:
    """Blank out fenced code blocks and inline code spans, keeping line numbers and offsets."""
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
                out.append(" " * len(line))
            else:
                out.append(_CODE_SPAN_RE.sub(lambda c: " " * len(c.group(0)), line))
        else:
            closing = m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
            if closing and not line.strip().strip(fence[0]):
                fence = None
            out.append(" " * len(line))
    return "\n".join(out)


def _parse_annotation_body(body: str, where: str) -> tuple[str, dict[str, _Raw]]:
    tokens = _tokenize(body, where)
    if not tokens or tokens[0][1] is not None:
        raise ConfigError(
            f"{where}: annotation has no check name; expected "
            "<!-- ruleproof: <check> key=value ... -->"
        )
    check = tokens[0][0]
    values: dict[str, _Raw] = {}
    for key, value in tokens[1:]:
        if key == "check":
            raise ConfigError(
                f"{where}: the check is the first word after 'ruleproof:', not a check= parameter"
            )
        if value is None:
            raise ConfigError(
                f"{where}: expected key=value after the check name, found {key!r}; "
                'quote values that contain spaces: key="a value"'
            )
        if not _KEY_RE.fullmatch(key):
            raise ConfigError(f"{where}: invalid parameter name {key!r}")
        if key in values:
            raise ConfigError(f"{where}: parameter {key!r} is given twice")
        values[key] = _Raw(value)
    return check, values


def _tokenize(body: str, where: str) -> list[tuple[str, str | None]]:
    """Split ``check key=value key="quoted value"`` into (word, value-or-None) pairs."""
    tokens: list[tuple[str, str | None]] = []
    i, n = 0, len(body)
    while True:
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            return tokens
        start = i
        while i < n and not body[i].isspace() and body[i] != "=":
            i += 1
        word = body[start:i]
        if i >= n or body[i] != "=":
            tokens.append((word, None))
            continue
        i += 1
        if i < n and body[i] in "\"'":
            quote = body[i]
            value: list[str] = []
            i += 1
            while i < n and body[i] != quote:
                if body[i] == "\\" and i + 1 < n and body[i + 1] in (quote, "\\"):
                    i += 1
                value.append(body[i])
                i += 1
            if i >= n:
                raise ConfigError(f"{where}: unterminated {quote} in value of {word!r}")
            i += 1
            tokens.append((word, "".join(value)))
        else:
            start = i
            while i < n and not body[i].isspace():
                i += 1
            tokens.append((word, body[start:i]))


def _preceding_prose(
    original: list[str], masked: list[str], comment_lines: set[int], line: int
) -> str:
    for idx in range(line - 2, -1, -1):
        if not masked[idx].strip() or idx in comment_lines:
            continue  # blank, inside a code block, or an HTML comment
        prose = _prose(original[idx])
        if prose:
            return prose
    return ""


def _prose(line: str) -> str:
    text = _LIST_MARKER_RE.sub("", line).strip()
    text = text.lstrip("#>").strip()
    for pattern, repl in _EMPHASIS_RES:
        text = pattern.sub(repl, text)
    return text


def _default_id(rel_path: str, line: int) -> str:
    p = PurePosixPath(rel_path)
    stem = p.stem
    parent = "" if str(p.parent) == "." else f"{p.parent}/"
    raw = f"{parent}{stem}-l{line}".lower()
    return re.sub(r"[^a-z0-9._/-]", "-", raw).lstrip("._/-")


def _default_scope(rel_path: str) -> str | None:
    p = PurePosixPath(rel_path)
    if rel_path in ROOT_INSTRUCTION_PATHS or p.name not in INSTRUCTION_NAMES:
        return None  # e.g. .github/copilot-instructions.md applies to the whole repo
    parent = str(p.parent)
    return None if parent == "." else parent


# ---------------------------------------------------------------------------- validation


def validate(rule: Rule) -> Rule:
    """Check ``rule`` against its check's parameter schema; return it with params coerced.

    Lists given as a single string become one-element lists, inline annotation values are
    converted from text, regexes are compiled to report errors early (params keep the
    pattern strings), and missing optional params get their defaults.
    """
    where = f"{rule.origin}: rule {rule.id!r}" if rule.origin else f"rule {rule.id!r}"
    if not _ID_RE.fullmatch(rule.id):
        suggestion = re.sub(r"[^a-z0-9._/-]+", "-", rule.id.lower()).strip("-._/")
        hint = f"; for example {suggestion!r}" if suggestion else ""
        raise ConfigError(
            f"{where}: invalid id; ids use lowercase letters, digits and . _ / - and start "
            f"with a letter or digit{hint}"
        )
    if rule.severity not in SEVERITIES:
        raise ConfigError(
            f"{where}: invalid severity {rule.severity!r}; expected one of "
            f"{', '.join(SEVERITIES)}{_did_you_mean(str(rule.severity).lower(), SEVERITIES)}"
        )
    checks = load_all()
    spec = checks.get(rule.check)
    if spec is None:
        raise ConfigError(
            f"{where}: unknown check {rule.check!r}{_did_you_mean(rule.check, checks)}; "
            f"available checks: {', '.join(sorted(checks)) or '(none registered)'}"
        )
    params = _validate_params(rule, spec, f"{where} ({rule.check})")
    return dataclasses.replace(
        rule,
        params=params,
        description=str(rule.description),
        source=None if rule.source is None else str(rule.source),
        scope=None if rule.scope is None else str(rule.scope),
    )


def _validate_params(rule: Rule, spec: CheckSpec, where: str) -> dict[str, Any]:
    for key in rule.params:
        if key not in spec.params:
            known = ", ".join(sorted(spec.params)) or "none"
            raise ConfigError(
                f"{where}: unknown parameter {key!r}"
                f"{_did_you_mean(key, [*spec.params, *RULE_FIELDS])}; "
                f"{spec.name} accepts: {known}"
            )
    missing = [k for k, p in spec.params.items() if p.required and k not in rule.params]
    if missing:
        raise ConfigError(
            f"{where}: missing required parameter{'s' if len(missing) > 1 else ''} "
            f"{', '.join(missing)}"
            + "".join(f"\n  {k}: {spec.params[k].doc}" for k in missing if spec.params[k].doc)
        )
    out: dict[str, Any] = {}
    for key, param in spec.params.items():
        if key in rule.params:
            out[key] = _coerce(rule.params[key], param, f"{where}: parameter {key!r}")
        else:
            default = param.default
            out[key] = list(default) if isinstance(default, list | tuple) else default
    return out


def _coerce(value: Any, param: Param, where: str) -> Any:
    kind = param.type
    if isinstance(value, _Raw):
        value = _from_text(str(value), kind, where)
    if kind in _LIST_TYPES:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            _wrong_type(value, kind, where)
        items = [str(v) for v in value]
        if kind == "regex_list":
            for item in items:
                _compile(item, where)
        return items
    if kind in ("str", "regex"):
        if not isinstance(value, str):
            _wrong_type(value, kind, where)
        if kind == "regex":
            _compile(value, where)
        return str(value)
    if kind == "int":
        if type(value) is not int:
            _wrong_type(value, kind, where)
        return value
    if type(value) is not bool:
        _wrong_type(value, kind, where)
    return value


def _from_text(text: str, kind: str, where: str) -> Any:
    if kind in _LIST_TYPES:
        return [item.strip() for item in text.split(",") if item.strip()]
    if kind == "bool":
        word = text.strip().lower()
        if word in ("true", "yes", "1"):
            return True
        if word in ("false", "no", "0"):
            return False
        raise ConfigError(f"{where}: expected true/false/yes/no/1/0, found {text!r}")
    if kind == "int":
        if not re.fullmatch(r"[+-]?\d+", text.strip()):
            raise ConfigError(f"{where}: expected a decimal integer, found {text!r}")
        return int(text)
    return text


def _compile(pattern: str, where: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        pos = exc.pos if exc.pos is not None else 0
        raise ConfigError(
            f"{where}: invalid regular expression: {exc.msg} at position {pos}\n"
            f"  {pattern}\n  {' ' * pos}^"
        ) from None


def _wrong_type(value: Any, kind: str, where: str) -> NoReturn:
    raise ConfigError(
        f"{where}: expected {_TYPE_EXPECTED[kind]}, found {_toml_type(value)} {_toml_repr(value)}"
    )


def _toml_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "table"
    return type(value).__name__


def _toml_repr(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return repr(value)
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _did_you_mean(word: str, candidates: Any) -> str:
    match = difflib.get_close_matches(word, list(candidates), n=1, cutoff=0.6)
    return f" (did you mean {match[0]!r}?)" if match else ""
